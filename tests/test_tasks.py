"""任务编排：任务图校验 + 调度器行为。"""
from __future__ import annotations

import asyncio
import time

from yotcode.tasks import DONE, FAILED, SKIPPED, TaskGraph
from yotcode.tools.orchestration import TaskPlanTool, run_graph


class FakeAgent:
    """假的 Agent：只实现 run_subagent，用来验证调度逻辑。"""

    def __init__(self, fail_ids=(), delay: float = 0.0) -> None:
        self.fail_ids = set(fail_ids)
        self.delay = delay
        self.calls: list[tuple[str, str, str]] = []
        self.max_concurrent = 0
        self._running = 0

    async def run_subagent(self, description, prompt, agent_type="general-purpose",
                           raise_on_error: bool = False) -> str:
        self.calls.append((description, prompt, agent_type))
        self._running += 1
        self.max_concurrent = max(self.max_concurrent, self._running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            task_id = description.split()[0] if description else ""
            if task_id in self.fail_ids:
                raise RuntimeError(f"{task_id} 故意失败")
            return f"{task_id} 的报告"
        finally:
            self._running -= 1


# -- 任务图校验 ----------------------------------------------------------
def test_graph_rejects_unknown_dependency():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a", "depends_on": ["nope"]},
    ])
    error = graph.validate()
    assert error is not None
    assert "nope" in error


def test_graph_rejects_cycle():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a", "depends_on": ["t2"]},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
    ])
    error = graph.validate()
    assert error is not None
    assert "环" in error


def test_graph_rejects_self_dependency():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a", "depends_on": ["t1"]},
    ])
    assert graph.validate() is not None


def test_graph_accepts_valid_dag():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a"},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
        {"id": "t3", "prompt": "c", "depends_on": ["t1"]},
    ])
    assert graph.validate() is None
    # t1 没有任何依赖，所以第一个就绪
    ready = graph.ready()
    assert [n.id for n in ready] == ["t1"]


# -- 调度器 --------------------------------------------------------------
async def test_runs_independent_tasks_concurrently():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a"},
        {"id": "t2", "prompt": "b"},
        {"id": "t3", "prompt": "c"},
    ])
    agent = FakeAgent(delay=0.2)
    begin = time.perf_counter()
    report = await run_graph(graph, agent, max_parallel=3)
    elapsed = time.perf_counter() - begin

    assert all(n.status == DONE for n in graph.nodes.values())
    # 三个并发跑，耗时应接近单个任务的耗时，而不是三倍
    assert elapsed < 0.2 * 2, f"没有并发执行，耗时 {elapsed:.3f}s"
    assert agent.max_concurrent == 3
    assert "[OK]" in report


async def test_dependency_is_respected():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a"},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
    ])
    agent = FakeAgent()
    await run_graph(graph, agent)
    order = [c[0].split()[0] for c in agent.calls]
    assert order.index("t1") < order.index("t2")


async def test_upstream_result_is_passed_downstream():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a"},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
    ])
    agent = FakeAgent()
    await run_graph(graph, agent)
    t2_prompt = agent.calls[1][1]
    assert "t1 的报告" in t2_prompt


async def test_failure_is_retried_then_marked_failed():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a", "max_attempts": 3},
    ])
    agent = FakeAgent(fail_ids={"t1"})
    await run_graph(graph, agent)
    node = graph.nodes["t1"]
    assert node.status == FAILED
    assert node.attempts == 3


async def test_failure_skips_dependents():
    graph = TaskGraph.from_spec([
        {"id": "t1", "prompt": "a", "max_attempts": 1},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
    ])
    agent = FakeAgent(fail_ids={"t1"})
    await run_graph(graph, agent)
    assert graph.nodes["t1"].status == FAILED
    assert graph.nodes["t2"].status == SKIPPED
    # t2 不该真的被跑起来
    assert not any(c[0].startswith("t2") for c in agent.calls)


async def test_tool_reports_validation_error(tmp_path):
    from yotcode.config import Config
    from yotcode.tools.base import ToolContext

    ctx = ToolContext(cwd=tmp_path, config=Config(cwd=tmp_path),
                      cancel=asyncio.Event())
    tool = TaskPlanTool()
    result = await tool.run({"tasks": [
        {"id": "t1", "prompt": "a", "depends_on": ["ghost"]},
    ]}, ctx)
    assert result.is_error
    assert "ghost" in result.text


async def test_tool_runs_graph(tmp_path):
    from yotcode.config import Config
    from yotcode.tools.base import ToolContext

    ctx = ToolContext(cwd=tmp_path, config=Config(cwd=tmp_path),
                      cancel=asyncio.Event())
    agent = FakeAgent()
    ctx.agent = agent  # type: ignore[assignment]
    tool = TaskPlanTool()
    result = await tool.run({"tasks": [
        {"id": "t1", "prompt": "a"},
        {"id": "t2", "prompt": "b", "depends_on": ["t1"]},
    ]}, ctx)
    assert not result.is_error
    assert "[OK]" in result.text

