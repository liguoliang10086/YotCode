"""供应商抽象层：统一的流式事件、重试与错误转换。

上层 Agent 只认识四种事件：
  TextDelta     一段正文增量
  ThinkingDelta 一段思考/推理增量
  ToolUseReady  一个完整的工具调用（参数已拼好 JSON）
  TurnDone      本轮结束，带完整内容、usage 和 stop_reason

具体供应商负责把自家 SSE 协议翻译成这些事件。网络错误、429、5xx 在
stream() 里统一重试；一旦已经输出过内容就不再重试，避免 UI 出现重复文本。
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, ClassVar, Union

import httpx

from ..types import Message, ToolUseBlock, Usage


# --------------------------------------------------------------------------
# events —— 供应商和 Agent 之间的"通用语言"
# --------------------------------------------------------------------------
# 每家大模型 API 返回的流式数据格式都不一样。这里定义了 4 种统一事件，
# anthropic.py / openai_compat.py 负责把自家格式翻译过来，Agent 只认这 4 种。
@dataclass
class TextDelta:
    text: str  # 一小段正文（模型一个字一个字吐出来的）


@dataclass
class ThinkingDelta:
    text: str  # 一小段"思考过程"（部分模型支持）


@dataclass
class ToolUseReady:
    block: ToolUseBlock  # 一个拼装好的工具调用（参数 JSON 已完整）


@dataclass
class TurnDone:
    content: list[Any]   # 本轮完整内容块列表
    usage: Usage         # 用了多少 token
    stop_reason: str     # 为什么停（正常结束/工具调用/超长等）


# Union 表示 StreamEvent 可以是上面 4 种里的任意一种
StreamEvent = Union[TextDelta, ThinkingDelta, ToolUseReady, TurnDone]


class ModelError(Exception):
    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None,
                 kind: str = "api_error"):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.kind = kind


class Interrupted(Exception):
    """Raised when the user presses escape / ctrl+c mid-turn."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def make_timeout(read: float = 600.0) -> httpx.Timeout:
    return httpx.Timeout(connect=30.0, read=read, write=60.0, pool=30.0)


async def iter_sse(
    response: httpx.Response, capture: list[str] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """解析 SSE（Server-Sent Events）流式响应，逐条产出 JSON 数据。

    SSE 是大模型 API 常用的推送格式：服务器一行一行发数据，每行以 "data:" 开头。
    AsyncIterator + yield 表示调用方可以用 `async for data in iter_sse(...)` 边收边处理，
    不用等全部数据到齐 —— 这就是你能看到文字逐字出现的原因。

    capture 不为 None 时，把每条 data 原文（含 [DONE]）追加进去，供 HTTP 日志记录。
    """
    async for line in response.aiter_lines():  # 异步逐行读取（不阻塞程序）
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()  # 去掉 "data:" 前缀和空白
        elif line.startswith("{"):
            payload = line.strip()      # 有些服务直接发裸 JSON，没有前缀
        else:
            continue
        if capture is not None:
            capture.append(payload)     # 原样记录，供 HTTP 报文日志使用
        if payload == "[DONE]":
            return  # OpenAI 兼容接口用这个标记表示流结束
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _raise_for_status(status: int, body: str, retry_after: str | None) -> None:
    message = body.strip()
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                message = err.get("message") or err.get("type") or message
            elif isinstance(err, str):
                message = err
            elif parsed.get("message"):
                message = str(parsed["message"])
    except Exception:
        pass
    message = message[:1200] or f"HTTP {status}"
    retryable = status in (408, 409, 429, 500, 502, 503, 504, 529) or "overloaded" in message.lower()
    delay: float | None = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = None
    kind = "rate_limit" if status == 429 else "server_error" if status >= 500 else "api_error"
    if status == 401:
        kind = "auth_error"
        message += "  (check your API key / base URL)"
    elif status == 404:
        kind = "not_found"
    raise ModelError(message, status=status, retryable=retryable,
                     retry_after=delay, kind=kind)


class Provider(ABC):
    name = "base"
    supports_images = True
    supports_thinking = False

    # 当前请求正在捕获的 SSE data 原文（只给 HTTP 调试日志用）。
    #
    # 用 ContextVar 而不是普通实例属性，是因为现在允许多个请求并发
    # （主代理 + 多个子代理同时问模型）。普通属性会被后一个请求覆盖，
    # 导致日志里两个请求的 SSE 原文混在一起；
    # ContextVar 则是「每个任务一份」，asyncio 每个 Task 都拷贝一份上下文，
    # 天然互不干扰。
    _sse_capture: ClassVar[ContextVar[list[str] | None]] = ContextVar(
        "yotcode_sse_capture", default=None
    )

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 max_tokens: int = 8192, timeout: float = 600.0,
                 max_retries: int = 4) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        # 用 event_hooks 在网络层拦截真实请求/响应，记录原汁原味的 HTTP 报文。
        # hook 闭包引用 self，因此 logger 可以稍后由工厂注入。
        self._http_logger: Any = None
        self._client = httpx.AsyncClient(
            timeout=make_timeout(timeout),
            follow_redirects=True,
            event_hooks={
                "request": [self._on_http_request],
                "response": [self._on_http_response],
            },
        )
        self._closed = False
        self._logger: Any = None  # ApiLogger，工厂函数可选注入

    async def _on_http_request(self, request: httpx.Request) -> None:
        """httpx 请求钩子：记录真正要发出去的报文。"""
        if self._http_logger:
            self._http_logger.log_request(request)

    async def _on_http_response(self, response: httpx.Response) -> None:
        """httpx 响应钩子：记录真实响应。
        非流式响应体此刻已就绪，直接完整记录；SSE 流式响应体尚未读取，
        只记状态行和响应头，body 由 iter_sse 捕获后回填。
        """
        if not self._http_logger:
            return
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or "event-stream" in content_type:
            self._http_logger.log_sse_response(response)
        else:
            self._http_logger.log_response(response)

    def log_sse_body(self) -> None:
        """流式响应读完后调用，把捕获到的 SSE 原文完整写进 HTTP 日志（不截断）。"""
        capture = self._sse_capture.get()
        if not self._http_logger or capture is None:
            return
        self._http_logger.log_sse_data(capture)
        self._sse_capture.set(None)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    def set_logger(self, logger: Any) -> None:
        """由工厂函数注入 ApiLogger（可选）。"""
        self._logger = logger

    # -- public API --------------------------------------------------------
    async def stream(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = False,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # 一旦已经向 UI 输出过内容，就不能安全重试，否则会出现重复的半截回答。
        emitted = False
        last_error: ModelError | None = None
        for attempt in range(self.max_retries):
            # 每次尝试（含重试）单独记一次日志上下文，便于对比失败原因
            log_ctx = self._logger.start_call() if self._logger else None
            if log_ctx and attempt == 0:  # 重试不重复写请求，只在首次写
                self._logger.record_request(
                    log_ctx, provider=self.name, model=self.model,
                    system=system, messages=messages, tools=tools,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature, thinking=thinking,
                )
            try:
                async for event in self._stream_once(
                    system=system,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature,
                    thinking=thinking,
                    cancel=cancel,
                ):
                    if isinstance(event, (TextDelta, ThinkingDelta, ToolUseReady)):
                        emitted = True
                    if log_ctx:
                        self._logger.record_event(log_ctx, event)
                    yield event
                if log_ctx:
                    self._logger.finish_call(log_ctx)
                return
            except Interrupted:
                if log_ctx:
                    self._logger.finish_call(log_ctx, error="用户打断")
                raise
            except ModelError as exc:
                last_error = exc
                if log_ctx:
                    self._logger.finish_call(log_ctx, error=f"ModelError: {exc}")
                if emitted or not exc.retryable or attempt >= self.max_retries - 1:
                    raise
                delay = exc.retry_after if exc.retry_after is not None else min(2 ** attempt, 8)
                await asyncio.sleep(delay)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                    httpx.WriteError, httpx.PoolTimeout) as exc:
                if log_ctx:
                    self._logger.finish_call(log_ctx, error=f"网络错误: {exc}")
                if emitted or attempt >= self.max_retries - 1:
                    raise ModelError(
                        f"Network error talking to {self.base_url}: {exc}",
                        retryable=True, kind="network_error",
                    ) from exc
                await asyncio.sleep(min(2 ** attempt, 8))
            finally:
                # 无论正常结束、用户打断还是中途报错，都把已捕获的 SSE 原文
                # 回填到 HTTP 日志（幂等：写入后置空，重复调用无副作用）。
                self.log_sse_body()
        if last_error:
            raise last_error
        raise ModelError("Model request failed")

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> str:
        last_error: ModelError | None = None
        for attempt in range(3):
            try:
                return await self._complete_once(
                    system=system, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                )
            except ModelError as exc:
                last_error = exc
                if not exc.retryable or attempt >= 2:
                    raise
                await asyncio.sleep(min(2 ** attempt, 6))
        if last_error:
            raise last_error
        return ""

    # -- to implement ------------------------------------------------------
    @abstractmethod
    def _stream_once(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        ...

    @abstractmethod
    async def _complete_once(self, **kwargs: Any) -> str:
        ...

    # -- shared helpers ----------------------------------------------------
    @staticmethod
    def check_cancel(cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise Interrupted()


def usage_from_anthropic(data: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        cache_creation_input_tokens=int(data.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(data.get("cache_read_input_tokens") or 0),
    )


def usage_from_openai(data: dict[str, Any]) -> Usage:
    details = data.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    prompt = int(data.get("prompt_tokens") or 0)
    completion = int(data.get("completion_tokens") or 0)
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=completion,
        cache_read_input_tokens=cached,
    )
