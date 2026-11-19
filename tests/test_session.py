"""JSONL session persistence and replay."""
from __future__ import annotations

from pathlib import Path

from yotcode.session import Session
from yotcode.types import TextBlock, Usage, assistant_message, user_message


def make_session(tmp_path: Path) -> Session:
    return Session.create(tmp_path / "sessions", tmp_path, "mock-claude", "mock")


def test_create_writes_meta(tmp_path):
    session = make_session(tmp_path)
    meta_path = tmp_path / "sessions" / f"{session.meta.id}.meta.json"
    assert meta_path.is_file()
    session.append_message(user_message("hi"))
    assert session.path.is_file()
    metas = Session.list_sessions(tmp_path / "sessions")
    assert [m.id for m in metas] == [session.meta.id]


def test_append_and_reload(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("Fix the bug"))
    session.append_message(assistant_message("On it."))
    session.append_message(assistant_message([TextBlock("Done.")]))
    session.set_todos([{"content": "fix", "status": "completed", "activeForm": "fixing"}])
    session.add_usage(Usage(100, 20), 0.001)

    restored = Session.load(tmp_path / "sessions", session.meta.id)
    assert restored is not None
    assert [m.role for m in restored.messages] == ["user", "assistant", "assistant"]
    assert restored.messages[0].text() == "Fix the bug"
    assert restored.todos[0]["status"] == "completed"
    assert restored.total_usage.input_tokens == 100
    assert restored.total_cost == 0.001
    assert restored.meta.title == "Fix the bug"
    assert restored.meta.turns == 2


def test_reset_event_clears_history(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("hello"))
    session.replace_messages([user_message("summary")], "compacted")
    restored = Session.load(tmp_path / "sessions", session.meta.id)
    assert [m.text() for m in restored.messages] == ["summary"]


def test_reset_persists_clear(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("hello"))
    session.append_message(assistant_message("hi"))
    session.set_todos([{"content": "x", "status": "pending", "activeForm": "x"}])
    session.add_usage(Usage(100, 20), 0.001)

    session.reset("clear")
    restored = Session.load(tmp_path / "sessions", session.meta.id)
    assert restored.messages == []
    assert restored.todos == []
    assert restored.total_usage.total_tokens == 0
    assert restored.total_cost == 0.0
    assert restored.meta.turns == 0


def test_compaction_reset_keeps_usage_and_todos(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("old"))
    session.set_todos([{"content": "keep", "status": "pending", "activeForm": "keep"}])
    session.add_usage(Usage(100, 20), 0.001)
    session.replace_messages([user_message("summary")], "compacted")

    restored = Session.load(tmp_path / "sessions", session.meta.id)
    assert [m.text() for m in restored.messages] == ["summary"]
    assert restored.todos[0]["content"] == "keep"
    assert restored.total_usage.prompt_tokens == 100
    assert restored.total_cost == 0.001


def test_memory_session_does_not_touch_disk(tmp_path):
    session = Session.memory(tmp_path, "m", "p")
    session.append_message(user_message("sub-agent prompt"))
    assert not list(tmp_path.glob("*.jsonl"))


def test_export_markdown(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("hello"))
    session.append_message(assistant_message("world"))
    markdown = session.export_markdown()
    assert "# YotCode session" in markdown
    assert "## ❯ User" in markdown
    assert "hello" in markdown and "world" in markdown


def test_load_skips_corrupt_lines(tmp_path):
    session = make_session(tmp_path)
    session.append_message(user_message("ok"))
    with open(session.path, "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    restored = Session.load(tmp_path / "sessions", session.meta.id)
    assert len(restored.messages) == 1


def test_load_missing_session_returns_none(tmp_path):
    assert Session.load(tmp_path / "sessions", "nope") is None
