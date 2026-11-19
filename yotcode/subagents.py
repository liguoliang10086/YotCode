"""子代理定义：内置 general-purpose / Explore + .yotcode/agents/*.md。

子代理拥有独立上下文窗口，适合“搜索整个仓库”“独立调查一个模块”等
会污染主对话的任务。自定义文件用 frontmatter 指定 description、tools、
model；tools 全是只读工具时会被标记为 read_only。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import home_dir, project_config_dir
from .utils import parse_frontmatter, read_text

EXPLORE_TOOLS = [
    "Read", "LS", "Glob", "Grep", "WebFetch", "WebSearch", "BashOutput", "TodoWrite",
]


@dataclass
class SubagentSpec:
    name: str
    description: str
    prompt: str = ""
    tools: list[str] = field(default_factory=list)  # empty = all
    model: str | None = None

    @property
    def read_only(self) -> bool:
        return bool(self.tools) and all(t in EXPLORE_TOOLS for t in self.tools)


def builtin_subagents() -> dict[str, SubagentSpec]:
    return {
        "general-purpose": SubagentSpec(
            name="general-purpose",
            description="General agent with access to all tools for multi-step tasks.",
        ),
        "Explore": SubagentSpec(
            name="Explore",
            description="Fast read-only agent for searching and understanding codebases.",
            tools=list(EXPLORE_TOOLS),
            prompt=(
                "You are a read-only exploration agent. Search the codebase and report "
                "exactly what you find. Never modify files. Return a concise report with "
                "file paths and line numbers."
            ),
        ),
    }


def _agent_dirs(cwd: Path) -> list[Path]:
    home = Path.home()
    return [
        project_config_dir(cwd) / "agents",
        Path(cwd) / ".claude" / "agents",
        home_dir() / "agents",
        home / ".claude" / "agents",
    ]


def load_subagents(cwd: Path) -> dict[str, SubagentSpec]:
    agents = builtin_subagents()
    for directory in _agent_dirs(cwd):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text, _, _ = read_text(path)
            except OSError:
                continue
            meta, body = parse_frontmatter(text)
            name = str(meta.get("name") or path.stem)
            tools = meta.get("tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            elif not isinstance(tools, list):
                tools = []
            agents[name] = SubagentSpec(
                name=name,
                description=str(meta.get("description") or ""),
                prompt=body.strip(),
                tools=[str(t) for t in tools],
                model=str(meta["model"]) if meta.get("model") else None,
            )
    return agents


def subagent_listing(cwd: Path) -> str:
    agents = load_subagents(cwd)
    lines = ["Available sub-agents:", ""]
    for name, spec in agents.items():
        ro = " (read-only)" if spec.read_only else ""
        lines.append(f"- {name}{ro}: {spec.description or 'no description'}")
    return "\n".join(lines)
