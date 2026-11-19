"""检查点（Checkpoint）：AI 改文件之前先留个后路，出问题能一键回退。

为什么需要：AI 编程助手最让人抓狂的场景就是"它把你的代码改坏了"。
本模块的思路很简单——**任何会改文件的操作执行前，先把原文件内容存一份**，
之后用 `/revert` 就能把文件恢复成改之前的样子。

存哪儿（都在项目里，不污染用户主目录）：

    <项目>/.yotcode/checkpoints/<会话ID>/<检查点ID>/
        meta.json      元信息：时间、哪个工具改的、涉及哪些文件
        blobs/0.txt    每个文件改之前的内容（0、1、2…对应 meta 里的 blob 序号）

按会话分目录，是因为检查点应该只回退"这次会话里改的东西"，
不会误伤你之前就有的工作。

恢复语义：
  - 文件改之前就存在 → 把旧内容写回去；
  - 文件改之前不存在（是这次新建的）→ 直接删掉。

保留策略：每个会话默认最多留 50 个检查点，超出的从旧到新删掉，
免得磁盘无限膨胀。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .utils import read_text, write_text

# 每个会话最多保留多少个检查点
MAX_CHECKPOINTS_PER_SESSION = 50
# 单个文件快照的上限（10MB），超了就只记个标记不存内容
MAX_BLOB_BYTES = 10 * 1024 * 1024


@dataclass
class FileSnapshot:
    """一个文件的改前状态。"""

    path: str          # 绝对路径（存成字符串，方便跨平台序列化）
    existed: bool      # 改之前这个文件存在吗
    blob: str = ""     # 内容在 blobs/ 下的文件名；为空表示没存内容
    truncated: bool = False  # 文件太大，只记了标记没存内容


@dataclass
class Checkpoint:
    """一次改动的快照。"""

    id: str
    session_id: str
    created_at: float
    tool: str
    note: str = ""
    files: list[FileSnapshot] = field(default_factory=list)

    @property
    def time_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))

    def summary(self) -> str:
        """一行摘要，给 `/checkpoints` 列表用。"""
        names = ", ".join(Path(f.path).name for f in self.files[:3])
        if len(self.files) > 3:
            names += f" 等 {len(self.files)} 个文件"
        return f"{self.id}  {self.time_text}  {self.tool}  {names}"


@dataclass
class RestoreResult:
    """恢复结果，告诉用户到底动了哪些文件。"""

    ok: bool = True
    restored: list[str] = field(default_factory=list)  # 写回旧内容的文件
    deleted: list[str] = field(default_factory=list)    # 删掉的新建文件
    failed: list[tuple[str, str]] = field(default_factory=list)  # (路径, 原因)

    def report(self) -> str:
        if not self.ok and not self.restored and not self.deleted:
            return "没有需要恢复的文件。"
        lines = []
        if self.restored:
            lines.append("已恢复（写回改前内容）：")
            lines += [f"  - {p}" for p in self.restored]
        if self.deleted:
            lines.append("已删除（改之前不存在）：")
            lines += [f"  - {p}" for p in self.deleted]
        if self.failed:
            lines.append("恢复失败：")
            lines += [f"  - {p}：{why}" for p, why in self.failed]
        return "\n".join(lines)


class CheckpointStore:
    """检查点仓库：负责存、列、恢复。

    目录结构见本模块顶部说明。所有写盘操作都做了异常保护——
    快照失败绝不能把主流程带崩（快照只是"保险"，不是必需品）。
    """

    def __init__(self, project_dir: Path) -> None:
        # project_dir 一般是 <项目>/.yotcode
        self.root = Path(project_dir) / "checkpoints"

    # -- 内部工具 ---------------------------------------------------------
    def _session_dir(self, session_id: str) -> Path:
        # 会话 id 里可能有路径分隔符之类的字符，净化一下
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
        return self.root / safe

    def _checkpoint_dir(self, session_id: str, checkpoint_id: str) -> Path:
        return self._session_dir(session_id) / checkpoint_id

    # -- 保存 --------------------------------------------------------------
    def save(self, *, session_id: str, tool: str, paths: list[Path],
             note: str = "") -> Checkpoint | None:
        """在改动发生**之前**调用，把这些文件的当前内容存下来。

        返回 None 表示快照没成功（调用方应该忽略，不要因此中断工具执行）。
        """
        if not paths:
            return None
        checkpoint_id = uuid.uuid4().hex[:12]
        directory = self._checkpoint_dir(session_id, checkpoint_id)
        blobs_dir = directory / "blobs"
        files: list[FileSnapshot] = []
        try:
            blobs_dir.mkdir(parents=True, exist_ok=True)
            for index, path in enumerate(paths):
                path = Path(path)
                existed = path.is_file()
                snapshot = FileSnapshot(path=str(path), existed=existed)
                if existed:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                    if size > MAX_BLOB_BYTES:
                        # 文件太大了，只记个标记，恢复时跳过它
                        snapshot.truncated = True
                    else:
                        try:
                            content, _, _ = read_text(path)
                        except OSError:
                            content = ""
                        blob_name = f"{index}.txt"
                        write_text(blobs_dir / blob_name, content)
                        snapshot.blob = blob_name
                files.append(snapshot)
            meta = {
                "id": checkpoint_id,
                "session_id": session_id,
                "created_at": time.time(),
                "tool": tool,
                "note": note,
                "files": [
                    {
                        "path": f.path,
                        "existed": f.existed,
                        "blob": f.blob,
                        "truncated": f.truncated,
                    }
                    for f in files
                ],
            }
            write_text(directory / "meta.json",
                       json.dumps(meta, ensure_ascii=False, indent=2))
        except Exception:
            # 快照是保险，保险本身出问题不能影响主流程
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception:
                pass
            return None
        self.prune(session_id)
        return Checkpoint(
            id=checkpoint_id,
            session_id=session_id,
            created_at=meta["created_at"],
            tool=tool,
            note=note,
            files=files,
        )

    # -- 读取 --------------------------------------------------------------
    def list(self, session_id: str | None = None) -> list[Checkpoint]:
        """列出检查点，新的在前。"""
        directories = (
            [self._session_dir(session_id)] if session_id
            else (sorted(self.root.iterdir()) if self.root.is_dir() else [])
        )
        result: list[Checkpoint] = []
        for session_dir in directories:
            if not session_dir.is_dir():
                continue
            for meta_path in session_dir.glob("*/meta.json"):
                checkpoint = self._read_meta(meta_path)
                if checkpoint is not None:
                    result.append(checkpoint)
        result.sort(key=lambda c: c.created_at, reverse=True)
        return result

    def _read_meta(self, meta_path: Path) -> Checkpoint | None:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        files = [
            FileSnapshot(
                path=str(item.get("path") or ""),
                existed=bool(item.get("existed")),
                blob=str(item.get("blob") or ""),
                truncated=bool(item.get("truncated")),
            )
            for item in data.get("files") or []
        ]
        return Checkpoint(
            id=str(data.get("id") or meta_path.parent.name),
            session_id=str(data.get("session_id") or ""),
            created_at=float(data.get("created_at") or 0),
            tool=str(data.get("tool") or ""),
            note=str(data.get("note") or ""),
            files=files,
        )

    def get(self, checkpoint_id: str, session_id: str | None = None) -> Checkpoint | None:
        """按 id 找检查点。给了 session_id 就只在该会话里找。"""
        if session_id:
            meta = self._checkpoint_dir(session_id, checkpoint_id) / "meta.json"
            if meta.is_file():
                return self._read_meta(meta)
            return None
        for checkpoint in self.list():
            if checkpoint.id == checkpoint_id:
                return checkpoint
        return None

    # -- 恢复 --------------------------------------------------------------
    def restore(self, checkpoint_id: str, session_id: str | None = None) -> RestoreResult:
        """把文件恢复成检查点记录的样子。"""
        checkpoint = self.get(checkpoint_id, session_id)
        if checkpoint is None:
            result = RestoreResult(ok=False)
            result.failed.append(("", f"找不到检查点 {checkpoint_id}"))
            return result
        result = RestoreResult()
        base = self._checkpoint_dir(checkpoint.session_id, checkpoint.id)
        for snapshot in checkpoint.files:
            path = Path(snapshot.path)
            try:
                if not snapshot.existed:
                    # 改之前没有这个文件 → 是这次新建的，恢复就是删掉
                    if path.is_file():
                        path.unlink()
                    result.deleted.append(str(path))
                    continue
                if snapshot.truncated or not snapshot.blob:
                    # 太大了没存内容，跳过并说明原因
                    result.failed.append((str(path), "文件过大，快照未保存内容"))
                    continue
                blob_path = base / "blobs" / snapshot.blob
                content, _, _ = read_text(blob_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                write_text(path, content)
                result.restored.append(str(path))
            except OSError as exc:
                result.failed.append((str(path), str(exc)))
        if result.failed and not result.restored and not result.deleted:
            result.ok = False
        return result

    # -- 清理 --------------------------------------------------------------
    def prune(self, session_id: str, keep: int = MAX_CHECKPOINTS_PER_SESSION) -> None:
        """只保留最近 keep 个检查点，旧的删掉。"""
        session_dir = self._session_dir(session_id)
        if not session_dir.is_dir():
            return
        metas = sorted(session_dir.glob("*/meta.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for meta_path in metas[keep:]:
            shutil.rmtree(meta_path.parent, ignore_errors=True)

    def clear(self, session_id: str | None = None) -> int:
        """清空检查点，返回删掉的数量。"""
        directories = (
            [self._session_dir(session_id)] if session_id
            else ([self.root] if self.root.is_dir() else [])
        )
        count = 0
        for directory in directories:
            if not directory.is_dir():
                continue
            for child in directory.iterdir():
                if child.is_dir():
                    count += 1
                    shutil.rmtree(child, ignore_errors=True)
        return count

