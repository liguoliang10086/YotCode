"""Agent 主循环：流式输出 → 工具调用 → 权限检查 → 结果回填 → 继续。

一次 run() 的流程：
  用户输入
    └─> provider.stream() 逐块产出 TextDelta / ThinkingDelta / ToolUseReady
        └─> 如果模型请求了工具：先跑 PreToolUse hook，再走权限判断
            └─> 执行工具，把 ToolResultBlock 作为 user 消息追加
                └─> 进入下一轮，让模型根据工具结果继续
                    └─> 没有 tool_use / 达到 max_turns / 用户打断时结束

所有消息都保存在 Session 中（JSONL），因此可以 --continue 恢复。
本文件只负责“编排”：API 协议在 providers/，工具实现在 tools/，
权限规则在 permissions.py，上下文压缩在同文件 maybe_compact()。
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .attachments import is_image_path, path_to_block
from .checkpoints import CheckpointStore
from .config import Config
from .hooks import HookRunner
from .memory import memory_summary
from .models import cost_for_usage
from .permissions import (
    PermissionDecision,
    PermissionManager,
    persist_allow_rule,
    suggest_rule,
)
from .prompts import build_system_prompt, compaction_prompt
from .providers.base import (
    Interrupted,
    ModelError,
    Provider,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnDone,
)
from .session import Session, SessionMeta
from .subagents import SubagentSpec, load_subagents
from .tools import (
    ShellManager,
    Tool,
    ToolContext,
    ToolResult,
    create_tools,
    filter_tools,
)
from .tools.interaction import ExitPlanModeTool
from .types import (
    Block,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    assistant_message,
    message_from_dict,
    message_to_dict,
    tool_result_message,
    user_message,
)
from .utils import read_text


# --------------------------------------------------------------------------
# callback plumbing
# --------------------------------------------------------------------------
@dataclass
class PermissionRequest:
    tool: Tool
    data: dict[str, Any]
    summary: str
    suggestion: str
    preview: tuple[str, str, str] | None = None
    kind: str = "tool"  # "tool" | "plan"
    warning: str = ""


class AgentCallbacks:
    """Override the methods you care about. Event callbacks are sync; prompts await."""

    def on_turn_start(self, index: int) -> None: ...
    def on_text(self, text: str) -> None: ...
    def on_thinking(self, text: str) -> None: ...
    def on_tool_start(self, block: ToolUseBlock) -> None: ...
    def on_tool_result(self, block: ToolUseBlock, result: ToolResult,
                       duration: float) -> None: ...
    def on_notice(self, message: str, level: str = "info") -> None: ...
    def on_todos(self, todos: list[dict[str, Any]]) -> None: ...
    def on_usage(self, usage: Usage, cost: float, total_usage: Usage,
                 total_cost: float) -> None: ...
    def on_turn_end(self, usage: Usage, stop_reason: str,
                    interrupted: bool = False) -> None: ...
    def on_status(self, status: str, detail: str = "") -> None: ...
    def on_compaction(self, summary: str, auto: bool) -> None: ...
    def on_subagent_progress(self, description: str, text: str) -> None: ...
    def on_subagent_start(self, description: str, agent_type: str) -> None: ...
    def on_subagent_end(self, description: str, report: str) -> None: ...

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(False, reason="No interactive UI available to ask for permission")

    async def ask_user(self, question: str, header: str, options: list[str],
                       descriptions: list[str], multi_select: bool) -> str | None:
        return None


class SubagentCallbacks(AgentCallbacks):
    def __init__(self, parent: "Agent", description: str) -> None:
        self.parent = parent
        self.description = description
        self.parts: list[str] = []

    def on_text(self, text: str) -> None:
        self.parts.append(text)
        self.parent.callbacks.on_subagent_progress(self.description, text)

    def on_thinking(self, text: str) -> None:
        self.parent.callbacks.on_subagent_progress(self.description, "")

    def on_tool_start(self, block: ToolUseBlock) -> None:
        self.parent.callbacks.on_subagent_progress(
            self.description, f"\n· {block.name} {_short(block.input)}\n"
        )

    def on_notice(self, message: str, level: str = "info") -> None:
        self.parent.callbacks.on_notice(message, level)

    def on_todos(self, todos: list[dict[str, Any]]) -> None:
        pass

    def on_usage(self, usage: Usage, cost: float, total_usage: Usage,
                 total_cost: float) -> None:
        pass

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        return await self.parent.callbacks.request_permission(request)

    async def ask_user(self, question: str, header: str, options: list[str],
                       descriptions: list[str], multi_select: bool) -> str | None:
        return await self.parent.callbacks.ask_user(
            question, header, options, descriptions, multi_select
        )


def _short(data: dict[str, Any], limit: int = 70) -> str:
    text = ", ".join(
        f"{k}={v}" if not isinstance(v, str) else f"{k}={v!r}"
        for k, v in list(data.items())[:2]
    )
    return text[:limit] + ("..." if len(text) > limit else "")


def _estimate_tokens(messages: list[Message]) -> int:
    chars = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
            elif isinstance(block, ThinkingBlock):
                chars += len(block.thinking)
            elif isinstance(block, ToolUseBlock):
                chars += len(str(block.input)) + len(block.name)
            elif isinstance(block, ToolResultBlock):
                content = block.content
                if isinstance(content, str):
                    chars += len(content)
                else:
                    chars += sum(len(getattr(b, "text", "")) for b in content)
    return int(chars / 3.2) + 32 * len(messages)


def _render_transcript(messages: list[Message], limit: int = 180_000) -> str:
    parts: list[str] = []
    for message in messages:
        parts.append(f"### {message.role.upper()}")
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                parts.append(block.text)
            elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                parts.append(f"[thinking] {block.thinking[:1500]}")
            elif isinstance(block, ToolUseBlock):
                parts.append(f"[tool_use {block.name}] {_short(block.input, 800)}")
            elif isinstance(block, ToolResultBlock):
                content = block.content
                if isinstance(content, list):
                    content = "\n".join(getattr(b, "text", "[image]") for b in content)
                text = str(content)
                if len(text) > 2500:
                    text = text[:2000] + f"\n... [{len(text) - 2500} chars omitted] ...\n" + text[-500:]
                parts.append(f"[tool_result{' ERROR' if block.is_error else ''}] {text}")
        parts.append("")
    out = "\n".join(parts)
    if len(out) > limit:
        out = out[: limit // 2] + "\n\n... [middle of transcript omitted] ...\n\n" + out[-limit // 2:]
    return out


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------
class Agent:
    def __init__(
        self,
        config: Config,
        provider: Provider,
        tools: list[Tool] | None = None,
        *,
        callbacks: AgentCallbacks | None = None,
        session: Session | None = None,
        depth: int = 0,
        permission_lock: asyncio.Lock | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.callbacks = callbacks or AgentCallbacks()
        self.depth = depth
        self.session = session or Session.create(
            config.session_dir, config.cwd, config.model, config.provider
        )
        self.messages: list[Message] = self.session.messages
        self.todos: list[dict[str, Any]] = list(self.session.todos)
        self.permissions = PermissionManager(config)
        self.hooks = HookRunner(config)
        self.cancel = asyncio.Event()
        # 权限弹窗锁：并发执行多个工具时用来把"询问用户"这一步串行化。
        # 子代理会共享父代理的这把锁，保证全局同一时刻只弹一个对话框。
        self._permission_lock = permission_lock or asyncio.Lock()
        self.tools: dict[str, Tool] = {}
        self.set_tools(tools or create_tools())
        self.ctx = ToolContext(
            cwd=config.cwd,
            config=config,
            cancel=self.cancel,
            session_id=self.session.meta.id,
            state={"shells": ShellManager(config.home / "tmp")},
            agent=self,
        )
        self.system_prompt = build_system_prompt(config)
        self.total_usage = self.session.total_usage
        self.total_cost = self.session.total_cost
        self.last_usage = Usage()
        self.turn_index = 0
        self.subagents = load_subagents(config.cwd)
        self.closed = False

    # -- registry ----------------------------------------------------------
    def set_tools(self, tools: list[Tool]) -> None:
        self.tools = {tool.name: tool for tool in tools}

    def add_tools(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.tools[tool.name] = tool

    def tool_specs(self) -> list[dict[str, Any]]:
        tools = filter_tools(
            list(self.tools.values()),
            allowed=None,
            disallowed=self.config.disallowed_tools,
        )
        if self.config.allowed_tools:
            tools = [
                tool for tool in tools
                if any(fnmatch.fnmatch(tool.name, pat) for pat in self.config.allowed_tools)
            ]
        return [tool.spec() for tool in tools if not tool.hidden]

    # -- state -------------------------------------------------------------
    def set_todos(self, todos: list[dict[str, Any]]) -> None:
        self.todos = todos
        self.session.set_todos(todos)
        self.callbacks.on_todos(todos)

    def set_permission_mode(self, mode: str) -> None:
        self.permissions.set_mode(mode)
        self.config.permission_mode = mode
        self.refresh_system_prompt()
        self.callbacks.on_status("mode", mode)

    def refresh_system_prompt(self) -> None:
        self.system_prompt = build_system_prompt(self.config)

    def interrupt(self) -> None:
        self.cancel.set()

    def reset_conversation(self) -> None:
        """Clear the transcript (used by /clear)."""
        self.messages.clear()
        self.session.reset("clear")
        self.session.messages = self.messages
        self.todos = []
        self.last_usage = Usage()
        self.total_usage = Usage()
        self.total_cost = 0.0
        self.turn_index = 0
        self.callbacks.on_todos([])

    async def set_model(self, model: str, provider: str | None = None,
                        api_key: str | None = None,
                        base_url: str | None = None) -> None:
        """Switch models/providers without losing the conversation."""
        from .providers import create_provider

        if provider:
            self.config.provider = provider
        if api_key is not None:
            self.config.api_key = api_key
        if base_url:
            self.config.base_url = base_url
        self.config.model = model
        old_provider = self.provider
        self.provider = create_provider(self.config)
        try:
            await old_provider.aclose()
        except Exception:
            pass
        self.session.set_model(self.config.model, self.config.provider)
        self.refresh_system_prompt()
        self.callbacks.on_status("model", model)

    def stats(self) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "turns": self.turn_index,
            "input_tokens": self.total_usage.prompt_tokens,
            "output_tokens": self.total_usage.output_tokens,
            "usage": self.total_usage,
            "cost": self.total_cost,
            "session_id": self.session.meta.id,
            "todos": self.todos,
        }

    # -- main loop ---------------------------------------------------------
    async def run(self, prompt: str | list[Block] | None = None, *, max_turns: int | None = None,
                  auto_compact: bool = True) -> str:
        """★ 整个项目最核心的方法：AI 助手的"思考-行动"主循环。

        下面是逐行阅读指南（配合 docs/代码导读.md 第五节的流程图看）：

        1. 如果传了 prompt（用户刚说的话），先把它存进会话，并跑 UserPromptSubmit
           钩子；钩子可以往消息里塞额外上下文，甚至直接拦下这句话。
        2. 进入 for 循环，每一轮就是"向大模型请求一次"：
           - maybe_compact()：历史太长时先压缩，避免超出模型的上下文长度；
           - _stream_assistant()：向大模型发请求，边收边把文字/工具调用拼起来；
           - 把模型这一轮的输出作为"助手消息"追加进历史；
           - 如果输出里没有工具调用，说明模型已经答完，循环结束；
           - 如果有工具调用：逐个执行（含权限检查），把结果作为"用户消息"
             回填，然后进入下一轮，让模型看到结果继续干活。
        3. 循环结束的三个出口：模型不再要工具 / 达到 max_turns / 用户打断。
        最后跑 Stop 钩子并返回最后一段文字。

        参数说明：
          prompt: 用户这句话。可以是纯文本 str，也可以是内容块列表 list[Block]
                  （图片/文件附件）；None 表示继续已有历史（比如 --continue）
          max_turns: 最多循环多少轮，防止无限调用
          auto_compact: 是否自动压缩过长的历史
        """
        self.cancel.clear()                                  # 清掉上一次遗留的"取消"信号
        max_turns = max_turns or self.config.max_turns       # 没指定就用配置里的默认值
        if prompt is not None:
            # 统一展开 @mentions：文本附件仍是 <file> 包装，图片附件变成 ImageBlock。
            # 无论入参是 str 还是 list[Block]，这里都归一成 list[Block]。
            blocks = self.expand_mentions(prompt)
            # 若消息里带图片，但当前供应商声明不支持图片，给出可读警告。
            # 仍照常发送（由模型端决定如何响应），只是不让用户困惑"图怎么没反应"。
            if any(isinstance(b, ImageBlock) for b in blocks):
                if not getattr(self.provider, "supports_images", False):
                    self.callbacks.on_notice(
                        "当前模型供应商不支持图片输入，图片可能不会被理解。",
                        "warning",
                    )
            self._append_message(user_message(blocks))
            # hooks 的 prompt 字段要字符串：从块里抽出纯文本，避免把对象塞进 JSON。
            hook_prompt = "\n".join(
                b.text for b in blocks if isinstance(b, TextBlock)
            )
            outcome = await self.hooks.run("UserPromptSubmit", {
                "event": "UserPromptSubmit",
                "prompt": hook_prompt,
                "cwd": str(self.config.cwd),
                "session_id": self.session.meta.id,
            })
            if outcome.context:
                self._append_message(user_message(
                    f"<hook-context>\n{outcome.context}\n</hook-context>"
                ))
            if outcome.blocked:
                self.callbacks.on_notice(
                    f"Prompt blocked by hook: {outcome.reason}", "error"
                )
                return ""

        # ---- 主循环：一次迭代 = 一次模型请求（可能伴随多个工具调用） ----
        # range(max_turns) 会依次给出 0,1,2...max_turns-1，即最多转这么多圈
        final_text = ""          # 累计"最后一段有文字的回答"，循环结束返回它
        interrupted = False      # 记录是不是被用户打断的（最后要告诉 Stop 钩子）
        stop_after = False       # 工具跑完后要不要立刻收尾（被用户打断时置 True）
        for turn in range(max_turns):
            # cancel 是一个 asyncio.Event：界面上按 Esc 时会被 set()，这里检测它
            if self.cancel.is_set():
                interrupted = True
                break
            self.turn_index = turn + 1  # 记录当前是第几轮（JSON 结果里会用到）
            if auto_compact:
                # 历史消息太长就压缩：把旧对话总结成一段，防止超出模型上下文窗口
                await self.maybe_compact()
            if self.cancel.is_set():
                interrupted = True
                break
            self.callbacks.on_status("thinking")  # 通知界面显示"思考中"
            # _stream_assistant() 做好真正的网络请求：把历史消息发给大模型，
            # 流式接收并把零散数据拼成完整内容块；返回 (内容, 用量, 停止原因, 是否被中断)
            content, usage, stop_reason, was_interrupted = await self._stream_assistant()
            if usage.total_tokens:
                # 按模型的价目表把这轮消耗换成美元，累计到总额并写进会话
                cost = cost_for_usage(self.config.model, usage)
                self.total_usage = self.total_usage.add(usage)
                self.total_cost += cost
                self.last_usage = usage
                self.session.add_usage(usage, cost)
                self.callbacks.on_usage(usage, cost, self.total_usage, self.total_cost)
            else:
                self.last_usage = Usage(
                    input_tokens=_estimate_tokens(self.messages),
                    output_tokens=0,
                )
            if content:
                self._append_message(assistant_message(content))
            self.callbacks.on_turn_end(usage, stop_reason, was_interrupted)
            text = "\n".join(
                b.text for b in content if isinstance(b, TextBlock) and b.text.strip()
            )
            if text:
                final_text = text
            if was_interrupted:
                interrupted = True
                self.callbacks.on_status("idle")
                break
            # 从模型这轮输出里挑出"工具调用"块；列表推导式是 Python 常用写法：
            # 遍历 content，只保留类型是 ToolUseBlock 的项
            tool_uses = [b for b in content if isinstance(b, ToolUseBlock)]
            if not tool_uses:
                # 模型没有请求工具，说明它已经给出最终回答。
                self.callbacks.on_status("idle")
                break
            # 模型一轮里可能同时要求多个工具：只读类工具并发跑，有副作用的按序串行。
            # 具体规则见 _execute_tool_batch。
            results = await self._execute_tool_batch(tool_uses)
            if self.cancel.is_set():
                stop_after = True
            # 工具结果必须作为 user 消息回填，模型下一轮才能看到。
            self._append_message(tool_result_message(results))
            if stop_after:
                interrupted = True
                self.callbacks.on_status("idle")
                break
        else:
            self.callbacks.on_notice(
                f"Reached the maximum of {max_turns} turns. Send another message to continue.",
                "warning",
            )

        self.callbacks.on_status("idle")
        await self.hooks.run("Stop", {
            "event": "Stop", "cwd": str(self.config.cwd),
            "session_id": self.session.meta.id, "interrupted": interrupted,
        })
        return final_text

    # -- streaming ---------------------------------------------------------
    async def _stream_assistant(
        self,
    ) -> tuple[list[Any], Usage, str, bool]:
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        ready_tools: list[ToolUseBlock] = []
        usage = Usage()
        stop_reason = "end_turn"
        interrupted = False
        content: list[Any] = []

        self.callbacks.on_turn_start(self.turn_index - 1)
        try:
            async for event in self.provider.stream(
                system=self.system_prompt,
                messages=self._api_messages(),
                tools=self.tool_specs(),
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                thinking=self.config.thinking,
                cancel=self.cancel,
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    self.callbacks.on_text(event.text)
                elif isinstance(event, ThinkingDelta):
                    thinking_parts.append(event.text)
                    self.callbacks.on_thinking(event.text)
                elif isinstance(event, ToolUseReady):
                    ready_tools.append(event.block)
                    self.callbacks.on_tool_start(event.block)
                elif isinstance(event, TurnDone):
                    content = event.content
                    usage = event.usage
                    stop_reason = event.stop_reason
        except Interrupted:
            interrupted = True
        except ModelError as exc:
            self.callbacks.on_notice(f"Model error: {exc}", "error")
            partial = self._partial_content(text_parts, thinking_parts)
            return partial, usage, "error", False

        if interrupted:
            content = self._partial_content(text_parts, thinking_parts)
        if not content and (text_parts or thinking_parts):
            content = self._partial_content(text_parts, thinking_parts)
        return content, usage, stop_reason, interrupted

    @staticmethod
    def _partial_content(text_parts: list[str], thinking_parts: list[str]) -> list[Any]:
        blocks: list[Any] = []
        if thinking_parts:
            blocks.append(ThinkingBlock("".join(thinking_parts)))
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        return blocks

    # -- tool execution ----------------------------------------------------
    def _snapshot_before_change(self, tool: Tool, data: dict[str, Any]) -> None:
        """工具改文件之前，把原内容存成检查点。

        只有会改文件的工具（Write / Edit / MultiEdit）声明了 touched_paths，
        其它工具这里直接跳过。

        整个过程包在 try 里：快照存不成功（比如磁盘满、目录没权限），
        也不能让工具执行失败——用户顶多是没有回退能力，不该因此中断干活。
        """
        try:
            if not self.config.checkpoints_enabled:
                return
            paths = tool.touched_paths(data, self.ctx)
            if not paths:
                return
            store = CheckpointStore(self.config.project_dir)
            store.save(
                session_id=self.session.meta.id,
                tool=tool.name,
                paths=paths,
            )
        except Exception:
            pass

    async def _execute_tool_batch(self, tool_uses: list[ToolUseBlock]) -> list[ToolResultBlock]:
        """执行同一轮里的多个工具调用，并发能并发的地方并发。

        为什么需要它：模型经常一轮里同时请求几个互不相关的操作
        （一次读 5 个文件、同时派 3 个子代理探索不同目录）。
        早期版本是一个一个 await 过去的，总耗时等于每个工具耗时之和；
        现在只读工具并发执行，总耗时接近其中最慢的那一个。

        三条规则：
          1. 工具自己声明了 parallel_safe = True 才可能并发（见 tools/base.py）。
             典型的就是 Read / LS / Glob / Grep / BashOutput / Task：
             它们不修改共享状态，也不依赖别的工具的结果。
          2. 有副作用的工具（Write / Edit / Bash 等）仍然按模型请求的顺序
             一个一个执行，避免两个写操作抢同一个文件。
          3. 返回给模型的 tool_result 顺序必须和模型请求的顺序一致
             （Anthropic 协议要求 tool_use 与 tool_result 一一对应），
             所以这里用「攒一批 → 并发跑完 → 按原顺序写回」的方式，
             遇到不能并发的工具就先 flush 掉攒着的那批，保证顺序不乱。
        """
        results: list[ToolResultBlock] = []
        batch: list[ToolUseBlock] = []  # 正在攒着准备并发执行的一批工具

        async def flush() -> None:
            """并发跑完当前攒着的这批工具，结果按原顺序追加到 results。"""
            if not batch:
                return
            # gather 保证返回值顺序和传入顺序一致，所以结果天然不会错位
            outputs = await asyncio.gather(
                *(self._execute_tool(block) for block in batch),
                return_exceptions=True,
            )
            for block, output in zip(batch, outputs):
                if isinstance(output, ToolResult):
                    results.append(ToolResultBlock(block.id, output.content,
                                                   is_error=output.is_error))
                else:
                    # _execute_tool 内部已经把异常转成 ToolResult 了，
                    # 走到这里说明是意外异常（比如 CancelledError），兜个底别让整轮崩掉
                    detail = f"{type(output).__name__}: {output}"
                    results.append(ToolResultBlock(block.id, detail, is_error=True))
            batch.clear()

        for block in tool_uses:
            if self.cancel.is_set():
                # 用户已经打断：先把之前攒下的那批跑完（它们会立刻返回"已打断"），
                # 再把没轮到的调用标记成"未执行"，这样结果顺序依然和请求顺序一致
                await flush()
                results.append(ToolResultBlock(
                    block.id, "Not executed: the user interrupted the response.",
                    is_error=True,
                ))
                continue
            tool = self.tools.get(block.name)
            if tool is not None and getattr(tool, "parallel_safe", False):
                # 先攒着，等遇到不能并发的工具或本轮结束时一次性并发跑
                batch.append(block)
                continue
            # 遇到有副作用的工具：先把攒着的并发跑完，保证顺序不乱，再执行自己
            await flush()
            result = await self._execute_tool(block)
            results.append(ToolResultBlock(block.id, result.content,
                                           is_error=result.is_error))
        await flush()
        return results

    async def _execute_tool(self, block: ToolUseBlock) -> ToolResult:
        tool = self.tools.get(block.name)
        started = time.time()
        if tool is None:
            result = ToolResult(
                f"No such tool available: {block.name}. "
                f"Available tools: {', '.join(sorted(self.tools))}",
                is_error=True,
            )
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result
        if not isinstance(block.input, dict):
            result = ToolResult("Invalid tool input: expected an object.", is_error=True)
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result

        # 顺序很重要：先校验参数，再跑 PreToolUse hook，最后才询问权限。
        # 参数都不合法的调用不应该打扰用户去点“允许”。
        validation_error = tool.validate(block.input)
        if validation_error:
            result = ToolResult(f"Invalid input for {tool.name}: {validation_error}",
                                is_error=True)
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result

        # 用户已经打断了就别再弹权限窗了。
        # 并发执行时这一点更重要：一批工具里只要有一个先被打断，
        # 其余的都不该再把对话框怼到用户脸上。
        if self.cancel.is_set():
            result = ToolResult("Interrupted by the user.", is_error=True)
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result

        payload = {
            "event": "PreToolUse",
            "tool_name": tool.name,
            "tool_input": block.input,
            "cwd": str(self.config.cwd),
            "session_id": self.session.meta.id,
        }
        try:
            hook = await self.hooks.run("PreToolUse", payload, tool.name)
        except Exception:
            hook = None
        if hook and hook.blocked:
            result = ToolResult(f"Blocked by PreToolUse hook: {hook.reason}", is_error=True)
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result

        try:
            # 权限弹窗一次只能弹一个（UI 用的是 push_screen_wait，重入会乱）。
            # 并发执行多个工具时，用这把锁把"弹窗"这一步串行化：
            # 工具本身照样并发跑，只有询问用户的那一瞬间是排队的。
            # 父子 Agent 共用同一把锁，所以子代理和主代理也不会同时弹窗。
            async with self._permission_lock:
                decision = await self.request_permission(tool, block.input)
        except Interrupted:
            self.cancel.set()
            result = ToolResult("Interrupted while waiting for permission.", is_error=True)
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result
        if not decision.allowed:
            reason = decision.reason or "the user denied this action"
            result = ToolResult(
                f"Permission denied: {reason}. Do not retry the exact same action; "
                "explain what you need or try a different approach.",
                is_error=True,
            )
            self.callbacks.on_tool_result(block, result, time.time() - started)
            return result

        self.callbacks.on_status("tool", tool.name)
        try:
            if self.cancel.is_set():
                raise Interrupted()
            # 改文件之前先留个检查点，AI 改坏了还能用 /revert 退回去。
            # 快照失败不影响工具执行（快照只是保险，不是必需品）。
            self._snapshot_before_change(tool, block.input)
            result = await tool.run(block.input, self.ctx)
        except Interrupted:
            self.cancel.set()
            result = ToolResult("Interrupted by the user.", is_error=True)
        except Exception as exc:
            detail = traceback.format_exc() if self.config.debug else ""
            result = ToolResult(
                f"{type(exc).__name__}: {exc}\n{detail}".strip(), is_error=True
            )
        duration = time.time() - started
        self.callbacks.on_tool_result(block, result, duration)
        try:
            await self.hooks.run("PostToolUse", {
                "event": "PostToolUse",
                "tool_name": tool.name,
                "tool_input": block.input,
                "tool_response": result.text[:5000],
                "cwd": str(self.config.cwd),
                "session_id": self.session.meta.id,
            }, tool.name)
        except Exception:
            pass
        return result

    # -- permissions -------------------------------------------------------
    async def request_permission(self, tool: Tool, data: dict[str, Any],
                                 *, force_ask: bool = False,
                                 kind: str = "tool") -> PermissionDecision:
        verdict, rule = self.permissions.evaluate(tool, data)
        if verdict == "allow" and not force_ask:
            return PermissionDecision(True)
        if verdict == "deny":
            return PermissionDecision(
                False, reason=f"blocked by permission rule `{rule}`"
            )
        if verdict == "plan-deny":
            return PermissionDecision(
                False,
                reason=(
                    "plan mode is active -- file edits and destructive commands are "
                    "disabled. Present a plan and call ExitPlanMode first"
                ),
            )
        suggestion = suggest_rule(tool.name, data, self.config.cwd)
        warning = ""
        if tool.name == "Bash":
            from .tools.shell_tools import is_dangerous

            if is_dangerous(str(data.get("command") or "")):
                warning = "This command looks destructive. Review it carefully."
        request = PermissionRequest(
            tool=tool,
            data=data,
            summary=tool.summary(data),
            suggestion=suggestion,
            preview=tool.get_diff(data, self.ctx) if kind == "tool" else None,
            kind=kind,
            warning=warning,
        )
        decision = await self.callbacks.request_permission(request)
        if decision.allowed and decision.remember and decision.rule:
            self.permissions.session_allow.add(decision.rule)
            if getattr(decision, "persist", False):
                persist_allow_rule(self.config.cwd, decision.rule)
                self.config.permissions.setdefault("allow", []).append(decision.rule)
        return decision

    async def request_plan_approval(self, plan: str) -> bool:
        tool = self.tools.get("ExitPlanMode") or ExitPlanModeTool()
        decision = await self.callbacks.request_permission(PermissionRequest(
            tool=tool,
            data={"plan": plan},
            summary="Approve this plan?",
            suggestion="",
            kind="plan",
        ))
        return decision.allowed

    async def ask_user(self, question: str, header: str, options: list[str],
                       descriptions: list[str], multi_select: bool) -> str | None:
        return await self.callbacks.ask_user(
            question, header, options, descriptions, multi_select
        )

    # -- compaction --------------------------------------------------------
    async def maybe_compact(self, force: bool = False, instructions: str = "") -> bool:
        if not self.messages:
            return False
        window = self.config.context_window
        used = self.last_usage.prompt_tokens or _estimate_tokens(self.messages)
        if not force and not self.config.auto_compact:
            return False
        if not force and used < window * self.config.compact_threshold:
            return False
        if len(self.messages) < 4 and not force:
            return False
        # 压缩 = 让另一个模型请求把旧对话总结成摘要，然后用摘要替换旧消息。
        # 摘要里保留用户目标、已改文件、关键决策和待办，避免后续失去上下文。
        self.callbacks.on_notice("Compacting conversation to fit the context window...", "info")
        transcript = _render_transcript(self.messages)
        prompt = (
            f"{transcript}\n\n---\n\n"
            + (instructions.strip() + "\n\n" if instructions.strip() else "")
            + "Write the summary now."
        )
        try:
            summary = await self.provider.complete(
                system=compaction_prompt(self.config.cwd),
                messages=[user_message(prompt)],
                max_tokens=4096,
                temperature=0.2,
            )
        except Exception as exc:
            self.callbacks.on_notice(f"Compaction failed: {exc}", "error")
            return False
        if not summary.strip():
            self.callbacks.on_notice("Compaction produced an empty summary; skipped.", "warning")
            return False
        marker = (
            "This session is being continued from a previous conversation that ran out "
            "of context. The summary below covers the earlier portion.\n\n"
            f"<summary>\n{summary}\n</summary>\n\n"
            "Continue the work from where it left off. Do not repeat completed steps."
        )
        new_messages = [user_message(marker)]
        self.messages = new_messages
        self.session.messages = new_messages
        self.session.replace_messages(new_messages, summary)
        self.last_usage = Usage()
        self.callbacks.on_compaction(summary, not force)
        return True

    # -- prompt helpers ----------------------------------------------------
    def expand_mentions(self, prompt: str | list[Block]) -> list[Block]:
        """把 @文件 引用展开成内容块（附件智能识别的单一事实来源）。

        规则（与 UI 的 @补全共用同一套路径解析）：
          * @图片.png / .jpg / ... -> ImageBlock（真正作为图片发给模型）
          * @代码.py 等文本       -> 沿用 <file> 包装塞进 TextBlock
          * 其他二进制文件        -> 一句说明性文本，避免把乱码塞进 prompt
          * 已经是块（UI 传入的附件）-> 原样保留，不重复展开

        返回 list[Block]：文本块在前，附件块在后。旧版返回 str，现在统一成块，
        由 run() 交给 user_message()（它本就支持 list[Block]）。
        """
        import re

        # 1) 先把入参归一成块列表：str 转成一个 TextBlock；已有块原样保留。
        if isinstance(prompt, str):
            blocks: list[Block] = [TextBlock(prompt)]
            text_indices = [0]
        else:
            blocks = list(prompt)
            # 只展开 TextBlock 里的 @mention；ImageBlock 等附件保持不动。
            text_indices = [i for i, b in enumerate(blocks) if isinstance(b, TextBlock)]

        pattern = re.compile(r"(?<![\w@])@([^\s@]+)")
        file_refs: list[tuple[str, str]] = []   # 文本文件：(token, 文本内容)
        seen: set[str] = set()                   # 同一文件只处理一次
        new_blocks: list[Block] = []             # 展开后的最终结果

        # 引用计数：文本 + 图片合计不超过 5 个（沿用旧上限，防止请求体膨胀）。
        use_attachments = self._attachments()

        def replace(match: re.Match[str]) -> str:
            token = match.group(1).rstrip(".,;:!?)\"'")
            if "://" in token:                     # URL 不当作文件
                return match.group(0)
            path = Path(token)
            if not path.is_absolute():
                path = self.config.cwd / path
            try:
                path = path.resolve()
            except OSError:
                return match.group(0)
            if not path.is_file() or str(path) in seen:
                return match.group(0)
            if len(file_refs) + len(use_attachments) >= 5:
                return token
            seen.add(str(path))

            # 图片扩展名 -> 走附件模块转成 ImageBlock（含大小校验）。
            if is_image_path(path):
                block = path_to_block(path)
                if isinstance(block, ImageBlock):
                    use_attachments.append(block)
                    return token

            # 非图片：尝试按文本读取；读不出来就给一句说明，不塞乱码。
            try:
                text, _, _ = read_text(path)
            except (OSError, UnicodeError):
                file_refs.append((token, f"[二进制文件，已跳过内容：{path.name}]"))
                return token
            if len(text) > 60_000:
                text = text[:60_000] + "\n... (truncated)"
            file_refs.append((token, text))
            return token

        # 2) 逐块展开：TextBlock 做 @替换，其他块原样带上。
        for index, block in enumerate(blocks):
            if index in text_indices:
                expanded = pattern.sub(replace, block.text)  # type: ignore[union-attr]
                new_blocks.append(TextBlock(expanded))
            else:
                new_blocks.append(block)

        # 3) 文本文件沿用 <referenced-files> 包装，保持提示词风格不变。
        if file_refs:
            wrapper = ["<referenced-files>"]
            for name, text in file_refs:
                wrapper.append(f'<file path="{name}">\n{text}\n</file>')
            wrapper.append("</referenced-files>")
            new_blocks.append(TextBlock("\n".join(wrapper)))

        # 4) 图片附件放最后，交给 provider 转成真正的图片消息。
        new_blocks.extend(use_attachments)
        return new_blocks

    def _attachments(self) -> list[Block]:
        """返回可变的附件累加器占位（供 expand_mentions 内部收集图片块）。"""
        return []

    def _api_messages(self) -> list[Message]:
        """Prune oversized old tool results before sending."""
        window = self.config.context_window
        messages = self.messages
        if _estimate_tokens(messages) < window * 0.6:
            return messages
        keep_from = max(0, len(messages) - 8)
        pruned: list[Message] = []
        for index, message in enumerate(messages):
            if index >= keep_from:
                pruned.append(message)
                continue
            copied = message_from_dict(message_to_dict(message))
            for block in copied.content:
                if isinstance(block, ToolResultBlock) and isinstance(block.content, str):
                    if len(block.content) > 1500:
                        block.content = (
                            block.content[:1000]
                            + "\n... [old tool result truncated to save context] ..."
                        )
            pruned.append(copied)
        return pruned

    def _append_message(self, message: Message) -> None:
        self.session.append_message(message)

    # -- subagents ---------------------------------------------------------
    async def run_subagent(self, description: str, prompt: str,
                           agent_type: str = "general-purpose",
                           raise_on_error: bool = False) -> str:
        """开一个子代理跑一件事，返回它的报告。

        raise_on_error=False（默认）：子代理失败时把错误当文字返回，
        工具调用照样算"成功"，模型自己看报告决定怎么办。
        raise_on_error=True：失败直接抛异常。
        任务编排器（TaskPlan）需要知道任务到底成没成功，所以用后者。
        """
        if self.depth >= 2:
            raise RuntimeError("subagents cannot spawn further subagents")
        spec: SubagentSpec | None = self.subagents.get(agent_type)
        if spec is None:
            matches = [s for n, s in self.subagents.items()
                       if fnmatch.fnmatch(n.lower(), agent_type.lower())]
            spec = matches[0] if matches else self.subagents["general-purpose"]
        tools = self._tools_for_spec(spec)
        sub_config = copy.copy(self.config)
        if spec.model:
            sub_config.model = spec.model
        system_append = list(filter(None, [
            self.config.system_prompt_append,
            spec.prompt or "",
        ]))
        sub_config.system_prompt_append = "\n\n".join(system_append)
        callbacks = SubagentCallbacks(self, description)
        sub_session = Session.memory(self.config.cwd, sub_config.model, sub_config.provider)
        sub = Agent(
            sub_config, self.provider, tools,
            callbacks=callbacks, session=sub_session, depth=self.depth + 1,
            # 共享父代理的权限弹窗锁：多个子代理并发跑时，
            # 不会在界面上同时弹出好几个权限对话框
            permission_lock=self._permission_lock,
        )
        sub.cancel = self.cancel
        sub.ctx.cancel = self.cancel
        self.callbacks.on_subagent_start(description, spec.name)
        try:
            report = await sub.run(prompt, max_turns=min(30, self.config.max_turns),
                                   auto_compact=True)
        except Exception as exc:
            if raise_on_error:
                # 编排器需要知道这个任务失败了，好触发重试 / 跳过下游
                raise
            report = f"Sub-agent failed: {exc}"
        finally:
            await sub.close()
        if not report:
            if raise_on_error:
                raise RuntimeError("the sub-agent returned no text")
            report = "(the sub-agent returned no text)"
        self.callbacks.on_subagent_end(description, report)
        return f"Sub-agent '{spec.name}' report:\n\n{report}"

    def _tools_for_spec(self, spec: SubagentSpec) -> list[Tool]:
        if not spec.tools:
            return list(self.tools.values())
        selected: list[Tool] = []
        for tool in self.tools.values():
            if tool.name.startswith("mcp__"):
                if any(fnmatch.fnmatch(tool.name, pat) for pat in spec.tools):
                    selected.append(tool)
                continue
            if any(fnmatch.fnmatch(tool.name, pat) for pat in spec.tools):
                selected.append(tool)
        return selected

    # -- lifecycle ---------------------------------------------------------
    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ctx.state["shells"].cleanup()
        except Exception:
            pass
        if self.depth == 0:
            try:
                await self.hooks.run("SessionEnd", {
                    "event": "SessionEnd",
                    "session_id": self.session.meta.id,
                    "cwd": str(self.config.cwd),
                })
            except Exception:
                pass
            try:
                await self.provider.aclose()
            except Exception:
                pass

    def memory_info(self) -> str:
        return memory_summary(self.config.cwd)
