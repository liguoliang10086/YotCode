"""检查点：快照、列举、恢复。

分两层测：
  1. 存储层（CheckpointStore）：存、列、恢复、清理；
  2. Agent 层：改文件之前到底有没有自动留下检查点。
"""
from __future__ import annotations

from yotcode.checkpoints import CheckpointStore


def _store(tmp_path) -> CheckpointStore:
    # 项目目录一般是 <项目>/.yotcode
    return CheckpointStore(tmp_path / ".yotcode")


def test_save_and_list(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("原始内容", encoding="utf-8")

    checkpoint = store.save(session_id="s1", tool="Write", paths=[target])
    assert checkpoint is not None
    assert checkpoint.files[0].existed is True

    listed = store.list("s1")
    assert len(listed) == 1
    assert listed[0].id == checkpoint.id
    assert listed[0].tool == "Write"


def test_restore_existing_file(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("原始内容", encoding="utf-8")
    checkpoint = store.save(session_id="s1", tool="Edit", paths=[target])

    # 模拟 AI 把文件改坏了
    target.write_text("改坏了", encoding="utf-8")

    result = store.restore(checkpoint.id, "s1")
    assert result.ok
    assert result.restored
    assert target.read_text(encoding="utf-8") == "原始内容"


def test_restore_deletes_newly_created_file(tmp_path):
    """文件改之前不存在 → 恢复时应该删掉它。"""
    store = _store(tmp_path)
    target = tmp_path / "new.txt"
    checkpoint = store.save(session_id="s1", tool="Write", paths=[target])
    assert checkpoint is not None
    assert checkpoint.files[0].existed is False

    target.write_text("新文件", encoding="utf-8")
    result = store.restore(checkpoint.id, "s1")

    assert result.deleted
    assert not target.exists()


def test_restore_multiple_files(tmp_path):
    store = _store(tmp_path)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    checkpoint = store.save(session_id="s1", tool="MultiEdit", paths=[a, b])

    a.write_text("A2", encoding="utf-8")
    b.write_text("B2", encoding="utf-8")
    store.restore(checkpoint.id, "s1")

    assert a.read_text(encoding="utf-8") == "A"
    assert b.read_text(encoding="utf-8") == "B"


def test_restore_unknown_checkpoint(tmp_path):
    store = _store(tmp_path)
    result = store.restore("nope", "s1")
    assert not result.ok
    assert result.failed


def test_checkpoints_are_scoped_per_session(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    store.save(session_id="s1", tool="Write", paths=[target])
    store.save(session_id="s2", tool="Write", paths=[target])

    assert len(store.list("s1")) == 1
    assert len(store.list("s2")) == 1
    assert len(store.list()) == 2  # 不带会话过滤就是全部


def test_prune_keeps_recent(tmp_path):
    store = _store(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    for _ in range(5):
        store.save(session_id="s1", tool="Write", paths=[target])
    store.prune("s1", keep=3)
    assert len(store.list("s1")) == 3


# --------------------------------------------------------------------------
# Agent 层：改文件之前到底有没有自动留检查点
# --------------------------------------------------------------------------
class _FakeProvider:
    """这里不跟模型打交道，只要能构造出 Agent 就行。"""

    name = "fake"

    async def complete(self, **kwargs):  # pragma: no cover - 测试里不会调用
        return ""

    async def aclose(self) -> None:
        return None


def _agent(tmp_path, *, checkpoints: bool = True):
    from yotcode.agent import Agent
    from yotcode.config import Config
    from yotcode.tools import create_tools

    config = Config(
        cwd=tmp_path,
        permission_mode="bypassPermissions",
        ui={"checkpoints": checkpoints},
    )
    return Agent(config, _FakeProvider(), create_tools())


async def test_agent_snapshots_before_write(tmp_path):
    """Agent 改文件之前应该自动留检查点，并且能原样退回去。"""
    from yotcode.types import ToolUseBlock

    target = tmp_path / "demo.py"
    target.write_text("print('原始')\n", encoding="utf-8")

    agent = _agent(tmp_path)
    result = await agent._execute_tool(ToolUseBlock(
        id="toolu_1",
        name="Write",
        input={"file_path": str(target), "content": "print('改坏了')\n"},
    ))
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "print('改坏了')\n"

    store = _store(tmp_path)
    checkpoints = store.list(agent.session.meta.id)
    assert len(checkpoints) == 1
    assert checkpoints[0].tool == "Write"

    # 回退之后应该变回原始内容
    restore = store.restore(checkpoints[0].id, agent.session.meta.id)
    assert restore.ok
    assert target.read_text(encoding="utf-8") == "print('原始')\n"


async def test_agent_snapshot_disabled(tmp_path):
    """关掉检查点之后就不该产生任何快照。"""
    from yotcode.types import ToolUseBlock

    target = tmp_path / "demo.py"
    target.write_text("x", encoding="utf-8")

    agent = _agent(tmp_path, checkpoints=False)
    await agent._execute_tool(ToolUseBlock(
        id="toolu_1",
        name="Write",
        input={"file_path": str(target), "content": "y"},
    ))

    assert _store(tmp_path).list(agent.session.meta.id) == []


async def test_read_only_tool_creates_no_checkpoint(tmp_path):
    """只读工具不改文件，就不该留检查点。"""
    from yotcode.types import ToolUseBlock

    target = tmp_path / "demo.py"
    target.write_text("hello", encoding="utf-8")

    agent = _agent(tmp_path)
    result = await agent._execute_tool(ToolUseBlock(
        id="toolu_1", name="Read", input={"file_path": str(target)},
    ))
    assert not result.is_error
    assert _store(tmp_path).list(agent.session.meta.id) == []

