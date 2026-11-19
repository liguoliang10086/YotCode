"""聊天记录组件与输入框。

AssistantMessage 用一个小型“排空任务”平滑消费流式文本：
模型可能在一秒内推来几百个小片段，如果每个片段都立即 append Markdown，
Textual 会忙于重排导致卡顿；_drain() 每次合并当前缓冲区、渲染后再让出
20ms，从而既保持流畅又不会丢字。

布局参考新版 Claude Code：
  - 用户消息用 `❯` 开头，助手消息用 `⏺` 开头；
  - 工具调用是一行 `└ Name arg  ✓ 0.4s`，输出用 `⎿` 缩进；
  - 顶部一行上下文胶囊，底部一行状态与上下文进度；
  - 不用大卡片，只有 diff 是真正的卡片。
"""
from __future__ import annotations

import asyncio
import dataclasses
import difflib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Collapsible, Markdown, OptionList, Static, TextArea
from textual.widgets._text_area import TextAreaTheme
from textual.widgets.option_list import Option

from .. import __version__
from ..config import Config
from ..utils import display_path, format_duration, git_info
from .render import (
    ACCENT,
    ACCENT_SOFT,
    BLUE,
    CYAN,
    DIM,
    FAINT,
    GREEN,
    MUTED,
    PURPLE,
    RED,
    SPINNER,
    SURFACE_2,
    TEXT,
    YELLOW,
    gradient_text,
    info_renderable,
    todos_renderable,
    tool_body,
    tool_name_style,
    version_pill,
    welcome_renderable,
    with_gutter,
)

if TYPE_CHECKING:  # pragma: no cover
    from .app import YotCodeApp


def _short_cwd(cwd: Path, limit: int = 48) -> str:
    raw = str(cwd).replace("\\", "/")
    home = str(Path.home()).replace("\\", "/")
    if raw.lower().startswith(home.lower()):
        raw = "~" + raw[len(home):]
    if len(raw) <= limit:
        return raw
    parts = raw.split("/")
    tail = "/".join(parts[-3:])
    return "…/" + tail if len(tail) + 2 <= limit else "…" + raw[-limit:]


def _model_label(model: str, context_label: str) -> str:
    label = (context_label or "").strip()
    if label and label != "?":
        return f"{model}  ·  {label}"
    return model


def _model_pill(model: str, context_label: str) -> Text:
    """模型胶囊：深色背景上的蓝字，长度只包住文字本身。"""
    label = (context_label or "").strip()
    pill = Text()
    pill.append(" ", style=f"on {SURFACE_2}")
    pill.append(model, style=f"{BLUE} on {SURFACE_2}")
    if label and label != "?":
        pill.append(f" · {label}", style=f"{FAINT} on {SURFACE_2}")
    pill.append(" ", style=f"on {SURFACE_2}")
    return pill


def _branch_pill(branch: str) -> Text:
    branch = branch[:18] + ("…" if len(branch) > 18 else "")
    pill = Text()
    pill.append(" ", style=f"on {SURFACE_2}")
    pill.append("●", style=f"{GREEN} on {SURFACE_2}")
    pill.append(f" {branch} ", style=f"{MUTED} on {SURFACE_2}")
    return pill


class HeaderBar(Horizontal):
    """顶部一行式上下文条：品牌、路径、分支/模型胶囊、快捷键提示。"""

    def __init__(self, config: Config) -> None:
        super().__init__(id="header")
        self.config = config

    def compose(self) -> ComposeResult:
        # 品牌：浅橙→陶土橙渐变字 + 反色版本胶囊
        brand = gradient_text("yotcode")
        brand.append("  ")
        brand.append_text(version_pill(__version__))
        yield Static(brand, classes="brand", markup=False)

        # 竖向细分隔：把品牌与路径信息分开
        yield Static(Text("│", style=FAINT), classes="sep", markup=False)

        # 路径：短路径（符号全部用 CP437 安全字形，cmd.exe 不乱码）
        cwd = Text()
        cwd.append("► ", style=FAINT)
        cwd.append(_short_cwd(self.config.cwd), style=DIM)
        yield Static(cwd, classes="cwd", markup=False)

        branch = ""
        try:
            branch = str(git_info(self.config.cwd).get("branch") or "")
        except Exception:
            branch = ""
        if branch:
            yield Static(_branch_pill(branch), classes="branch", markup=False)
        yield Static(
            _model_pill(self.config.model, self.config.model_context_label()),
            classes="model",
            id="header-model",
            markup=False,
        )
        key = Text()
        key.append(" ^P ", style=f"{DIM} on {SURFACE_2}")
        yield Static(key, classes="keyhint", markup=False)

    def update_model(self, model: str, context_label: str) -> None:
        try:
            self.query_one("#header-model", Static).update(
                _model_pill(model, context_label)
            )
        except Exception:
            pass


class WelcomePanel(Static):
    def __init__(self, config: Config, resumed: bool = False,
                 session_title: str = "") -> None:
        super().__init__(
            welcome_renderable(config, resumed, session_title),
            classes="welcome",
        )


class UserMessage(Horizontal):
    """用户消息：`>` + 正文，正文自动换行并对齐到符号右侧。

    图标参考 Claude Code：用户输入用橙色右尖括号。
    """

    def __init__(self, text: str) -> None:
        super().__init__(classes="user-message")
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(">", classes="msg-bullet user-bullet", markup=False)
        yield Static(Text(self.text, style=TEXT), classes="user-text", markup=False)


class AssistantMessage(Horizontal):
    """A streaming markdown block, prefixed by a Claude-style bullet.

    图标参考 Claude Code：助手回答用实心圆点 `●`（对应官方的 ⏺）。
    """

    def __init__(self, initial: str = "") -> None:
        super().__init__(classes="assistant-message")
        self._buffer = initial
        self._ready = False
        self._mounted = False
        self._drain_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Static("●", classes="msg-bullet assistant-bullet", markup=False)
        yield Markdown("", classes="assistant-md")

    def on_mount(self) -> None:
        self._mounted = True
        if self._ready and self._buffer:
            self._kick()

    def start(self) -> None:
        self._ready = True
        if self._mounted and self._buffer:
            self._kick()

    def feed(self, text: str) -> None:
        self._buffer += text
        if self._ready and self._mounted:
            self._kick()

    def _kick(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        """把缓冲区里的增量成批刷到 Markdown 组件，避免每个 token 都重排。"""
        markdown = self.query_one(Markdown)
        while self._buffer:
            chunk, self._buffer = self._buffer, ""
            try:
                await markdown.append(chunk)
            except Exception:
                return
            await asyncio.sleep(0.02)

    async def finish(self) -> None:
        if self._drain_task is not None:
            try:
                await self._drain_task
            except Exception:
                pass
        if self._buffer:
            try:
                await self.query_one(Markdown).append(self._buffer)
            except Exception:
                pass
            self._buffer = ""


class ThinkingPanel(Collapsible):
    def __init__(self) -> None:
        self._text = ""
        self._body = Static(Text("", style=f"italic {DIM}"), classes="thinking-body")
        super().__init__(self._body, title="thinking", collapsed=True,
                         classes="thinking-panel")

    def feed(self, text: str) -> None:
        self._text += text
        self._body.update(Text(self._text, style=f"italic {DIM}"))

    def finish(self) -> None:
        if self._text:
            self.title = f"thinking · {len(self._text)} chars"


class ToolHeader(Horizontal):
    """一行工具标题：`└ Read  src/app.py   ✓ 0.4s`。"""

    def __init__(self, card: "ToolCard", tool_name: str, summary: str) -> None:
        super().__init__(classes="tool-header")
        self.card = card
        self.tool_name = tool_name
        self.summary = summary
        summary = " ".join((summary or "").split())
        if len(summary) > 100:
            summary = summary[:99] + "…"
        self._mark = Static("└", classes="tool-mark", markup=False)
        self._label = Static(
            Text(tool_name, style=tool_name_style(tool_name)),
            classes="tool-name",
            markup=False,
        )
        self._arg = Static(
            Text(summary, style=MUTED) if summary else "",
            classes="tool-arg",
            markup=False,
        )
        self._status = Static("", classes="tool-status", markup=False)
        self._duration = Static("", classes="tool-duration", markup=False)

    def compose(self) -> ComposeResult:
        # 左侧是树形分支符号，中间是名字和参数，右侧是状态与耗时。
        yield self._mark
        yield self._label
        yield self._arg
        yield self._status
        yield self._duration

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.card.toggle()

    def set_status(self, icon: str, css_class: str) -> None:
        try:
            self._status.update(Text(icon, style={
                "done": GREEN,
                "error": RED,
                "waiting": CYAN,
                "running": YELLOW,
            }.get(css_class, FAINT)))
            self._status.remove_class("done", "error", "waiting", "running")
            self._status.add_class(css_class)
        except Exception:
            pass

    def set_duration(self, duration: float) -> None:
        try:
            self._duration.update(
                Text(format_duration(duration), style=FAINT) if duration else ""
            )
        except Exception:
            pass


class ToolCard(Vertical):
    def __init__(self, block: Any, tool: Any,
                 diff: tuple[str, str, str] | None = None) -> None:
        super().__init__(classes="tool-card running")
        self.block = block
        self.tool = tool
        self.diff = diff
        self.result: Any = None
        self.duration = 0.0
        self.expanded = False
        self.progress = ""
        self._body = Static("", classes="tool-body")
        self._body.display = False
        self._footer = Static("", classes="tool-footer")
        self._header = ToolHeader(self, block.name, tool.summary(block.input)
                                  if tool is not None else "")

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body
        yield self._footer

    def on_mount(self) -> None:
        self._refresh_body()

    # -- state -------------------------------------------------------------
    def set_waiting(self) -> None:
        self._header.set_status("○", "waiting")
        self._body.update(with_gutter(
            Text("Waiting for permission…", style=f"italic {YELLOW}")
        ))
        self._body.display = True

    def set_running(self) -> None:
        self.remove_class("waiting")
        self.add_class("running")
        self._header.set_status("~", "running")
        if self.result is None and not self.progress:
            self._body.display = False

    def set_result(self, result: Any, duration: float = 0.0) -> None:
        self.result = result
        self.duration = duration
        self.remove_class("running", "waiting")
        if result.is_error:
            self.add_class("error")
            self._header.set_status("x", "error")
        else:
            self.add_class("done")
            self._header.set_status("√", "done")
        self._header.set_duration(duration)
        self._refresh_body()

    def add_progress(self, text: str) -> None:
        if not text:
            return
        self.progress += text
        self._refresh_body()

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._refresh_body()

    # -- rendering ---------------------------------------------------------
    def _is_diff(self) -> bool:
        return bool(self.diff) and self.block.name in ("Edit", "MultiEdit", "Write")

    def _refresh_body(self) -> None:
        if self.result is None:
            if self.progress:
                self._body.update(with_gutter(
                    Text(self.progress[-4000:], style=FAINT)
                ))
                self._body.display = True
            else:
                self._body.display = False
            return
        self._body.display = True
        output = self.result.text
        body = tool_body(
            self.block.name, self.block.input, output,
            diff=self.diff, expanded=self.expanded,
            error=self.result.is_error, progress=self.progress,
            meta=getattr(self.result, "meta", {}) or {},
            duration=self.duration,
        )
        # diff 本身就是一张卡片，再套 `⎿` 会显得拥挤。
        if self._is_diff():
            self._body.update(body)
        else:
            self._body.update(with_gutter(body))

        if self._is_diff():
            before = self.diff[1].replace("\r\n", "\n").splitlines()
            after = self.diff[2].replace("\r\n", "\n").splitlines()
            total = len(list(difflib.unified_diff(before, after, lineterm="", n=3)))
            has_more = total > 40
            label = f"… {max(0, total - 40)} more diff lines · click to expand"
        else:
            lines = len(output.splitlines())
            has_more = lines > 10
            label = f"… {max(0, lines - 10)} more lines · click to expand"
        if self.expanded:
            self._footer.update(
                Text("click to collapse", style=FAINT) if has_more else ""
            )
        elif has_more:
            self._footer.update(Text(label, style=FAINT))
        else:
            self._footer.update("")


class TodoPanel(Static):
    def __init__(self) -> None:
        super().__init__("", classes="todo-panel")
        self.display = False

    def update_todos(self, todos: list[dict[str, Any]]) -> None:
        self.todos = todos
        if not todos:
            self.display = False
            return
        self.display = True
        self.update(todos_renderable(todos))


class InfoPanel(Static):
    def __init__(self, title: str, body: str) -> None:
        super().__init__(info_renderable(title, body), classes="info-panel")

    @classmethod
    def markdown(cls, title: str, body: str) -> "InfoPanel":
        panel = cls(title, "")
        panel.update_markdown(title, body)
        return panel

    @classmethod
    def plain(cls, title: str, body: str) -> "InfoPanel":
        """报告类信息用纯文本渲染：没有 Markdown 列表的额外上下留白。"""
        panel = cls(title, "")
        panel.update_plain(title, body)
        return panel

    def update_markdown(self, title: str, body: str) -> None:
        self.update(info_renderable(title, body))

    def update_plain(self, title: str, body: str) -> None:
        renderable = Text()
        renderable.append("• ", style=ACCENT)
        renderable.append(title, style=ACCENT_SOFT)
        renderable.append("\n")
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                renderable.append("\n")
                continue
            if line.startswith("- "):
                renderable.append("  • ", style=FAINT)
                renderable.append(line[2:], style=TEXT)
            elif stripped.endswith(":") and len(stripped) < 48:
                renderable.append("  " + line, style=ACCENT_SOFT)
            elif line.startswith("  "):
                renderable.append("  " + line, style=DIM)
            else:
                renderable.append("  " + line, style=MUTED)
            renderable.append("\n")
        self.update(renderable)


class NoticePanel(Static):
    def __init__(self, message: str, level: str = "info") -> None:
        color = {"error": RED, "warning": YELLOW, "success": GREEN}.get(level, DIM)
        icon = {"error": "x", "warning": "!", "success": "√"}.get(level, "·")
        renderable = Text()
        renderable.append(f"{icon} ", style=color)
        renderable.append(message, style=color if level != "info" else DIM)
        super().__init__(renderable, classes="notice-panel", markup=False)


class ThinkingIndicator(Static):
    """对话流里的转圈指示器：`◐ Thinking 3s`，出现在用户消息下方。

    样式参考 Claude Code：一个灰色的小圆环在转，后面跟着灰色的 Thinking
    文字，整体弱化，不抢正文的视觉重心。输入框下方的状态栏不再重复显示
    thinking，避免同一件事出现两次。
    """

    def __init__(self) -> None:
        super().__init__("", classes="thinking-indicator", markup=False)
        self._index = 0
        self._start = 0.0
        self._timer = None

    def on_mount(self) -> None:
        self._start = time.monotonic()
        self._timer = self.set_interval(0.12, self._tick)
        self._tick()

    def _tick(self) -> None:
        self._index = (self._index + 1) % len(SPINNER)
        glyph = SPINNER[self._index]
        secs = int(time.monotonic() - self._start)
        text = Text()
        # 转圈和文字都用灰色：思考是背景噪音，正文才是主角
        text.append(f"{glyph} ", style=MUTED)
        text.append("Thinking", style=DIM)
        if secs:
            text.append(f" {secs}s", style=FAINT)
        self.update(text)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None


class StatusBar(Static):
    """单行底栏：整行作为一个 Static 更新。

    conhost 下把左右两段分别 update，会在切换模式时留下上一行残影；
    这里拼成一整行一次性写入，避免差量渲染产生重复状态栏。
    """

    def __init__(self) -> None:
        super().__init__("", id="status")
        self._left: Text = Text("")
        self._right: Text = Text("")
        self._spin_index = 0
        self._spin_label = ""
        self._spin_color = ACCENT
        self._spin_start = 0.0
        self._spin_timer = None

    def set_left(self, text: str | Text, color: str = DIM) -> None:
        self.stop_spinner()
        self._left = text if isinstance(text, Text) else Text(text, style=color)
        self._refresh_line()

    def set_right(self, text: str | Text, color: str = FAINT) -> None:
        self._right = text if isinstance(text, Text) else Text(text, style=color)
        self._refresh_line()

    def on_resize(self) -> None:
        self._refresh_line()

    def start_spinner(self, label: str, color: str = ACCENT) -> None:
        """启动转圈指示器：► Label… 3s（状态栏左侧）。"""
        self._spin_label = label
        self._spin_color = color
        self._spin_start = time.monotonic()
        self._spin_index = 0
        self._render_spinner()
        if self._spin_timer is None:
            self._spin_timer = self.set_interval(0.12, self._tick_spinner)

    def stop_spinner(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _tick_spinner(self) -> None:
        self._spin_index = (self._spin_index + 1) % len(SPINNER)
        self._render_spinner()

    def _render_spinner(self) -> None:
        glyph = SPINNER[self._spin_index]
        secs = int(time.monotonic() - self._spin_start)
        text = Text()
        text.append(f"{glyph} ", style=ACCENT)
        text.append(self._spin_label, style=self._spin_color)
        text.append(f" {secs}s", style=FAINT)
        self._left = text
        self._refresh_line()

    def _refresh_line(self) -> None:
        # 首帧布局前 size 可能是 0；此时写整行会让 Rich 把内容按 1 列折行，
        # conhost 下这些折行会残留在输入框和状态栏之间。等 on_resize 拿到真实
        # 宽度再渲染。
        content_width = getattr(self, "content_size", self.size).width
        if content_width <= 1:
            return
        # 末尾留一列空白，避免 conhost wrap-pending 把整屏上滚。
        width = content_width - 1
        left = self._left
        right = self._right
        left_len = len(left.plain)
        right_len = len(right.plain)
        if left_len + right_len + 1 > width:
            keep = max(width - right_len - 1, 0)
            left = Text(left.plain[:keep], style=left.style)
            left_len = len(left.plain)
        gap = max(1, width - left_len - right_len)
        line = Text()
        line.append_text(left)
        line.append(" " * gap)
        line.append_text(right)
        # 硬性禁止折行：即使某帧 width 计算偏小，底栏也只能被裁切，绝不能
        # 溢出成第二行——conhost 下多出的那一行会留在屏幕上，表现为“多行
        # 状态栏 / 多行重发”。
        line.no_wrap = True
        line.overflow = "crop"
        self.update(line)


class SendHint(Static):
    """输入框右侧的发送按钮，点击等价于回车。"""

    def __init__(self, owner: "PromptArea") -> None:
        super().__init__(">", id="send-hint", markup=False)
        self.owner = owner
        self._last_click = 0.0

    def on_click(self, event: events.Click) -> None:
        event.stop()
        # 防抖：conhost 下鼠标停在底栏附近时，一次物理点击偶尔会被 Windows
        # 上报成多次 Click，导致同一条消息被重复发送（多行“重发”）。
        now = time.monotonic()
        if now - self._last_click < 0.25:
            return
        self._last_click = now
        self.owner.prompt.focus()
        self.owner.submit()


class PromptTextArea(TextArea):
    """TextArea with Claude-Code-ish key handling.

    注意：prompt 被包在 `#prompt-row` 这个 Horizontal 里，所以不能再用
    `self.parent` 找 PromptArea；进入输入框时把 owner 引用传进来即可。
    """

    owner: "PromptArea | None" = None

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        owner = self.owner
        # 空输入时按 ? 打开帮助；有内容时正常输入问号。
        if key in ("question_mark", "?") and not self.text.strip():
            event.stop()
            event.prevent_default()
            if owner is not None:
                owner.yot.action_help()
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            if owner is not None:
                # 补全面板开着时，回车 = 接受当前高亮的那一项（而不是把内容发出去）。
                # 没有补全面板时才是"发送"。
                if not owner.accept_completion():
                    owner.submit()
            return
        if key in ("ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "tab":
            # Tab 只用于补全；没有补全时也吞掉，避免焦点跳出发送按钮。
            if owner is not None:
                owner.accept_completion()
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def action_cursor_up(self, select: bool = False) -> None:  # type: ignore[override]
        if not select and self.cursor_location[0] == 0 and self.owner is not None:
            if self.owner.navigate_up():
                return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:  # type: ignore[override]
        total = max(1, len(self.text.splitlines()))
        if not select and self.cursor_location[0] >= total - 1 and self.owner is not None:
            if self.owner.navigate_down():
                return
        super().action_cursor_down(select)


class PromptArea(Vertical):
    def __init__(self, app: "YotCodeApp") -> None:
        super().__init__(id="prompt-area")
        self.yot = app
        self.history: list[str] = []
        self.history_pos = 0
        self._suppress = False
        self._completion_token: tuple[int, int, str] | None = None
        self.chevron = Static("> ", id="prompt-chevron", markup=False)
        self.prompt = PromptTextArea(
            soft_wrap=True, show_line_numbers=False, tab_behavior="focus",
            # 不使用 TextArea 自带的 placeholder：部分终端/宽度下 Textual 会
            # 把它重复渲染到多行，且光标会盖住首字母，故留空。
            placeholder="",
            id="prompt",
        )
        self.prompt.owner = self
        # 细光标：用下划线代替占满整格的实心反色块，视觉上更小、更像
        # Claude Code 的竖线光标。CSS 的组件类无法覆盖 theme.cursor_style，
        # 必须注册一个只改 cursor_style 的 TextAreaTheme。
        theme = TextAreaTheme.get_builtin_theme("monokai")
        if theme is not None:
            self.prompt.register_theme(dataclasses.replace(
                theme,
                name="yotcode-cursor",
                cursor_style=Style(color=ACCENT, underline=True),
            ))
            self.prompt.theme = "yotcode-cursor"
        self.completions = OptionList(id="completions")
        self.send = SendHint(self)

    def compose(self) -> ComposeResult:
        yield self.completions
        with Horizontal(id="prompt-row"):
            yield self.chevron
            yield self.prompt
            yield self.send

    def on_mount(self) -> None:
        self.completions.display = False
        self.set_mode(self.yot.config.permission_mode)
        # 首帧布局可能改变输入框位置/高度，等布局稳定后再设置焦点与高度，
        # 避免光标落在旧坐标上（“输入框和输入焦点错位”）。
        self.yot.call_after_refresh(self._focus_and_size)

    def _focus_and_size(self) -> None:
        self._update_height()
        self.prompt.focus()
        self.yot.sync_cursor()

    def set_mode(self, mode: str) -> None:
        # 前缀一律占 2 列（不足补空格）。宽度恒定，切模式时 Horizontal 不会
        # 重新布局输入框边框——conhost 差量渲染下宽度变化会让旧边框行残留。
        prefixes = {
            "default": "> ",
            "acceptEdits": ">>",
            "plan": "# ",
            "bypassPermissions": "! ",
        }
        colors = {
            "default": ACCENT,
            "acceptEdits": GREEN,
            "plan": BLUE,
            "bypassPermissions": YELLOW,
        }
        self.chevron.update(prefixes.get(mode, "> "))
        self.chevron.styles.color = colors.get(mode, ACCENT)

    # -- submission / history ---------------------------------------------
    def submit(self) -> None:
        text = self.prompt.text.strip()
        if not text:
            return
        self.history.append(text)
        self.history_pos = len(self.history)
        self.prompt.clear()
        self._hide_completions()
        self.yot.handle_submit(text)

    def navigate_up(self) -> bool:
        if self.completions.display and self.completions.option_count:
            self.completions.action_cursor_up()
            return True
        if not self.history:
            return False
        if self.history_pos == 0:
            return True
        self.history_pos -= 1
        self._set_text(self.history[self.history_pos])
        return True

    def navigate_down(self) -> bool:
        if self.completions.display and self.completions.option_count:
            self.completions.action_cursor_down()
            return True
        if not self.history:
            return False
        if self.history_pos >= len(self.history) - 1:
            self.history_pos = len(self.history)
            self._set_text("")
            return True
        self.history_pos += 1
        self._set_text(self.history[self.history_pos])
        return True

    def _set_text(self, text: str) -> None:
        self._suppress = True
        self.prompt.load_text(text)
        last_row = max(0, len(text.splitlines()) - 1)
        line = text.splitlines()[-1] if text else ""
        self.prompt.move_cursor((last_row, len(line)))
        self._suppress = False
        self._update_height()

    # -- completions -------------------------------------------------------
    def accept_completion(self) -> bool:
        if not self.completions.display or not self.completions.option_count:
            return False
        option = self.completions.highlighted_option or self.completions.get_option_at_index(0)
        if option is None or option.id is None:
            return False
        value = str(option.id)
        token = self._completion_token
        if token is None:
            return False
        start, end, _ = token
        prefix = self.prompt.text[:start]
        suffix = self.prompt.text[end:]
        insert = value + ("" if value.endswith("/") else " ")
        new_text = prefix + insert + suffix
        self._suppress = True
        self.prompt.load_text(new_text)
        row = new_text[:start + len(insert)].count("\n")
        col = len(new_text[:start + len(insert)].split("\n")[-1])
        self.prompt.move_cursor((row, col))
        self._suppress = False
        self._hide_completions()
        self._update_height()
        return True

    def _hide_completions(self) -> None:
        self.completions.display = False
        self._completion_token = None
        self.yot.call_after_refresh(self.yot.sync_cursor)

    def _current_token(self) -> tuple[int, int, str] | None:
        text = self.prompt.text
        row, col = self.prompt.cursor_location
        lines = text.splitlines(keepends=True)
        if row >= len(lines):
            cursor = len(text)
        else:
            cursor = sum(len(line) for line in lines[:row]) + col
        before = text[:cursor]
        match = None
        for index in range(len(before) - 1, -1, -1):
            if before[index].isspace():
                match = index + 1
                break
        start = 0 if match is None else match
        token = before[start:]
        if not token or token[0] not in "/@":
            return None
        if token.startswith("/") and start != 0:
            return None
        return start, cursor, token

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self.prompt:
            return
        self._update_height()
        if self._suppress:
            return
        token = self._current_token()
        if token is None:
            self._hide_completions()
            return
        start, end, value = token
        if value.startswith("/"):
            items = self.yot.command_completions(value[1:])
        else:
            items = self.yot.file_completions(value[1:])
        if not items:
            self._hide_completions()
            return
        self._completion_token = (start, end, value)
        self.completions.clear_options()
        for display, insert, description in items[:40]:
            prompt = Text()
            prompt.append(display, style=f"bold {TEXT}")
            if description:
                prompt.append("  " + description, style=FAINT)
            self.completions.add_option(Option(prompt, id=insert))
        self.completions.display = True
        if self.completions.option_count:
            self.completions.highlighted = 0
        # 补全列表弹出会移动输入框，布局稳定后同步真实光标
        self.yot.call_after_refresh(self.yot.sync_cursor)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self.completions:
            return
        event.stop()
        self.accept_completion()

    # -- cosmetics ---------------------------------------------------------
    def _update_height(self, width: int | None = None) -> None:
        """按输入内容自适应输入框高度。

        TextArea 无自身边框，内容高度直接等于文本行数（上限 10 行）。
        换行数必须以 **TextArea 自己的内容宽度** 为准：它已经扣除了外层
        边框、chevron、发送按钮和 padding。以前 resize 路径传进来的是
        PromptArea 宽度（再减 8），而输入路径传进来的是 TextArea 宽度
        （又减 8），两条路径算出的可用宽度不一致，导致打字和拖动窗口时
        输入框高度跳变、光标看起来跑到框外。现在统一用 TextArea 宽度。
        """
        text = self.prompt.text
        if not text.strip():
            self.prompt.styles.height = 1
            return
        usable = max(1, self.prompt.content_size.width or self.prompt.size.width or 80)
        lines = 0
        for line in text.splitlines() or [""]:
            lines += max(1, -(-len(line) // usable))  # ceil
        self.prompt.styles.height = max(1, min(10, lines))

    def on_resize(self, event: events.Resize) -> None:
        """宽度变了 → 换行数变了 → 等布局稳定后重算输入框高度。"""
        self.yot.call_after_refresh(self._update_height)
