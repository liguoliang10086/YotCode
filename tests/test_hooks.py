"""Hook configuration compatibility."""
from __future__ import annotations

from pathlib import Path

from yotcode.config import Config
from yotcode.hooks import HookRunner


def test_hook_entries_accept_standard_and_shorthand():
    config = Config(
        cwd=Path.cwd(),
        hooks={
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"command": "check.sh"}]},
                {"matcher": "Write", "command": "check_write.sh"},
            ]
        },
    )
    runner = HookRunner(config)
    assert runner._entries("PreToolUse", "Bash")[0]["command"] == "check.sh"
    assert runner._entries("PreToolUse", "Write")[0]["command"] == "check_write.sh"
    assert runner._entries("PreToolUse", "Read") == []
