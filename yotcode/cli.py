"""命令行入口：交互式 TUI、无头 -p 模式、会话恢复。

启动路径：
  yot                       -> 交互式 Textual TUI
  yot "问题"                -> TUI，启动后自动发送这条消息
  yot -p "问题"             -> 无头模式，结果打印到 stdout 后退出
  stdin 不是 TTY            -> 自动按无头模式处理，方便管道 / CI

无头模式支持 text / json / stream-json 三种输出，适合脚本调用；
权限弹窗在非交互环境下无法显示，所以需要提前用 --allowedTools 或
--permission-mode bypassPermissions 放行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .agent import Agent, AgentCallbacks, PermissionRequest
from .commands import expand_custom_command, load_custom_commands, parse_slash
from .config import (
    PERMISSION_MODES,
    Config,
    build_config,
    home_dir,
    migrate_legacy_dirs,
)
from .permissions import PermissionDecision
from .providers import create_provider
from .providers.base import Provider
from .session import Session
from .tools import create_tools
from .types import Block
from .utils import configure_stdio, format_duration, human_tokens

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


# --------------------------------------------------------------------------
# headless callbacks
# --------------------------------------------------------------------------
class PrintCallbacks(AgentCallbacks):
    def __init__(
        self, *, verbose: bool = False, json_mode: str | None = None, model: str = ""
    ) -> None:
        self.verbose = verbose
        self.json_mode = json_mode
        self.model = model
        self.text_parts: list[str] = []
        self.started = time.time()

    def on_text(self, text: str) -> None:
        self.text_parts.append(text)
        if self.json_mode == "stream-json":
            print(
                json.dumps({"type": "text_delta", "text": text}, ensure_ascii=False),
                flush=True,
            )
        elif self.json_mode is None:
            sys.stdout.write(text)
            sys.stdout.flush()

    def on_thinking(self, text: str) -> None:
        if self.json_mode == "stream-json":
            print(
                json.dumps(
                    {"type": "thinking_delta", "text": text}, ensure_ascii=False
                ),
                flush=True,
            )
        elif self.verbose:
            err_console.print(text, end="", style="dim italic")

    def on_tool_start(self, block) -> None:
        if self.json_mode == "stream-json":
            print(
                json.dumps(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif self.verbose:
            err_console.print(
                f"\n● {block.name}({_preview(block.input)})", style="bold"
            )

    def on_tool_result(self, block, result, duration: float) -> None:
        if self.json_mode == "stream-json":
            print(
                json.dumps(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": result.is_error,
                        "content": result.text[:4000],
                        "duration_ms": int(duration * 1000),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif self.verbose:
            icon = "x" if result.is_error else "√"
            err_console.print(
                f"  {icon} {block.name} ({format_duration(duration)})",
                style="red" if result.is_error else "green",
            )

    def on_notice(self, message: str, level: str = "info") -> None:
        if self.json_mode == "stream-json":
            print(
                json.dumps(
                    {"type": "notice", "level": level, "message": message},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            err_console.print(
                f"ⓘ {message}",
                style={
                    "error": "red",
                    "warning": "yellow",
                    "info": "dim",
                }.get(level, "dim"),
            )

    def on_usage(self, usage, cost, total_usage, total_cost) -> None:
        if self.json_mode == "stream-json":
            print(
                json.dumps(
                    {
                        "type": "usage",
                        "usage": usage.to_dict(),
                        "cost_usd": cost,
                        "total_cost_usd": total_cost,
                    }
                ),
                flush=True,
            )
        elif self.verbose:
            err_console.print(
                f"  tokens in={usage.input_tokens} out={usage.output_tokens} "
                f"cost=${cost:.4f}",
                style="dim",
            )

    async def request_permission(
        self, request: PermissionRequest
    ) -> PermissionDecision:
        if not sys.stdin.isatty() or self.json_mode:
            return PermissionDecision(
                False,
                reason="non-interactive mode; pass --permission-mode bypassPermissions "
                "or allow the tool in settings",
            )
        description = [f"[bold]{request.tool.name}[/bold] {request.summary}"]
        if request.warning:
            description.append(f"[bold red]{request.warning}[/bold red]")
        if request.preview:
            path, before, after = request.preview
            import difflib

            diff = list(
                difflib.unified_diff(
                    before.splitlines(),
                    after.splitlines(),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    lineterm="",
                    n=3,
                )
            )
            if len(diff) > 80:
                diff = diff[:60] + ["... diff truncated ..."] + diff[-15:]
            description.append("\n".join(diff))
        err_console.print(
            Panel(
                "\n".join(description),
                title="Permission required",
                border_style="yellow",
            )
        )
        try:
            answer = await asyncio.to_thread(input, "Allow? [y]es / [a]lways / [N]o: ")
        except (EOFError, KeyboardInterrupt):
            return PermissionDecision(False, reason="no answer")
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return PermissionDecision(True)
        if answer in ("a", "always"):
            return PermissionDecision(
                True, remember=True, persist=True, rule=request.suggestion
            )
        return PermissionDecision(False, reason="the user declined")

    async def ask_user(self, question, header, options, descriptions, multi_select):
        if not sys.stdin.isatty() or self.json_mode:
            return None
        err_console.print(Panel(question, title=header or "Question"))
        for index, option in enumerate(options, 1):
            extra = (
                f" — {descriptions[index - 1]}"
                if index - 1 < len(descriptions) and descriptions[index - 1]
                else ""
            )
            err_console.print(f"  {index}. {option}{extra}")
        try:
            answer = await asyncio.to_thread(input, "Your answer: ")
        except (EOFError, KeyboardInterrupt):
            return None
        return answer.strip() or None


def _preview(data: dict[str, Any]) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in list(data.items())[:2])
    return text[:80] + ("..." if len(text) > 80 else "")


# --------------------------------------------------------------------------
# session helpers
# --------------------------------------------------------------------------
def find_session(session_id: str) -> tuple[Path, str] | None:
    base = home_dir() / "projects"
    if not base.is_dir():
        return None
    for directory in base.iterdir():
        if (directory / f"{session_id}.jsonl").is_file():
            return directory, session_id
    return None


def latest_session(cwd: Path) -> Session | None:
    from .utils import slug_path

    directory = home_dir() / "projects" / slug_path(cwd)
    metas = Session.list_sessions(directory)
    if not metas:
        return None
    return Session.load(directory, metas[0].id)


def resume_session(
    cwd: Path, requested: str | None
) -> tuple[Session | None, str | None]:
    if requested:
        found = find_session(requested)
        if found:
            return Session.load(*found), None
        directory = home_dir() / "projects" / requested
        metas = Session.list_sessions(directory)
        if metas:
            return Session.load(directory, metas[0].id), None
        return None, f"Session not found: {requested}"
    session = latest_session(cwd)
    if session is None:
        return None, "No previous sessions found for this directory."
    return session, None


def pick_session_interactive(cwd: Path) -> tuple[Session | None, str | None]:
    """--resume 不带 id 时：列出会话让用户选一个（文本式选择器）。

    类似 `claude -r`：打印带序号的会话列表，读入用户输入的序号确认。
    无 TTY（管道 / CI）时读取 stdin，读不到就返回提示，绝不挂起。
    """
    from .utils import slug_path

    directory = home_dir() / "projects" / slug_path(cwd)
    metas = Session.list_sessions(directory)
    if not metas:
        return None, "No previous sessions found for this directory."

    # 打印列表：序号 / 日期 / 轮数 / 标题，最多 30 条（与 --list-sessions 一致）。
    lines = []
    for index, meta in enumerate(metas[:30], start=1):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated))
        title = meta.title or "(untitled)"
        lines.append(f"  {index:>2}. {when}  {meta.turns:>2} turns  {title}")
    console.print(
        Panel("\n".join(lines), title="选择要恢复的会话 / Select a session", border_style="cyan")
    )
    console.print("输入序号回车确认，直接回车取消：", style="dim")

    try:
        answer = sys.stdin.readline().strip()
    except (OSError, KeyboardInterrupt):
        return None, None
    if not answer:
        return None, None
    if not answer.isdigit():
        return None, f"Invalid selection: {answer}"
    index = int(answer) - 1
    if not (0 <= index < len(metas[:30])):
        return None, f"Selection out of range: {answer}"
    chosen = metas[index]
    return Session.load(directory, chosen.id), None


def print_sessions(cwd: Path) -> None:
    from .utils import slug_path

    directory = home_dir() / "projects" / slug_path(cwd)
    metas = Session.list_sessions(directory)
    if not metas:
        console.print("No sessions for this directory.", style="dim")
        return
    table = Text()
    for meta in metas[:30]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated))
        title = meta.title or "(untitled)"
        table.append(f"{meta.id}  {when}  {meta.turns:>2} turns  ", style="cyan")
        table.append(title + "\n", style="white")
    console.print(Panel(table, title=f"Sessions in {cwd}", border_style="cyan"))


# --------------------------------------------------------------------------
# setup screen
# --------------------------------------------------------------------------
SETUP_MESSAGE = """[bold]No model API key configured.[/bold]

Set one of these environment variables (or put them in a [cyan].env[/cyan] file):

  [green]ANTHROPIC_API_KEY[/green]=sk-ant-...            Anthropic Claude
  [green]OPENAI_API_KEY[/green]=sk-...                   OpenAI or any compatible endpoint
  [green]DEEPSEEK_API_KEY[/green]=sk-...                 DeepSeek (base URL auto-detected)

For an OpenAI-compatible gateway also set [green]OPENAI_BASE_URL[/green]
(for example [cyan]https://api.deepseek.com/v1[/cyan]).

Then run [bold]yot[/bold] again, or try the offline demo:

  [bold cyan]yot --mock[/bold cyan]

Settings live in [cyan]~/.yotcode/settings.json[/cyan] and
[cyan].yotcode/settings.json[/cyan] inside a project."""


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yot",
        description="YotCode — a Python recreation of Claude Code (terminal AI coding agent).",
    )
    parser.add_argument("prompt", nargs="*", help="Prompt to run (optional).")
    parser.add_argument(
        "-p",
        "--print",
        action="store_true",
        dest="print_mode",
        help="Non-interactive: print the result and exit.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Output format for -p mode.",
    )
    parser.add_argument(
        "--model", help="Model name (claude-sonnet-4-5, deepseek-chat, ...)."
    )
    parser.add_argument(
        "--provider", choices=["anthropic", "openai"], help="Force a provider."
    )
    parser.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODES,
        help="default | acceptEdits | plan | bypassPermissions",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="Same as --permission-mode bypassPermissions.",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recent session in this directory.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        help="Resume a session by id (open a picker if id omitted).",
    )
    parser.add_argument("--session", help="Load a specific session id.")
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List sessions for this directory and exit.",
    )
    parser.add_argument(
        "--allowedTools",
        "--allowed-tools",
        dest="allowed_tools",
        action="append",
        default=[],
        help="Comma separated tool allowlist.",
    )
    parser.add_argument(
        "--disallowedTools",
        "--disallowed-tools",
        dest="disallowed_tools",
        action="append",
        default=[],
        help="Comma separated tool denylist.",
    )
    parser.add_argument("--max-turns", type=int, help="Maximum agent turns per prompt.")
    parser.add_argument("--system-prompt", help="Replace the system prompt.")
    parser.add_argument("--append-system-prompt", help="Append to the system prompt.")
    parser.add_argument("--api-key", help="API key (overrides environment).")
    parser.add_argument("--base-url", help="API base URL.")
    # 无头模式附加文件：图片走 ImageBlock，其他文件读成文本块。可重复传多个。
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Attach an image file (repeatable). Sent to the model as an image block.",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="Attach a text file (repeatable). Read and inlined into the prompt.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the offline demo provider (no API key needed).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show tool calls and thinking."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Show tracebacks for tool errors."
    )
    parser.add_argument(
        "--setup-path",
        action="store_true",
        help="Add the directory containing yot/yotcode to your user PATH and exit.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Show first-run setup help: config templates and PATH instructions.",
    )
    parser.add_argument(
        "--version", action="version", version=f"yot {__version__}"
    )
    return parser


def _flatten(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


# --------------------------------------------------------------------------
# headless runner
# --------------------------------------------------------------------------
async def run_headless(
    config: Config,
    prompt: str | list[Block],
    session: Session | None,
    args: argparse.Namespace,
) -> int:
    provider: Provider = create_provider(config)
    callbacks = PrintCallbacks(
        verbose=args.verbose,
        json_mode=args.output_format if args.output_format != "text" else None,
        model=config.model,
    )
    agent = Agent(
        config, provider, create_tools(), callbacks=callbacks, session=session
    )
    if config.mcp_servers:
        from .tools.mcp import MCPManager

        manager = MCPManager(config.mcp_servers, config.cwd)
        callbacks.on_notice("Connecting MCP servers...", "info")
        agent.add_tools(await manager.connect_all(callbacks.on_notice))
    started = time.time()
    try:
        result = await agent.run(prompt, max_turns=args.max_turns)
    except KeyboardInterrupt:
        result = "".join(callbacks.text_parts)
    finally:
        await agent.close()
    duration = time.time() - started

    if args.output_format == "json":
        payload = {
            "type": "result",
            "subtype": "success",
            "result": result,
            "session_id": agent.session.meta.id,
            "duration_ms": int(duration * 1000),
            "num_turns": agent.turn_index,
            "usage": agent.total_usage.to_dict(),
            "total_cost_usd": round(agent.total_cost, 6),
            "model": config.model,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.output_format == "stream-json":
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": result,
                    "session_id": agent.session.meta.id,
                    "duration_ms": int(duration * 1000),
                    "num_turns": agent.turn_index,
                    "usage": agent.total_usage.to_dict(),
                    "total_cost_usd": round(agent.total_cost, 6),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    else:
        sys.stdout.write("\n" if result and not result.endswith("\n") else "")
        if args.verbose:
            err_console.print(
                f"\n[session {agent.session.meta.id}] "
                f"{human_tokens(agent.total_usage.prompt_tokens)} in / "
                f"{human_tokens(agent.total_usage.output_tokens)} out · "
                f"${agent.total_cost:.4f} · {format_duration(duration)}",
                style="dim",
            )
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """程序主入口：双击 run.cmd 最终就会执行到这里。

    大致流程：解析命令行参数 → 加载配置 → 处理会话恢复 → 分流到两种模式。
    返回的整数会成为程序退出码：0 正常，1 出错，130 用户按 Ctrl+C 打断。
    """
    configure_stdio()  # 修正 Windows 终端的编码/颜色设置
    parser = build_parser()  # 定义所有命令行参数（-p、--model 等）
    args = parser.parse_args(argv)  # 解析用户实际输入的参数
    cwd = Path.cwd()  # 当前工作目录（也就是项目根目录）
    prompt_text = " ".join(args.prompt).strip()  # 位置参数拼成一句话

    # --setup-path：把命令所在目录加入用户 PATH，然后退出。
    # pip 安装后若直接敲 yot 提示"找不到命令"，运行一次这个即可。
    if args.setup_path:
        from .setup import setup_path
        changed, message, directory = setup_path()
        err_console.print(f"命令目录：{directory}", style="bold")
        err_console.print(message, style="green" if changed else "yellow")
        if changed:
            err_console.print(
                "提示：已经打开的终端需要重新打开才会看到新 PATH。", style="dim"
            )
        return 0

    # --setup：打印首次配置指引（模板位置 + PATH 用法），然后退出。
    if args.setup:
        from .config import home_dir
        from .setup import ENV_TEMPLATE, SETTINGS_TEMPLATE, ensure_user_templates
        created = ensure_user_templates()
        home = home_dir()
        err_console.print("YotCode 首次配置指引", style="bold")
        err_console.print(f"全局配置目录：{home}", style="cyan")
        for path in created:
            err_console.print(f"  已生成模板：{path}", style="green")
        if not created:
            err_console.print("  配置文件已存在，未做任何改动。", style="dim")
        err_console.print(
            f"\n环境变量：编辑 {home / '.env'}（取消注释需要的行）", style="dim"
        )
        err_console.print(
            f"全局设置：编辑 {home / 'settings.json'}（支持 // 注释）", style="dim"
        )
        err_console.print(
            "\n若终端找不到 yot 命令，先运行：yot --setup-path", style="dim"
        )
        err_console.print("\n模板内容预览（.env）：", style="bold")
        err_console.print(ENV_TEMPLATE, style="dim")
        err_console.print("模板内容预览（settings.json）：", style="bold")
        err_console.print(SETTINGS_TEMPLATE, style="dim")
        return 0

    # 老版本（claudepy）的配置目录自动搬到新名字（yotcode）。
    # 必须放在读配置之前，否则老的 settings.json 根本找不到。
    for note in migrate_legacy_dirs(cwd):
        err_console.print(f"已迁移配置目录：{note}", style="dim")

    # --allowedTools "Read,Grep" 这种逗号分隔的写法，统一拆成列表
    merged_allowed = _flatten(args.allowed_tools)
    merged_disallowed = _flatten(args.disallowed_tools)
    permission_mode = args.permission_mode
    if args.dangerously_skip_permissions:  # --dangerously-skip-permissions 的简写效果
        permission_mode = "bypassPermissions"  # 跳过所有权限确认（慎用）

    # 把所有参数交给 config.py 合并成一份最终配置（设置文件 + 环境变量 + 命令行）
    config = build_config(
        cwd,
        model=args.model,
        provider=args.provider,
        permission_mode=permission_mode,
        allowed_tools=merged_allowed,
        disallowed_tools=merged_disallowed,
        max_turns=args.max_turns,
        mock=args.mock,
        verbose=args.verbose,
        debug=args.debug,
        api_key=args.api_key,
        base_url=args.base_url,
        system_prompt_append=args.append_system_prompt,
    )

    if args.system_prompt:
        config.extra["systemPrompt"] = args.system_prompt

    if args.list_sessions:
        print_sessions(cwd)
        return 0

    if not config.provider_ready():
        err_console.print(
            Panel(SETUP_MESSAGE, border_style="yellow", title="YotCode setup")
        )
        return 1

    # resume?
    session: Session | None = None
    resume_error: str | None = None
    if args.session:
        session, resume_error = resume_session(cwd, args.session)
    elif args.resume is not None:
        if args.resume:
            # 带 id：按 id 精确恢复（可跨项目）。
            session, resume_error = resume_session(cwd, args.resume)
        else:
            # 不带 id：弹出文本式会话选择器，而不是直接取最近一条。
            session, resume_error = pick_session_interactive(cwd)
    elif args.continue_session:
        session, resume_error = resume_session(cwd, None)
    if resume_error:
        err_console.print(f"ⓘ {resume_error}", style="yellow")

    # 判断进入哪种模式：
    #   -p 参数、或 stdin/stdout 不是终端（被管道/CI 调用）→ 无头模式
    #   否则 → 交互式全屏 TUI
    # isatty() = "is a terminal"，意思是"这个输入输出是真正的键盘/屏幕吗"
    headless = args.print_mode or not sys.stdin.isatty() or not sys.stdout.isatty()

    # 无头模式下如果没给提示词，就从标准输入读（支持 `echo "问题" | yot -p`）
    if headless and not prompt_text and args.print_mode:
        try:
            prompt_text = sys.stdin.read().strip()
        except OSError:
            prompt_text = ""

    if headless and prompt_text.startswith("/"):
        parsed = parse_slash(prompt_text)
        if parsed:
            name, cmd_args = parsed
            custom = load_custom_commands(cwd)
            if name in custom:
                prompt_text = asyncio.run(
                    expand_custom_command(custom[name], cmd_args, config)
                )

    # 把 --image / --file 指定的附件转成内容块，与提示词文字合并。
    # 没有附件时 prompt 仍是纯 str，保持旧行为完全不变。
    from .attachments import merge_prompt_blocks, paths_to_blocks

    attachment_paths: list[str] = list(args.image) + list(args.file)
    prompt: str | list[Block] = prompt_text
    if attachment_paths:
        # --image 和 --file 都走统一转换：按扩展名自动分辨图片 / 文本。
        attached = paths_to_blocks(attachment_paths, cwd)
        prompt = merge_prompt_blocks(prompt_text, attached)

    if headless:
        # 无头模式：没有提示词、也没有附件就没法工作，报错退出。
        if not prompt_text and not attachment_paths:
            err_console.print(
                "Not a terminal (or -p was given). Provide a prompt or --image/--file: "
                '`yot -p "your prompt"`.',
                style="red",
            )
            return 1
        try:
            # asyncio.run 启动异步世界，运行 run_headless（最终调用 Agent.run）
            return asyncio.run(run_headless(config, prompt, session, args))
        except KeyboardInterrupt:
            return 130  # 130 是 Unix 约定：被 Ctrl+C 中断

    # 交互模式：启动 Textual 全屏应用，它内部会创建 Agent 并异步调用 run()
    # （如果命令行带了提示词，TUI 启动后会自动发出这条消息）
    try:
        from .console_font import prepare_console
        from .ui.app import YotCodeApp

        # 经典 cmd 窗口：按「最大化 → 调字号 → 关快速编辑 → 等尺寸稳定 →
        # 同步缓冲区并复位可视区」的顺序准备，让界面铺满窗口、输入框贴底、
        # 不出现灰色选区块和底部黑带。
        # 字号来自 config.font_size（.env 的 YOTCODE_FONT_SIZE 或
        # settings.json 的 ui.fontSize），没配置时用程序默认值。
        prepare_console(config.font_size)
        # prompt 可能是带附件的内容块列表（--image/--file），TUI 已支持。
        app = YotCodeApp(config, initial_prompt=prompt or None, session=session)
        app.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
