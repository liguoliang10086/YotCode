"""Tool implementations against a temporary workspace."""
from __future__ import annotations

import asyncio

import pytest

from yotcode.config import Config
from yotcode.tools.base import ToolContext
from yotcode.tools.file_tools import (
    EditTool,
    GlobTool,
    GrepTool,
    LSTool,
    MultiEditTool,
    ReadTool,
    WriteTool,
)


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(cwd=tmp_path, config=Config(cwd=tmp_path),
                       cancel=asyncio.Event())


async def test_write_then_read(tmp_path, ctx):
    write = WriteTool()
    result = await write.run({"file_path": "pkg/mod.py", "content": "print('hi')\n"}, ctx)
    assert not result.is_error
    assert (tmp_path / "pkg" / "mod.py").read_text(encoding="utf-8") == "print('hi')\n"

    read = ReadTool()
    result = await read.run({"file_path": "pkg/mod.py"}, ctx)
    assert not result.is_error
    assert "print('hi')" in result.text
    assert "1\t" in result.text


async def test_read_respects_offset_and_limit(tmp_path, ctx):
    (tmp_path / "lines.txt").write_text("\n".join(f"L{i}" for i in range(1, 11)),
                                        encoding="utf-8")
    result = await ReadTool().run(
        {"file_path": "lines.txt", "offset": 4, "limit": 2}, ctx)
    assert "L4" in result.text and "L5" in result.text
    assert "L3" not in result.text and "L6" not in result.text


async def test_read_missing_file_is_error(tmp_path, ctx):
    result = await ReadTool().run({"file_path": "nope.txt"}, ctx)
    assert result.is_error and "does not exist" in result.text.lower()


async def test_edit_replaces_unique_string(tmp_path, ctx):
    path = tmp_path / "app.py"
    path.write_text("x = 1\ny = 2\n", encoding="utf-8")
    result = await EditTool().run(
        {"file_path": "app.py", "old_string": "y = 2", "new_string": "y = 3"}, ctx)
    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "x = 1\ny = 3\n"


async def test_edit_rejects_ambiguous_match(tmp_path, ctx):
    path = tmp_path / "app.py"
    path.write_text("a\na\na\n", encoding="utf-8")
    result = await EditTool().run(
        {"file_path": "app.py", "old_string": "a", "new_string": "b"}, ctx)
    assert result.is_error
    assert "unique" in result.text.lower()


async def test_edit_replace_all(tmp_path, ctx):
    path = tmp_path / "app.py"
    path.write_text("a\na\n", encoding="utf-8")
    result = await EditTool().run(
        {"file_path": "app.py", "old_string": "a", "new_string": "b",
         "replace_all": True}, ctx)
    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "b\nb\n"


async def test_multi_edit_applies_in_order(tmp_path, ctx):
    path = tmp_path / "app.py"
    path.write_text("one two three\n", encoding="utf-8")
    result = await MultiEditTool().run({
        "file_path": "app.py",
        "edits": [
            {"old_string": "one", "new_string": "1"},
            {"old_string": "two", "new_string": "2"},
        ],
    }, ctx)
    assert not result.is_error, result.text
    assert path.read_text(encoding="utf-8") == "1 2 three\n"


async def test_ls_lists_dirs_first(tmp_path, ctx):
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "adir").mkdir()
    result = await LSTool().run({"path": "."}, ctx)
    assert not result.is_error
    lines = result.text.splitlines()
    assert lines[1].endswith("/")
    assert "z.txt" in result.text


async def test_glob_finds_python_files(tmp_path, ctx):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("b", encoding="utf-8")
    result = await GlobTool().run({"pattern": "**/*.py"}, ctx)
    assert not result.is_error
    assert "a.py" in result.text and "b.ts" not in result.text


async def test_grep_content_mode(tmp_path, ctx):
    (tmp_path / "a.py").write_text("import os\nprint('needle')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("pass\n", encoding="utf-8")
    result = await GrepTool().run(
        {"pattern": "needle", "output_mode": "content"}, ctx)
    assert not result.is_error
    assert "needle" in result.text
    assert "b.py" not in result.text


async def test_grep_files_with_matches(tmp_path, ctx):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("hay\n", encoding="utf-8")
    result = await GrepTool().run({"pattern": "needle"}, ctx)
    assert not result.is_error
    assert "a.py" in result.text and "b.py" not in result.text
