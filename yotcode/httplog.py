"""HTTP 报文日志：原汁原味记录发给大模型的 HTTP 请求/响应。

和 apilog.py（结构化摘要）不同，这个模块记录的是**网络层真实报文**：
  - 请求行、请求头（API Key 脱敏）、请求体（实际发送的 JSON 字节）
  - 响应状态行、响应头、响应体（SSE 流式逐条 data 原文 / 非流式 JSON 原文）

日志文件：项目根目录/.yotcode/api-http.txt（追加模式，历史永久保留）。
通过 httpx 的 event_hooks 在网络层拦截，因此无论哪个供应商都自动覆盖。

重要行为（为研究日志而设计）：
  - 追加模式：每次启动、每次请求都往同一个文件尾部追加，绝不覆盖历史。
  - 不截断：请求体、响应体、SSE 流式每条 data 原文全部完整写入。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


# 请求头里这些字段含密钥，只显示前后少量字符
_SECRET_HEADERS = {"authorization", "x-api-key", "api-key"}


class HttpLogger:
    """把真实 HTTP 请求/响应写成报文风格的文本日志。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = True
        self._seq = 0

    def clear(self) -> None:
        """确保日志文件存在并以追加方式写入会话分隔（绝不清空历史）。"""
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            marker = (
                "\n\n"
                + "#" * 72 + "\n"
                + f"# HTTP 报文日志 · 新会话开始  {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + "#" * 72 + "\n"
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(marker)
        except OSError:
            self.enabled = False

    def log_request(self, request: httpx.Request) -> None:
        """记录真实发出的请求：请求行 + 全部请求头 + 请求体原文。"""
        if not self.enabled:
            return
        self._seq += 1
        lines = [
            f"【HTTP 请求 #{self._seq}】{datetime.now():%H:%M:%S}",
            f"{request.method} {request.url}",
        ]
        lines.append("── 请求头 (Headers) ──")
        for key, value in request.headers.items():
            lines.append(f"{key}: {_mask(key, value)}")

        lines.append("── 请求体 (Body) ──")
        raw = request.content
        if not raw:
            lines.append("(无请求体)")
        else:
            text = raw.decode("utf-8", errors="replace")
            lines.append(_pretty_json_or_raw(text))
        lines.append("")  # 请求与响应之间留空行，便于区分
        self._write("\n".join(lines))

    def log_response(self, response: httpx.Response) -> None:
        """记录非流式响应：状态行 + 全部响应头 + 响应体原文。"""
        if not self.enabled:
            return
        lines = [
            f"── HTTP 响应 #{self._seq} ──",
            f"{_http_version(response)} {response.status_code} {response.reason_phrase}",
        ]
        lines.append("── 响应头 (Headers) ──")
        for key, value in response.headers.items():
            lines.append(f"{key}: {_mask(key, value)}")
        lines.append("── 响应体 (Body) ──")
        body = _response_text(response)
        lines.append(_pretty_json_or_raw(body) if body else "(无响应体)")
        lines += ["", "=" * 72, ""]
        self._write("\n".join(lines))

    def log_sse_response(self, response: httpx.Response) -> None:
        """SSE 流式响应：先记状态行和响应头，body 留待 log_sse_data 回填。"""
        if not self.enabled:
            return
        lines = [
            f"── HTTP 响应 #{self._seq} ──",
            f"{_http_version(response)} {response.status_code} {response.reason_phrase}",
        ]
        lines.append("── 响应头 (Headers) ──")
        for key, value in response.headers.items():
            lines.append(f"{key}: {_mask(key, value)}")
        lines.append("── 响应体 (Body, SSE 流式) ──")
        self._write("\n".join(lines))  # 不换行结尾，等 body 追加

    def log_sse_data(self, sse_lines: list[str]) -> None:
        """把流式响应逐条 data 原文追加到刚写的响应体后面（完整不截断）。"""
        if not self.enabled:
            return
        lines = []
        if sse_lines:
            lines.append(f"[共 {len(sse_lines)} 条 data 事件]")
            for index, payload in enumerate(sse_lines, 1):
                lines.append(f"data #{index}: {payload}")
        else:
            lines.append("(无 data 事件)")
        lines += ["", "=" * 72, ""]
        self._write("\n".join(lines))

    def _write(self, text: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            self.enabled = False


def _mask(key: str, value: str) -> str:
    """对密钥类请求头脱敏，其余原样显示。"""
    if key.lower() not in _SECRET_HEADERS or len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}  <已脱敏>"


def _http_version(response: httpx.Response) -> str:
    """返回状态码前的协议串，如 HTTP/1.1（httpx 已带 HTTP/ 前缀）。"""
    version = getattr(response, "http_version", None)
    if version:
        return str(version)  # httpx 里是 "HTTP/1.1"
    return "HTTP/1.1"


def _response_text(response: httpx.Response) -> str:
    """安全读取响应体；流式响应未读会报错，返回空。"""
    try:
        content = response.content
    except (httpx.ResponseNotRead, RuntimeError):
        return ""
    return content.decode("utf-8", errors="replace")


def _pretty_json_or_raw(text: str) -> str:
    """尽量按 JSON 美化输出；不是 JSON 就原样输出，绝不截断。"""
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            text = json.dumps(json.loads(stripped), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            pass  # 不是合法 JSON，保持原文
    return text
