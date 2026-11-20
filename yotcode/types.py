"""全局统一的消息 / 内容块数据模型。

内部结构刻意贴近 Anthropic Messages API：
  Message(role, content=[TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock])

这样做的好处：
  * 会话 JSONL、上下文压缩、UI 渲染只需要处理一种格式；
  * OpenAI 兼容供应商在 providers/openai_compat.py 里做一次转换即可；
  * 新增供应商时不必改动 Agent 主循环。

一条典型的工具调用对话：
  user      -> TextBlock("帮我读一下 a.py")
  assistant -> ToolUseBlock("Read", {"file_path": "a.py"})
  user      -> ToolResultBlock(tool_use_id=..., content="文件内容")
  assistant -> TextBlock("这个文件的作用是……")
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Union


# --------------------------------------------------------------------------
# content blocks
# --------------------------------------------------------------------------
@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str | None = None
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"

    @staticmethod
    def new(name: str, data: dict[str, Any]) -> "ToolUseBlock":
        return ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:16]}", name=name, input=data)


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Union[str, list["Block"]]
    is_error: bool = False
    type: str = "tool_result"


@dataclass
class ImageBlock:
    media_type: str
    data: str  # base64
    type: str = "image"
    # 可选的文件名，仅用于 UI 筹码展示；不影响发给模型的协议内容。
    # 放在 type 之后且给默认值，保证旧代码的位置参数构造不受影响。
    filename: str | None = None

    def to_data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"


Block = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock]


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------
@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: list[Block] = field(default_factory=list)

    # -- convenience -------------------------------------------------------
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]

    def images(self) -> list[ImageBlock]:
        return [b for b in self.content if isinstance(b, ImageBlock)]

    def copy(self) -> "Message":
        return message_from_dict(message_to_dict(self))


def user_message(content: str | list[Block]) -> Message:
    if isinstance(content, str):
        return Message("user", [TextBlock(content)])
    return Message("user", list(content))


def assistant_message(content: str | list[Block]) -> Message:
    if isinstance(content, str):
        return Message("assistant", [TextBlock(content)])
    return Message("assistant", list(content))


def tool_result_message(results: Iterable[ToolResultBlock]) -> Message:
    return Message("user", list(results))


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens
            + other.cache_read_input_tokens,
        )

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "Usage":
        data = data or {}
        return Usage(
            int(data.get("input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("cache_creation_input_tokens") or 0),
            int(data.get("cache_read_input_tokens") or 0),
        )


# --------------------------------------------------------------------------
# (de)serialisation
# --------------------------------------------------------------------------
def block_to_dict(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        out = {"type": "thinking", "thinking": block.thinking}
        if block.signature:
            out["signature"] = block.signature
        return out
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        content = block.content
        if isinstance(content, list):
            content = [block_to_dict(b) for b in content]
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": content,
            "is_error": block.is_error,
        }
    if isinstance(block, ImageBlock):
        out: dict[str, Any] = {
            "type": "image",
            "source": {"type": "base64", "media_type": block.media_type, "data": block.data},
        }
        # 文件名是 UI 用的元数据，有就一起存，恢复会话后还能显示出来。
        if block.filename:
            out["filename"] = block.filename
        return out
    raise TypeError(f"unknown block {block!r}")


def block_from_dict(data: dict[str, Any]) -> Block:
    kind = data.get("type")
    if kind == "text":
        return TextBlock(data.get("text", ""))
    if kind == "thinking":
        return ThinkingBlock(data.get("thinking", ""), data.get("signature"))
    if kind == "tool_use":
        return ToolUseBlock(
            id=data.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
            name=data.get("name", ""),
            input=data.get("input") or {},
        )
    if kind == "tool_result":
        content = data.get("content", "")
        if isinstance(content, list):
            content = [block_from_dict(b) for b in content]
        return ToolResultBlock(
            tool_use_id=data.get("tool_use_id", ""),
            content=content,
            is_error=bool(data.get("is_error")),
        )
    if kind == "image":
        source = data.get("source") or {}
        return ImageBlock(
            source.get("media_type", "image/png"),
            source.get("data", ""),
            filename=data.get("filename"),
        )
    if kind == "image_url":  # tolerated for imported transcripts
        return TextBlock("[image]")
    return TextBlock(str(data))


def message_to_dict(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": [block_to_dict(b) for b in message.content]}


def message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        role=data.get("role", "user"),
        content=[block_from_dict(b) for b in (data.get("content") or [])],
    )


def extract_text(content: str | list[Block]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            if isinstance(block.content, str):
                parts.append(block.content)
            else:
                parts.append(extract_text(block.content))
    return "\n".join(p for p in parts if p)
