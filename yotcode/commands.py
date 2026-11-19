"""斜杠命令：内置命令元数据 + 自定义 Markdown 命令。

自定义命令放在 .yotcode/commands/*.md（或 .claude/commands），
文件开头可写 YAML frontmatter（description、allowed-tools 等）；
正文支持 $ARGUMENTS、!`shell 命令` 和 @文件 三种插值。
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, home_dir, project_config_dir
from .utils import parse_frontmatter, read_text

BUILTIN_COMMANDS: list[dict[str, str]] = [
    {"name": "help", "args": "", "description": "Show commands and keyboard shortcuts"},
    {"name": "clear", "args": "", "description": "Start a fresh conversation"},
    {"name": "compact", "args": "[instructions]", "description": "Summarise the conversation to free context"},
    {"name": "cost", "args": "", "description": "Show token usage and estimated cost"},
    {"name": "status", "args": "", "description": "Show model, cwd, permissions and session info"},
    {"name": "doctor", "args": "", "description": "Check the environment and configuration"},
    {"name": "model", "args": "[name]", "description": "Switch the model"},
    {"name": "config", "args": "", "description": "Show settings files and effective values"},
    {"name": "permissions", "args": "", "description": "Show permission rules and mode"},
    {"name": "memory", "args": "", "description": "List loaded CLAUDE.md memory files"},
    {"name": "init", "args": "", "description": "Analyse the repo and create/update CLAUDE.md"},
    {"name": "todos", "args": "", "description": "Show the current todo list"},
    {"name": "agents", "args": "", "description": "List available sub-agents"},
    {"name": "skills", "args": "", "description": "List available skills"},
    {"name": "checkpoints", "args": "", "description": "List file checkpoints"},
    {"name": "revert", "args": "[id]", "description": "Restore files from a checkpoint"},
    {"name": "mcp", "args": "", "description": "Show MCP server status"},
    {"name": "resume", "args": "[id]", "description": "Resume a previous session"},
    {"name": "export", "args": "[path]", "description": "Export the transcript as markdown"},
    {"name": "add-dir", "args": "<path>", "description": "Allow edits in another directory"},
    {"name": "exit", "args": "", "description": "Exit YotCode"},
]

KEYBINDINGS = [
    ("Enter", "Send message"),
    ("Shift+Enter / Ctrl+J", "New line"),
    ("Esc", "Interrupt the current response"),
    ("Ctrl+C", "Interrupt, or clear input / exit when idle"),
    ("Ctrl+D", "Exit"),
    ("Ctrl+L", "Clear the screen"),
    ("Ctrl+O", "Expand / collapse tool output"),
    ("Shift+Tab", "Cycle permission mode"),
    ("Ctrl+P", "Command palette"),
    ("Up / Down", "Prompt history, or navigate completions"),
    ("Tab", "Accept a completion"),
    ("Ctrl+R", "Choose a session to resume"),
]


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    argument_hint: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    source: Path | None = None
    namespace: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.namespace}:{self.name}" if self.namespace else self.name


def _command_dirs(cwd: Path) -> list[tuple[Path, str]]:
    home = Path.home()
    return [
        (project_config_dir(cwd) / "commands", ""),
        (Path(cwd) / ".claude" / "commands", ""),
        (home_dir() / "commands", "user"),
        (home / ".claude" / "commands", "user"),
    ]


def load_custom_commands(cwd: Path) -> dict[str, CustomCommand]:
    commands: dict[str, CustomCommand] = {}
    for directory, namespace in _command_dirs(cwd):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if not path.is_file():
                continue
            try:
                text, _, _ = read_text(path)
            except OSError:
                continue
            meta, body = parse_frontmatter(text)
            name = str(meta.get("name") or path.stem)
            if not namespace:
                rel = path.relative_to(directory)
                if len(rel.parts) > 1:
                    name = f"{rel.parent.as_posix()}/{name}"
            tools = meta.get("allowed-tools") or meta.get("allowedTools") or []
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            command = CustomCommand(
                name=name,
                description=str(meta.get("description") or "Custom command"),
                body=body.strip(),
                argument_hint=str(meta.get("argument-hint") or meta.get("argumentHint") or ""),
                allowed_tools=[str(t) for t in tools],
                model=str(meta["model"]) if meta.get("model") else None,
                source=path,
                namespace=namespace,
            )
            commands[command.full_name] = command
    return commands


async def _run_shell(command: str, config: Config) -> str:
    from .tools.shell_tools import ShellManager, build_shell_invocation

    argv = build_shell_invocation(config.shell, command)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(config.cwd),
        env={**os.environ, **{str(k): str(v) for k, v in (config.env or {}).items()}},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    return out.decode("utf-8", errors="replace").strip()


async def expand_custom_command(command: CustomCommand, args: str,
                                config: Config) -> str:
    body = command.body
    parts = args.split()
    body = body.replace("$ARGUMENTS", args)
    for index in range(1, 10):
        value = parts[index - 1] if len(parts) >= index else ""
        body = body.replace(f"${index}", value)

    # !`command` -> inject shell output
    import re

    pattern = re.compile(r"!`([^`]+)`")

    async def run_block(match: re.Match[str]) -> str:
        return await _run_shell(match.group(1), config)

    out: list[str] = []
    last = 0
    for match in pattern.finditer(body):
        out.append(body[last:match.start()])
        try:
            out.append(await run_block(match))
        except Exception as exc:
            out.append(f"(command failed: {exc})")
        last = match.end()
    out.append(body[last:])
    body = "".join(out)

    # @file includes
    def include_file(match: re.Match[str]) -> str:
        token = match.group(1)
        path = Path(token)
        if not path.is_absolute():
            path = config.cwd / path
        if not path.is_file():
            return match.group(0)
        try:
            text, _, _ = read_text(path)
        except OSError:
            return match.group(0)
        return f"<file path=\"{token}\">\n{text[:80_000]}\n</file>"

    body = re.sub(r"(?<![\w@])@([^\s@]+)", include_file, body)
    if args and "$ARGUMENTS" not in command.body and not re.search(r"\$\d", command.body):
        body += f"\n\nArguments: {args}"
    return body


def parse_slash(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    body = stripped[1:]
    if not body:
        return None
    name, _, args = body.partition(" ")
    return name.strip(), args.strip()


def help_markdown(config: Config, custom: dict[str, CustomCommand]) -> str:
    lines = ["# YotCode commands", "", "## Built-in", ""]
    for item in BUILTIN_COMMANDS:
        args = f" {item['args']}" if item["args"] else ""
        lines.append(f"- `/{item['name']}{args}` — {item['description']}")
    if custom:
        lines += ["", "## Custom commands", ""]
        for command in custom.values():
            extra = f" {command.argument_hint}" if command.argument_hint else ""
            lines.append(
                f"- `/{command.full_name}{extra}` — {command.description}"
                f"  <sub>({command.source})</sub>"
            )
    lines += ["", "## Keyboard shortcuts", ""]
    for key, description in KEYBINDINGS:
        lines.append(f"- `{key}` — {description}")
    lines += [
        "",
        "## Tips",
        "",
        "- `@path/to/file` in a message attaches the file contents.",
        "- `!command` runs a shell command directly.",
        "- `#note` appends a note to CLAUDE.md.",
        "- `Shift+Tab` cycles: default → accept edits → plan mode.",
    ]
    return "\n".join(lines)


def init_prompt() -> str:
    return (
        "Analyse this codebase and create a CLAUDE.md file at the project root that "
        "will help future coding agents work effectively. Include: the project's "
        "purpose, the tech stack, key commands (install, build, test, lint, run), the "
        "architecture and important directories, coding conventions, and any gotchas. "
        "If CLAUDE.md already exists, improve it rather than duplicating it. Be concise "
        "and use bullet points. Do not list every file."
    )
