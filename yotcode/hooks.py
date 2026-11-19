"""生命周期 Hook：在 settings.json 里配置外部命令，扩展 Agent 行为。

支持的事件：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse /
Stop / SessionEnd。Hook 通过 stdin 收到 JSON 上下文，可以返回
{"decision":"block"} 阻止操作，或返回 {"context":"..."} 向对话注入额外信息。
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .tools.shell_tools import build_shell_invocation

HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd",
)


@dataclass
class HookOutcome:
    blocked: bool = False
    reason: str = ""
    context: str = ""


class HookRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.hooks = config.hooks or {}

    def _entries(self, event: str, tool_name: str | None) -> list[dict[str, Any]]:
        configured = self.hooks.get(event) or []
        if isinstance(configured, dict):
            configured = [configured]
        out: list[dict[str, Any]] = []
        for entry in configured:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher") or "*")
            if tool_name and matcher not in ("*", "") and matcher != tool_name:
                # allow simple glob-ish prefix matching
                if not tool_name.startswith(matcher.replace("*", "")):
                    continue
            # 标准写法：{"matcher": "Bash", "hooks": [{"command": "..."}]}。
            # 为了兼容更简洁的配置，也接受把 command 直接写在 entry 上：
            # {"matcher": "Bash", "command": "..."}。
            commands = entry.get("hooks") or []
            if isinstance(commands, dict):
                commands = [commands]
            if not commands and entry.get("command"):
                commands = [entry]
            for command in commands:
                if isinstance(command, dict) and command.get("command"):
                    out.append(command)
        return out

    async def run(self, event: str, payload: dict[str, Any],
                  tool_name: str | None = None) -> HookOutcome:
        outcome = HookOutcome()
        for hook in self._entries(event, tool_name):
            command = str(hook.get("command") or "")
            if not command:
                continue
            timeout = float(hook.get("timeout") or 60)
            argv = build_shell_invocation(self.config.shell, command)
            env = dict(os.environ)
            env.update({str(k): str(v) for k, v in (self.config.env or {}).items()})
            env["CLAUDE_PROJECT_DIR"] = str(self.config.cwd)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self.config.cwd),
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(
                            json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    outcome.reason = f"Hook timed out after {timeout:.0f}s: {command}"
                    outcome.blocked = event == "PreToolUse"
                    continue
            except Exception as exc:
                outcome.reason = f"Hook failed to start: {exc}"
                continue
            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()
            if process.returncode == 2:
                outcome.blocked = True
                outcome.reason = err_text or out_text or "Blocked by hook"
            elif process.returncode not in (0, None):
                outcome.reason = err_text or f"Hook exited with {process.returncode}"
            if event == "UserPromptSubmit" and out_text:
                outcome.context = (outcome.context + "\n" + out_text).strip()
            if event == "PreToolUse" and out_text:
                outcome.context = (outcome.context + "\n" + out_text).strip()
        return outcome
