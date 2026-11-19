"""项目 / 用户记忆（CLAUDE.md、CLAUDEPY.md、YOTCODE.md）。

查找顺序：用户家目录 -> 项目根目录 -> 当前目录，逐级向上收集；
支持在记忆文件里用单独一行 `@相对路径` 导入其他文件（最多递归两层）。
TUI 里的 `#笔记` 会把内容追加到项目根目录的 CLAUDE.md。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import home_dir, project_config_dir
from .utils import read_text

# 记忆文件名：CLAUDE.md 是 Claude Code 的约定（优先），
# YOTCODE.md 是改名后的新名字，CLAUDEPY.md 是老名字（继续认）。
MEMORY_NAMES = ("CLAUDE.md", "YOTCODE.md", "CLAUDEPY.md")
MAX_FILE_BYTES = 200_000


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            text, _, _ = read_text(path)
            return text[:MAX_FILE_BYTES] + "\n... (memory file truncated)"
        text, _, _ = read_text(path)
        return text.strip()
    except OSError:
        return ""


def _expand_imports(text: str, base: Path, depth: int = 0) -> str:
    if depth > 2:
        return text

    def replace(match: re.Match[str]) -> str:
        rel = match.group(1).strip()
        target = (base / rel).resolve()
        if not target.is_file():
            return match.group(0)
        content = _read(target)
        return _expand_imports(content, target.parent, depth + 1) if content else ""

    return re.sub(r"(?m)^@([^\s]+)\s*$", replace, text)


def expand_imports(text: str, base: Path, depth: int = 0) -> str:
    """公开版本：把单独一行的 `@相对路径` 展开成文件内容（最多递归两层）。

    记忆文件和 skill 正文都用这套导入语法，base 是相对路径的基准目录。
    """
    return _expand_imports(text, base, depth)


def memory_files(cwd: Path) -> list[Path]:
    cwd = Path(cwd).resolve()
    candidates: list[Path] = []
    home = Path.home()
    candidates += [
        home_dir() / "CLAUDEPY.md",
        home_dir() / "YOTCODE.md",
        home / ".claude" / "CLAUDE.md",
    ]
    chain = [cwd, *cwd.parents]
    # Stop at the git root (inclusive) if there is one, otherwise walk to the drive root.
    git_root = None
    for parent in chain:
        if (parent / ".git").exists():
            git_root = parent
            break
    if git_root:
        chain = chain[: chain.index(git_root) + 1]
    for directory in reversed(chain):
        for name in MEMORY_NAMES:
            candidates.append(directory / name)
        candidates.append(project_config_dir(directory) / "CLAUDEPY.md")
        candidates.append(project_config_dir(directory) / "YOTCODE.md")
        candidates.append(directory / ".claude" / "CLAUDE.md")
    seen: set[Path] = set()
    out: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def memory_contents(cwd: Path) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    seen_text: set[str] = set()
    for path in memory_files(cwd):
        text = _read(path)
        if not text:
            continue
        text = _expand_imports(text, path.parent)
        if text in seen_text:
            continue
        seen_text.add(text)
        results.append((path, text))
    return results


def memory_prompt(cwd: Path) -> str:
    entries = memory_contents(cwd)
    if not entries:
        return ""
    blocks = [
        "Codebase and user instructions are shown below. These instructions "
        "OVERRIDE any default behavior and you MUST follow them exactly.",
        "",
    ]
    for path, text in entries:
        blocks.append(f"## Memory: {path}\n{text}")
    return "\n\n".join(blocks)


def memory_summary(cwd: Path) -> str:
    entries = memory_contents(cwd)
    if not entries:
        return "No CLAUDE.md / CLAUDEPY.md / YOTCODE.md memory files found."
    lines = [f"Memory files ({len(entries)}):"]
    for path, text in entries:
        lines.append(f"  - {path}  ({len(text.splitlines())} lines)")
    return "\n".join(lines)


def append_memory(cwd: Path, note: str) -> Path:
    target = Path(cwd) / "CLAUDE.md"
    existing = ""
    if target.is_file():
        existing, _, _ = read_text(target)
    prefix = "" if not existing else ("" if existing.endswith("\n") else "\n")
    block = f"{prefix}\n## Notes\n- {note.strip()}\n"
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(existing + block)
    return target
