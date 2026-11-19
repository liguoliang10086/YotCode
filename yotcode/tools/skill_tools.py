"""Skill 工具：让模型按需加载技能包正文。

system prompt 里只放 skill 的名字和描述（几十 token），
模型判断某个 skill 和当前任务相关时，才调用这个工具把正文读进来。
这就是「渐进式披露」——不相关的 skill 永远不会占用上下文。
"""
from __future__ import annotations

from typing import Any

from ..skills import load_skills, skill_body
from .base import Tool, ToolContext, ToolResult


class SkillTool(Tool):
    name = "Skill"
    description = (
        "Load the full instructions of a skill listed in the system prompt. A skill is a "
        "reusable playbook or knowledge pack stored as a markdown file (SKILL.md). Call "
        "this only when a skill's description matches the current task, then follow the "
        "instructions it returns."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name, exactly as listed in the system prompt.",
            },
        },
        "required": ["name"],
    }
    # 只是读文件，不改任何东西，也不需要弹权限窗 → 可以和别的工具并发
    read_only = True
    requires_permission = False
    parallel_safe = True

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("name") or "skill")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(data.get("name") or "").strip()
        if not name:
            return self.error("Skill name is required.")
        skills = load_skills(ctx.cwd, ctx.config.home)
        spec = skills.get(name)
        if spec is None:
            # 名字对不上时把可选项列出来，模型可以自己纠正
            available = ", ".join(sorted(skills)) or "(none)"
            return self.error(
                f"No skill named '{name}'. Available skills: {available}."
            )
        body = skill_body(spec)
        if not body.strip():
            return self.error(f"Skill '{name}' is empty.")
        header = [f"# Skill: {spec.name}"]
        if spec.description:
            header.append(spec.description)
        if spec.allowed_tools:
            header.append(
                "Suggested tools for this skill: " + ", ".join(spec.allowed_tools)
            )
        header.append(f"Source: {spec.path}")
        return self.ok("\n".join(header) + "\n\n" + body)

