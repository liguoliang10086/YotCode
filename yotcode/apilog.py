"""API 交互日志：把"发给大模型的请求"和"大模型返回的内容"完整写进文本日志。

日志文件位置：项目根目录/.yotcode/api-log.txt

重要行为（为研究日志而设计）：
  - 追加模式：每次启动、每次调用都往同一个文件尾部追加，绝不覆盖历史。
  - 不截断：system 提示词、全部 messages、每个流式事件的完整内容、工具结果、
    甚至图片的 base64 数据，全部原样完整写入。文件会持续增长，请自行管理磁盘。
  - 日志失败绝不影响正常对话：写文件出错时静默关闭日志。

记录内容（每一轮模型请求一条）：
  - 请求：system 提示词、messages（每条消息的内容块，标准 JSON）、工具清单、参数
  - 响应：每个流式事件的完整内容（thinking / text / tool_use / usage）
  - 元信息：时间、供应商、模型、耗时、第几次调用
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import Message, block_to_dict


class ApiLogger:
    """把每次请求/响应完整追加进日志文件（不截断、不清空历史）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = True
        self._call_no = 0          # 本次进程里第几次调用模型
        self._started_at = time.time()

    def clear(self) -> None:
        """确保日志文件存在并写入一个会话分隔标记。

        注意：这里【不会清空】已有内容——只用追加方式写一条"新会话开始"分隔，
        历史日志永久保留，方便跨多次启动研究。
        """
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            marker = (
                "\n\n"
                + "#" * 72 + "\n"
                + f"# 新会话开始  {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + "#" * 72 + "\n"
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(marker)
        except OSError:
            self.enabled = False

    def start_call(self) -> dict[str, Any]:
        """开始一次模型调用，返回上下文字典供 record_event / finish_call 使用。"""
        self._call_no += 1
        return {
            "call_no": self._call_no,
            "started": time.time(),
            "events": [],          # (相对秒, 事件类型, 完整内容)
            "parts": {"text": [], "thinking": [], "tool": []},
        }

    def record_request(
        self,
        ctx: dict[str, Any],
        *,
        provider: str,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        max_tokens: int | None,
        temperature: float | None,
        thinking: bool,
    ) -> None:
        """把发出去的完整请求写进日志（system / messages / 工具 / 参数全部完整）。"""
        if not self.enabled:
            return
        lines = [
            f"【第 {ctx['call_no']} 次模型调用】"
            f"{datetime.now():%H:%M:%S}  provider={provider}  model={model}",
            "",
            "─── 请求 (REQUEST) ───",
            f"参数: max_tokens={max_tokens}  temperature={temperature}  thinking={thinking}",
            f"工具数: {len(tools)}  ->  {', '.join(t.get('name', '?') for t in tools) or '(无)'}",
            "",
            "system 提示词:",
        ]
        lines.append(_indent(system) if system else "(空)")
        lines.append("")
        lines.append("messages（发给模型的完整对话历史，标准 JSON 格式）:")
        for index, msg in enumerate(messages):
            lines.append(f"  [{index}] role={msg.role}")
            for block in msg.content:
                lines.append(_indent(_format_block_json(block), 6))
        lines.append("")
        # 完整的工具 schema 也写进去，方便研究发给模型的工具定义
        if tools:
            lines.append("tools schema（发给模型的完整工具定义，标准 JSON 格式）:")
            lines.append(json.dumps(tools, ensure_ascii=False, indent=2))
            lines.append("")
        self._write("\n".join(lines))

    def record_event(self, ctx: dict[str, Any], event: Any) -> None:
        """记录流式返回里的每一个事件的【完整内容】（不截断）。"""
        if not self.enabled:
            return
        offset = time.time() - ctx["started"]
        kind = type(event).__name__
        if kind == "TextDelta":
            ctx["parts"]["text"].append(event.text)
            content = event.text
        elif kind == "ThinkingDelta":
            ctx["parts"]["thinking"].append(event.text)
            content = event.text
        elif kind == "ToolUseReady":
            blk = event.block
            ctx["parts"]["tool"].append(blk)
            # 工具名 + 完整参数 JSON（不截断）
            content = f"{blk.name} {json.dumps(blk.input, ensure_ascii=False)}"
        elif kind == "TurnDone":
            # 结束事件：stop_reason + 用量 + 完整内容块
            usage = getattr(event, "usage", None)
            usage_json = _usage_to_json(usage)
            content = (
                f"stop_reason={event.stop_reason}  blocks={len(event.content)}  "
                f"usage={usage_json}"
            )
        else:
            content = str(event)
        ctx["events"].append((offset, kind, content))

    def finish_call(self, ctx: dict[str, Any], *, error: str = "") -> None:
        """把响应汇总和耗时写进日志（每次调用结束必须调用）。"""
        if not self.enabled:
            return
        elapsed = time.time() - ctx["started"]
        parts = ctx["parts"]
        lines = [
            "─── 响应 (RESPONSE) ───",
            f"耗时: {elapsed:.2f}s",
        ]
        if error:
            lines.append(f"错误: {error}")
        if parts["thinking"]:
            lines += ["", "thinking（思考过程，完整）:", _indent("".join(parts["thinking"]))]
        if parts["text"]:
            lines += ["", "text（正文回答，完整）:", _indent("".join(parts["text"]))]
        if parts["tool"]:
            lines += ["", "tool_use（工具调用请求，完整）:"]
            for blk in parts["tool"]:
                lines.append(_indent(f"{blk.name}: {json.dumps(blk.input, ensure_ascii=False)}"))
        # 每个流式事件的完整内容（不截断，含时间戳）
        lines += ["", f"事件流共 {len(ctx['events'])} 个事件（完整内容）:"]
        for offset, kind, content in ctx["events"]:
            lines.append(f"  +{offset:6.2f}s  {kind:<14} {content}")
        lines += ["", "=" * 72, ""]
        self._write("\n".join(lines))

    def _write(self, text: str) -> None:
        """以追加模式写入（绝不覆盖）。"""
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            self.enabled = False


def _indent(text: str, spaces: int = 4) -> str:
    """给多行文本统一加缩进，方便在日志里区分层级。"""
    pad = " " * spaces
    return "\n".join(pad + line for line in str(text).splitlines())


def _usage_to_json(usage: Any) -> str:
    """把 Usage 对象转成 JSON 字符串（不截断）。"""
    if usage is None:
        return "null"
    if isinstance(usage, dict):
        return json.dumps(usage, ensure_ascii=False)
    data = getattr(usage, "__dict__", None)
    if data is not None:
        return json.dumps(data, ensure_ascii=False, default=str)
    return str(usage)


def _format_block_json(block: Any) -> str:
    """把内容块转成标准 JSON（双引号、true/false），完整保留所有内容不截断。"""
    if type(block).__name__ == "ImageBlock":
        # 图片 base64 也完整写入（用户要求不截断）；有文件名则一并记录。
        payload = {
            "type": "image",
            "media_type": block.media_type,
            "data": block.data,
        }
        filename = getattr(block, "filename", None)
        if filename:
            payload["filename"] = filename
        return json.dumps(payload, ensure_ascii=False)
    data = block_to_dict(block)
    return json.dumps(data, ensure_ascii=False, indent=2)
