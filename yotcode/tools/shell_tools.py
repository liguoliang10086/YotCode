"""Shell 工具：Bash / BashOutput / KillShell，以及后台进程管理。

Windows 上没有 bash 时会自动退回 PowerShell；Git Bash 存在时优先用它，
这样模型写出的 `ls`、`grep`、`&&` 等命令可以直接工作。
所有命令都通过 build_shell_invocation() 组装，统一 UTF-8 输出。

run_in_background=true 时进程交给 ShellManager，输出写入
~/.yotcode/tmp/bash_xxx.log，之后用 BashOutput 增量读取、KillShell 结束。
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import format_duration, truncate_middle
from .base import Tool, ToolContext, ToolResult

MAX_OUTPUT = 30_000
DANGEROUS_PATTERNS = [
    r"\brm\s+-[a-z]*[rf]", r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)",
    r"\bformat\b", r"\bmkfs\b", r"\bdd\s+if=", r"\bshutdown\b", r"\breboot\b",
    r"\bdel\s+/[sq]\b", r"\brmdir\s+/s\b", r">\s*/dev/sd", r":\(\)\s*\{",
]


def is_dangerous(command: str) -> bool:
    """粗粒度危险命令检测，仅用于权限弹窗里的红色警告。"""
    lowered = command.lower()
    return any(re.search(pat, lowered) for pat in DANGEROUS_PATTERNS)


def build_shell_invocation(shell: str, command: str) -> list[str]:
    shell = shell or ""
    name = Path(shell).name.lower()
    if "powershell" in name or shell.lower() in ("powershell", "pwsh"):
        exe = shell if Path(shell).exists() else (shutil.which("pwsh") or "powershell")
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Console]::OutputEncoding; "
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    if name in ("cmd.exe",) or shell.lower() == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", f"chcp 65001>nul & {command}"]
    # bash / sh / zsh / fish / git-bash
    exe = shell
    if not Path(shell).exists():
        exe = shutil.which(shell) or shutil.which("bash") or "/bin/sh"
    return [exe, "-c", command]


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except Exception:
        pass


@dataclass
class BackgroundShell:
    id: str
    command: str
    process: asyncio.subprocess.Process
    log_path: Path
    output: str = ""
    read_offset: int = 0
    exit_code: int | None = None
    started: float = field(default_factory=time.time)
    reader: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.exit_code is None


class ShellManager:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self.shells: dict[str, BackgroundShell] = {}
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    async def start(self, command: str, ctx: ToolContext) -> BackgroundShell:
        shell_id = f"bash_{uuid.uuid4().hex[:8]}"
        argv = build_shell_invocation(ctx.config.shell, command)
        env = self._env(ctx)
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ctx.cwd), env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **self._popen_kwargs(),
        )
        log_path = self.tmp_dir / f"{shell_id}.log"
        shell = BackgroundShell(shell_id, command, process, log_path)
        self.shells[shell_id] = shell
        shell.reader = asyncio.create_task(self._reader(shell))
        return shell

    @staticmethod
    def _popen_kwargs() -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": 0x00000200}  # CREATE_NEW_PROCESS_GROUP
        return {"start_new_session": True}

    def _env(self, ctx: ToolContext) -> dict[str, str]:
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (ctx.config.env or {}).items()})
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("GIT_PAGER", "cat")
        env.setdefault("PAGER", "cat")
        env.setdefault("TERM", "xterm-256color")
        return env

    async def _reader(self, shell: BackgroundShell) -> None:
        assert shell.process.stdout is not None
        try:
            with open(shell.log_path, "wb") as log:
                while True:
                    chunk = await shell.process.stdout.read(4096)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    shell.output += chunk.decode("utf-8", errors="replace")
        except Exception:
            pass
        finally:
            shell.exit_code = await shell.process.wait()

    def read(self, shell_id: str) -> tuple[BackgroundShell | None, str]:
        shell = self.shells.get(shell_id)
        if not shell:
            return None, ""
        new = shell.output[shell.read_offset:]
        shell.read_offset = len(shell.output)
        return shell, new

    async def kill(self, shell_id: str) -> BackgroundShell | None:
        shell = self.shells.get(shell_id)
        if not shell:
            return None
        await _kill_tree(shell.process)
        if shell.reader:
            try:
                await asyncio.wait_for(shell.reader, timeout=3)
            except Exception:
                pass
        if shell.exit_code is None:
            shell.exit_code = shell.process.returncode if shell.process.returncode is not None else -1
        return shell

    async def cleanup(self) -> None:
        for shell in list(self.shells.values()):
            if shell.running:
                await _kill_tree(shell.process)


class BashTool(Tool):
    name = "Bash"
    description = (
        "Executes a shell command in a persistent working directory and returns its "
        "output. Use it for git, builds, tests, package managers and scripts. Never use "
        "it for file reading/searching/editing when the dedicated tools can do the job. "
        "Prefer non-interactive commands. Output is truncated to 30000 characters.\n\n"
        "Guidelines:\n"
        "- Quote paths containing spaces.\n"
        "- Use `timeout` (ms) for long builds, max 600000.\n"
        "- Use run_in_background for dev servers / watch tasks, then read output with "
        "BashOutput and stop it with KillShell.\n"
        "- Chain dependent commands with `&&`; use `;` only when failures are acceptable."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute."},
            "description": {
                "type": "string",
                "description": "5-10 word description of what the command does.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (max 600000, default 120000).",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Run in the background and return immediately.",
            },
        },
        "required": ["command"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        # 界面上显示真正的命令，比 description 更有信息量；
        # permission 提示同样需要看到要执行什么。
        command = str(data.get("command") or "").replace("\n", " ; ")
        return command[:90] + ("..." if len(command) > 90 else "")

    def get_diff(self, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str, str] | None:
        return None

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(data.get("command") or "").strip()
        if not command:
            return self.error("command is required")
        if len(command) > 30_000:
            return self.error("command is too long (max 30000 characters)")
        timeout_ms = int(data.get("timeout") or 120_000)
        timeout_ms = max(1000, min(timeout_ms, 600_000))
        shells: ShellManager = ctx.shells

        # 后台任务立即返回 shell id，避免 dev server 之类的命令卡住整个回合。
        if data.get("run_in_background"):
            shell = await shells.start(command, ctx)
            return self.ok(
                f"Command running in background with ID: {shell.id}\n"
                f"Output is being written to {shell.log_path}\n"
                f"Use BashOutput with bash_id='{shell.id}' to read it.",
                bash_id=shell.id, background=True, command=command,
            )

        argv = build_shell_invocation(ctx.config.shell, command)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(ctx.cwd), env=shells._env(ctx),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **ShellManager._popen_kwargs(),
            )
        except FileNotFoundError:
            return self.error(
                f"Shell not found: {ctx.config.shell}. Set 'shell' in settings or CCPY_SHELL."
            )
        except Exception as exc:
            return self.error(f"Failed to start command: {exc}")

        started = time.time()
        chunks: list[str] = []
        interrupted = False
        timed_out = False
        assert process.stdout is not None
        deadline = started + timeout_ms / 1000
        while True:
            if ctx.cancel.is_set():
                interrupted = True
                await _kill_tree(process)
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                await _kill_tree(process)
                break
            try:
                chunk = await asyncio.wait_for(process.stdout.read(8192), timeout=min(0.25, remaining))
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="replace"))
            if sum(len(c) for c in chunks) > MAX_OUTPUT * 3:
                # keep memory bounded; further output is still drained below
                chunks = ["".join(chunks)[-MAX_OUTPUT * 2:]]
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            await _kill_tree(process)
        output = "".join(chunks).replace("\r\n", "\n").replace("\r", "\n").strip()
        elapsed = time.time() - started
        exit_code = process.returncode

        if interrupted:
            return self.error(
                "Command interrupted by the user."
                + (f"\n\nPartial output:\n{output}" if output else ""),
                command=command,
            )
        if timed_out:
            return self.error(
                f"Command timed out after {format_duration(elapsed)} (limit {timeout_ms}ms). "
                "Consider run_in_background for long-running processes."
                + (f"\n\nPartial output:\n{output}" if output else ""),
                command=command, timed_out=True,
            )

        display, truncated = truncate_middle(output, MAX_OUTPUT)
        if not display.strip():
            display = "(no output)"
        if exit_code not in (0, None):
            display += f"\n\n(exit code {exit_code})"
        meta = {"command": command, "exit_code": exit_code, "duration": elapsed}
        if truncated:
            meta["truncated"] = True
        return ToolResult(display, is_error=exit_code not in (0, None), meta=meta)


class BashOutputTool(Tool):
    name = "BashOutput"
    read_only = True
    requires_permission = False
    # 只是读取后台任务的新输出，不影响进程本身，可以并发
    parallel_safe = True
    description = (
        "Reads new output from a background shell started with Bash(run_in_background=true). "
        "Returns output produced since the last read, plus the running status."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "bash_id": {"type": "string", "description": "ID returned by the Bash tool."},
            "filter": {"type": "string", "description": "Optional regex; only matching lines are returned."},
        },
        "required": ["bash_id"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("bash_id") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shells: ShellManager = ctx.shells
        shell_id = str(data.get("bash_id") or "")
        shell, new = shells.read(shell_id)
        if shell is None:
            return self.error(f"No background shell with id {shell_id}")
        if data.get("filter"):
            try:
                regex = re.compile(str(data["filter"]))
                new = "\n".join(line for line in new.splitlines() if regex.search(line))
            except re.error:
                pass
        status = "running" if shell.running else f"completed (exit code {shell.exit_code})"
        if not new.strip():
            new = "(no new output)"
        display, _ = truncate_middle(new, MAX_OUTPUT)
        return self.ok(
            f"Status: {status}\nDuration: {format_duration(time.time() - shell.started)}\n\n{display}",
            bash_id=shell_id, running=shell.running,
        )


class KillShellTool(Tool):
    name = "KillShell"
    read_only = False
    requires_permission = False
    description = "Kills a background shell by ID and returns its final status."
    input_schema = {
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell ID (bash_...)."},
            "bash_id": {"type": "string", "description": "Alias for shell_id."},
        },
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("shell_id") or data.get("bash_id") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        shells: ShellManager = ctx.shells
        shell_id = str(data.get("shell_id") or data.get("bash_id") or "")
        shell = await shells.kill(shell_id)
        if shell is None:
            return self.error(f"No background shell with id {shell_id}")
        return self.ok(f"Killed background shell {shell_id} (exit code {shell.exit_code}).")
