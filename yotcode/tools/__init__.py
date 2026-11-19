"""Built-in tool registry."""
from __future__ import annotations

import fnmatch
from typing import Any

from .base import Tool, ToolContext, ToolResult
from .file_tools import (
    EditTool,
    GlobTool,
    GrepTool,
    LSTool,
    MultiEditTool,
    ReadTool,
    WriteTool,
)
from .interaction import AskUserQuestionTool, ExitPlanModeTool, TaskTool
from .mcp import MCPManager, MCPTool
from .orchestration import TaskPlanTool
from .planning import TodoWriteTool
from .shell_tools import (
    BashOutputTool,
    BashTool,
    KillShellTool,
    ShellManager,
)
from .skill_tools import SkillTool
from .web_tools import WebFetchTool, WebSearchTool

__all__ = [
    "Tool", "ToolContext", "ToolResult", "ShellManager", "MCPManager", "MCPTool",
    "create_tools", "filter_tools", "READ_ONLY_NAMES",
]

READ_ONLY_NAMES = {
    "Read", "LS", "Glob", "Grep", "BashOutput", "WebFetch", "WebSearch",
    "Task", "AskUserQuestion", "ExitPlanMode", "Skill", "TaskPlan",
}


def create_tools() -> list[Tool]:
    return [
        ReadTool(),
        WriteTool(),
        EditTool(),
        MultiEditTool(),
        LSTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        BashOutputTool(),
        KillShellTool(),
        WebFetchTool(),
        WebSearchTool(),
        TodoWriteTool(),
        TaskTool(),
        AskUserQuestionTool(),
        ExitPlanModeTool(),
        SkillTool(),
        TaskPlanTool(),
    ]


def filter_tools(
    tools: list[Tool],
    *,
    allowed: list[str] | None = None,
    disallowed: list[str] | None = None,
    permission_mode: str = "default",
) -> list[Tool]:
    result: list[Tool] = []
    allowed = [a for a in (allowed or []) if a]
    disallowed = [d for d in (disallowed or []) if d]
    for tool in tools:
        name = tool.name
        if allowed and not any(fnmatch.fnmatch(name, pattern) for pattern in allowed):
            continue
        if disallowed and any(fnmatch.fnmatch(name, pattern) for pattern in disallowed):
            continue
        result.append(tool)
    return result
