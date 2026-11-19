"""文件系统工具：Read / Write / Edit / MultiEdit / LS / Glob / Grep。

几个容易踩坑的细节：
  * 路径通过 ctx.resolve() 展开 ~ 和相对路径，最终以当前 cwd 为基准；
  * Read 会检测编码与 BOM，按 `cat -n` 风格返回行号，并拒绝明显二进制文件；
  * Edit 要求 old_string 精确且唯一，避免模型“猜”错位置改坏文件；
  * 写入时保留原文件的编码、BOM 和换行风格（CRLF/LF）；
  * 遍历目录时跳过 .git、node_modules、.venv 等大目录。
"""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..types import ImageBlock, TextBlock
from ..utils import (
    IMAGE_EXTS,
    detect_newline,
    display_path,
    format_size,
    is_probably_binary,
    read_text,
    truncate_middle,
    which,
    write_text,
)
from .base import Tool, ToolContext, ToolResult

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".nuxt", "dist", "build", "target", "vendor",
    ".tox", ".gradle", ".cache", "coverage", "htmlcov", ".svelte-kit",
}

TYPE_GLOBS = {
    "py": "*.py", "python": "*.py", "js": "*.js", "jsx": "*.jsx",
    "ts": "*.ts", "tsx": "*.tsx", "rust": "*.rs", "go": "*.go",
    "java": "*.java", "kotlin": "*.kt", "c": "*.c", "h": "*.h",
    "cpp": "*.cpp", "cxx": "*.cc", "cs": "*.cs", "rb": "*.rb",
    "php": "*.php", "swift": "*.swift", "md": "*.md", "json": "*.json",
    "yaml": "*.yaml", "yml": "*.yml", "toml": "*.toml", "html": "*.html",
    "css": "*.css", "scss": "*.scss", "sh": "*.sh", "sql": "*.sql",
    "vue": "*.vue", "svelte": "*.svelte", "lua": "*.lua", "r": "*.r",
}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
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
    return re.compile("".join(out))


def _walk_files(base: Path, ignore: list[str] | None = None,
                max_files: int = 20000) -> list[Path]:
    base = Path(base)
    if base.is_file():
        return [base]
    ignored = set(IGNORED_DIRS) | {i.strip("/\\") for i in (ignore or []) if i}
    found: list[Path] = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ignored and not d.startswith(".") or d in (".claude", ".yotcode", ".claudepy", ".github")]
        for name in files:
            found.append(Path(root) / name)
            if len(found) >= max_files:
                return found
    return found


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------
class ReadTool(Tool):
    name = "Read"
    read_only = True
    requires_permission = False
    # 读文件互不干扰，可以和同一轮里的其它读操作并发执行
    parallel_safe = True
    description = (
        "Reads a file from the local filesystem. You can access any file directly by "
        "using this tool. By default it reads up to 2000 lines starting at the "
        "beginning. Use offset/limit for large files. Results are returned with line "
        "numbers in `cat -n` style. Prefer this over `cat` in Bash."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or relative path to the file."},
            "offset": {"type": "integer", "description": "1-based line number to start from."},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default 2000)."},
        },
        "required": ["file_path"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return display_path(Path(str(data.get("file_path", ""))), Path.cwd()) if data.get("file_path") else ""

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = str(data.get("file_path") or "").strip()
        if not raw:
            return self.error("file_path is required")
        path = ctx.resolve(raw)
        if not path.exists():
            return self.error(f"File does not exist: {path}")
        if path.is_dir():
            return self.error(f"Illegal operation: {path} is a directory. Use LS instead.")
        suffix = path.suffix.lower()
        if suffix in IMAGE_EXTS:
            size = path.stat().st_size
            if size > 5 * 1024 * 1024:
                return self.error(f"Image too large ({format_size(size)}); limit is 5MB.")
            provider = getattr(ctx.agent, "provider", None)
            if provider is not None and getattr(provider, "supports_images", False):
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                return ToolResult(
                    [
                        TextBlock(f"Image: {path} ({IMAGE_EXTS[suffix]}, {format_size(size)})"),
                        ImageBlock(IMAGE_EXTS[suffix], encoded),
                    ],
                    meta={"path": str(path), "image": True},
                )
            return self.error(
                f"{path} is an image and the current model does not accept image input."
            )
        if is_probably_binary(path):
            return self.error(
                f"Cannot read binary file: {path} ({format_size(path.stat().st_size)})."
            )

        offset = max(1, int(data.get("offset") or 1))
        limit = int(data.get("limit") or 2000)
        limit = max(1, min(limit, 20000))
        try:
            text, _, _ = read_text(path)
        except OSError as exc:
            return self.error(f"Could not read {path}: {exc}")

        all_lines = text.splitlines()
        total = len(all_lines)
        if offset > total and total > 0:
            return self.error(f"offset {offset} is beyond end of file ({total} lines)")
        chunk = all_lines[offset - 1: offset - 1 + limit]
        rendered = []
        for i, line in enumerate(chunk, start=offset):
            if len(line) > 2000:
                line = line[:2000] + " … [line truncated]"
            rendered.append(f"{i:>6}\t{line}")
        if not rendered and total == 0:
            body = "(file is empty)"
        else:
            body = "\n".join(rendered)
        remaining = total - (offset - 1 + len(chunk))
        header = f"{display_path(path, ctx.cwd)} ({total} lines)"
        if remaining > 0:
            body += f"\n\n... ({remaining} more lines, use offset={offset + len(chunk)} to continue)"
        return ToolResult(f"{header}\n{body}", meta={"path": str(path), "lines": total})


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------
class WriteTool(Tool):
    name = "Write"
    description = (
        "Writes a file to the local filesystem, overwriting it if it exists and "
        "creating parent directories as needed. Prefer Edit for modifying existing "
        "files; use Write for brand new files or complete rewrites. You MUST read a "
        "file before overwriting it unless you just created it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or relative path to write."},
            "content": {"type": "string", "description": "The complete file content."},
        },
        "required": ["file_path", "content"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("file_path") or "")

    def touched_paths(self, data: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """Write 会覆盖这个文件 → 改动前留个检查点。"""
        return [ctx.resolve(str(data.get("file_path") or ""))]

    def get_diff(self, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str, str] | None:
        path = ctx.resolve(str(data.get("file_path") or ""))
        before = ""
        if path.is_file():
            try:
                before, _, _ = read_text(path)
            except OSError:
                before = ""
        return str(path), before, str(data.get("content") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(data.get("file_path") or ""))
        content = data.get("content")
        if content is None:
            return self.error("content is required")
        content = str(content)
        if path.exists() and path.is_dir():
            return self.error(f"{path} is a directory")
        existed = path.exists()
        encoding, bom, newline = "utf-8", False, None
        if existed:
            try:
                before, encoding, bom = read_text(path)
                newline = detect_newline(before)
                if before == content:
                    return self.error(f"No changes made to {path}: the content is identical.")
                if "\r\n" not in content and newline == "\r\n":
                    content = content.replace("\r\n", "\n").replace("\n", "\r\n")
                if "\r\n" in content and newline == "\n":
                    content = content.replace("\r\n", "\n")
            except OSError as exc:
                return self.error(f"Could not read existing file {path}: {exc}")
        try:
            write_text(path, content, encoding=encoding, bom=bom)
        except OSError as exc:
            return self.error(f"Could not write {path}: {exc}")
        lines = content.count("\n") + 1
        verb = "updated" if existed else "created"
        return self.ok(
            f"File {verb} successfully: {path} ({lines} lines, {format_size(len(content.encode('utf-8')))})",
            path=str(path), created=not existed,
        )


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------
def _apply_edit(text: str, old: str, new: str, replace_all: bool) -> tuple[str | None, str | None]:
    """执行精确字符串替换；返回 (新内容, 错误信息)。

    故意不提供“模糊匹配”：模型必须先 Read 文件，再给出足够上下文，
    否则一次错误替换可能悄悄破坏代码。
    """
    if old == "":
        return None, "old_string cannot be empty -- use Write to create or replace a file"
    count = text.count(old)
    if count == 0:
        return None, "old_string not found in the file. Re-read the file and match the text exactly, including whitespace/indentation."
    if count > 1 and not replace_all:
        return None, (
            f"old_string appears {count} times. Add more surrounding context to make it "
            "unique, or set replace_all=true."
        )
    if replace_all:
        return text.replace(old, new), None
    return text.replace(old, new, 1), None


def _snippet(path: Path, text: str, needle: str, ctx: ToolContext) -> str:
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if needle and needle.splitlines()[0] in line:
            start = i
            break
    lo = max(0, start - 4)
    hi = min(len(lines), start + max(6, len(needle.splitlines()) + 4))
    rendered = [f"{i + 1:>6}\t{lines[i]}" for i in range(lo, hi)]
    return (
        f"The file {display_path(path, ctx.cwd)} has been updated. "
        f"Here's the result of running `cat -n` on a snippet:\n\n"
        + "\n".join(rendered)
    )


class EditTool(Tool):
    name = "Edit"
    description = (
        "Performs an exact string replacement in a file. The old_string must match the "
        "file exactly (including whitespace) and must be unique unless replace_all is "
        "true. Read the file first. Prefer several small, precise edits over rewriting "
        "whole files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text (must differ)."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("file_path") or "")

    def touched_paths(self, data: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """Edit 会改这个文件 → 改动前留个检查点。"""
        return [ctx.resolve(str(data.get("file_path") or ""))]

    def validate(self, data: dict[str, Any]) -> str | None:
        err = super().validate(data)
        if err:
            return err
        if data.get("old_string") == data.get("new_string"):
            return "old_string and new_string are identical -- no change would be made"
        return None

    def get_diff(self, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str, str] | None:
        path = ctx.resolve(str(data.get("file_path") or ""))
        if not path.is_file():
            return None
        try:
            before, _, _ = read_text(path)
        except OSError:
            return None
        old = str(data.get("old_string") or "")
        new = str(data.get("new_string") or "")
        after, _ = _apply_edit(before.replace("\r\n", "\n"), old.replace("\r\n", "\n"),
                               new.replace("\r\n", "\n"), bool(data.get("replace_all")))
        return str(path), before, after if after is not None else before

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(data.get("file_path") or ""))
        if not path.exists():
            return self.error(f"File does not exist: {path}. Use Write to create it.")
        if path.is_dir():
            return self.error(f"{path} is a directory")
        old = str(data.get("old_string") or "")
        new = str(data.get("new_string") or "")
        try:
            before, encoding, bom = read_text(path)
        except OSError as exc:
            return self.error(f"Could not read {path}: {exc}")
        newline = detect_newline(before)
        normalized = before.replace("\r\n", "\n")
        old_n = old.replace("\r\n", "\n")
        new_n = new.replace("\r\n", "\n")
        after, err = _apply_edit(normalized, old_n, new_n, bool(data.get("replace_all")))
        if err or after is None:
            return self.error(err or "edit failed")
        if after == normalized:
            return self.error("No changes were made (old and new content are equivalent).")
        out = after if newline == "\n" else after.replace("\n", "\r\n")
        try:
            write_text(path, out, encoding=encoding, bom=bom)
        except OSError as exc:
            return self.error(f"Could not write {path}: {exc}")
        return self.ok(_snippet(path, after, new_n, ctx), path=str(path))


class MultiEditTool(Tool):
    name = "MultiEdit"
    description = (
        "Applies several exact string replacements to a single file in order. All edits "
        "are validated against the evolving content; if any edit fails, nothing is "
        "written. Prefer this over many sequential Edit calls for one file."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["file_path", "edits"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("file_path") or "")

    def touched_paths(self, data: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """MultiEdit 会改这个文件 → 改动前留个检查点。"""
        return [ctx.resolve(str(data.get("file_path") or ""))]

    def get_diff(self, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str, str] | None:
        path = ctx.resolve(str(data.get("file_path") or ""))
        if not path.is_file():
            return None
        try:
            before, _, _ = read_text(path)
        except OSError:
            return None
        current = before.replace("\r\n", "\n")
        for edit in data.get("edits") or []:
            current, _ = _apply_edit(
                current,
                str(edit.get("old_string") or "").replace("\r\n", "\n"),
                str(edit.get("new_string") or "").replace("\r\n", "\n"),
                bool(edit.get("replace_all")),
            ) or (current, None)
        return str(path), before, current

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(data.get("file_path") or ""))
        edits = data.get("edits")
        if not isinstance(edits, list) or not edits:
            return self.error("edits must be a non-empty array")
        if not path.is_file():
            return self.error(f"File does not exist: {path}")
        try:
            before, encoding, bom = read_text(path)
        except OSError as exc:
            return self.error(f"Could not read {path}: {exc}")
        newline = detect_newline(before)
        current = before.replace("\r\n", "\n")
        for index, edit in enumerate(edits):
            old = str(edit.get("old_string") or "").replace("\r\n", "\n")
            new = str(edit.get("new_string") or "").replace("\r\n", "\n")
            updated, err = _apply_edit(current, old, new, bool(edit.get("replace_all")))
            if err or updated is None:
                return self.error(f"Edit #{index + 1} failed: {err}. No changes were written.")
            current = updated
        if current == before.replace("\r\n", "\n"):
            return self.error("No changes were made.")
        out = current if newline == "\n" else current.replace("\n", "\r\n")
        try:
            write_text(path, out, encoding=encoding, bom=bom)
        except OSError as exc:
            return self.error(f"Could not write {path}: {exc}")
        return self.ok(
            _snippet(path, current, str(edits[-1].get("new_string") or ""), ctx)
            + f"\n\n({len(edits)} edits applied)",
            path=str(path),
        )


# --------------------------------------------------------------------------
# LS
# --------------------------------------------------------------------------
class LSTool(Tool):
    name = "LS"
    read_only = True
    requires_permission = False
    parallel_safe = True
    description = (
        "Lists files and directories at a path. Shows sizes for files. Use Glob for "
        "recursive pattern matching instead."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (default: cwd)."},
            "ignore": {"type": "array", "items": {"type": "string"}, "description": "Glob patterns to ignore."},
        },
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("path") or ".")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(data.get("path") or "."))
        if not path.exists():
            return self.error(f"Path does not exist: {path}")
        if path.is_file():
            return self.ok(f"{display_path(path, ctx.cwd)} ({format_size(path.stat().st_size)})")
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return self.error(f"Could not list {path}: {exc}")
        ignore = [str(p) for p in (data.get("ignore") or [])]
        rows: list[str] = []
        for entry in entries:
            if any(fnmatch.fnmatch(entry.name, pat) for pat in ignore):
                continue
            if entry.name in (".DS_Store", "Thumbs.db"):
                continue
            if entry.is_dir():
                rows.append(f"{entry.name}/")
            else:
                try:
                    size = format_size(entry.stat().st_size)
                except OSError:
                    size = "?"
                rows.append(f"{entry.name}  ({size})")
            if len(rows) >= 500:
                rows.append("... (truncated)")
                break
        header = f"{display_path(path, ctx.cwd)}/"
        body = "\n".join(rows) if rows else "(empty directory)"
        return self.ok(f"{header}\n{body}")


# --------------------------------------------------------------------------
# Glob
# --------------------------------------------------------------------------
class GlobTool(Tool):
    name = "Glob"
    read_only = True
    requires_permission = False
    parallel_safe = True
    description = (
        "Fast file pattern matching. Supports patterns like '**/*.py' or 'src/**/*.ts'. "
        "Returns matching paths sorted by modification time (newest first)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'."},
            "path": {"type": "string", "description": "Directory to search (default: cwd)."},
        },
        "required": ["pattern"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("pattern") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(data.get("pattern") or "").strip()
        if not pattern:
            return self.error("pattern is required")
        base = ctx.resolve(str(data.get("path") or "."))
        if not base.exists():
            return self.error(f"Path does not exist: {base}")
        if base.is_file():
            files = [base]
        else:
            files = _walk_files(base)
        normalized = pattern.replace("\\", "/")
        regex = _glob_to_regex(normalized)
        basename_only = "/" not in normalized
        matches: list[Path] = []
        for file in files:
            try:
                rel = file.relative_to(base).as_posix()
            except ValueError:
                rel = file.as_posix()
            target = file.name if basename_only else rel
            if regex.match(target):
                matches.append(file)
        matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        truncated = len(matches) > 100
        matches = matches[:100]
        if not matches:
            return self.ok(f"No files found matching pattern '{pattern}' in {display_path(base, ctx.cwd)}")
        lines = [display_path(p, ctx.cwd) for p in matches]
        if truncated:
            lines.append("... (showing first 100 results)")
        return self.ok("\n".join(lines), count=len(matches))


# --------------------------------------------------------------------------
# Grep
# --------------------------------------------------------------------------
class GrepTool(Tool):
    name = "Grep"
    read_only = True
    requires_permission = False
    parallel_safe = True
    description = (
        "Search file contents with a regular expression. Prefer this over `grep` or "
        "`rg` in Bash. Output modes: 'content' (matching lines with line numbers), "
        "'files_with_matches' (paths only) and 'count'. Supports glob/type filters and "
        "context lines."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": "File or directory to search (default: cwd)."},
            "glob": {"type": "string", "description": "Glob filter, e.g. '*.py'."},
            "type": {"type": "string", "description": "File type shorthand: py, js, ts, rust, go, md ..."},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Default: files_with_matches.",
            },
            "-i": {"type": "boolean", "description": "Case insensitive."},
            "-A": {"type": "integer", "description": "Lines of trailing context."},
            "-B": {"type": "integer", "description": "Lines of leading context."},
            "-C": {"type": "integer", "description": "Lines of context on both sides."},
            "head_limit": {"type": "integer", "description": "Max output lines (default 250)."},
            "multiline": {"type": "boolean"},
        },
    }

    def summary(self, data: dict[str, Any]) -> str:
        pat = str(data.get("pattern") or "")
        path = data.get("path")
        return f"{pat}" + (f"  in {path}" if path else "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(data.get("pattern") or "")
        if not pattern:
            return self.error("pattern is required")
        base = ctx.resolve(str(data.get("path") or "."))
        if not base.exists():
            return self.error(f"Path does not exist: {base}")
        mode = str(data.get("output_mode") or "files_with_matches")
        head_limit = int(data.get("head_limit") or (250 if mode == "content" else 200))
        glob_filter = data.get("glob")
        type_filter = data.get("type")
        if type_filter and str(type_filter) in TYPE_GLOBS:
            glob_filter = TYPE_GLOBS[str(type_filter)]
        ignore_case = bool(data.get("-i"))
        before = int(data.get("-B") or 0)
        after = int(data.get("-A") or 0)
        context = int(data.get("-C") or 0)
        if context:
            before = after = context
        multiline = bool(data.get("multiline"))

        rg = which("rg")
        if rg:
            cmd = [rg, "--no-heading", "--color", "never", "--line-number"]
            if ignore_case:
                cmd.append("-i")
            if multiline:
                cmd.append("-U")
            if before:
                cmd += ["-B", str(before)]
            if after:
                cmd += ["-A", str(after)]
            if glob_filter:
                cmd += ["--glob", str(glob_filter)]
            if mode == "files_with_matches":
                cmd.append("-l")
            elif mode == "count":
                cmd.append("-c")
            cmd += ["-e", pattern, "."]
            try:
                proc = subprocess.run(
                    cmd, cwd=str(base if base.is_dir() else base.parent),
                    capture_output=True, timeout=45, encoding="utf-8", errors="replace",
                )
                if proc.returncode in (0, 1):
                    out = proc.stdout.strip()
                    if not out:
                        return self.ok(f"No matches found for '{pattern}'")
                    lines = out.splitlines()
                    truncated = len(lines) > head_limit
                    out = "\n".join(lines[:head_limit])
                    if truncated:
                        out += f"\n... ({len(lines) - head_limit} more lines truncated)"
                    return self.ok(out, count=len(lines))
                # ripgrep failed (bad regex etc.) -> fall through to python
            except Exception:
                pass

        return self._python_grep(
            pattern, base, ctx, mode, glob_filter, ignore_case,
            before, after, head_limit, multiline,
        )

    def _python_grep(self, pattern: str, base: Path, ctx: ToolContext, mode: str,
                     glob_filter: str | None, ignore_case: bool, before: int,
                     after: int, head_limit: int, multiline: bool) -> ToolResult:
        flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return self.error(f"Invalid regular expression: {exc}")
        files = _walk_files(base) if base.is_dir() else [base]
        if glob_filter:
            files = [f for f in files if fnmatch.fnmatch(f.name, str(glob_filter))]
        content_rows: list[str] = []
        file_hits: list[str] = []
        counts: list[tuple[str, int]] = []
        total_lines = 0
        for file in files:
            try:
                if file.stat().st_size > 4 * 1024 * 1024 or is_probably_binary(file):
                    continue
                text, _, _ = read_text(file)
            except OSError:
                continue
            rel = display_path(file, ctx.cwd)
            if multiline:
                matches = list(regex.finditer(text))
                if matches:
                    file_hits.append(rel)
                    counts.append((rel, len(matches)))
                    if mode == "content":
                        for m in matches[:20]:
                            line_no = text[:m.start()].count("\n") + 1
                            snippet = text[m.start():m.end()].replace("\n", "\\n")
                            content_rows.append(f"{rel}:{line_no}:{snippet[:400]}")
                    total_lines += len(matches)
            else:
                lines = text.splitlines()
                file_count = 0
                for i, line in enumerate(lines):
                    if regex.search(line):
                        file_count += 1
                        if mode == "content" and len(content_rows) < head_limit + 50:
                            lo = max(0, i - before)
                            hi = min(len(lines), i + after + 1)
                            if before or after:
                                content_rows.append("--")
                            for j in range(lo, hi):
                                marker = ":" if j == i else "-"
                                content_rows.append(f"{rel}{marker}{j + 1}{marker}{lines[j][:500]}")
                if file_count:
                    file_hits.append(rel)
                    counts.append((rel, file_count))
                    total_lines += file_count
            if len(file_hits) > 500:
                break

        if mode == "files_with_matches":
            if not file_hits:
                return self.ok(f"No matches found for '{pattern}'")
            lines = file_hits[:head_limit]
            extra = len(file_hits) - len(lines)
            out = "\n".join(lines)
            if extra > 0:
                out += f"\n... ({extra} more files)"
            return self.ok(out, count=len(file_hits))
        if mode == "count":
            if not counts:
                return self.ok(f"No matches found for '{pattern}'")
            return self.ok("\n".join(f"{p}:{n}" for p, n in counts[:head_limit]),
                           count=sum(n for _, n in counts))
        if not content_rows:
            return self.ok(f"No matches found for '{pattern}'")
        out = "\n".join(content_rows[:head_limit])
        if len(content_rows) > head_limit:
            out += f"\n... ({len(content_rows) - head_limit} more lines truncated)"
        return self.ok(out, count=total_lines)
