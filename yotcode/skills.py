"""Skill（技能包）：把一套可复用的流程/知识写成文件，模型按需加载。

思路和 Claude Code 的 Skills 一致，叫「渐进式披露」：

  1. 启动时只在 system prompt 里放每个 skill 的 name + description（几十 token）；
  2. 模型判断某个 skill 和当前任务相关时，调用 Skill 工具把正文读进来；
  3. 这样既不会让无关知识污染上下文，也不会白白占满 token。

为什么需要它：CLAUDE.md 是「无条件全量塞进 system prompt」的，
项目一大就又长又吵，而且模型分不清哪段记忆和当前任务有关。
Skill 把它拆成一个个独立的小包，由模型自己决定要不要打开。

文件格式（目录任选其一，项目级优先于用户级）：

    .yotcode/skills/<名字>/SKILL.md
    .claude/skills/<名字>/SKILL.md
    ~/.yotcode/skills/<名字>/SKILL.md
    ~/.claude/skills/<名字>/SKILL.md

内容示例：

    ---
    name: release
    description: 发布流程：跑测试、打 tag、写 changelog
    allowed-tools: Bash, Read
    ---

    # 发布步骤

    1. 先跑 `pytest -q`
    2. 再按 CHANGELOG 更新版本号
    @scripts/release.py

约定：
  - name 缺省用目录名；
  - description 强烈建议写清楚「什么时候该用它」——它是模型唯一的判断依据；
  - allowed-tools 可选，目前只是随正文一起交给模型的提示，不做强制拦截；
  - 正文里可以单独一行写 `@相对路径` 导入其它文件（相对 skill 所在目录）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import home_dir, project_config_dir
from .memory import expand_imports
from .utils import parse_frontmatter, read_text

# skill 正文最多读多少字节，防止一个手滑写进巨型文件把上下文撑爆
MAX_SKILL_BYTES = 200_000


@dataclass
class SkillSpec:
    """一个技能包的元信息。body 是正文（按需加载时才读）。"""

    name: str
    description: str
    path: Path
    body: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    source: str = ""  # 来自哪个目录，方便 UI 展示来源

    @property
    def directory(self) -> Path:
        return self.path.parent


def skill_dirs(cwd: Path, home: Path | None = None) -> list[Path]:
    """返回所有会被扫描的 skill 目录，按优先级从高到低排列。

    项目级目录排在用户级前面：同名 skill 以先扫到的为准，
    也就是项目里的定义会覆盖用户全局的定义。
    """
    base = Path(cwd)
    user_home = Path(home) if home is not None else home_dir()
    return [
        project_config_dir(base) / "skills",
        base / ".claude" / "skills",
        user_home / "skills",
        user_home / ".claude" / "skills",
    ]


def _read_skill_file(path: Path) -> tuple[dict, str]:
    try:
        if path.stat().st_size > MAX_SKILL_BYTES:
            text, _, _ = read_text(path)
            raw = text[:MAX_SKILL_BYTES] + "\n... (skill file truncated)"
        else:
            raw, _, _ = read_text(path)
    except OSError:
        return {}, ""
    meta, body = parse_frontmatter(raw)
    return meta, body


def load_skills(cwd: Path, home: Path | None = None) -> dict[str, SkillSpec]:
    """扫描所有 skill 目录，返回 {名字: SkillSpec}。

    同名 skill 只保留优先级最高的那个（项目级覆盖用户级）。
    只读元信息（frontmatter），正文留到真正调用 Skill 工具时才读。
    """
    skills: dict[str, SkillSpec] = {}
    for directory in skill_dirs(cwd, home):
        if not directory.is_dir():
            continue
        # 两种放法都支持：<目录>/SKILL.md 和 <目录>/<名字>.md
        candidates = sorted(directory.glob("*/SKILL.md"))
        candidates += sorted(directory.glob("*.md"))
        for path in candidates:
            meta, _ = _read_skill_file(path)
            # SKILL.md 用它所在的目录名兜底，普通 .md 用文件名兜底
            default_name = path.parent.name if path.name == "SKILL.md" else path.stem
            name = str(meta.get("name") or default_name).strip()
            if not name:
                continue
            if name in skills:
                # 已经有更高优先级的同名 skill 了，跳过
                continue
            tools = meta.get("allowed-tools") or meta.get("allowed_tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            elif not isinstance(tools, list):
                tools = []
            skills[name] = SkillSpec(
                name=name,
                description=str(meta.get("description") or "").strip(),
                path=path,
                allowed_tools=[str(t) for t in tools],
                source=str(directory),
            )
    return skills


def skill_body(spec: SkillSpec) -> str:
    """读取 skill 正文，并把 `@相对路径` 展开成文件内容。"""
    meta, body = _read_skill_file(spec.path)
    if not body.strip():
        return ""
    # 相对路径以 skill 文件所在目录为基准
    return expand_imports(body, spec.path.parent).strip()


def skill_listing(cwd: Path, home: Path | None = None) -> str:
    """生成放进 system prompt 的清单：只有名字和描述，不含正文。

    这是「渐进式披露」的关键——几百个 skill 也只占几十 token，
    正文要等模型自己决定要用时再通过 Skill 工具读。
    """
    skills = load_skills(cwd, home)
    if not skills:
        return ""
    lines = [
        "# Skills",
        "",
        "这些是可复用的流程/知识包。只有当任务和某个 skill 相关时，"
        "才调用 Skill 工具把它的正文读进来；不需要就完全不要碰。",
        "",
    ]
    for name, spec in skills.items():
        desc = spec.description or "(no description)"
        tools = ""
        if spec.allowed_tools:
            tools = f"  (建议只用这些工具: {', '.join(spec.allowed_tools)})"
        lines.append(f"- {name}: {desc}{tools}")
    return "\n".join(lines)
 
