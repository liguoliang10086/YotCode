"""计划类工具：TodoWrite。

模型的待办列表会同步到 TUI 的 TodoPanel，并写入会话 JSONL；
状态只允许 pending / in_progress / completed 三种。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult

VALID_STATUS = {"pending", "in_progress", "completed"}


class TodoWriteTool(Tool):
    name = "TodoWrite"
    requires_permission = False
    plan_mode_ok = True
    description = (
        "Create and maintain a structured task list for the current session. Use it for "
        "multi-step or non-trivial work (3+ steps) so the user can follow progress. "
        "Update it as you go: mark a task in_progress when starting it (exactly one at a "
        "time), completed immediately when done. Skip it for trivial single-step tasks."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The complete todo list (it replaces the previous one).",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Imperative form, e.g. 'Run tests'."},
                        "status": {"type": "string", "enum": sorted(VALID_STATUS)},
                        "activeForm": {"type": "string", "description": "Present continuous, e.g. 'Running tests'."},
                    },
                    "required": ["content", "status", "activeForm"],
                },
            }
        },
        "required": ["todos"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        todos = data.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return f"{done}/{len(todos)} tasks"

    def validate(self, data: dict[str, Any]) -> str | None:
        todos = data.get("todos")
        if not isinstance(todos, list):
            return "todos must be an array"
        if len(todos) > 100:
            return "too many todos (max 100)"
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return f"todos[{index}] must be an object"
            if not str(todo.get("content") or "").strip():
                return f"todos[{index}].content is required"
            if todo.get("status") not in VALID_STATUS:
                return f"todos[{index}].status must be one of {sorted(VALID_STATUS)}"
            if not str(todo.get("activeForm") or "").strip():
                return f"todos[{index}].activeForm is required"
        return None

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = [
            {
                "content": str(todo["content"]).strip(),
                "status": todo["status"],
                "activeForm": str(todo["activeForm"]).strip(),
            }
            for todo in data.get("todos") or []
        ]
        if ctx.agent is not None:
            ctx.agent.set_todos(todos)
        return self.ok(
            "Todos have been modified successfully. Keep using the todo list to track "
            "your progress; mark items completed as soon as they are done.",
            todos=todos,
        )
