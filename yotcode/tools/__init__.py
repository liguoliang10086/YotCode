"""内置工具注册中心：把所有工具装配成一份清单交给 Agent。

Built-in tool registry.

create_tools() 在这里 new 出全部内置工具（Read / Write / Edit / Bash / Grep 等），
再按配置里的白名单 / 黑名单过滤。Agent 拿到清单后，把每个工具的 name、
description、input_schema 告诉大模型；模型请求某个工具时，Agent 按名字从这份
清单里找到对应实例并调用它的 run()。

想加一个新工具：写好子类后，来这里的 create_tools() 里注册一次即可。
"""
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
