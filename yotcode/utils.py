"""跨模块共用的小工具：编码、读写、路径、Git 信息、Markdown 解析等。

这里刻意不引入第三方依赖（除了标准库），方便单独测试。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------
def configure_stdio() -> None:
    """Make console output UTF-8 so emoji / box drawing don't explode on Windows."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
            ctypes.windll.kernel32.SetConsoleCP(65001)  # type: ignore[attr-defined]
        except Exception:
            pass


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".woff",
    ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".avi", ".pyc", ".class",
    ".jar", ".wasm", ".db", ".sqlite", ".bin",
}

IMAGE_EXTS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


def read_text(path: Path) -> tuple[str, str, bool]:
    """Read a text file, tolerating UTF-8, BOM, GB18030 and cp1252.

    Returns (text, encoding, had_bom).
    """
    data = Path(path).read_bytes()
    had_bom = data.startswith(b"\xef\xbb\xbf")
    if had_bom:
        try:
            return data.decode("utf-8-sig"), "utf-8", True
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(enc), enc, False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8", False


def write_text(path: Path, text: str, encoding: str = "utf-8", bom: bool = False,
               newline: str | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if newline is not None:
        text = text.replace("\r\n", "\n").replace("\n", newline)
    elif "\r\n" in text:
        # keep whatever the caller passed in
        pass
    data = text.encode("utf-8" if encoding == "utf-8" else encoding, errors="replace")
    if bom:
        data = b"\xef\xbb\xbf" + data
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024  # type: ignore[assignment]
    return f"{num:.1f}GB"


def truncate_middle(text: str, max_chars: int = 30_000,
                    head_ratio: float = 0.6) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * head_ratio)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n\n... [{omitted} characters truncated] ...\n\n"
        + text[-tail:],
        True,
    )


def truncate_lines(text: str, max_lines: int, keep: str = "tail") -> tuple[str, int]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, 0
    omitted = len(lines) - max_lines
    if keep == "head":
        kept = lines[:max_lines]
    elif keep == "middle":
        head = max_lines * 2 // 3
        tail = max_lines - head
        kept = lines[:head] + [f"... [{omitted} lines omitted] ..."] + lines[-tail:]
        return "\n".join(kept), omitted
    else:
        kept = lines[-max_lines:]
    return "\n".join(kept), omitted


def is_binary_bytes(data: bytes) -> bool:
    return b"\x00" in data[:8192]


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def expand_path(raw: str, base: Path | None = None) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    if not p.is_absolute() and base is not None:
        p = base / p
    return p.resolve()


def display_path(path: Path, cwd: Path) -> str:
    try:
        rel = Path(path).resolve().relative_to(Path(cwd).resolve())
        return str(rel).replace("\\", "/")
    except (ValueError, OSError):
        return str(path).replace("\\", "/")


def slug_path(path: Path, max_len: int = 60) -> str:
    raw = str(Path(path).resolve()).replace("\\", "/")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-").lower()
    if len(slug) > max_len:
        slug = slug[:max_len]
    import hashlib

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def which(name: str) -> str | None:
    return shutil.which(name)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------
def run_capture(cmd: list[str], cwd: Path, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def find_git_root(path: Path) -> Path | None:
    cur = Path(path).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def git_info(cwd: Path) -> dict[str, Any]:
    root = find_git_root(cwd)
    if not root:
        return {"is_repo": False}
    branch = run_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    status = run_capture(["git", "status", "--short"], root)
    log = run_capture(["git", "log", "--oneline", "-5"], root)
    return {
        "is_repo": True,
        "root": str(root),
        "branch": branch or "detached",
        "status": status,
        "log": log,
    }


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    return f"{minutes}m {int(seconds % 60)}s"


def format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def safe_json_loads(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the tiny YAML subset used by commands/agents files."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\r\n")
    meta: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and current_key:
            item = line.strip().lstrip("-").strip()
            if isinstance(meta.get(current_key), list):
                meta[current_key].append(_coerce(item))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        current_key = key
        if not value:
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [_coerce(v.strip()) for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = _coerce(value)
    return meta, body


def _coerce(value: str) -> Any:
    lower = value.lower()
    if lower in ("true", "yes"):
        return True
    if lower in ("false", "no"):
        return False
    if lower in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


_MERGE_LIST_KEYS = {"hooks", "allow", "deny", "ask", "additionalDirectories"}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge settings; permission and hook lists are additive."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list) and key in _MERGE_LIST_KEYS:
            merged = out[key] + value
            seen: set[str] = set()
            deduped: list = []
            for item in merged:
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker in seen:
                    continue
                seen.add(marker)
                deduped.append(item)
            out[key] = deduped
        else:
            out[key] = value
    return out


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env loader (KEY=VALUE, # comments, optional quotes)."""
    values: dict[str, str] = {}
    try:
        text, _, _ = read_text(path)
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key] = value
    return values


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_timestamp(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime())
