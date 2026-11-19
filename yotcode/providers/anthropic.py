"""Anthropic Messages API 供应商（流式）。

请求地址默认为 https://api.anthropic.com/v1/messages，鉴权头是 x-api-key。
流式响应是一串 SSE 事件，核心映射关系：
  message_start                  -> 初始 usage
  content_block_start            -> 新建 text / thinking / tool_use 块
  content_block_delta            -> text_delta / thinking_delta / input_json_delta
  content_block_stop             -> tool_use 的 JSON 参数拼接完成，产出 ToolUseReady
  message_delta                  -> stop_reason 与最终 usage
  message_stop                   -> 本轮结束

系统提示和工具定义都加了 cache_control，命中 Anthropic prompt caching 时可省钱。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from ..types import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .base import (
    Interrupted,
    ModelError,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnDone,
    _raise_for_status,
    iter_sse,
    usage_from_anthropic,
)

ANTHROPIC_VERSION = "2023-06-01"


def _convert_result_content(content: str | list[Any]) -> Any:
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            out.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.data,
                },
            })
    return out or "(empty)"


def convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text:
                    blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    },
                })
            elif isinstance(block, ThinkingBlock):
                # The API only accepts signed thinking blocks from a previous turn.
                if block.signature:
                    blocks.append({
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": block.signature,
                    })
            elif isinstance(block, ToolUseBlock):
                blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input or {},
                })
            elif isinstance(block, ToolResultBlock):
                item = {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": _convert_result_content(block.content),
                }
                if block.is_error:
                    item["is_error"] = True
                blocks.append(item)
        if not blocks:
            continue
        if out and out[-1]["role"] == message.role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": message.role, "content": blocks})
    return out


class AnthropicProvider(Provider):
    name = "anthropic"
    supports_thinking = True

    def _headers(self, thinking: bool = False) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "user-agent": "yotcode/0.1.0",
        }
        if self.api_key:
            # Helpful for proxies / Anthropic-compatible gateways.
            headers["authorization"] = f"Bearer {self.api_key}"
        if thinking:
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        return headers

    def _body(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        thinking: bool,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": convert_messages(messages),
        }
        if system:
            body["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            tool_defs = []
            for tool in tools:
                item = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema") or {"type": "object"},
                }
                tool_defs.append(item)
            # Cache the tool definitions too -- they are stable per session.
            tool_defs[-1]["cache_control"] = {"type": "ephemeral"}
            body["tools"] = tool_defs
        if thinking and self.supports_thinking:
            budget = max(1024, min(8192, max_tokens // 2))
            if budget >= max_tokens:
                budget = max(512, max_tokens - 1024)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            body["max_tokens"] = max(max_tokens, budget + 1024)
            # Extended thinking requires temperature=1; omit the parameter.
        elif temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
        return body

    def _url(self) -> str:
        base = self.base_url
        if base.endswith("/v1/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    async def _stream_once(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        thinking: bool,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self._body(
            system=system, messages=messages, tools=tools,
            max_tokens=max_tokens, temperature=temperature,
            thinking=thinking, stream=True,
        )
        # Anthropic 的流式事件按 index 推送；这里用字典累积每个内容块，
        # 等 content_block_stop 时再组装成完整的 ToolUseBlock。
        blocks: dict[int, dict[str, Any]] = {}
        tool_blocks: dict[int, ToolUseBlock] = {}
        usage = Usage()
        stop_reason = "end_turn"
        order: list[int] = []

        # 初始化 SSE 原文捕获（HTTP 报文日志用）；非流式响应时为 None
        if self._http_logger:
            self._sse_capture.set([])
        async with self._client.stream(
            "POST", self._url(), headers=self._headers(thinking), json=body
        ) as response:
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", errors="replace")
                _raise_for_status(
                    response.status_code, raw, response.headers.get("retry-after")
                )
            async for data in iter_sse(response, self._sse_capture.get()):
                self.check_cancel(cancel)
                kind = data.get("type")
                if kind == "message_start":
                    msg = data.get("message") or {}
                    usage = usage_from_anthropic(msg.get("usage") or {})
                elif kind == "content_block_start":
                    index = int(data.get("index", len(order)))
                    block = data.get("content_block") or {}
                    order.append(index)
                    if block.get("type") == "tool_use":
                        blocks[index] = {
                            "type": "tool_use",
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "json": "",
                            "input": block.get("input") or {},
                        }
                    elif block.get("type") == "thinking":
                        blocks[index] = {
                            "type": "thinking",
                            "text": block.get("thinking") or "",
                            "signature": "",
                        }
                    else:
                        blocks[index] = {
                            "type": "text",
                            "text": block.get("text") or "",
                        }
                elif kind == "content_block_delta":
                    index = int(data.get("index", 0))
                    delta = data.get("delta") or {}
                    dtype = delta.get("type")
                    entry = blocks.setdefault(index, {"type": "text", "text": ""})
                    if dtype == "text_delta":
                        text = delta.get("text") or ""
                        entry["text"] = entry.get("text", "") + text
                        if text:
                            yield TextDelta(text)
                    elif dtype == "thinking_delta":
                        text = delta.get("thinking") or ""
                        entry["text"] = entry.get("text", "") + text
                        if text:
                            yield ThinkingDelta(text)
                    elif dtype == "signature_delta":
                        entry["signature"] = entry.get("signature", "") + (
                            delta.get("signature") or ""
                        )
                    elif dtype == "input_json_delta":
                        entry["json"] = entry.get("json", "") + (
                            delta.get("partial_json") or ""
                        )
                elif kind == "content_block_stop":
                    index = int(data.get("index", 0))
                    entry = blocks.get(index)
                    if entry and entry.get("type") == "tool_use":
                        raw_json = entry.get("json") or ""
                        parsed = entry.get("input") or {}
                        if raw_json.strip():
                            try:
                                parsed = json.loads(raw_json)
                            except json.JSONDecodeError:
                                parsed = {"_raw_input": raw_json}
                        block = ToolUseBlock(
                            id=entry.get("id") or f"toolu_{index:04d}",
                            name=entry.get("name") or "",
                            input=parsed if isinstance(parsed, dict) else {"value": parsed},
                        )
                        tool_blocks[index] = block
                        yield ToolUseReady(block)
                elif kind == "message_delta":
                    delta = data.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    u = data.get("usage") or {}
                    if u.get("output_tokens") is not None:
                        usage.output_tokens = int(u["output_tokens"])
                    if u.get("input_tokens") is not None:
                        usage.input_tokens = int(u["input_tokens"])
                    if u.get("cache_read_input_tokens") is not None:
                        usage.cache_read_input_tokens = int(u["cache_read_input_tokens"])
                    if u.get("cache_creation_input_tokens") is not None:
                        usage.cache_creation_input_tokens = int(
                            u["cache_creation_input_tokens"]
                        )
                elif kind == "error":
                    err = data.get("error") or {}
                    raise ModelError(
                        err.get("message") or "Anthropic API error",
                        retryable=err.get("type") in ("overloaded_error", "api_error"),
                        kind=str(err.get("type") or "api_error"),
                    )
                elif kind == "message_stop":
                    break
            # 流读取完毕，把捕获到的 SSE data 原文回填到 HTTP 日志
            self.log_sse_body()

        content: list[Any] = []
        for index in sorted(blocks.keys()):
            entry = blocks[index]
            if entry["type"] == "text":
                if entry.get("text"):
                    content.append(TextBlock(entry["text"]))
            elif entry["type"] == "thinking":
                if entry.get("text"):
                    content.append(
                        ThinkingBlock(entry["text"], entry.get("signature") or None)
                    )
            elif entry["type"] == "tool_use":
                block = tool_blocks.get(index)
                if block:
                    content.append(block)
        if stop_reason == "tool_use":
            # Ensure tool blocks are present even if parsing was odd.
            for block in tool_blocks.values():
                if block not in content:
                    content.append(block)
        yield TurnDone(content=content, usage=usage, stop_reason=stop_reason)

    async def _complete_once(self, *, system: str, messages: list[Message],
                             max_tokens: int, temperature: float) -> str:
        body = self._body(
            system=system, messages=messages, tools=[],
            max_tokens=max_tokens, temperature=temperature,
            thinking=False, stream=False,
        )
        response = await self._client.post(self._url(), headers=self._headers(), json=body)
        if response.status_code >= 400:
            _raise_for_status(
                response.status_code, response.text, response.headers.get("retry-after")
            )
        data = response.json()
        parts = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts).strip()
