"""Skill（技能包）的加载、清单生成与 Skill 工具。"""
from __future__ import annotations

import asyncio

from yotcode.config import Config
from yotcode.skills import load_skills, skill_body, skill_listing
from yotcode.tools.base import ToolContext
from yotcode.tools.skill_tools import SkillTool


def _write_skill(root, name: str, frontmatter: str, body: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8"
    )


def test_load_skill_from_project_dir(tmp_path):
    _write_skill(
        tmp_path / ".yotcode" / "skills",
        "release",
        "name: release\ndescription: 发布流程",
        "# 发布\n\n先跑测试。",
    )
    skills = load_skills(tmp_path, tmp_path / "home")
    assert "release" in skills
    assert skills["release"].description == "发布流程"
    assert "先跑测试" in skill_body(skills["release"])


def test_project_skill_overrides_user_skill(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / ".yotcode" / "skills", "dup",
                 "name: dup\ndescription: 用户级", "用户级正文")
    _write_skill(tmp_path / ".yotcode" / "skills", "dup",
                 "name: dup\ndescription: 项目级", "项目级正文")

    skills = load_skills(tmp_path, home)
    assert skills["dup"].description == "项目级"
    assert "项目级正文" in skill_body(skills["dup"])


def test_listing_only_contains_metadata(tmp_path):
    """清单里只能有名字和描述，不能把正文塞进去（那会毁掉渐进式披露）。"""
    _write_skill(
        tmp_path / ".yotcode" / "skills",
        "big",
        "name: big\ndescription: 一个很长的技能",
        "正文" + "X" * 5000,
    )
    listing = skill_listing(tmp_path, tmp_path / "home")
    assert "big" in listing
    assert "一个很长的技能" in listing
    assert "X" * 100 not in listing


def test_skill_body_expands_at_file_imports(tmp_path):
    directory = tmp_path / ".yotcode" / "skills" / "release"
    directory.mkdir(parents=True)
    (directory / "helper.txt").write_text("辅助内容", encoding="utf-8")
    (directory / "SKILL.md").write_text(
        "---\nname: release\ndescription: d\n---\n\n# 步骤\n@helper.txt\n", encoding="utf-8"
    )
    skills = load_skills(tmp_path, tmp_path / "home")
    body = skill_body(skills["release"])
    assert "辅助内容" in body
    assert "@helper.txt" not in body


async def test_skill_tool_loads_body(tmp_path):
    _write_skill(
        tmp_path / ".yotcode" / "skills",
        "release",
        "name: release\ndescription: 发布流程\nallowed-tools: Bash, Read",
        "# 发布\n\n记得先跑测试。",
    )
    ctx = ToolContext(
        cwd=tmp_path,
        config=Config(cwd=tmp_path),
        cancel=asyncio.Event(),
    )
    tool = SkillTool()
    result = await tool.run({"name": "release"}, ctx)
    assert not result.is_error
    assert "记得先跑测试" in result.text
    assert "Bash" in result.text  # 允许的工具会一起交给模型


async def test_skill_tool_reports_unknown_name(tmp_path):
    ctx = ToolContext(
        cwd=tmp_path,
        config=Config(cwd=tmp_path),
        cancel=asyncio.Event(),
    )
    result = await SkillTool().run({"name": "nope"}, ctx)
    assert result.is_error
    assert "No skill named" in result.text

