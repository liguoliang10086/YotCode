"""任务编排工具：模型声明一张任务图，代码负责调度执行。

和 `Task` 工具的区别：

  - `Task`      —— 一次开一个子代理，跑完返回，适合"一件事"；
  - `TaskPlan`  —— 一次声明一整张任务图（带依赖），由调度器并发执行、
                   自动重试、把上游结果传给下游，适合"一件大事拆成好几块"。

调度循环长这样：

    while 还有没跑完的任务:
        就绪 = 依赖都满足、还没跑过的任务
        if 没有就绪的:
            把"上游挂了所以轮不到"的标记成跳过，然后结束
        并发执行就绪的这一批（最多 max_parallel 个）
        成功 → 标记 done；失败 → 还没用完重试次数就再来一次
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..tasks import (
    DONE,
    FAILED,
    RUNNING,
    TaskGraph,
    TaskNode,
)
from .base import Tool, ToolContext, ToolResult

# 一批最多同时跑几个任务（子代理并发也不能无限开，否则会打爆 API 配额）
DEFAULT_MAX_PARALLEL = 4


class TaskPlanTool(Tool):
    name = "TaskPlan"
    description = (
        "Plan and execute a multi-step task as a dependency graph. Declare every "
        "sub-task up front (id, title, prompt, depends_on); the scheduler then runs "
        "independent tasks concurrently, waits for dependencies, passes each finished "
        "report into the tasks that depend on it, and retries failures. Each task is "
        "executed by a sub-agent with its own context. Use this for work that splits "
        "into several independent pieces; use the plain Task tool for a single piece."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "Sub-tasks to execute.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Unique short id, e.g. 't1'."},
                        "title": {"type": "string", "description": "Short label."},
                        "prompt": {
                            "type": "string",
                            "description": "Complete, self-contained instructions for the sub-agent.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids that must finish first. Empty = can start now.",
                        },
                        "subagent_type": {
                            "type": "string",
                            "description": "general-purpose (default) or Explore (read-only).",
                        },
                        "max_attempts": {
                            "type": "integer",
                            "description": "How many times to try (default 2, max 5).",
                        },
                    },
                    "required": ["id", "prompt"],
                },
            },
            "max_parallel": {
                "type": "integer",
                "description": "How many tasks to run at once (default 4).",
            },
        },
        "required": ["tasks"],
    }
    # 只是跑子代理，不直接改文件；并发安全
    read_only = True
    requires_permission = False
    parallel_safe = True

    def summary(self, data: dict[str, Any]) -> str:
        tasks = data.get("tasks") or []
        return f"{len(tasks)} 个任务"

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        graph = TaskGraph.from_spec(data.get("tasks") or [])
        error = graph.validate()
        if error:
            return self.error(error)
        agent = ctx.agent
        if agent is None:
            return self.error("当前环境不支持任务编排（没有可用的 Agent）。")
        try:
            max_parallel = int(data.get("max_parallel") or DEFAULT_MAX_PARALLEL)
        except (TypeError, ValueError):
            max_parallel = DEFAULT_MAX_PARALLEL
        max_parallel = max(1, min(max_parallel, 8))

        report = await run_graph(graph, agent, max_parallel=max_parallel)
        return self.ok(report)


async def run_graph(graph: TaskGraph, agent: Any, *,
                    max_parallel: int = DEFAULT_MAX_PARALLEL) -> str:
    """执行整张任务图，返回汇总报告。

    agent 需要提供 `run_subagent(description, prompt, agent_type)` 方法
    （见 yotcode/agent.py 的 Agent.run_subagent）。
    """
    while graph.pending_count() > 0:
        ready = graph.ready()
        if not ready:
            # 没有就绪的了 → 剩下的都是"上游挂了"，标记跳过
            graph.mark_unreachable()
            break
        batch = ready[:max_parallel]
        for node in batch:
            node.status = RUNNING
        # 这一批并发跑
        outcomes = await asyncio.gather(
            *(run_one_task(node, graph, agent) for node in batch),
            return_exceptions=True,
        )
        for node, outcome in zip(batch, outcomes):
            # run_one_task 内部已经把成功/失败状态写进 node 了，
            # 这里只兜底处理"连 run_one_task 本身都炸了"的情况
            if isinstance(outcome, BaseException):
                node.status = FAILED
                node.error = f"{type(outcome).__name__}: {outcome}"
    graph.mark_unreachable()
    return graph.report()


async def run_one_task(node: TaskNode, graph: TaskGraph, agent: Any) -> None:
    """跑一个任务，失败自动重试。

    重试次数用完还是失败 → 标记 FAILED，它的下游会被 mark_unreachable 跳过。
    """
    prompt = build_prompt(node, graph)
    last_error = ""
    for attempt in range(node.max_attempts):
        node.attempts = attempt + 1
        node.status = RUNNING
        try:
            report = await agent.run_subagent(
                node.title, prompt, node.subagent_type, raise_on_error=True,
            )
            node.result = report or ""
            node.status = DONE
            node.error = ""
            return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            node.error = last_error
            if attempt + 1 < node.max_attempts:
                # 还有重试机会，稍等一下再试（避免立刻打同一个坏点）
                await asyncio.sleep(0.5)
    node.status = FAILED
    node.error = last_error or "任务失败"


def build_prompt(node: TaskNode, graph: TaskGraph) -> str:
    """拼出子代理的提示词：任务本身 + 上游任务的报告。"""
    parts = [node.prompt]
    upstream = []
    for dep_id in node.depends_on:
        dep = graph.nodes.get(dep_id)
        if dep is None or dep.status != DONE:
            continue
        upstream.append(f"### 上游任务 {dep.id}：{dep.title}\n{dep.result}")
    if upstream:
        parts.append(
            "以下是已经完成的上游任务结果，请基于它们继续：\n\n"
            + "\n\n".join(upstream)
        )
    return "\n\n".join(parts)

