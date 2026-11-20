"""CLI 附件与会话选择器测试。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _env(tmp: Path) -> dict[str, str]:
    return {
        **os.environ,
        "YOTCODE_HOME": str(tmp / "home"),
        "PYTHONIOENCODING": "utf-8",
    }


def _run(tmp: Path, args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "yotcode", *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=_env(tmp),
        cwd=str(tmp),
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )


def test_parser_accepts_image_and_file_args():
    from yotcode.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "-p", "hi", "--image", "a.png", "--image", "b.jpg", "--file", "c.txt"
    ])
    assert args.image == ["a.png", "b.jpg"]
    assert args.file == ["c.txt"]


def test_headless_image_and_file_are_persisted():
    tmp = Path(tempfile.mkdtemp())
    img = tmp / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    code = tmp / "note.txt"
    code.write_text("unique-marker-text", encoding="utf-8")

    result = _run(tmp, [
        "--mock", "-p", "describe", "--image", str(img), "--file", str(code)
    ])
    assert result.returncode == 0

    sessions = list((tmp / "home" / "projects").rglob("*.jsonl"))
    assert sessions
    content = sessions[0].read_text(encoding="utf-8")
    assert '"type": "image"' in content or '"type":"image"' in content
    assert "unique-marker-text" in content


def test_resume_without_id_shows_picker():
    tmp = Path(tempfile.mkdtemp())
    # 造两条会话
    for i in range(2):
        result = _run(tmp, ["--mock", "-p", f"session {i}"])
        assert result.returncode == 0

    result = _run(tmp, ["--mock", "--resume", "-p", "continue"], stdin="1\n")
    assert result.returncode == 0
    assert "Select a session" in (result.stdout or "")
