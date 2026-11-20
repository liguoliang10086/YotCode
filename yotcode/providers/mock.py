"""离线模拟供应商：不需要 API Key 就能跑通整个流程（yot --mock）。

Deterministic offline provider used for demos and tests.

为什么需要它：
  * 新手体验：没有 Key 也能 run.cmd --mock 看完整界面和工具链；
  * 自动化测试：返回内容固定（deterministic），测试断言才稳定。

它和其他供应商一样实现 Provider.stream()，会"假装"流式吐出一段 Markdown
文本，并在你问到特定关键词时模拟工具调用，从而把 agent.py 的主循环
（请求 -> 工具 -> 回填 -> 再来一轮）完整走一遍。真实请求不会发生。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from ..types import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .base import Provider, StreamEvent, TextDelta, ThinkingDelta, ToolUseReady, TurnDone


# 经典 conhost（cmd.exe）的控制台字体不含 emoji 字形，实测 📖 ✏️ 🖥️ 🌐 🗂️ 🤖
# 会被渲染成方框（豆腐块）。这里统一改用「实测一定能显示」的几何符号：
# ▤ ▣ » → ▦ ◆ —— 语义对应且在 cmd.exe / Windows Terminal 下都正常。
DEMO_TEXT = """## 你好，我是 **YotCode**

这是一个 Python 复刻版的 Claude Code —— 终端里的 AI 编程搭档。当前运行在 **mock 模式**（无需 API Key），所有 UI 与工具链都是真实的。

### 我可以做什么

- ▤ 读取、搜索并理解整个代码库
- ▣ 精确编辑文件（带 diff 预览）
- » 执行 shell 命令、后台任务
- → 抓取网页、搜索资料
- ▦ 维护 TODO 列表，拆解复杂任务
- ◆ 派生子 agent 并行探索代码
- ⛓ 用任务图编排多步工作（依赖、并发、失败重试）
- ⏪ 改文件前自动留检查点，一条命令就能回退

### 快速开始

```bash
# 配置密钥后即可连接真实模型
export ANTHROPIC_API_KEY=sk-ant-...
yot
```

> 提示：输入 `/help` 查看所有命令；按 `Shift+Tab` 切换权限模式；`Esc` 可随时打断生成。

试着让我「列出当前目录的文件」，我会真的调用工具去查看。"""


class MockProvider(Provider):
    name = "mock"
    supports_images = False

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("api_key", "")
        kwargs.setdefault("base_url", "mock://local")
        kwargs.setdefault("model", "mock-claude")
        super().__init__(**kwargs)
        self._turns = 0

    async def _stream_once(self, *, system: str, messages: list[Message],
                           tools: list[dict[str, Any]], max_tokens: int,
                           temperature: float | None, thinking: bool,
                           cancel: asyncio.Event | None = None) -> AsyncIterator[StreamEvent]:
        self._turns += 1
        last = messages[-1] if messages else None
        last_text = last.text() if last else ""
        has_results = bool(
            last and any(isinstance(b, ToolResultBlock) for b in last.content)
        )

        if has_results:
            results = [b for b in last.content if isinstance(b, ToolResultBlock)]
            lines = []
            for result in results[-3:]:
                text = str(result.content)
                preview = "\n".join(text.splitlines()[:12])
                lines.append(f"```text\n{preview}\n```")
            summary = (
                "### 工具执行完成\n\n"
                + "\n\n".join(lines)
                + "\n\n这就是真实的工具调用结果 —— 换成真实模型后，我会根据它们继续推理。"
            )
            async for event in self._emit_text(summary, cancel):
                yield event
            return

        lowered = last_text.lower()
        if any(k in lowered for k in ("列", "list", "目录", "文件")):
            block = ToolUseBlock.new("LS", {"path": ".", "ignore": []})
            yield ToolUseReady(block)
            for event in self._done_with([block]):
                yield event
            return
        if "todo" in lowered or "待办" in lowered:
            block = ToolUseBlock.new("TodoWrite", {"todos": [
                {"content": "阅读项目结构", "status": "completed", "activeForm": "阅读项目结构"},
                {"content": "实现一个 demo 功能", "status": "in_progress", "activeForm": "实现 demo 功能"},
                {"content": "运行测试并验证", "status": "pending", "activeForm": "运行测试"},
            ]})
            yield ToolUseReady(block)
            for event in self._done_with([block]):
                yield event
            return

        if thinking:
            thinking_text = (
                "用户在体验这个 Python 版 Claude Code。先展示一段结构清晰、"
                "带代码块和列表的 markdown，再提示可用命令。"
            )
            for chunk in self._chunks(thinking_text, 12):
                self.check_cancel(cancel)
                yield ThinkingDelta(chunk)
                await asyncio.sleep(0.01)

        async for event in self._emit_text(DEMO_TEXT, cancel):
            yield event
        yield self._done(DEMO_TEXT)

    async def _emit_text(self, text: str, cancel: asyncio.Event | None) -> AsyncIterator[StreamEvent]:
        for chunk in self._chunks(text, 18):
            self.check_cancel(cancel)
            yield TextDelta(chunk)
            await asyncio.sleep(0.006)

    @staticmethod
    def _chunks(text: str, size: int) -> list[str]:
        return [text[i:i + size] for i in range(0, len(text), size)]

    def _done_with(self, blocks: list[Any]) -> list[StreamEvent]:
        return [TurnDone(
            content=blocks,
            usage=Usage(input_tokens=120, output_tokens=32),
            stop_reason="tool_use",
        )]

    def _done(self, text: str) -> StreamEvent:
        return TurnDone(
            content=[TextBlock(text)] if text else [],
            usage=Usage(input_tokens=120, output_tokens=max(1, len(text) // 4)),
            stop_reason="end_turn",
        )

    async def _complete_once(self, *, system: str, messages: list[Message],
                             max_tokens: int, temperature: float) -> str:
        last = messages[-1].text() if messages else ""
        return f"(mock summary) {last[:400]}"
