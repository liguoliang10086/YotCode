"""权限规则与 allow / ask / deny 决策引擎。

规则写法与 Claude Code 类似：
  Bash(npm test:*)          前缀匹配（冒号星号是 Bash 专用语法）
  Bash(git push:*)          只允许 git push 开头的命令
  Read(./src/**)            项目内相对路径 glob
  Read(~/notes/*.md)        家目录路径
  WebFetch(domain:github.com)
  Write                       整个工具（不带括号）
  *                           所有工具

评估顺序（见 PermissionManager.evaluate）：
  1. 本会话临时允许（用户刚才选了 “always”）
  2. deny 规则（永远最高优先级）
  3. bypassPermissions 模式 -> 全部允许
  4. plan 模式 -> 只允许只读工具
  5. ask 规则 -> 弹窗询问
  6. allow 规则 -> 直接允许
  7. 工具本身声明不需要权限 -> 允许
  8. acceptEdits 模式下项目内的写文件 -> 允许
  9. 其余 -> 询问用户
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, project_config_dir


@dataclass
class PermissionDecision:
    allowed: bool
    remember: bool = False
    persist: bool = False
    rule: str | None = None
    reason: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


def _norm(value: str) -> str:
    """Normalise a path for rule matching using forward slashes on every OS."""
    value = str(value).replace("\\", "/")
    return value.casefold() if os.name == "nt" else value


def _glob_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/")
    out = ["^"]
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out), re.IGNORECASE if os.name == "nt" else 0)


def _expand_file_pattern(pattern: str, cwd: Path) -> str:
    p = pattern.strip()
    if p.startswith("//"):
        return p[1:]
    if p.startswith("~/"):
        return str(Path.home() / p[2:])
    if p.startswith("./"):
        return str(Path(cwd) / p[2:])
    return p


def match_file_rule(pattern: str, path: Path, cwd: Path) -> bool:
    raw = pattern.strip()
    relative_pattern = raw[2:] if raw.startswith("./") else None
    expanded = _expand_file_pattern(pattern, cwd)

    try:
        rel_path: Path | None = Path(path).resolve().relative_to(Path(cwd).resolve())
    except (ValueError, OSError):
        rel_path = None
    candidate_abs = _norm(str(Path(path).resolve()))
    candidate_rel = _norm(str(rel_path)) if rel_path is not None else candidate_abs
    target = _norm(expanded)

    if any(ch in expanded for ch in "*?["):
        patterns = [expanded]
        if relative_pattern:
            patterns.append(relative_pattern)
        for pat in patterns:
            regex = _glob_regex(_norm(pat))
            if regex.match(candidate_abs) or regex.match(candidate_rel):
                return True
        return False
    if expanded.endswith(("/", "\\")):
        return candidate_abs.startswith(target) or candidate_rel.startswith(target)
    return candidate_abs == target or candidate_rel == target


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def _split_compound(command: str) -> list[str]:
    # Conservative split; if we can't confidently split, treat as one command.
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", command.strip())
    return [p for p in parts if p]


def match_bash_rule(pattern: str, command: str) -> bool:
    pattern = pattern.strip()
    command = command.strip()
    segments = _split_compound(command)
    if not segments:
        return False

    def one(cmd: str) -> bool:
        if pattern.endswith(":*"):
            prefix = pattern[:-2].strip()
            return cmd == prefix or cmd.startswith(prefix + " ") or cmd.startswith(prefix + "\t")
        if pattern.endswith(" *"):
            prefix = pattern[:-2].strip()
            return cmd.startswith(prefix + " ")
        if any(ch in pattern for ch in "*?["):
            return fnmatch.fnmatch(cmd, pattern)
        return cmd == pattern

    if any(sep in command for sep in ("&&", "||", ";")) and len(segments) > 1:
        return all(one(seg) for seg in segments)
    return one(command)


def parse_rule(rule: str) -> tuple[str, str | None]:
    rule = rule.strip()
    if "(" in rule and rule.endswith(")"):
        name, _, rest = rule.partition("(")
        return name.strip(), rest[:-1]
    return rule, None


def match_rule(rule: str, tool_name: str, data: dict[str, Any], cwd: Path) -> bool:
    name, pattern = parse_rule(rule)
    if name != tool_name and name != "*":
        return False
    if pattern is None or pattern.strip() in ("", "*"):
        return True
    pattern = pattern.strip()
    if tool_name == "Bash":
        return match_bash_rule(pattern, str(data.get("command") or ""))
    if tool_name == "WebFetch":
        if pattern.startswith("domain:"):
            from urllib.parse import urlparse

            host = (urlparse(str(data.get("url") or "")).hostname or "").lower()
            wanted = pattern.split(":", 1)[1].strip().lower()
            return host == wanted or host.endswith("." + wanted)
        return fnmatch.fnmatch(str(data.get("url") or ""), pattern)
    if tool_name == "WebSearch":
        return fnmatch.fnmatch(str(data.get("query") or ""), pattern)
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "LS", "Glob", "Grep"):
        raw = data.get("file_path") or data.get("path")
        if not raw:
            return False
        return match_file_rule(pattern, Path(str(raw)), cwd)
    if tool_name == "Task":
        return fnmatch.fnmatch(str(data.get("subagent_type") or "general-purpose"), pattern)
    return fnmatch.fnmatch(str(data.get("description") or ""), pattern)


def suggest_rule(tool_name: str, data: dict[str, Any], cwd: Path) -> str:
    if tool_name == "Bash":
        command = str(data.get("command") or "").strip()
        first = _split_compound(command)[0] if command else ""
        tokens = first.split()
        prefix = " ".join(tokens[:2]) if len(tokens) > 1 else first
        return f"Bash({prefix}:*)"
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "LS", "Glob", "Grep"):
        raw = data.get("file_path") or data.get("path")
        if raw:
            path = Path(str(raw))
            if not path.is_absolute():
                path = cwd / path
            try:
                rel = path.resolve().relative_to(cwd.resolve()).as_posix()
                return f"{tool_name}(./{rel})"
            except (ValueError, OSError):
                return f"{tool_name}({path.as_posix()})"
        return f"{tool_name}"
    if tool_name == "WebFetch":
        from urllib.parse import urlparse

        host = urlparse(str(data.get("url") or "")).hostname or ""
        return f"WebFetch(domain:{host})" if host else "WebFetch"
    if tool_name == "WebSearch":
        return f"WebSearch({data.get('query') or '*'})"
    if tool_name.startswith("mcp__"):
        return tool_name
    return tool_name


class PermissionManager:
    def __init__(self, config: Config) -> None:
        perms = config.permissions or {}
        self.allow: list[str] = list(perms.get("allow") or [])
        self.deny: list[str] = list(perms.get("deny") or [])
        self.ask: list[str] = list(perms.get("ask") or [])
        self.additional_dirs = [
            Path(os.path.expanduser(p)) for p in (perms.get("additionalDirectories") or [])
        ]
        self.mode = config.permission_mode
        self.cwd = Path(config.cwd)
        self.session_allow: set[str] = set()

    # -- mode --------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def _in_project(self, path: Path) -> bool:
        if _is_relative_to(path, self.cwd):
            return True
        return any(_is_relative_to(path, d) for d in self.additional_dirs)

    # -- evaluation --------------------------------------------------------
    def evaluate(self, tool, data: dict[str, Any]) -> tuple[str, str | None]:
        """Return (verdict, matched_rule) where verdict is allow/deny/ask."""
        name = tool.name
        # Session approvals
        for rule in list(self.session_allow):
            if match_rule(rule, name, data, self.cwd):
                return "allow", rule
        # Deny rules always win
        for rule in self.deny:
            if match_rule(rule, name, data, self.cwd):
                return "deny", rule
        if self.mode == "bypassPermissions":
            return "allow", "bypassPermissions"
        if self.mode == "plan":
            if getattr(tool, "read_only", False) or getattr(tool, "plan_mode_ok", False):
                return "allow", "plan-mode"
            return "plan-deny", "plan-mode"
        for rule in self.ask:
            if match_rule(rule, name, data, self.cwd):
                return "ask", rule
        for rule in self.allow:
            if match_rule(rule, name, data, self.cwd):
                return "allow", rule
        if not getattr(tool, "requires_permission", True):
            return "allow", "read-only"
        if self.mode == "acceptEdits" and name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            raw = data.get("file_path") or ""
            path = Path(str(raw))
            if not path.is_absolute():
                path = self.cwd / path
            if self._in_project(path):
                return "allow", "acceptEdits"
        return "ask", None


def persist_allow_rule(cwd: Path, rule: str) -> Path:
    """Add an allow rule to <project>/.yotcode/settings.local.json."""
    import json

    from .utils import read_text

    path = project_config_dir(Path(cwd)) / "settings.local.json"
    data = {}
    if path.is_file():
        try:
            data = json.loads(read_text(path)[0])
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if rule not in allow:
        allow.append(rule)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path
