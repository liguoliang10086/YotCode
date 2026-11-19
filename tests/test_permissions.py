"""Permission modes, rule matching and persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yotcode.config import Config
from yotcode.permissions import (
    PermissionManager,
    match_bash_rule,
    match_file_rule,
    match_rule,
    parse_rule,
    persist_allow_rule,
    suggest_rule,
)


class FakeTool:
    def __init__(self, name: str, *, read_only: bool = False,
                 requires_permission: bool = True) -> None:
        self.name = name
        self.read_only = read_only
        self.requires_permission = requires_permission


def make_manager(tmp_path: Path, mode: str = "default", **permissions) -> PermissionManager:
    config = Config(cwd=tmp_path, permission_mode=mode)
    config.permissions = {
        "allow": permissions.get("allow", []),
        "deny": permissions.get("deny", []),
        "ask": permissions.get("ask", []),
        "additionalDirectories": [],
    }
    return PermissionManager(config)


def test_parse_rule():
    assert parse_rule("Bash(npm run test:*)") == ("Bash", "npm run test:*")
    assert parse_rule("Read") == ("Read", None)


@pytest.mark.parametrize("pattern,command,expected", [
    ("npm test", "npm test", True),
    ("npm test", "npm test -- --watch", False),
    ("npm:*", "npm install", True),
    ("npm:*", "npm", True),
    ("npm:*", "npmx install", False),
    ("git status", "git status && git diff", False),
    ("git status", "git status && rm -rf /", False),
    ("git:*", "git status && git diff", True),
])
def test_bash_rule_matching(pattern, command, expected):
    assert match_bash_rule(pattern, command) is expected


def test_file_rule_matching(tmp_path):
    target = tmp_path / "src" / "app.py"
    assert match_file_rule("src/**/*.py", target, tmp_path)
    assert match_file_rule("./src/**/*.py", target, tmp_path)
    assert match_file_rule("./src/app.py", target, tmp_path)
    assert match_file_rule(str(target), target, tmp_path)
    assert not match_file_rule("src/**/*.ts", target, tmp_path)
    assert match_file_rule("src/", tmp_path / "src" / "nested" / "x.py", tmp_path)


def test_default_mode_allows_reads_and_asks_for_writes(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.evaluate(FakeTool("Read", requires_permission=False), {})[0] == "allow"
    assert manager.evaluate(FakeTool("Write"), {"file_path": "a.txt"})[0] == "ask"


def test_accept_edits_only_inside_project(tmp_path):
    manager = make_manager(tmp_path, "acceptEdits")
    inside = tmp_path / "src" / "a.py"
    outside = tmp_path.parent / "elsewhere" / "b.py"
    assert manager.evaluate(FakeTool("Write"), {"file_path": str(inside)})[0] == "allow"
    assert manager.evaluate(FakeTool("Write"), {"file_path": str(outside)})[0] == "ask"


def test_plan_mode_blocks_side_effects(tmp_path):
    manager = make_manager(tmp_path, "plan")
    assert manager.evaluate(FakeTool("Read", read_only=True), {})[0] == "allow"
    assert manager.evaluate(FakeTool("Write"), {"file_path": "a.txt"})[0] == "plan-deny"


def test_plan_mode_allows_plan_and_readonly_tools(tmp_path):
    from yotcode.tools.interaction import ExitPlanModeTool, TaskTool
    from yotcode.tools.web_tools import WebFetchTool, WebSearchTool

    manager = make_manager(tmp_path, "plan")
    for tool in (ExitPlanModeTool(), TaskTool(), WebFetchTool(), WebSearchTool()):
        assert manager.evaluate(tool, {})[0] == "allow"
    assert manager.evaluate(FakeTool("Bash"), {"command": "npm install"})[0] == "plan-deny"


def test_bypass_permissions_allows_everything(tmp_path):
    manager = make_manager(tmp_path, "bypassPermissions")
    assert manager.evaluate(FakeTool("Bash"), {"command": "rm -rf build"})[0] == "allow"


def test_deny_wins_over_allow(tmp_path):
    manager = make_manager(
        tmp_path,
        allow=["Bash(git status)", "Bash"],
        deny=["Bash(git status)"],
    )
    assert manager.evaluate(FakeTool("Bash"), {"command": "git status"})[0] == "deny"


def test_ask_rule_beats_allow(tmp_path):
    manager = make_manager(
        tmp_path,
        allow=["Bash(*)"],
        ask=["Bash(rm:*)"],
    )
    assert manager.evaluate(FakeTool("Bash"), {"command": "rm -rf x"})[0] == "ask"


def test_session_allow_short_circuits(tmp_path):
    manager = make_manager(tmp_path)
    manager.session_allow.add("Bash(pytest:*)")
    verdict, rule = manager.evaluate(FakeTool("Bash"), {"command": "pytest -q"})
    assert verdict == "allow" and rule == "Bash(pytest:*)"


def test_suggest_rule(tmp_path):
    assert suggest_rule("Bash", {"command": "npm run build"}, tmp_path) == "Bash(npm run:*)"
    assert suggest_rule("Write", {"file_path": str(tmp_path / "src" / "a.py")},
                        tmp_path) == "Write(./src/a.py)"
    assert suggest_rule("WebFetch", {"url": "https://docs.python.org/3/"}, tmp_path) == \
        "WebFetch(domain:docs.python.org)"


def test_persist_allow_rule(tmp_path):
    path = persist_allow_rule(tmp_path, "Bash(pytest:*)")
    assert path == tmp_path / ".yotcode" / "settings.local.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(pytest:*)"]
    # calling again must not duplicate the rule
    persist_allow_rule(tmp_path, "Bash(pytest:*)")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"].count("Bash(pytest:*)") == 1


def test_match_rule_ignores_other_tools(tmp_path):
    assert not match_rule("Read(./a.py)", "Write", {"file_path": "a.py"}, tmp_path)
    assert match_rule("*", "Write", {"file_path": "a.py"}, tmp_path)
