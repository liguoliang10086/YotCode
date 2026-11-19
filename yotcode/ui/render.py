"""Rich 渲染组件：Markdown、diff、工具结果、待办列表等。

把“数据 -> Rich renderable”的逻辑集中在这里，widgets.py 只负责布局和交互。

视觉语言来自 Claude Code 终端界面：
  - 暖炭背景（#1e1d1b）+ 陶土橙强调色（#d97757）+ 浅橙渐变标题；
  - 只有 diff 使用真正的卡片，其它内容靠行首符号、缩进和色阶分层；
  - 工具调用是一行 `└ Name arg  ✓ 0.4s`，输出用 `⎿` 缩进；
  - 字号无法在终端里改，但可以通过减少粗体、收紧留白让视觉更轻。
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..config import Config
from ..utils import human_tokens

# ── 设计令牌（暖炭 Terminal Noir）────────────────────────────────────
BG = "#1e1d1b"            # 页面背景：暖炭（参考界面）
SURFACE = "#262523"       # 输入框 / 卡片表面
SURFACE_2 = "#2a2927"     # 胶囊、浮层
SURFACE_3 = "#33312e"     # 悬停
LINE = "#4d4b48"          # 常规分隔线 / 卡片边框（参考 BORDER）
LINE_SOFT = "#3a3835"     # 极淡分隔线 / 时间线（参考 SEP）
TEXT = "#e8e6e3"          # 主文字（参考 FG）
TEXT_STRONG = "#f5f2ed"   # 标题
MUTED = "#a8a59f"         # 次要文字
DIM = "#8a8a85"           # 弱化文字（参考 DIM）
FAINT = "#5f5e5b"         # 提示 / 时间（参考 FAINT）
ACCENT = "#d97757"        # Claude 陶土橙
ACCENT_SOFT = "#f5b98f"   # 浅橙（渐变起点）
ACCENT_DIM = "#8f4f38"    # 橙色边框
GREEN = "#8db78d"         # 成功（参考工具结果绿）
GREEN_TEXT = "#b7dbb7"    # diff 新增文字
GREEN_BG = "#1c241c"      # diff 新增行背景
RED = "#ff7b72"           # 错误
RED_TEXT = "#ffb0ab"      # diff 删除文字
RED_BG = "#261a19"        # diff 删除行背景
BLUE = "#7fb0e6"          # 读取类工具 / 链接
PURPLE = "#c297ff"        # 编辑类工具 / 变量名
YELLOW = "#e3b341"        # 进行中 / 警告
CYAN = "#56d4dd"          # 网页 / 提示

# 渐变标题：浅橙 → Claude 橙（逐字符插值）
GRAD_START = (0xF5, 0xB9, 0x8F)
GRAD_END = (0xD9, 0x77, 0x57)
# 转圈图标：四象限圆循环旋转，像一个转动的圆环。
# 实测 conhost 默认字体支持 ◐◓◑◒，而 ► 在部分字体里会变成方框，
# 所以转圈只用这四个字符，保证 cmd.exe 下不乱码。
SPINNER = ("◐", "◓", "◑", "◒")

# 工具名按类别取色：只改变名字本身，避免整行花花绿绿。
TOOL_COLORS = {
    "Read": BLUE,
    "Grep": BLUE,
    "Glob": BLUE,
    "LS": BLUE,
    "Edit": PURPLE,
    "Write": PURPLE,
    "MultiEdit": PURPLE,
    "Bash": GREEN,
    "BashOutput": GREEN,
    "KillShell": GREEN,
    "TodoWrite": YELLOW,
    "Task": PURPLE,
    "AskUserQuestion": PURPLE,
    "ExitPlanMode": PURPLE,
    "WebFetch": CYAN,
    "WebSearch": CYAN,
}


def tool_name_style(name: str) -> str:
    """返回工具名的 Rich 颜色。"""
    return TOOL_COLORS.get(name, TEXT)


def gradient_text(text: str, *, bold: bool = True) -> Text:
    """水平渐变文字：逐字符在浅橙与 Claude 橙之间插值。"""
    out = Text()
    n = max(len(text) - 1, 1)
    for index, char in enumerate(text):
        k = index / n
        r = int(GRAD_START[0] + (GRAD_END[0] - GRAD_START[0]) * k)
        g = int(GRAD_START[1] + (GRAD_END[1] - GRAD_START[1]) * k)
        b = int(GRAD_START[2] + (GRAD_END[2] - GRAD_START[2]) * k)
        style = f"#{r:02x}{g:02x}{b:02x}"
        if bold:
            style = f"bold {style}"
        out.append(char, style=style)
    return out


def version_pill(version: str) -> Text:
    """反色版本胶囊：深色字落在橙底上。"""
    pill = Text()
    pill.append(f" v{version} ", style=f"bold {BG} on {ACCENT}")
    return pill


# ── ASCII Logo（ANSI Shadow 风格，每字母宽 9，6 行等高）───────────────
_LETTERS = {
    "C": (" ██████╗ ", "██╔════╝ ", "██║      ", "██║      ",
          "╚██████╗ ", " ╚═════╝ "),
    "L": ("██╗      ", "██║      ", "██║      ", "██║      ",
          "███████╗ ", "╚══════╝ "),
    "A": (" █████╗  ", "██╔══██╗ ", "███████║ ", "██╔══██║ ",
          "██║  ██║ ", "╚═╝  ╚═╝ "),
    "U": ("██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║",
          "╚██████╔╝", " ╚═════╝ "),
    "D": ("██████╗  ", "██╔══██╗ ", "██║  ██║ ", "██║  ██║ ",
          "██████╔╝ ", "╚═════╝  "),
    "E": ("███████╗ ", "██╔════╝ ", "█████╗   ", "██╔══╝   ",
          "███████╗ ", "╚══════╝ "),
    "P": ("██████╗  ", "██╔══██╗ ", "██████╔╝ ", "██╔═══╝  ",
          "██║      ", "╚═╝      "),
    "Y": ("██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ",
          "   ██║   ", "   ╚═╝   "),
    # ── YOTCODE 用到的字母 ───────────────────────────────────────────
    "V": ("██╗   ██╗", "██║   ██║", "██║   ██║", "╚██╗ ██╔╝",
          " ╚████╔╝ ", "  ╚═══╝  "),
    "I": ("   ██╗   ", "   ██║   ", "   ██║   ", "   ██║   ",
          "   ██║   ", "   ╚═╝   "),
    "S": ("███████╗ ", "██╔════╝ ", "███████╗ ", "╚════██║",
          "███████║", "╚══════╝"),
    "O": (" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║",
          "╚██████╔╝", " ╚═════╝ "),
    "N": ("███╗   ██╗", "████╗  ██║", "██╔██╗ ██║", "██║╚██╗██║",
          "██║ ╚████║", "╚═╝  ╚═══╝"),
    "K": ("██╔╗  ██╗", "██║╚██╗██║", "██║ ╚████║", "██║  ██╔╝",
          "██║  ██║ ", "╚═╝  ╚═╝ "),
    "T": ("█████████", "╚════██╝ ", " ══════╝ ", "  ═════╝ ",
          "   ═══╝ ", "   ═══╝ "),
}


def logo_renderable(word: str = "YOTCODE") -> Text:
    """渐变 ASCII Logo：同一列的字符取同一渐变色，横向扫过整块 Logo。"""
    letters = tuple(_LETTERS[ch] for ch in word.upper())
    rows = ["".join(letter[i] for letter in letters) for i in range(6)]
    out = Text()
    for row in rows:
        out.append_text(gradient_text(row.rstrip(), bold=False))
        out.append("\n")
    return out


def _chip(text: str, color: str, bg: str = SURFACE_2) -> Text:
    """用带背景的空格拼出一个胶囊标签，避免为一行文字画边框。"""
    chip = Text()
    chip.append(" ", style=f"on {bg}")
    chip.append(text, style=f"{color} on {bg}")
    chip.append(" ", style=f"on {bg}")
    return chip


def welcome_renderable(config: Config, resumed: bool = False,
                       session_title: str = "") -> Any:
    """欢迎屏：新会话先展示渐变 ASCII Logo，再接圆角面板。

    风格参考 Claude Code：Logo 逐列渐变，版本是反色胶囊，
    快捷键用两列表格对齐，整块内容用低调的圆角边框收拢。
    恢复会话时不显示 Logo，保持紧凑。
    """
    from .. import __version__

    spec = config.spec
    model_label = config.model
    if spec:
        model_label += f"  {spec.context_label}"

    # ── 标题：恢复会话用品牌；新会话已有大 Logo，改用轻量小标题 ──
    title = gradient_text("yotcode" if resumed else "quick start")
    title.append("  ")
    title.append_text(version_pill(__version__))

    # ── 上下文信息：model / provider / mode / cwd ──────────────────
    info = Text()
    info.append("model    ", style=FAINT)
    info.append(model_label, style=TEXT)
    info.append("\n")
    info.append("provider ", style=FAINT)
    info.append(config.provider, style=BLUE)
    info.append("    ", style="")
    info.append("mode ", style=FAINT)
    info.append(config.permission_mode, style=GREEN)
    if config.mock:
        info.append("    ", style="")
        info.append("offline demo", style=YELLOW)

    cwd = Text()
    cwd.append("cwd ", style=FAINT)
    try:
        cwd_raw = str(config.cwd).replace("\\", "/")
        home = str(Path.home()).replace("\\", "/")
        if cwd_raw.lower().startswith(home.lower()):
            cwd_raw = "~" + cwd_raw[len(home):]
    except Exception:
        cwd_raw = str(config.cwd)
    cwd.append(cwd_raw, style=DIM)

    body: list[Any] = [info, cwd]
    if resumed and session_title:
        resume = Text()
        resume.append("resumed  ", style=YELLOW)
        resume.append(session_title, style=DIM)
        body.insert(1, resume)

    panel = Panel(
        Group(*body),
        title=title,
        title_align="left",
        box=box.ROUNDED,
        border_style=LINE_SOFT,
        padding=(0, 2),
    )
    if resumed:
        return panel
    # 新会话：渐变大 Logo 打头，隔一行再接面板，类似 Claude Code 启动画面
    return Group(logo_renderable(), Text(""), panel)


def _limit_lines(text: str, limit: int) -> tuple[list[str], int]:
    lines = text.splitlines()
    if len(lines) <= limit:
        return lines, 0
    return lines[:limit], len(lines) - limit


def _truncate_line(line: str, width: int = 400) -> str:
    if len(line) <= width:
        return line
    return line[:width] + " …"


def _join_lines(lines: list[Text]) -> Text:
    """用换行连接多行 Text，避免末尾多出一个空行。"""
    if not lines:
        return Text()
    return Text("\n").join(lines)


def _diff_counts(before: str, after: str) -> tuple[int, int]:
    diff = difflib.unified_diff(
        before.replace("\r\n", "\n").splitlines(),
        after.replace("\r\n", "\n").splitlines(),
        lineterm="", n=0,
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return added, removed


def diff_renderable(path: str, before: str, after: str, *, max_lines: int = 160,
                    show_path: bool = True, show_stats: bool = True) -> Any:
    """渲染一张带边框的 diff 卡片。

    卡片结构参考参考界面：顶部是文件名和 `+n −m`，下面每行由
    1 列符号 + 1 列代码组成；新增/删除行用很淡的绿色/红色背景铺满整行。
    用 Rich Table(expand=True) 而不是 Text，才能让背景色覆盖整行宽度。
    """
    before_lines = before.replace("\r\n", "\n").splitlines()
    after_lines = after.replace("\r\n", "\n").splitlines()
    diff = list(difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"a/{path}" if show_path else "",
        tofile=f"b/{path}" if show_path else "",
        lineterm="", n=3,
    ))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    # 顶部：左边文件名（或 "diff"），右边统计。
    header = Table.grid(expand=True, padding=0)
    header.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    header.add_column(justify="right", no_wrap=True)
    if show_path and path:
        left = Text(str(path), style=f"bold {TEXT}")
    else:
        left = Text("diff", style=FAINT)
    stats = Text()
    if show_stats:
        stats.append(f"+{added}", style=GREEN_TEXT)
        stats.append("  ", style=FAINT)
        stats.append(f"−{removed}", style=RED_TEXT)
    header.add_row(left, stats)

    if not diff:
        body: list[Any] = [header, Rule(style=LINE_SOFT),
                           Text("(no changes)", style=DIM)]
        return Panel(
            Group(*body), box=box.ROUNDED, border_style=LINE,
            padding=(0, 1), style="",
        )

    rows = Table.grid(expand=True, padding=0, collapse_padding=True)
    rows.add_column(width=1, no_wrap=True)
    rows.add_column(ratio=1, no_wrap=True, overflow="ellipsis")

    shown = 0
    hidden = 0
    for line in diff:
        if line.startswith(("+++", "---")):
            # 文件头已经在卡片顶部展示，不再重复。
            continue
        if shown >= max_lines:
            hidden += 1
            continue
        line = _truncate_line(line, 600)
        if line.startswith("@@"):
            sign = Text(" ", style=FAINT)
            code = Text(line, style=PURPLE)
            rows.add_row(sign, code)
        elif line.startswith("+"):
            sign = Text("+", style=GREEN_TEXT)
            code = Text(line[1:], style=GREEN_TEXT)
            rows.add_row(sign, code, style=f"on {GREEN_BG}")
        elif line.startswith("-"):
            sign = Text("-", style=RED_TEXT)
            code = Text(line[1:], style=RED_TEXT)
            rows.add_row(sign, code, style=f"on {RED_BG}")
        else:
            sign = Text(" ", style=FAINT)
            code = Text(line[1:] if line.startswith(" ") else line, style=MUTED)
            rows.add_row(sign, code)
        shown += 1

    body = [header, Rule(style=LINE_SOFT), rows]
    if hidden:
        body.append(Text(f"… {hidden} more diff lines", style=FAINT))
    return Panel(
        Group(*body), box=box.ROUNDED, border_style=LINE,
        padding=(0, 1), style="",
    )


def _ansi_text(raw: str) -> Text:
    try:
        return Text.from_ansi(raw)
    except Exception:
        return Text(raw)


def grep_text(output: str) -> Text:
    lines: list[Text] = []
    for line in output.splitlines():
        if line.startswith("--"):
            lines.append(Text(line, style=FAINT))
            continue
        match = line.split(":", 2)
        if len(match) == 3 and match[1].isdigit():
            parsed = Text()
            parsed.append(match[0], style=BLUE)
            parsed.append(":", style=FAINT)
            parsed.append(match[1], style=YELLOW)
            parsed.append(":", style=FAINT)
            parsed.append(match[2], style=TEXT)
            lines.append(parsed)
        else:
            lines.append(Text(line, style=MUTED))
    return _join_lines(lines)


def read_text(output: str) -> Text:
    """把 Read 工具的 `行号\\t内容` 输出渲染成带彩色行号的代码块。"""
    lines: list[Text] = []
    for index, line in enumerate(output.splitlines()):
        number = code = None
        for separator in ("\t", "→"):
            if separator in line:
                left, right = line.split(separator, 1)
                if left.strip().isdigit():
                    number, code = left, right
                    break
        if number is not None:
            parsed = Text()
            parsed.append(number.rjust(5), style=FAINT)
            parsed.append("  ", style=FAINT)
            parsed.append(_truncate_line(code, 600), style=TEXT)
            lines.append(parsed)
            continue
        if index == 0 and " lines)" in line:
            # Read 的第一行是 "path (N lines)"，用蓝色区分于正文。
            lines.append(Text(line, style=BLUE))
        elif line.startswith("... ("):
            lines.append(Text(line, style=FAINT))
        else:
            lines.append(Text(_truncate_line(line, 600), style=MUTED))
    return _join_lines(lines)


def paths_text(output: str) -> Text:
    lines: list[Text] = []
    for line in output.splitlines():
        if line.startswith("...") or line.startswith("No "):
            lines.append(Text(line, style=FAINT))
        elif line.endswith("/"):
            lines.append(Text(line, style=BLUE))
        else:
            lines.append(Text(line, style=TEXT))
    return _join_lines(lines)


def todos_renderable(todos: list[dict[str, Any]], *,
                     show_header: bool = True) -> Text:
    """任务清单：√ 完成 / ~ 进行中 / ○ 待办（全部用 CP437 安全字形）。"""
    total = len(todos)
    done = sum(1 for t in todos if t.get("status") == "completed")
    lines: list[Text] = []
    if show_header:
        head = Text()
        head.append("Tasks", style=ACCENT)
        head.append("  ", style=FAINT)
        filled = 10 if not total else round(done / total * 10)
        for index in range(10):
            head.append("█" if index < filled else "░",
                        style=GREEN if index < filled else LINE)
        head.append(f"  {done}/{total}", style=FAINT)
        lines.append(head)
    for todo in todos:
        status = todo.get("status")
        content = str(todo.get("content") or "")
        line = Text()
        if status == "completed":
            line.append("  √ ", style=GREEN)
            line.append(content, style=f"strike {FAINT}")
        elif status == "in_progress":
            line.append("  ~ ", style=ACCENT)
            line.append(content, style=TEXT)
        else:
            line.append("  ○ ", style=FAINT)
            line.append(content, style=MUTED)
        lines.append(line)
    return _join_lines(lines)


def with_gutter(body: Any, marker: str = "└") -> Table:
    """给工具输出加 Claude Code 式的 `└` 引导线。

    用 Rich 的两列表格实现：左列只有首行有 `└`，右列承载真正的输出。
    这样多行输出会自动在右侧缩进，而不会每行都重复符号。
    """
    if isinstance(body, str):
        body = Text(body)
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(width=2, no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row(Text(marker, style=FAINT), body)
    return grid


def tool_body(tool_name: str, data: dict[str, Any], output: str,
              diff: tuple[str, str, str] | None = None, *,
              expanded: bool = False, error: bool = False,
              progress: str = "", meta: dict[str, Any] | None = None,
              duration: float = 0.0) -> Any:
    limit = 400 if expanded else 10
    meta = meta or {}

    if tool_name in ("Edit", "MultiEdit", "Write") and diff:
        path, before, after = diff
        return diff_renderable(
            path, before, after,
            max_lines=400 if expanded else 40,
            show_path=False,
        )

    if tool_name == "Bash":
        # 命令本身已经显示在工具标题 `Bash(...)` 里，正文直接从输出开始。
        if not output or output.strip() in ("", "(no output)"):
            exit_code = meta.get("exit_code")
            label = "done" if not error else "failed"
            if exit_code is not None:
                label = f"exit {exit_code}"
            return Text(f"√ {label}" if not error else f"x {label}",
                        style=GREEN if not error else RED)
        lines, hidden = _limit_lines(output, limit)
        text = _ansi_text("\n".join(lines))
        if hidden:
            text.append(f"\n… {hidden} lines hidden (ctrl+o to expand)", style=FAINT)
        return text

    if tool_name == "TodoWrite":
        todos = data.get("todos") or []
        return todos_renderable(todos, show_header=False)

    if tool_name == "Read":
        lines, hidden = _limit_lines(output, limit)
        text = read_text("\n".join(lines))
        if hidden:
            text.append(f"\n… {hidden} lines hidden (ctrl+o to expand)", style=FAINT)
        return text

    if tool_name == "Grep":
        lines, hidden = _limit_lines(output, limit)
        text = grep_text("\n".join(lines))
        if hidden:
            text.append(f"\n… {hidden} lines hidden", style=FAINT)
        return text

    if tool_name in ("Glob", "LS"):
        lines, hidden = _limit_lines(output, limit)
        text = paths_text("\n".join(lines))
        if hidden:
            text.append(f"\n… {hidden} lines hidden", style=FAINT)
        return text

    if tool_name == "Task":
        parts: list[Text] = []
        if progress:
            for line in progress.splitlines()[-limit:]:
                parts.append(Text(line[:500], style=FAINT))
        if output:
            lines, hidden = _limit_lines(output, limit)
            if progress:
                parts.append(Text(""))
            parts.append(Text(
                "\n".join(_truncate_line(line, 300) for line in lines),
                style=TEXT,
            ))
            if hidden:
                parts.append(Text(f"… {hidden} lines hidden", style=FAINT))
        return _join_lines(parts) if parts else Text("(no output)", style=FAINT)

    if tool_name in ("WebFetch", "WebSearch", "Write"):
        lines, hidden = _limit_lines(output, limit)
        text = Text(
            "\n".join(_truncate_line(line, 500) for line in lines),
            style=RED if error else TEXT,
        )
        if hidden:
            text.append(f"\n… {hidden} lines hidden (ctrl+o to expand)", style=FAINT)
        return text

    lines, hidden = _limit_lines(output or "(no output)", limit)
    text = Text(
        "\n".join(_truncate_line(line, 400) for line in lines),
        style=RED if error else TEXT,
    )
    if hidden:
        text.append(f"\n… {hidden} lines hidden", style=FAINT)
    return text


def result_preview(result_text: str, max_chars: int = 400) -> str:
    text = result_text.strip().replace("\n", " ")
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def info_renderable(title: str, body_markdown: str) -> Group:
    head = Text()
    head.append("• ", style=ACCENT)
    head.append(title, style=ACCENT_SOFT)
    return Group(head, Text(""), RichMarkdown(body_markdown))


def usage_text(usage, cost: float) -> Text:
    text = Text()
    text.append(human_tokens(usage.prompt_tokens), style=TEXT)
    text.append(" in / ", style=FAINT)
    text.append(human_tokens(usage.output_tokens), style=TEXT)
    text.append(" out", style=FAINT)
    if cost > 0:
        text.append("  ·  ", style=FAINT)
        text.append(f"${cost:.4f}", style=GREEN)
    return text
