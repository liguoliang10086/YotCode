"""并发工具执行的测试。

覆盖四件事：
  1. 标了 parallel_safe 的工具在同一轮里真的会并发跑（总耗时 ≈ 最慢那一个）；
  2. 回填给模型的 tool_result 顺序必须和模型请求的顺序一致；
  3. 有副作用的工具仍然串行，不会和别的工具抢同一个文件；
  4. 用户打断后，剩下的工具要明确标记成"未执行"，不能静默丢掉。
"""
from __future__ import annotations

import asyncio
import time

from yotcode.agent import Agent
from yotcode.config import Config
from yotcode.tools.base import Tool, ToolResult
from yotcode.types import ToolUseBlock


class _FakeProvider:
    """并发测试用不到真实模型，这里只要能构造出 Agent 即可。"""

    name = "fake"

    async def complete(self, **kwargs):  # pragma: no cover - 测试里不会调用
        return ""

    async def aclose(self) -> None:
        return None


class _ParallelTool(Tool):
    """假的"只读"工具，只用来告诉 Agent 这个工具名可以并发。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.parallel_safe = True
        self.read_only = True
        self.requires_permission = False

    async def run(self, data, ctx):  # pragma: no cover - 测试里不会真的调用
        return ToolResult("ok")


class _SerialTool(_ParallelTool):
    """假的"有副作用"工具。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.parallel_safe = False
        self.read_only = False


def _agent(tmp_path) -> Agent:
    return Agent(Config(cwd=tmp_path), _FakeProvider(), [])


def _block(name: str, index: int) -> ToolUseBlock:
    return ToolUseBlock(id=f"toolu_{index}", name=name, input={"i": index})


async def test_parallel_safe_tools_overlap(tmp_path, monkeypatch):
    """三个只读工具应该同时跑起来，总耗时远小于三者之和。"""
    agent = _agent(tmp_path)
    started: list[str] = []
    finished: list[str] = []

    async def fake_execute(block: ToolUseBlock) -> ToolResult:
        started.append(block.id)
        await asyncio.sleep(0.15)
        finished.append(block.id)
        return ToolResult(f"done:{block.id}")

    events: list[str] = []

    async def fake_execute_tracked(block: ToolUseBlock) -> ToolResult:
        events.append(f"start:{block.id}")
        result = await fake_execute(block)
        events.append(f"end:{block.id}")
        return result

    monkeypatch.setattr(agent, "_execute_tool", fake_execute_tracked)
    for name in ("Read", "LS", "Glob"):
        agent.tools[name] = _ParallelTool(name)

    blocks = [_block("Read", 1), _block("LS", 2), _block("Glob", 3)]
    begin = time.perf_counter()
    results = await agent._execute_tool_batch(blocks)
    elapsed = time.perf_counter() - begin

    # 并发：三个都跑完了，且总耗时约等于单个工具的耗时
    assert len(started) == 3
    assert len(finished) == 3
    assert elapsed < 0.15 * 2, f"工具没有并发执行，耗时 {elapsed:.3f}s"
    # 真正的并发判据：三个工具都"开始了"之后，才有一个"结束"
    assert events[:3] == [f"start:{b.id}" for b in blocks], events
    assert len(results) == 3


async def test_results_keep_request_order(tmp_path, monkeypatch):
    """回填给模型的 tool_result 顺序必须和模型请求的顺序一致。"""
    agent = _agent(tmp_path)

    async def fake_execute(block: ToolUseBlock) -> ToolResult:
        # 故意让后面的先返回，验证 Agent 仍然按请求顺序组装结果
        await asyncio.sleep(0.05 * (5 - int(block.input["i"])))
        return ToolResult(f"done:{block.id}")

    monkeypatch.setattr(agent, "_execute_tool", fake_execute)
    for name in ("Read", "LS", "Glob"):
        agent.tools[name] = _ParallelTool(name)

    blocks = [_block("Read", 1), _block("LS", 2), _block("Glob", 3)]
    results = await agent._execute_tool_batch(blocks)

    assert [r.tool_use_id for r in results] == [b.id for b in blocks]


async def test_side_effect_tools_stay_sequential(tmp_path, monkeypatch):
    """Write 这类有副作用的工具必须夹在只读工具中间按序执行。"""
    agent = _agent(tmp_path)
    order: list[str] = []

    async def fake_execute(block: ToolUseBlock) -> ToolResult:
        order.append(f"start:{block.id}")
        await asyncio.sleep(0.02)
        order.append(f"end:{block.id}")
        return ToolResult(f"done:{block.id}")

    monkeypatch.setattr(agent, "_execute_tool", fake_execute)
    agent.tools["Read"] = _ParallelTool("Read")
    agent.tools["Write"] = _SerialTool("Write")

    blocks = [
        _block("Read", 1),
        _block("Write", 2),
        _block("Read", 3),
    ]
    results = await agent._execute_tool_batch(blocks)

    assert [r.tool_use_id for r in results] == [b.id for b in blocks]
    # Write 必须在两个 Read 之间完整跑完，不能和 Read 交错
    assert order.index("start:toolu_2") > order.index("end:toolu_1")
    assert order.index("end:toolu_2") < order.index("start:toolu_3")


async def test_cancel_marks_remaining_tools(tmp_path, monkeypatch):
    """用户打断后，剩下的工具要标记成"未执行"，而不是静默丢掉。"""
    agent = _agent(tmp_path)

    async def fake_execute(block: ToolUseBlock) -> ToolResult:
        return ToolResult(f"done:{block.id}")

    monkeypatch.setattr(agent, "_execute_tool", fake_execute)
    agent.tools["Read"] = _ParallelTool("Read")
    agent.cancel.set()

    blocks = [_block("Read", 1), _block("LS", 2)]
    results = await agent._execute_tool_batch(blocks)

    assert len(results) == 2
    assert all(r.is_error for r in results)
    assert "Not executed" in str(results[0].content)


async def test_parallel_safe_flags():
    """只读工具和 Task 标了可并发，写操作没有。"""
    from yotcode.tools.file_tools import GlobTool, GrepTool, LSTool, ReadTool, WriteTool
    from yotcode.tools.interaction import TaskTool
    from yotcode.tools.shell_tools import BashOutputTool, BashTool

    for tool_cls in (ReadTool, LSTool, GlobTool, GrepTool, BashOutputTool, TaskTool):
        assert tool_cls.parallel_safe is True, tool_cls.__name__

    for tool_cls in (WriteTool, BashTool):
        assert tool_cls.parallel_safe is False, tool_cls.__name__

