"""OpenAI 兼容的 /chat/completions 供应商。

OpenAI、DeepSeek、Qwen/DashScope、Kimi/Moonshot、智谱 GLM、Ollama、vLLM、
LM Studio 等都可以用这一个类，区别只在 base_url、api_key 和 model。

两个关键适配点：
  1. 内部 Anthropic 风格消息 -> OpenAI 的 system/user/assistant/tool 消息；
  2. 流式 delta 里的 tool_calls 分片按 index 拼接，最后解析成 ToolUseBlock。

推理模型（o1/o3/o4/gpt-5、*-reasoner、qwq 等）使用 max_completion_tokens，
并且不接受 temperature；DeepSeek 的 reasoning_content 会转成 ThinkingDelta。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

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
    ModelError,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnDone,
    _raise_for_status,
    iter_sse,
    usage_from_openai,
)


def _result_text(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            # OpenAI 协议不允许在 role="tool" 消息里放图片，无法像 Anthropic
            # 那样透传 base64。这里给出明确的占位说明（含文件名/类型），
            # 让模型知道"这里本应有一张图"，而不是静默丢失、毫无提示。
            name = getattr(block, "filename", None)
            label = name or block.media_type
            parts.append(f"[图片已省略：{label}；当前供应商不支持在工具结果中传图]")
    return "\n".join(parts) or "(empty)"


def convert_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        # OpenAI 要求工具结果放在独立的 role="tool" 消息里，且必须紧跟在
        # 带 tool_calls 的 assistant 消息之后；因此这里先拆出 ToolResultBlock。
        tool_results = [b for b in message.content if isinstance(b, ToolResultBlock)]
        others = [b for b in message.content if not isinstance(b, ToolResultBlock)]

        if message.role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input or {}, ensure_ascii=False),
                        },
                    })
            if not text_parts and not tool_calls:
                continue
            item: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
            }
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
            continue

        # user turn: tool results must directly follow the assistant tool calls
        for result in tool_results:
            out.append({
                "role": "tool",
                "tool_call_id": result.tool_use_id,
                "content": _result_text(result.content),
            })

        texts: list[str] = []
        images: list[ImageBlock] = []
        for block in others:
            if isinstance(block, TextBlock):
                texts.append(block.text)
            elif isinstance(block, ImageBlock):
                images.append(block)
        if images:
            parts: list[dict[str, Any]] = []
            if texts:
                parts.append({"type": "text", "text": "\n".join(texts)})
            for image in images:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": image.to_data_url()},
                })
            out.append({"role": "user", "content": parts})
        elif texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _is_openai_reasoning_model(model: str) -> bool:
    """OpenAI 自家推理模型（o1/o3/o4/gpt-5）必须用 max_completion_tokens。"""
    return model.lower().startswith(("o1", "o3", "o4", "gpt-5"))


def _is_reasoning_model(model: str) -> bool:
    """判断是否为“推理模型”，这类模型通常不接受 temperature。"""
    m = model.lower()
    return (
        _is_openai_reasoning_model(m)
        or "reasoner" in m
        or "thinking" in m
        or m.startswith(("qwq", "deepseek-r1"))
    )


class OpenAIProvider(Provider):
    name = "openai"
    supports_thinking = True

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "user-agent": "yotcode/0.1.0",
        }
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _body(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int,
        temperature: float | None,
        stream: bool,
        include_usage: bool = True,
    ) -> dict[str, Any]:
        converted: list[dict[str, Any]] = []
        if system:
            converted.append({"role": "system", "content": system})
        converted.extend(convert_messages(messages))
        body: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "stream": stream,
        }
        reasoning = _is_reasoning_model(self.model)
        # 注意：DeepSeek reasoner / Qwen QwQ 等兼容接口仍使用 max_tokens；
        # 只有 OpenAI 自家 o 系列 / gpt-5 要求 max_completion_tokens。
        if _is_openai_reasoning_model(self.model):
            body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens
        if temperature is not None and not reasoning:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema") or {"type": "object"},
                    },
                }
                for tool in tools
            ]
            body["tool_choice"] = "auto"
        if stream and include_usage:
            body["stream_options"] = {"include_usage": True}
        return body

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
        # 某些自建网关不认 stream_options，会在 400 报错里提到它；
        # 遇到这种情况自动去掉该参数重试一次。
        include_usage = True
        for attempt in range(2):
            body = self._body(
                system=system, messages=messages, tools=tools,
                max_tokens=max_tokens, temperature=temperature,
                stream=True, include_usage=include_usage,
            )
            if self._http_logger:
                self._sse_capture.set([])
            async with self._client.stream(
                "POST", self._url(), headers=self._headers(), json=body
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    if (
                        response.status_code == 400
                        and include_usage
                        and "stream_options" in raw.lower()
                    ):
                        include_usage = False
                        continue
                    _raise_for_status(
                        response.status_code, raw, response.headers.get("retry-after")
                    )
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    payload = json.loads((await response.aread()).decode("utf-8", "replace"))
                    async for event in self._events_from_completion(payload):
                        yield event
                    return
                yield_stream = self._parse_stream(response, cancel)
                async for event in yield_stream:
                    yield event
                # 流读取完毕，回填 SSE 原文；非 SSE（如 application/json）分支已在 hook 里记全
                if "event-stream" in content_type:
                    self.log_sse_body()
                return
        raise ModelError("OpenAI-compatible request failed after retry")

    async def _events_from_completion(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        if payload.get("error"):
            err = payload["error"]
            raise ModelError(
                err.get("message") if isinstance(err, dict) else str(err),
                retryable=True,
            )
        choices = payload.get("choices") or []
        content_blocks: list[Any] = []
        if choices:
            message = choices[0].get("message") or {}
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if reasoning:
                content_blocks.append(ThinkingBlock(str(reasoning)))
                yield ThinkingDelta(str(reasoning))
            text = message.get("content")
            if isinstance(text, list):
                text = "".join(
                    p.get("text", "") for p in text if isinstance(p, dict)
                )
            if text:
                content_blocks.append(TextBlock(str(text)))
                yield TextDelta(str(text))
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_raw_input": fn.get("arguments")}
                block = ToolUseBlock(
                    id=call.get("id") or "toolu_openai",
                    name=fn.get("name") or "",
                    input=args if isinstance(args, dict) else {"value": args},
                )
                content_blocks.append(block)
                yield ToolUseReady(block)
            stop = choices[0].get("finish_reason") or "stop"
        else:
            stop = "stop"
        usage = usage_from_openai(payload.get("usage") or {})
        yield TurnDone(
            content=content_blocks,
            usage=usage,
            stop_reason="tool_use" if stop in ("tool_calls", "function_call") else stop,
        )

    async def _parse_stream(
        self, response, cancel: asyncio.Event | None
    ) -> AsyncIterator[StreamEvent]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        stop_reason = "stop"
        async for data in iter_sse(response, self._sse_capture.get()):
            self.check_cancel(cancel)
            if data.get("error"):
                err = data["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise ModelError(message or "API error", retryable=True)
            if data.get("usage"):
                usage = usage_from_openai(data["usage"])
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                    yield ThinkingDelta(str(reasoning))
                content = delta.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    yield TextDelta(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("text"):
                            text_parts.append(part["text"])
                            yield TextDelta(part["text"])
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    entry = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                    if call.get("id"):
                        entry["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        entry["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["args"] += fn["arguments"]
                if choice.get("finish_reason"):
                    stop_reason = choice["finish_reason"]

        content: list[Any] = []
        if reasoning_parts:
            content.append(ThinkingBlock("".join(reasoning_parts)))
        if text_parts:
            content.append(TextBlock("".join(text_parts)))
        for index in sorted(calls):
            entry = calls[index]
            if not entry.get("name"):
                continue
            args: Any
            try:
                args = json.loads(entry.get("args") or "{}")
            except json.JSONDecodeError:
                args = {"_raw_input": entry.get("args")}
            block = ToolUseBlock(
                id=entry.get("id") or f"toolu_{index:04d}",
                name=entry["name"],
                input=args if isinstance(args, dict) else {"value": args},
            )
            content.append(block)
            yield ToolUseReady(block)

        if usage.total_tokens == 0:
            # 部分本地服务器流式响应里不带 usage，只能粗略估算输出 token。
            usage = Usage(output_tokens=max(1, len("".join(text_parts)) // 4))
        yield TurnDone(
            content=content,
            usage=usage,
            stop_reason="tool_use" if stop_reason in ("tool_calls", "function_call") else stop_reason,
        )

    async def _complete_once(self, *, system: str, messages: list[Message],
                             max_tokens: int, temperature: float) -> str:
        body = self._body(
            system=system, messages=messages, tools=[],
            max_tokens=max_tokens, temperature=temperature, stream=False,
        )
        response = await self._client.post(self._url(), headers=self._headers(), json=body)
        if response.status_code >= 400:
            _raise_for_status(
                response.status_code, response.text, response.headers.get("retry-after")
            )
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        text = (choices[0].get("message") or {}).get("content")
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return (text or "").strip()
