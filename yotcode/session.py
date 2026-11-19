"""会话持久化：JSONL 事件日志 + 单独的 meta.json 侧车文件。

每个会话位于 ~/.yotcode/projects/<cwd-slug>/<session-id>.jsonl，
一行一个 JSON 事件，便于追加写入和崩溃后恢复：
  {"type":"message", "message": {...}}   一条完整消息
  {"type":"todos",   "todos": [...]}     待办列表快照
  {"type":"usage",   "usage": {...}}     单轮 token / 成本
  {"type":"model",   ...}                切换模型
  {"type":"reset",   "reason": "clear"}  清空对话（/clear 或压缩）
  {"type":"compaction", "summary": ...}  压缩摘要

加载时按顺序重放事件，遇到 reset 就清空之前的内容；这样 /clear 和
/compact 都不需要重写整个文件。导出的 Markdown 见 export_markdown()。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .types import Message, Usage, message_from_dict, message_to_dict
from .utils import read_text


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


@dataclass
class SessionMeta:
    id: str
    title: str = ""
    cwd: str = ""
    model: str = ""
    provider: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    turns: int = 0
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "cwd": self.cwd,
            "model": self.model, "provider": self.provider,
            "created": self.created, "updated": self.updated,
            "turns": self.turns, "cost": self.cost,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SessionMeta":
        return SessionMeta(
            id=data.get("id") or _new_id(),
            title=data.get("title") or "",
            cwd=data.get("cwd") or "",
            model=data.get("model") or "",
            provider=data.get("provider") or "",
            created=float(data.get("created") or time.time()),
            updated=float(data.get("updated") or time.time()),
            turns=int(data.get("turns") or 0),
            cost=float(data.get("cost") or 0.0),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
        )


class Session:
    def __init__(self, directory: Path, meta: SessionMeta,
                 messages: list[Message] | None = None,
                 todos: list[dict[str, Any]] | None = None,
                 total_usage: Usage | None = None, persist: bool = True) -> None:
        self.directory = directory
        self.meta = meta
        self.messages: list[Message] = messages or []
        self.todos: list[dict[str, Any]] = todos or []
        self.total_usage = total_usage or Usage()
        self.total_cost = meta.cost
        self.persist = persist
        self.path = directory / f"{meta.id}.jsonl"
        self._meta_path = directory / f"{meta.id}.meta.json"
        self._dirty = False

    # -- creation / loading ------------------------------------------------
    @staticmethod
    def create(directory: Path, cwd: Path, model: str, provider: str) -> "Session":
        directory.mkdir(parents=True, exist_ok=True)
        meta = SessionMeta(id=_new_id(), cwd=str(cwd), model=model, provider=provider)
        session = Session(directory, meta)
        session._write_meta()
        return session

    @staticmethod
    def memory(cwd: Path, model: str, provider: str) -> "Session":
        meta = SessionMeta(id="sub-" + _new_id(), cwd=str(cwd),
                          model=model, provider=provider)
        return Session(Path(cwd), meta, persist=False)

    @staticmethod
    def load(directory: Path, session_id: str) -> "Session | None":
        path = directory / f"{session_id}.jsonl"
        if not path.is_file():
            return None
        meta_path = directory / f"{session_id}.meta.json"
        meta = SessionMeta(id=session_id)
        if meta_path.is_file():
            try:
                meta = SessionMeta.from_dict(json.loads(read_text(meta_path)[0]))
            except Exception:
                pass
        messages: list[Message] = []
        todos: list[dict[str, Any]] = []
        usage = Usage()
        cost = 0.0
        try:
            text, _, _ = read_text(path)
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "message":
                try:
                    messages.append(message_from_dict(event["message"]))
                except Exception:
                    continue
            elif kind == "reset":
                messages = []
                if event.get("reason") == "clear":
                    todos = []
                    usage = Usage()
                    cost = 0.0
            elif kind == "todos":
                todos = event.get("todos") or []
            elif kind == "usage":
                usage = usage.add(Usage.from_dict(event.get("usage")))
                cost += float(event.get("cost") or 0.0)
        session = Session(directory, meta, messages, todos, usage)
        session.total_cost = cost
        return session

    @staticmethod
    def list_sessions(directory: Path) -> list[SessionMeta]:
        if not directory.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in directory.glob("*.meta.json"):
            try:
                metas.append(SessionMeta.from_dict(json.loads(read_text(path)[0])))
            except Exception:
                continue
        metas.sort(key=lambda m: m.updated, reverse=True)
        return metas

    # -- writing -----------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> None:
        if not self.persist:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        event.setdefault("ts", time.time())
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_meta(self) -> None:
        if not self.persist:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta.updated = time.time()
        with open(self._meta_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(self.meta.to_dict(), ensure_ascii=False, indent=2))

    def append_message(self, message: Message) -> None:
        self.messages.append(message)
        self._append({"type": "message", "message": message_to_dict(message)})
        if message.role == "assistant":
            self.meta.turns += 1
        if not self.meta.title and message.role == "user":
            text = message.text().strip().replace("\n", " ")
            if text and not text.startswith("/"):
                self.meta.title = text[:70]
        self._write_meta()

    def set_todos(self, todos: list[dict[str, Any]]) -> None:
        self.todos = todos
        self._append({"type": "todos", "todos": todos})
        self._write_meta()

    def add_usage(self, usage: Usage, cost: float) -> None:
        self.total_usage = self.total_usage.add(usage)
        self.total_cost += cost
        self.meta.cost = self.total_cost
        self.meta.input_tokens = self.total_usage.prompt_tokens
        self.meta.output_tokens = self.total_usage.output_tokens
        self._append({"type": "usage", "usage": usage.to_dict(), "cost": cost})
        self._write_meta()

    def set_model(self, model: str, provider: str) -> None:
        self.meta.model = model
        self.meta.provider = provider
        self._append({"type": "model", "model": model, "provider": provider})
        self._write_meta()

    def update_title(self, title: str) -> None:
        self.meta.title = title[:90]
        self._write_meta()

    def reset(self, reason: str = "clear") -> None:
        """Forget the conversation (used by /clear) and persist the reset."""
        self.messages.clear()
        self.todos = []
        self.total_usage = Usage()
        self.total_cost = 0.0
        self.meta.turns = 0
        self.meta.cost = 0.0
        self.meta.input_tokens = 0
        self.meta.output_tokens = 0
        self._append({"type": "reset", "reason": reason})
        self._append({"type": "todos", "todos": []})
        self._write_meta()

    def replace_messages(self, messages: list[Message], summary: str) -> None:
        """Used by /compact: keep a marker plus the surviving messages."""
        self._append({"type": "compaction", "summary": summary, "kept": len(messages)})
        self._append({"type": "reset", "reason": "compaction", "messages": len(messages)})
        for message in messages:
            self._append({"type": "message", "message": message_to_dict(message)})
        self._write_meta()

    def export_markdown(self) -> str:
        lines = [f"# YotCode session {self.meta.id}", ""]
        if self.meta.title:
            lines.append(f"**{self.meta.title}**")
        lines.append(f"- cwd: `{self.meta.cwd}`")
        lines.append(f"- model: `{self.meta.model}`")
        lines.append(f"- created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(self.meta.created))}")
        lines.append("")
        for message in self.messages:
            if message.role == "user":
                texts = [b for b in message.content if getattr(b, "type", "") == "text"]
                if texts:
                    lines.append("## ❯ User")
                    lines.append("\n".join(b.text for b in texts))
                    lines.append("")
            else:
                texts = [b for b in message.content if getattr(b, "type", "") == "text"]
                tools = [b for b in message.content if getattr(b, "type", "") == "tool_use"]
                if texts:
                    lines.append("## ✻ Assistant")
                    lines.append("\n".join(b.text for b in texts))
                    lines.append("")
                for tool in tools:
                    lines.append(f"<sub>🔧 `{tool.name}` {json.dumps(tool.input, ensure_ascii=False)[:300]}</sub>")
        return "\n".join(lines)
