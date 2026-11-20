"""Textual 主应用：对话记录、输入框、流式渲染与命令分发。

UI 与 Agent 通过 UICallbacks 解耦：
  Agent 回调 on_text / on_tool_start / on_usage ...
    -> UICallbacks 转发给 YotCodeApp.stream_text / tool_started / ...
      -> 更新 AssistantMessage、ToolCard、TodoPanel、StatusBar

用户提交内容后（handle_submit）按前缀分流：
  !command   直接执行 shell（不经过模型）
  #note      追加到项目记忆 CLAUDE.md
  /command   内置或自定义斜杠命令
  其他       作为 prompt 交给 Agent；忙时进入队列
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Static

from .. import __version__, models
from ..agent import Agent, AgentCallbacks, PermissionRequest
from ..commands import (
    BUILTIN_COMMANDS,
    expand_custom_command,
    help_markdown,
    init_prompt,
    load_custom_commands,
    parse_slash,
)
from ..checkpoints import CheckpointStore
from ..config import Config, build_config
from ..memory import append_memory, memory_summary
from ..permissions import PermissionDecision
from ..providers import create_provider
from ..session import Session, SessionMeta
from ..skills import skill_listing
from ..subagents import subagent_listing
from ..utils import display_path
from ..tools import ToolResult, create_tools
from ..types import (
    Block,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    extract_text,
)
from ..utils import (
    expand_path,
    git_info,
    human_tokens,
    read_text,
    write_text,
)
from .modals import (
    CommandPaletteScreen,
    HelpScreen,
    ModelPickerScreen,
    PermissionScreen,
    QuestionScreen,
    SessionPickerScreen,
)
from .render import (
    ACCENT,
    ACCENT_SOFT,
    BG,
    BLUE,
    DIM,
    FAINT,
    GREEN,
    LINE,
    MUTED,
    RED,
    SURFACE,
    SURFACE_2,
    TEXT,
    TEXT_STRONG,
    YELLOW,
)
from .widgets import (
    AssistantMessage,
    HeaderBar,
    InfoPanel,
    NoticePanel,
    PromptArea,
    StatusBar,
    ThinkingIndicator,
    ThinkingPanel,
    TodoPanel,
    ToolCard,
    UserMessage,
    WelcomePanel,
)

MODE_ORDER = ("default", "acceptEdits", "plan")
# 参考 Claude Code 的状态栏文案，符号全部用 CP437 安全字形（cmd.exe 不乱码）。
MODE_LABELS = {
    "default": "default",
    "acceptEdits": ">> accept edits",
    "plan": "# plan",
    "bypassPermissions": "! bypass",
}
MODE_COLORS = {
    "default": DIM,
    "acceptEdits": GREEN,
    "plan": BLUE,
    "bypassPermissions": YELLOW,
}


class UICallbacks(AgentCallbacks):
    """Bridge agent events onto the Textual UI."""

    def __init__(self, app: "YotCodeApp") -> None:
        self.app = app

    def on_turn_start(self, index: int) -> None:
        self.app.begin_turn(index)

    def on_text(self, text: str) -> None:
        self.app.stream_text(text)

    def on_thinking(self, text: str) -> None:
        self.app.stream_thinking(text)

    def on_tool_start(self, block: ToolUseBlock) -> None:
        self.app.tool_started(block)

    def on_tool_result(self, block: ToolUseBlock, result: ToolResult,
                       duration: float) -> None:
        self.app.tool_finished(block, result, duration)

    def on_notice(self, message: str, level: str = "info") -> None:
        self.app.add_notice(message, level)

    def on_todos(self, todos: list[dict[str, Any]]) -> None:
        self.app.update_todos(todos)

    def on_usage(self, usage: Usage, cost: float, total_usage: Usage,
                 total_cost: float) -> None:
        self.app.usage_updated(usage, cost, total_usage, total_cost)

    def on_turn_end(self, usage: Usage, stop_reason: str,
                    interrupted: bool = False) -> None:
        self.app.turn_ended(stop_reason, interrupted)

    def on_status(self, status: str, detail: str = "") -> None:
        self.app.set_status(status, detail)

    def on_compaction(self, summary: str, auto: bool) -> None:
        title = "Conversation compacted" if auto else "Compacted on request"
        self.app.add_info_markdown(title, summary)

    def on_subagent_start(self, description: str, agent_type: str) -> None:
        self.app.add_notice(f"Sub-agent started: {description} ({agent_type})", "info")

    def on_subagent_progress(self, description: str, text: str) -> None:
        self.app.subagent_progress(description, text)

    def on_subagent_end(self, description: str, report: str) -> None:
        self.app.add_notice(f"Sub-agent finished: {description}", "success")

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        return await self.app.ask_permission(request)

    async def ask_user(self, question: str, header: str, options: list[str],
                       descriptions: list[str], multi_select: bool) -> str | None:
        return await self.app.ask_question(
            question, header, options, descriptions, multi_select
        )


class YotCodeApp(App[None]):
    CSS_PATH = "theme.tcss"
    TITLE = "YotCode"
    SUB_TITLE = "Python edition"
    ENABLE_COMMAND_PALETTE = False
    # 禁用 Textual 的任意文本选择：在终端里用鼠标点击对话区时，Textual 默认
    # 会选中并高亮文字，看起来像“界面乱了”。关掉它，点击不再产生高亮块；
    # 输入框自身的文本选择不受影响（TextArea 有内置支持）。
    ALLOW_SELECT = False

    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_quit", "Interrupt", priority=True, show=False),
        Binding("escape", "escape", "Interrupt", show=False),
        Binding("shift+tab", "cycle_mode", "Permission mode", priority=True, show=False),
        Binding("ctrl+d", "ctrl_d", "Exit", priority=True, show=False),
        Binding("ctrl+l", "clear_screen", "Clear screen", priority=True, show=False),
        Binding("ctrl+o", "toggle_tools", "Expand tools", priority=True, show=False),
        Binding("ctrl+p", "command_palette", "Commands", priority=True, show=False),
        Binding("ctrl+r", "resume_picker", "Resume session", priority=True, show=False),
        Binding("ctrl+t", "show_todos", "Todos", priority=True, show=False),
        Binding("f1", "help", "Help", priority=True, show=False),
    ]

    def __init__(self, config: Config, initial_prompt: str | list[Block] | None = None,
                 session: Session | None = None) -> None:
        # 使用 Textual 原生 WindowsDriver。曾经注入过自定义 driver 并在进入
        # 备用屏幕后手动 \x1b[2J，又叠加 _on_resize override + LINES=-1 +
        # 定时 poll，几套机制互相打架，制造了 app/screen/driver 三者尺寸不
        # 一致（54 vs 53），conhost 差量渲染随即错位、底部堆积重影行。
        # Textual 原生驱动在 conhost 下可以正确处理尺寸与渲染，无需这些 hack。
        super().__init__()
        self.config = config
        self.initial_prompt = initial_prompt
        self.provider = create_provider(config)
        self.callbacks = UICallbacks(self)
        self.agent = Agent(
            config, self.provider, create_tools(),
            callbacks=self.callbacks, session=session,
        )
        self.session = self.agent.session
        self.mcp_manager: Any = None
        self.mcp_tools: list[Any] = []
        self.custom_commands = load_custom_commands(config.cwd)
        self.busy = False
        self.current_assistant: AssistantMessage | None = None
        self.current_thinking: ThinkingPanel | None = None
        self._thinking_indicator: ThinkingIndicator | None = None
        self.cards: dict[str, ToolCard] = {}
        self.task_cards: dict[str, ToolCard] = {}
        self.queue: list[str | list[Block]] = []
        self._last_ctrl_c = 0.0
        self._last_scroll = 0.0
        self._file_cache: list[tuple[str, bool]] | None = None
        self._file_cache_at = 0.0
        self._pending_initial = bool(initial_prompt)

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield TodoPanel()
        yield PromptArea(self)
        yield StatusBar()
        # 末尾预留一个空行：conhost 写入屏幕缓冲区最后一行时会把整屏上滚，
        # BottomBar/status 更新就会在下方留下旧行残影（多行状态栏 / 输入框
        # 边框重复）。用一行空白占住物理末行，状态栏落在倒数第二行，避开它。
        yield Static("  ", id="bottom-guard", markup=False)

    async def on_mount(self) -> None:
        self._install_theme()
        self.show_welcome()
        self.replay_session()
        self.update_todos(self.session.todos)
        self.set_status("idle")
        self._refresh_usage()
        # 启动后滚回顶部即可。不做任何手动整屏重绘：Textual 自己在备用
        # 屏幕缓冲区里渲染，手动调 compositor 重写会和它的差量渲染打架，
        # 在 conhost 下产生重影行（如状态栏出现两条）。
        self.call_after_refresh(self._startup_settle)
        if self.config.mcp_servers:
            self.run_worker(self._connect_mcp(), group="mcp", exclusive=True)
        if self.initial_prompt:
            self.call_after_refresh(self._start_initial_prompt)

    def _startup_settle(self) -> None:
        """布局稳定后的启动收尾：滚回顶部、同步真实光标。"""
        try:
            if not self.session.messages:
                self.transcript().scroll_home(animate=False)
        except Exception:
            pass
        self.sync_cursor()

    def sync_cursor(self) -> None:
        """把 Textual 记录的真实光标位置同步到输入框。

        只更新 app.cursor_position 这一个属性，让 Textual 在它自己的渲染
        周期里统一定位光标。绝不直接往终端写 VT 序列——那会和 Textual 的
        差量渲染打架，造成屏幕抖动和残留横线。
        """
        try:
            prompt = self.prompt_area().prompt
            if prompt.has_focus:
                self.cursor_position = prompt.cursor_screen_offset
        except Exception:
            pass

    def on_click(self, event: events.Click) -> None:
        """点击对话区等非输入区域后，把焦点交还输入框。

        ALLOW_SELECT=False 关闭了文本高亮，但点击仍会把焦点转给 VerticalScroll，
        导致之后敲键盘输不进文字。这里在主界面（非模态框）的任何点击后，
        延迟一帧把焦点拉回输入框，保证随时可以打字。
        """
        # 有模态框/弹窗打开时不要抢焦点（它们需要自己的输入控件）
        if len(self.screen_stack) > 1:
            return
        try:
            prompt = self.prompt_area().prompt
        except Exception:
            return
        if not prompt.has_focus:
            # 立即抢回焦点：延迟到下一帧会让点击后立刻输入的第一个字符
            # 落给被点击的滚动容器，看起来像“输入框和焦点错位”。
            prompt.focus()
            self.sync_cursor()

    async def on_unmount(self) -> None:
        try:
            await self.agent.close()
        except Exception:
            pass
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close()
            except Exception:
                pass

    def _install_theme(self) -> None:
        try:
            self.register_theme(Theme(
                name="yotcode",
                primary=ACCENT,
                secondary=ACCENT_SOFT,
                warning=YELLOW,
                error=RED,
                success=GREEN,
                accent=ACCENT,
                foreground=TEXT,
                background=BG,
                surface=SURFACE,
                panel=SURFACE_2,
                dark=True,
            ))
            self.theme = "yotcode"
        except Exception:
            pass

    # -- transcript helpers ------------------------------------------------
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def scroll_down(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_scroll < 0.12:
            return
        self._last_scroll = now
        try:
            self.transcript().scroll_end(animate=False)
        except Exception:
            pass

    def _scroll_soon(self) -> None:
        self.call_after_refresh(self.scroll_down, True)

    def show_welcome(self) -> None:
        resumed = bool(self.session.messages)
        panel = WelcomePanel(
            self.config,
            resumed=resumed,
            session_title=self.session.meta.title,
        )
        self.transcript().mount(panel)

    def add_user(self, content: str | list[Block]) -> None:
        """显示一条用户消息。content 可为纯文本或内容块（含图片/文件附件）。"""
        self.transcript().add_class("has-messages")
        self.transcript().mount(UserMessage(content))
        self._scroll_soon()

    def add_notice(self, message: str, level: str = "info") -> None:
        self.transcript().mount(NoticePanel(message, level))
        self._scroll_soon()

    def add_info(self, title: str, body: str) -> None:
        self.transcript().mount(InfoPanel.plain(title, body))
        self._scroll_soon()

    def add_info_markdown(self, title: str, body: str) -> None:
        self.transcript().mount(InfoPanel.markdown(title, body))
        self._scroll_soon()

    # -- 检查点 / 回退 ------------------------------------------------------
    def _checkpoint_store(self) -> CheckpointStore:
        """检查点仓库（存在项目里的 .yotcode/checkpoints/）。"""
        return CheckpointStore(self.config.project_dir)

    def _checkpoints_report(self) -> str:
        """`/checkpoints` 的输出：列出本次会话留下的检查点。"""
        store = self._checkpoint_store()
        try:
            checkpoints = store.list(self.session.meta.id)
        except Exception as exc:
            return f"读取检查点失败：{exc}"
        if not checkpoints:
            return ("本次会话还没有检查点。\n\n"
                    "AI 每次改文件之前会自动存一份原内容，"
                    "改坏了可以用 /revert 退回去。")
        lines = [
            f"共 {len(checkpoints)} 个检查点（新的在前）：",
            "",
        ]
        for checkpoint in checkpoints:
            lines.append(f"- {checkpoint.summary()}")
        lines += [
            "",
            "用 /revert <检查点ID> 恢复指定的那一次改动；"
            "直接 /revert 则回退最近一次。",
        ]
        return "\n".join(lines)

    def _revert(self, arg: str) -> None:
        """`/revert [id]`：把文件恢复成检查点记录的样子。

        不给 id 就回退最近一个检查点。恢复完会明确告诉你动了哪些文件。
        """
        store = self._checkpoint_store()
        arg = arg.strip()
        try:
            if arg:
                checkpoint = store.get(arg, self.session.meta.id)
                if checkpoint is None:
                    # 允许不带会话限制地找（万一用户记的是别的会话的 id）
                    checkpoint = store.get(arg)
                if checkpoint is None:
                    self.add_notice(f"找不到检查点 {arg}，用 /checkpoints 看看有哪些。",
                                    "error")
                    return
            else:
                checkpoints = store.list(self.session.meta.id)
                if not checkpoints:
                    self.add_notice("本次会话还没有检查点可以回退。", "warning")
                    return
                checkpoint = checkpoints[0]
            result = store.restore(checkpoint.id, checkpoint.session_id)
        except Exception as exc:
            self.add_notice(f"回退失败：{exc}", "error")
            return
        if result.failed and not result.restored and not result.deleted:
            self.add_notice(result.report(), "error")
            return
        self.add_notice(
            f"已回退到检查点 {checkpoint.id}（{checkpoint.time_text}，{checkpoint.tool}）\n"
            + result.report(),
            "success",
        )

    # -- streaming ---------------------------------------------------------
    def _mount_thinking_indicator(self) -> None:
        """在对话流末尾挂一个 `◐ Thinking Ns` 转圈指示器（用户消息下方）。

        幂等：已存在时不重复挂。首个回答内容到达后由 _remove 移除。
        """
        if self._thinking_indicator is not None:
            return
        indicator = ThinkingIndicator()
        self._thinking_indicator = indicator
        self.transcript().mount(indicator)
        self.scroll_down()

    def _remove_thinking_indicator(self) -> None:
        indicator = self._thinking_indicator
        if indicator is None:
            return
        self._thinking_indicator = None
        indicator.stop()
        try:
            indicator.remove()
        except Exception:
            pass

    def begin_turn(self, index: int) -> None:
        self.current_assistant = None
        self.current_thinking = None
        # 工具循环的下一回合：在工具卡片下方重新出现转圈指示器
        self._mount_thinking_indicator()
        self.set_status("thinking")

    def stream_text(self, text: str) -> None:
        if self.current_assistant is None:
            self._remove_thinking_indicator()
            widget = AssistantMessage()
            self.transcript().mount(widget)
            widget.start()
            self.current_assistant = widget
            self.current_thinking = None
        self.current_assistant.feed(text)
        self.scroll_down()

    def stream_thinking(self, text: str) -> None:
        if self.current_thinking is None:
            self._remove_thinking_indicator()
            panel = ThinkingPanel()
            self.transcript().mount(panel)
            self.current_thinking = panel
        self.current_thinking.feed(text)
        self.scroll_down()

    def tool_started(self, block: ToolUseBlock) -> None:
        self._remove_thinking_indicator()
        tool = self.agent.tools.get(block.name)
        diff = None
        if tool is not None:
            try:
                diff = tool.get_diff(block.input, self.agent.ctx)
                if diff:
                    diff = (
                        display_path(Path(diff[0]), self.config.cwd),
                        diff[1],
                        diff[2],
                    )
            except Exception:
                diff = None
        card = ToolCard(block, tool, diff)
        self.cards[block.id] = card
        if block.name == "Task":
            description = str(block.input.get("description") or "")
            if description:
                self.task_cards[description] = card
        self.transcript().mount(card)
        self._scroll_soon()

    def tool_finished(self, block: ToolUseBlock, result: ToolResult,
                      duration: float) -> None:
        card = self.cards.get(block.id)
        if card is not None:
            try:
                card.set_result(result, duration)
            except Exception:
                pass
        self._scroll_soon()

    def subagent_progress(self, description: str, text: str) -> None:
        card = self.task_cards.get(description)
        if card is not None:
            card.add_progress(text)
        self.scroll_down()

    def update_todos(self, todos: list[dict[str, Any]]) -> None:
        try:
            self.query_one(TodoPanel).update_todos(todos)
        except Exception:
            pass
        # todo 带会移动输入框位置，布局稳定后同步真实光标
        self.call_after_refresh(self.sync_cursor)

    def usage_updated(self, usage: Usage, cost: float, total_usage: Usage,
                      total_cost: float) -> None:
        self._refresh_usage()

    def turn_ended(self, stop_reason: str, interrupted: bool) -> None:
        pass

    # -- status ------------------------------------------------------------
    def set_status(self, status: str, detail: str = "") -> None:
        try:
            bar = self.query_one(StatusBar)
        except Exception:
            return
        if status == "thinking":
            # thinking 只在对话流里显示（用户消息正下方的转圈指示器），
            # 输入框下方的状态栏不再重复一遍，底栏保持只显示权限模式。
            self._set_mode_status(bar)
            return
        if status == "tool":
            bar.start_spinner(f"{detail} running…" if detail else "Running…", YELLOW)
            return
        if status == "error":
            label = Text("x ", style=RED)
            label.append("Error", style=RED)
        else:
            label = self._mode_status_label()
        bar.set_left(label)

    def _mode_status_label(self) -> Text:
        color = MODE_COLORS.get(self.config.permission_mode, DIM)
        label = Text("● ", style=color)
        label.append(self._idle_text(), style=color)
        return label

    def _set_mode_status(self, bar: StatusBar) -> None:
        bar.set_left(self._mode_status_label())

    def _idle_text(self) -> str:
        label = MODE_LABELS.get(self.config.permission_mode, self.config.permission_mode)
        if self.queue:
            return f"{label}  ·  {len(self.queue)} queued"
        # 精简：输入区已显示快捷键提示，状态栏只保留模式，避免重复和拥挤
        return label

    def _refresh_usage(self) -> None:
        """右下角状态：token 用量、费用、上下文占用条和帮助提示。"""
        try:
            bar = self.query_one(StatusBar)
        except Exception:
            return
        usage = self.agent.total_usage
        cost = self.agent.total_cost
        right = Text()
        first = True

        def sep() -> None:
            nonlocal first
            if not first:
                right.append("  ·  ", style=LINE)
            first = False

        if usage.prompt_tokens or usage.output_tokens:
            sep()
            right.append(f"{human_tokens(usage.prompt_tokens)} in", style=FAINT)
            right.append(" / ", style=LINE)
            right.append(f"{human_tokens(usage.output_tokens)} out", style=FAINT)
        if cost > 0:
            sep()
            right.append(f"${cost:.4f}", style=GREEN)

        window = self.config.context_window
        if window:
            pct = round(self.agent.last_usage.prompt_tokens / window * 100)
            pct = max(0, min(100, pct))
            sep()
            right.append("ctx ", style=FAINT)
            filled = round(pct / 100 * 8)
            for index in range(8):
                right.append("█" if index < filled else "░",
                             style=ACCENT if index < filled else LINE)
            right.append(f" {pct}%", style=FAINT)

        sep()
        right.append("? help", style=FAINT)
        bar.set_right(right)

    def _update_header(self) -> None:
        try:
            self.query_one(HeaderBar).update_model(
                self.config.model, self.config.model_context_label()
            )
        except Exception:
            pass

    # -- prompt plumbing ---------------------------------------------------
    def prompt_area(self) -> PromptArea:
        return self.query_one(PromptArea)

    def handle_submit(self, content: str | list[Block]) -> None:
        """输入框回车后的总入口：区分 shell、记忆、斜杠命令和普通提问。

        content 可以是纯文本，也可以是带图片/文件附件的内容块列表。
        shell / 记忆 / 斜杠命令只对纯文本有意义；带附件时一律当作普通提问。
        """
        # 从内容块里抽出纯文本，用于前缀判断（! / # / /）；图片块会被忽略。
        text = (
            content.strip()
            if isinstance(content, str)
            else "\n".join(b.text for b in content if isinstance(b, TextBlock)).strip()
        )
        has_attachments = isinstance(content, list) and any(
            isinstance(b, ImageBlock) for b in content
        )
        if not text and not has_attachments:
            return
        # 只有纯文本消息才解析 ! / # / 斜杠命令；带附件的消息直接发给模型。
        if not has_attachments:
            # "!!" 是转义写法，表示把 "!xxx" 作为普通消息发给模型。
            if text.startswith("!!"):
                text = text[1:]
            if text.startswith("!"):
                self.run_worker(self._run_shell(text[1:].strip()), group="shell", exclusive=True)
                return
            if text.startswith("#"):
                self._remember(text[1:].strip())
                return
            parsed = parse_slash(text)
            if parsed is not None:
                name, args = parsed
                self.run_worker(self._handle_slash(name, args), group="command", exclusive=True)
                return
        if self.busy:
            # 模型正在输出时继续提交不会丢，先排队，当前回合结束后自动发送。
            self.queue.append(content)
            self.add_notice("Queued — I'll send it when the current turn finishes.", "info")
            self.set_status("idle")
            return
        self.send_prompt(content)

    def send_prompt(self, content: str | list[Block]) -> None:
        self.add_user(content)
        # 用户消息正下方立即出现转圈指示器（不等模型首包）
        self._mount_thinking_indicator()
        self.busy = True
        self.set_status("thinking")
        self.run_worker(self._agent_worker(content), group="agent", exclusive=True)

    def _start_initial_prompt(self) -> None:
        if not self._pending_initial:
            return
        self._pending_initial = False
        initial = self.initial_prompt
        if isinstance(initial, list):
            # 命令行 --image/--file 传入的内容块，直接作为一条带附件的消息。
            if initial:
                self.handle_submit(initial)
            return
        prompt = (initial or "").strip()
        if prompt:
            self.handle_submit(prompt)

    async def _agent_worker(self, prompt: str | list[Block]) -> None:
        try:
            await self.agent.run(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = ""
            if self.config.debug:
                import traceback

                detail = "\n" + traceback.format_exc()
            self.add_notice(f"{type(exc).__name__}: {exc}{detail}", "error")
        finally:
            await self._finish_turn()

    async def _finish_turn(self) -> None:
        self._remove_thinking_indicator()
        assistant = self.current_assistant
        if assistant is not None:
            try:
                await asyncio.wait_for(assistant.finish(), timeout=5)
            except Exception:
                pass
        self.busy = False
        self._refresh_usage()
        self.set_status("idle")
        self.scroll_down(force=True)
        if self.queue:
            next_text = self.queue.pop(0)
            self.call_after_refresh(lambda: self.send_prompt(next_text))

    # -- shell / memory shortcuts -----------------------------------------
    async def _run_shell(self, command: str) -> None:
        if not command:
            return
        self.add_user("!" + command)
        tool = self.agent.tools.get("Bash")
        if tool is None:
            self.add_notice("Bash tool unavailable.", "error")
            return
        self.set_status("tool", "Bash")
        panel = InfoPanel.markdown(
            "Shell", f"```console\n$ {command}\n```\n*Running…*"
        )
        self.transcript().mount(panel)
        self.scroll_down(force=True)
        try:
            result = await tool.run({"command": command}, self.agent.ctx)
            output = result.text
            if len(output) > 20_000:
                output = output[:20_000] + "\n… (truncated)"
            body = f"```console\n$ {command}\n{output}\n```"
            if result.is_error:
                body += "\n\n**exit: error**"
            panel.update_markdown("Shell", body)
        except Exception as exc:
            panel.update_markdown(
                "Shell",
                f"```console\n$ {command}\n```\n\n**{type(exc).__name__}:** {exc}",
            )
        finally:
            self.set_status("idle")
            self._scroll_soon()

    def _remember(self, note: str) -> None:
        if not note:
            return
        try:
            path = append_memory(self.config.cwd, note)
        except Exception as exc:
            self.add_notice(f"Could not update memory: {exc}", "error")
            return
        self.add_notice(f"Added to {path}", "success")

    # -- permissions / questions ------------------------------------------
    async def ask_permission(self, request: PermissionRequest) -> PermissionDecision:
        try:
            decision = await self.push_screen_wait(
                PermissionScreen(request, self.config.permission_mode)
            )
        except Exception as exc:
            return PermissionDecision(False, reason=f"permission dialog failed: {exc}")
        if decision is None:
            return PermissionDecision(False, reason="the user closed the dialog")
        return decision

    async def ask_question(self, question: str, header: str, options: list[str],
                           descriptions: list[str], multi_select: bool) -> str | None:
        try:
            return await self.push_screen_wait(QuestionScreen(
                question, header, options, descriptions, multi_select
            ))
        except Exception:
            return None

    # -- slash commands ----------------------------------------------------
    async def _handle_slash(self, name: str, args: str) -> None:
        name = name.lower()
        try:
            handled = await self._builtin_slash(name, args)
            if handled:
                return
            custom = load_custom_commands(self.config.cwd)
            self.custom_commands = custom
            command = custom.get(name)
            if command is None:
                self.add_notice(
                    f"Unknown command: /{name}. Type /help to see the list.", "error"
                )
                return
            expanded = await expand_custom_command(command, args, self.config)
            display = f"/{name} {args}".strip()
            if self.busy:
                self.add_notice("Finish or interrupt the current turn first.", "warning")
                return
            self.add_user(display)
            self.busy = True
            self.set_status("thinking")
            await self._agent_worker(expanded)
        except Exception as exc:
            self.add_notice(f"/{name} failed: {type(exc).__name__}: {exc}", "error")

    async def _builtin_slash(self, name: str, args: str) -> bool:
        if name in ("exit", "quit"):
            await self._quit()
            return True
        if name == "help":
            self.push_screen(HelpScreen(help_markdown(self.config, self.custom_commands)))
            return True
        if name == "clear":
            if self.busy:
                self.add_notice("Interrupt the current turn before clearing.", "warning")
                return True
            await self._clear_conversation()
            return True
        if name == "compact":
            if self.busy:
                self.add_notice("Cannot compact while a turn is running.", "warning")
                return True
            ok = await self.agent.maybe_compact(force=True, instructions=args)
            if not ok:
                self.add_notice("Nothing to compact yet.", "info")
            return True
        if name == "cost":
            self.add_info("Usage and cost", self._cost_report())
            return True
        if name == "status":
            self.add_info("Status", self._status_report())
            return True
        if name == "doctor":
            self.add_info("Doctor", self._doctor_report())
            return True
        if name == "model":
            if args.strip():
                await self._switch_model(models.resolve_model_id(args.strip()) or args.strip())
            else:
                choice = await self.push_screen_wait(
                    ModelPickerScreen(self.config.model, mock=self.config.mock)
                )
                if choice:
                    await self._switch_model(choice)
            return True
        if name == "config":
            self.add_info("Configuration", self._config_report())
            return True
        if name == "permissions":
            self.add_info("Permissions", self._permissions_report())
            return True
        if name == "memory":
            self.add_info("Memory", memory_summary(self.config.cwd))
            return True
        if name == "init":
            if self.busy:
                self.add_notice("Finish the current turn first.", "warning")
                return True
            self.add_user("/init")
            self.busy = True
            self.set_status("thinking")
            await self._agent_worker(init_prompt())
            return True
        if name == "todos":
            self.add_info("Todos", self._todos_report())
            return True
        if name == "agents":
            self.add_info("Sub-agents", subagent_listing(self.config.cwd))
            return True
        if name == "skills":
            listing = skill_listing(self.config.cwd, self.config.home)
            self.add_info("Skills", listing or "No skills found.")
            return True
        if name == "checkpoints":
            self.add_info("Checkpoints", self._checkpoints_report())
            return True
        if name == "revert":
            self._revert(args.strip())
            return True
        if name == "mcp":
            self.add_info("MCP servers", self._mcp_report())
            return True
        if name == "resume":
            await self._pick_session()
            return True
        if name == "export":
            self._export(args.strip())
            return True
        if name == "add-dir":
            self._add_dir(args.strip())
            return True
        return False

    async def _clear_conversation(self) -> None:
        self.agent.reset_conversation()
        self.cards.clear()
        self.task_cards.clear()
        self.current_assistant = None
        self.current_thinking = None
        self._thinking_indicator = None  # transcript 将被清空，引用一并作废
        try:
            await self.transcript().remove_children()
        except Exception:
            pass
        self.transcript().remove_class("has-messages")
        self.show_welcome()
        self.add_notice("Conversation cleared. The session id is unchanged.", "success")
        self._refresh_usage()
        self.set_status("idle")

    async def _switch_model(self, model: str) -> None:
        if not model:
            return
        if model == self.config.model:
            self.add_notice(f"Already using {model}.", "info")
            return
        spec = models.get_spec(model)
        provider = spec.provider if spec else self.config.provider
        try:
            fresh = build_config(
                self.config.cwd, model=model, provider=provider,
                mock=self.config.mock,
            )
        except Exception as exc:
            self.add_notice(f"Could not build config: {exc}", "error")
            return
        if not fresh.provider_ready():
            self.add_notice(
                f"No API key configured for provider '{provider}'.", "error"
            )
            return
        await self.agent.set_model(fresh.model, fresh.provider, fresh.api_key,
                                   fresh.base_url)
        self.config.model = fresh.model
        self.config.provider = fresh.provider
        self._update_header()
        self.add_notice(f"Model: {self.config.model} ({self.config.provider})", "success")

    async def _pick_session(self) -> None:
        if self.busy:
            self.add_notice("Interrupt the current turn before resuming.", "warning")
            return
        metas = Session.list_sessions(self.config.session_dir)
        if not metas:
            self.add_notice("No saved sessions for this directory.", "info")
            return
        meta = await self.push_screen_wait(
            SessionPickerScreen(metas, self.session.meta.id)
        )
        if meta is None:
            return
        await self._load_session(meta)

    async def _load_session(self, meta: SessionMeta) -> None:
        session = Session.load(self.config.session_dir, meta.id)
        if session is None:
            self.add_notice(f"Session {meta.id} could not be loaded.", "error")
            return
        try:
            await self.agent.close()
        except Exception:
            pass
        self.provider = create_provider(self.config)
        self.agent = Agent(
            self.config, self.provider, create_tools(),
            callbacks=self.callbacks, session=session,
        )
        if self.mcp_tools:
            self.agent.add_tools(self.mcp_tools)
        self.session = session
        self.cards.clear()
        self.task_cards.clear()
        self.current_assistant = None
        self.current_thinking = None
        self._thinking_indicator = None  # transcript 将被清空，引用一并作废
        try:
            await self.transcript().remove_children()
        except Exception:
            pass
        self.show_welcome()
        self.replay_session()
        self.update_todos(session.todos)
        self._refresh_usage()
        self.set_status("idle")
        self.add_notice(f"Resumed session {meta.id}", "success")

    def replay_session(self) -> None:
        if not self.session.messages:
            return
        self.transcript().add_class("has-messages")
        results: dict[str, ToolResultBlock] = {}
        for message in self.session.messages:
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    results[block.tool_use_id] = block
        for message in self.session.messages:
            if message.role == "user":
                texts = [
                    block.text for block in message.content
                    if isinstance(block, TextBlock) and block.text.strip()
                ]
                # 保留图片附件块，恢复会话后仍能看到用户当时发的图。
                image_blocks = [b for b in message.content if isinstance(b, ImageBlock)]
                text = "\n".join(texts)
                if text.startswith("<hook-context>") or text.startswith("<referenced-files>"):
                    continue
                if text.startswith("This session is being continued"):
                    self.add_notice("Resumed from a compaction summary.", "info")
                    continue
                # 构造要显示的内容：文本块 + 图片块（UserMessage 已支持块列表）。
                display: str | list[Block] = (
                    [TextBlock(text), *image_blocks] if image_blocks else text
                )
                if not text and not image_blocks:
                    continue
                self.transcript().mount(UserMessage(display))
                continue
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    widget = AssistantMessage(block.text)
                    self.transcript().mount(widget)
                    widget.start()
                elif isinstance(block, ToolUseBlock):
                    tool = self.agent.tools.get(block.name)
                    diff = None
                    if tool is not None:
                        try:
                            diff = tool.get_diff(block.input, self.agent.ctx)
                        except Exception:
                            diff = None
                    card = ToolCard(block, tool, diff)
                    result_block = results.get(block.id)
                    if result_block is not None:
                        content = result_block.content
                        if isinstance(content, list):
                            content = extract_text(content)
                        card.set_result(
                            ToolResult(str(content), is_error=result_block.is_error), 0.0
                        )
                    self.transcript().mount(card)
        self._scroll_soon()

    # -- reports -----------------------------------------------------------
    def _cost_report(self) -> str:
        usage = self.agent.total_usage
        cost = self.agent.total_cost
        spec = models.get_spec(self.config.model)
        lines = [
            f"- Model: {self.config.model}",
            f"- Input tokens: {usage.input_tokens:,}",
            f"- Output tokens: {usage.output_tokens:,}",
            f"- Cache write: {usage.cache_creation_input_tokens:,}",
            f"- Cache read: {usage.cache_read_input_tokens:,}",
            f"- Estimated cost: ${cost:.4f}",
        ]
        if spec:
            lines += [
                "",
                "Estimated prices (USD per 1M tokens):",
                f"- Input: ${spec.input_price:g}",
                f"- Output: ${spec.output_price:g}",
                f"- Cache read: ${spec.cache_read_price:g}",
                f"- Cache write: ${spec.cache_write_price:g}",
            ]
        if self.config.mock:
            lines += ["", "Offline mock provider — no real tokens are billed."]
        return "\n".join(lines)

    def _status_report(self) -> str:
        usage = self.agent.total_usage
        lines = [
            f"- Model: {self.config.model} · provider {self.config.provider}",
            f"- Working directory: {self.config.cwd}",
            f"- Permission mode: {self.config.permission_mode}",
            f"- Session: {self.session.meta.id} · {self.session.meta.turns} turns",
            f"- Tokens: {human_tokens(usage.prompt_tokens)} in / "
            f"{human_tokens(usage.output_tokens)} out · ${self.agent.total_cost:.4f}",
            f"- Context window: {self.config.context_window:,} tokens",
            f"- Shell: {self.config.shell}",
            f"- Todos: {len(self.agent.todos)}",
        ]
        git = git_info(self.config.cwd)
        if git.get("is_repo"):
            lines.append(f"- Git branch: {git.get('branch')}")
        return "\n".join(lines)

    def _doctor_report(self) -> str:
        checks: list[str] = []
        checks.append(f"- Python {sys.version.split()[0]} on {platform.system()} "
                      f"{platform.release()}")
        checks.append(f"- YotCode v{__version__}")
        checks.append(f"- Terminal: {'interactive' if sys.stdin.isatty() else 'not a tty'}")
        checks.append(f"- Working directory: {self.config.cwd}")
        writable = os.access(self.config.cwd, os.W_OK)
        checks.append(f"- Working directory writable: {'yes' if writable else 'no'}")
        shell = self.config.shell
        shell_ok = Path(shell).is_file() if os.path.isabs(shell) else bool(
            __import__("shutil").which(shell)
        )
        checks.append(f"- Shell: {shell} ({'found' if shell_ok else 'not found'})")
        key = self.config.api_key or ""
        masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else "missing")
        checks.append(f"- Provider: {self.config.provider} · API key: {masked}")
        checks.append(f"- Base URL: {self.config.base_url}")
        checks.append(f"- Context window: {self.config.context_window:,} tokens")
        settings = self.config.settings_files or []
        checks.append("- Settings files: " + (
            ", ".join(settings) if settings else "none (defaults)"
        ))
        memory = memory_summary(self.config.cwd)
        checks.append("- " + memory.replace("\n", "\n  "))
        git = git_info(self.config.cwd)
        checks.append("- Git: " + (
            f"branch {git.get('branch')}" if git.get("is_repo") else "not a repository"
        ))
        if self.config.mcp_servers:
            connected = len(getattr(self.mcp_manager, "clients", {}) or {})
            checks.append(f"- MCP servers: {connected}/{len(self.config.mcp_servers)} connected")
        else:
            checks.append("- MCP servers: none configured")
        sessions = Session.list_sessions(self.config.session_dir)
        checks.append(f"- Saved sessions here: {len(sessions)}")
        return "\n".join(checks)

    def _config_report(self) -> str:
        files = self.config.settings_files
        lines = ["Settings files (later files win):", ""]
        lines += [f"- {path}" for path in files] or ["- none found; defaults are in use"]
        lines.append("")
        lines.append("Effective values:")
        lines.append("")
        for key, value in [
            ("model", self.config.model),
            ("provider", self.config.provider),
            ("permission mode", self.config.permission_mode),
            ("max tokens", self.config.max_tokens),
            ("max turns", self.config.max_turns),
            ("auto compact", self.config.auto_compact),
            ("compact threshold", self.config.compact_threshold),
            ("thinking", self.config.thinking),
            ("shell", self.config.shell),
        ]:
            lines.append(f"- {key}: {value}")
        return "\n".join(lines)

    def _permissions_report(self) -> str:
        perms = self.config.permissions or {}
        lines = [f"Mode: {self.config.permission_mode}", ""]
        for label, key in (
            ("Allow", "allow"), ("Ask", "ask"), ("Deny", "deny"),
            ("Additional directories", "additionalDirectories"),
        ):
            values = perms.get(key) or []
            lines.append(f"{label}:")
            if values:
                lines += [f"- {value}" for value in values]
            else:
                lines.append("- (none)")
            lines.append("")
        session_allow = sorted(self.agent.permissions.session_allow)
        if session_allow:
            lines.append("Allowed this session:")
            lines += [f"- {rule}" for rule in session_allow]
        lines += [
            "",
            "Shift+Tab cycles default → accept edits → plan. "
            "Rules are saved to .yotcode/settings.local.json when you choose "
            "\"always allow\".",
        ]
        return "\n".join(lines)

    def _todos_report(self) -> str:
        todos = self.agent.todos
        if not todos:
            return "No todos yet. Ask YotCode to plan multi-step work."
        symbols = {
            "completed": "√", "in_progress": "~", "pending": "○",
        }
        done = sum(1 for t in todos if t.get("status") == "completed")
        lines = [f"{done}/{len(todos)} complete", ""]
        for todo in todos:
            mark = symbols.get(str(todo.get("status")), "○")
            lines.append(f"- {mark} {todo.get('content') or ''}")
        return "\n".join(lines)

    def _mcp_report(self) -> str:
        servers = self.config.mcp_servers or {}
        if not servers:
            return "No MCP servers configured. Add mcpServers to settings.json."
        lines: list[str] = []
        clients = getattr(self.mcp_manager, "clients", {}) or {}
        errors = getattr(self.mcp_manager, "errors", {}) or {}
        for name, cfg in servers.items():
            if name in clients:
                count = len(getattr(clients[name], "tools", []) or [])
                lines.append(f"- {name}: connected ({count} tools)")
            elif name in errors:
                lines.append(f"- {name}: error: {errors[name]}")
            else:
                lines.append(f"- {name}: not connected")
        tools = [tool.name for tool in self.mcp_tools]
        if tools:
            lines += ["", "Tools:", ""] + [f"- {tool}" for tool in tools]
        return "\n".join(lines)

    def _export(self, target: str) -> None:
        if target:
            path = expand_path(target, self.config.cwd)
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.config.cwd / f"yotcode-session-{stamp}.md"
        try:
            write_text(path, self.session.export_markdown(), newline="\n")
        except Exception as exc:
            self.add_notice(f"Export failed: {exc}", "error")
            return
        self.add_notice(f"Exported transcript to {path}", "success")

    def _add_dir(self, raw: str) -> None:
        if not raw:
            self.add_notice("Usage: /add-dir <path>", "warning")
            return
        path = expand_path(raw, self.config.cwd)
        if not path.is_dir():
            self.add_notice(f"Not a directory: {path}", "error")
            return
        dirs = self.config.permissions.setdefault("additionalDirectories", [])
        if str(path) not in dirs:
            dirs.append(str(path))
        if path not in self.agent.permissions.additional_dirs:
            self.agent.permissions.additional_dirs.append(path)
        self.add_notice(f"Additional directory allowed: {path}", "success")

    # -- MCP ---------------------------------------------------------------
    async def _connect_mcp(self) -> None:
        from ..tools.mcp import MCPManager

        self.mcp_manager = MCPManager(self.config.mcp_servers, self.config.cwd)
        tools = await self.mcp_manager.connect_all(self.callbacks.on_notice)
        self.mcp_tools = tools
        if tools:
            self.agent.add_tools(tools)

    # -- completions -------------------------------------------------------
    def command_completions(self, prefix: str) -> list[tuple[str, str, str]]:
        prefix = prefix.lower()
        items: list[tuple[str, str, str]] = []
        for item in BUILTIN_COMMANDS:
            name = item["name"]
            if prefix and not name.lower().startswith(prefix):
                continue
            display = "/" + name + ((" " + item["args"]) if item["args"] else "")
            items.append((display, "/" + name, item["description"]))
        for name, command in self.custom_commands.items():
            if prefix and not name.lower().startswith(prefix):
                continue
            items.append(("/" + name, "/" + name, command.description))
        return items

    def _files(self) -> list[tuple[str, bool]]:
        now = time.time()
        if self._file_cache is not None and now - self._file_cache_at < 30:
            return self._file_cache
        from ..tools.file_tools import IGNORED_DIRS

        cwd = self.config.cwd
        entries: list[tuple[str, bool]] = []
        try:
            for root, dirs, files in os.walk(cwd):
                dirs[:] = [
                    d for d in dirs
                    if d not in IGNORED_DIRS
                    and (not d.startswith(".") or d in (".claude", ".yotcode", ".claudepy", ".github"))
                ]
                rel_root = os.path.relpath(root, cwd).replace("\\", "/")
                for directory in dirs:
                    rel = directory if rel_root == "." else f"{rel_root}/{directory}"
                    entries.append((rel + "/", True))
                for name in files:
                    rel = name if rel_root == "." else f"{rel_root}/{name}"
                    entries.append((rel, False))
                if len(entries) > 25_000:
                    break
        except OSError:
            pass
        self._file_cache = entries
        self._file_cache_at = now
        return entries

    def file_completions(self, prefix: str) -> list[tuple[str, str, str]]:
        needle = prefix.lower().replace("\\", "/")
        files = self._files()
        if needle:
            matches = [entry for entry in files if needle in entry[0].lower()]
            matches.sort(key=lambda entry: (
                not entry[0].lower().startswith(needle),
                entry[0].count("/"),
                len(entry[0]),
            ))
        else:
            matches = sorted(files, key=lambda entry: (entry[0].count("/"), entry[0]))[:200]
        out: list[tuple[str, str, str]] = []
        for rel, is_dir in matches[:60]:
            label = rel if is_dir else rel.rsplit("/", 1)[-1]
            description = "" if is_dir else (rel.rsplit("/", 1)[0] if "/" in rel else "")
            out.append((label, "@" + rel, description))
        return out

    # -- palette -----------------------------------------------------------
    def _palette_items(self) -> list[tuple[str, str, str]]:
        items = [(item["name"], item["args"], item["description"])
                 for item in BUILTIN_COMMANDS]
        items += [(name, "", command.description)
                  for name, command in self.custom_commands.items()]
        return items

    # -- actions -----------------------------------------------------------
    def _modal_open(self) -> bool:
        return isinstance(self.screen, (
            PermissionScreen, HelpScreen, ModelPickerScreen,
            SessionPickerScreen, QuestionScreen, CommandPaletteScreen,
        ))

    def action_interrupt_or_quit(self) -> None:
        screen = self.screen
        if isinstance(screen, PermissionScreen):
            screen.dismiss(PermissionDecision(False, reason="the user interrupted"))
            return
        if isinstance(screen, (HelpScreen, ModelPickerScreen, SessionPickerScreen,
                               QuestionScreen, CommandPaletteScreen)):
            screen.dismiss(None)
            return
        if self.busy:
            self.agent.interrupt()
            self.add_notice("Interrupting the current turn…", "warning")
            return
        prompt = self.prompt_area().prompt
        if prompt.text:
            prompt.clear()
            return
        now = time.time()
        if now - self._last_ctrl_c < 1.5:
            self.action_quit_app()
        else:
            self._last_ctrl_c = now
            self.add_notice("Press Ctrl+C again to exit, or use /exit.", "info")

    def action_escape(self) -> None:
        if self.busy:
            self.agent.interrupt()
            return
        try:
            area = self.prompt_area()
            area._hide_completions()
        except Exception:
            pass

    def action_cycle_mode(self) -> None:
        current = self.config.permission_mode
        if current in MODE_ORDER:
            index = MODE_ORDER.index(current)
            next_mode = MODE_ORDER[(index + 1) % len(MODE_ORDER)]
        else:
            next_mode = "default"
        self.agent.set_permission_mode(next_mode)
        # 模式同时体现在输入框前缀和输入框下方的状态栏。
        # set_permission_mode() 内部已经通过 on_status 回调更新了状态栏，
        # 这里只更新输入框前缀，避免同一帧内二次重绘整行底栏（conhost 下
        # 二次重绘会让上一行模式文案残留，出现多行状态栏）。
        try:
            self.prompt_area().set_mode(next_mode)
        except Exception:
            pass

    def action_ctrl_d(self) -> None:
        prompt = self.prompt_area().prompt
        if prompt.text:
            try:
                prompt.action_delete_right()
            except Exception:
                pass
            return
        self.action_quit_app()

    def action_quit_app(self) -> None:
        self.run_worker(self._quit(), group="quit", exclusive=True)

    async def _quit(self) -> None:
        try:
            await self.agent.close()
        except Exception:
            pass
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.close()
            except Exception:
                pass
        self.exit()

    def action_clear_screen(self) -> None:
        if self._modal_open():
            return
        self.run_worker(self._clear_screen(), group="ui", exclusive=True)

    async def _clear_screen(self) -> None:
        try:
            await self.transcript().remove_children()
        except Exception:
            pass
        self.transcript().remove_class("has-messages")
        self.show_welcome()
        self.add_notice("Screen cleared — the conversation is still in context.", "info")

    def action_toggle_tools(self) -> None:
        if self._modal_open():
            return
        for card in self.query(ToolCard):
            card.toggle()
        self.scroll_down(force=True)

    def action_help(self) -> None:
        if self._modal_open():
            return
        self.push_screen(HelpScreen(help_markdown(self.config, self.custom_commands)))

    def action_command_palette(self) -> None:
        if self._modal_open():
            return
        self.push_screen(
            CommandPaletteScreen(self._palette_items()), self._palette_chosen
        )

    def _palette_chosen(self, name: str | None) -> None:
        if not name:
            return
        area = self.prompt_area()
        area._set_text("/" + name + " ")
        area.prompt.focus()

    def action_resume_picker(self) -> None:
        if self._modal_open():
            return
        self.run_worker(self._pick_session(), group="command", exclusive=True)

    def action_show_todos(self) -> None:
        if self._modal_open():
            return
        self.add_info("Todos", self._todos_report())
