"""交互类工具：Task（启动子代理）、AskUserQuestion、ExitPlanMode。

这些工具没有实际“副作用”，但会让 Agent 暂停等待用户或子代理返回，
因此需要 UI 回调配合（见 Agent.ask_user / request_plan_approval）。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolResult


class TaskTool(Tool):
    name = "Task"
    requires_permission = False
    # plan 模式下子代理会继承只读限制，因此允许用它做调查。
    plan_mode_ok = True
    # 子代理有自己的上下文和工具集，互不干扰。
    # 同一轮里请求多个 Task 时它们会真正并发执行（各自独立跑完整循环），
    # 所以"同时派 3 个代理去查 3 个不同目录"这种用法才真正省时间。
    # 权限弹窗由 Agent 侧的一把共享锁串行化，不会并发弹窗。
    parallel_safe = True
    description = (
        "Launch a sub-agent to handle a self-contained piece of work and return its "
        "final report. The sub-agent has its own context window, so this is ideal for "
        "broad exploration ('find every place that uses X') or parallel research; it "
        "keeps your main context small. Give it a detailed, self-sufficient prompt -- "
        "it cannot see our conversation. subagent_type: 'general-purpose' (all tools) or "
        "'Explore' (read-only, fast). Several Task calls in the same turn run "
        "concurrently, so use one call per independent area to research."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short 3-5 word task label."},
            "prompt": {"type": "string", "description": "Complete, self-contained instructions."},
            "subagent_type": {
                "type": "string",
                "description": "general-purpose | Explore | a custom agent name from .yotcode/agents.",
            },
        },
        "required": ["description", "prompt"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("description") or "subagent")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return self.error("prompt is required")
        agent_type = str(data.get("subagent_type") or "general-purpose")
        if ctx.agent is None:
            return self.error("subagents are not available in this context")
        try:
            report = await ctx.agent.run_subagent(
                description=str(data.get("description") or "subtask"),
                prompt=prompt,
                agent_type=agent_type,
            )
        except Exception as exc:
            return self.error(f"Sub-agent failed: {exc}")
        return self.ok(report, subagent_type=agent_type)


class AskUserQuestionTool(Tool):
    name = "AskUserQuestion"
    requires_permission = False
    plan_mode_ok = True
    description = (
        "Ask the user a question with 1-4 multiple-choice options when you are genuinely "
        "blocked on a decision that only they can make (ambiguous requirements, taste, "
        "credentials). Do not use it for questions you can answer yourself. Users can "
        "always type a custom answer."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask."},
            "header": {"type": "string", "description": "Very short label (max 20 chars)."},
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label"],
                },
            },
            "multi_select": {"type": "boolean"},
        },
        "required": ["question", "options"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("question") or "")

    def validate(self, data: dict[str, Any]) -> str | None:
        options = data.get("options")
        if not isinstance(options, list) or len(options) < 2:
            return "options must contain at least 2 choices"
        if len(options) > 4:
            return "options must contain at most 4 choices"
        return None

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.agent is None:
            return self.error("user interaction is not available")
        answer = await ctx.agent.ask_user(
            question=str(data.get("question") or ""),
            header=str(data.get("header") or "Question"),
            options=[str(o.get("label") if isinstance(o, dict) else o) for o in data.get("options") or []],
            descriptions=[
                str(o.get("description") or "") if isinstance(o, dict) else ""
                for o in data.get("options") or []
            ],
            multi_select=bool(data.get("multi_select")),
        )
        if answer is None:
            return ToolResult("The user did not answer (cancelled).", is_error=True)
        return self.ok(f"User answered: {answer}", answer=answer)


class ExitPlanModeTool(Tool):
    name = "ExitPlanMode"
    requires_permission = False
    # 关键：plan 模式下必须先允许调用它，否则永远无法请求退出计划模式。
    plan_mode_ok = True
    description = (
        "Call this when you have finished researching and have written a plan, to ask "
        "the user to approve it and switch from plan mode to implementation. The plan "
        "should be included in your message before calling this tool."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "plan": {"type": "string", "description": "The plan you are proposing."},
        },
    }

    def summary(self, data: dict[str, Any]) -> str:
        return "Plan ready for approval"

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.agent is None:
            return self.error("plan mode is not available")
        approved = await ctx.agent.request_plan_approval(str(data.get("plan") or ""))
        if approved:
            ctx.agent.set_permission_mode("acceptEdits")
            return self.ok(
                "User approved the plan. Plan mode is now off -- you may implement it. "
                "Keep the user informed as you go."
            )
        return ToolResult(
            "User rejected the plan. Stay in plan mode, revise your approach and ask "
            "again when ready.",
            is_error=True,
        )
