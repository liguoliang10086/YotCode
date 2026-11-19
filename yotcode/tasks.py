"""任务编排：把「一个大任务拆成若干带依赖的小任务」这件事交给代码来做。

背景：原来只有 TodoWrite，模型自己在脑子里拆、自己在脑子里排顺序，
代码全程不参与编排。这里补上一套真正的调度器：

  - **依赖关系**：任务 A 依赖 B，那 B 没跑完 A 就不该跑；
  - **自动调度**：每轮挑出所有"依赖已满足"的任务，**并发**执行；
  - **失败重试**：某个任务失败会自动重试，超过次数才判定失败；
  - **失败传播**：依赖失败任务的下游直接标记为跳过，不会白跑；
  - **任务 → 子代理自动绑定**：每个任务默认交给一个子代理去干，
    子代理有自己的上下文，主对话不会被一堆搜索结果撑爆；
  - **结果传递**：上游任务的报告会拼进下游任务的提示词里。

数据流：

    模型调用 TaskPlan 工具，声明一张任务图
        ↓
    TaskGraph 校验（未知依赖 / 环 / 空图）
        ↓
    调度循环：就绪任务并发跑 → 收集结果 → 唤醒下游 → 下一轮
        ↓
    汇总报告返回给模型
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 任务状态
PENDING = "pending"    # 还没开始（等依赖）
RUNNING = "running"    # 正在跑
DONE = "done"          # 成功
FAILED = "failed"      # 失败（重试用尽）
SKIPPED = "skipped"    # 跳过（依赖的上游失败了）

# 状态的中文名，给用户看的
STATUS_LABELS = {
    PENDING: "待执行",
    RUNNING: "执行中",
    DONE: "已完成",
    FAILED: "失败",
    SKIPPED: "已跳过",
}


@dataclass
class TaskNode:
    """任务图里的一个节点。"""

    id: str
    title: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    subagent_type: str = "general-purpose"
    max_attempts: int = 2          # 总共试几次（含第一次）
    status: str = PENDING
    attempts: int = 0
    result: str = ""               # 成功时的报告
    error: str = ""                # 失败时的原因

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass
class TaskGraph:
    """一张任务图：节点 + 依赖边。"""

    nodes: dict[str, TaskNode] = field(default_factory=dict)

    # -- 构造 --------------------------------------------------------------
    @classmethod
    def from_spec(cls, items: list[dict]) -> TaskGraph:
        """从模型给的原始列表构造任务图。

        每项支持：id / title / prompt / depends_on / subagent_type / max_attempts。
        """
        graph = cls()
        for index, raw in enumerate(items or []):
            raw = raw or {}
            task_id = str(raw.get("id") or f"t{index + 1}").strip() or f"t{index + 1}"
            title = str(raw.get("title") or raw.get("description") or task_id).strip()
            prompt = str(raw.get("prompt") or title).strip()
            depends = raw.get("depends_on") or raw.get("dependsOn") or []
            if isinstance(depends, str):
                depends = [d.strip() for d in depends.split(",") if d.strip()]
            elif not isinstance(depends, list):
                depends = []
            try:
                attempts = int(raw.get("max_attempts") or 2)
            except (TypeError, ValueError):
                attempts = 2
            graph.nodes[task_id] = TaskNode(
                id=task_id,
                title=title,
                prompt=prompt,
                depends_on=[str(d) for d in depends],
                subagent_type=str(raw.get("subagent_type")
                                  or raw.get("subagentType") or "general-purpose"),
                max_attempts=max(1, min(attempts, 5)),
            )
        return graph

    # -- 校验 --------------------------------------------------------------
    def validate(self) -> str | None:
        """校验任务图。返回错误说明，None 表示没问题。"""
        if not self.nodes:
            return "任务列表为空。"
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    return (f"任务 {node.id} 依赖了不存在的任务 {dep}。"
                            f"可用的任务 id：{', '.join(sorted(self.nodes))}")
                if dep == node.id:
                    return f"任务 {node.id} 依赖了自己。"
        cycle = self.find_cycle()
        if cycle:
            return "任务依赖存在环：" + " -> ".join(cycle)
        return None

    def find_cycle(self) -> list[str] | None:
        """用 DFS 找依赖环，找不到返回 None。

        颜色标记法：0=没访问过，1=在当前路径上，2=已完成。
        遇到颜色 1 的节点说明走回了自己 → 有环。
        """
        color: dict[str, int] = {node_id: 0 for node_id in self.nodes}
        stack: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            color[node_id] = 1
            stack.append(node_id)
            for dep in self.nodes[node_id].depends_on:
                if color.get(dep) == 1:
                    # 找到环：从 dep 出现在栈里的位置截断
                    index = stack.index(dep)
                    return stack[index:] + [dep]
                if color.get(dep) == 0:
                    found = visit(dep)
                    if found:
                        return found
            stack.pop()
            color[node_id] = 2
            return None

        for node_id in self.nodes:
            if color.get(node_id) == 0:
                found = visit(node_id)
                if found:
                    return found
        return None

    # -- 调度辅助 ----------------------------------------------------------
    def ready(self) -> list[TaskNode]:
        """所有"依赖都已完成"、可以立刻执行的任务。

        依赖里只要有一个失败或被跳过，这个任务就永远不该跑——
        那种情况由 mark_unreachable 统一标记成 skipped。
        """
        result: list[TaskNode] = []
        for node in self.nodes.values():
            if node.status != PENDING:
                continue
            if all(self.nodes[dep].status == DONE for dep in node.depends_on):
                result.append(node)
        return result

    def mark_unreachable(self) -> list[TaskNode]:
        """把"上游挂了所以永远轮不到"的任务标记成跳过。"""
        skipped: list[TaskNode] = []
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.status != PENDING:
                    continue
                if any(self.nodes[dep].status in (FAILED, SKIPPED)
                       for dep in node.depends_on):
                    node.status = SKIPPED
                    node.error = "依赖的任务失败了，跳过。"
                    skipped.append(node)
                    changed = True
        return skipped

    def pending_count(self) -> int:
        return sum(1 for n in self.nodes.values()
                   if n.status in (PENDING, RUNNING))

    def report(self) -> str:
        """生成给模型看的汇总报告。"""
        lines = ["# 任务编排结果", ""]
        for node in self.nodes.values():
            mark = {
                DONE: "OK", FAILED: "FAIL", SKIPPED: "SKIP",
                RUNNING: "RUN", PENDING: "PEND",
            }.get(node.status, node.status)
            lines.append(f"[{mark}] {node.id} {node.title}"
                         f"（尝试 {node.attempts} 次）")
            if node.error:
                lines.append(f"      错误：{node.error}")
        # 附上每个成功任务的报告摘要，方便模型直接用
        for node in self.nodes.values():
            if node.status == DONE and node.result:
                lines.append("")
                lines.append(f"## {node.id} {node.title}")
                lines.append(node.result)
        return "\n".join(lines)

    def status_line(self) -> str:
        """一行状态，给 UI 实时显示用。"""
        done = sum(1 for n in self.nodes.values() if n.status == DONE)
        failed = sum(1 for n in self.nodes.values() if n.status == FAILED)
        skipped = sum(1 for n in self.nodes.values() if n.status == SKIPPED)
        total = len(self.nodes)
        return f"{done}/{total} 完成" + (
            f" · {failed} 失败" if failed else "") + (
            f" · {skipped} 跳过" if skipped else "")

