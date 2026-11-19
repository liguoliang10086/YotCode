"""Message / block / usage serialisation."""
from __future__ import annotations

import json

from yotcode.types import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    assistant_message,
    extract_text,
    message_from_dict,
    message_to_dict,
    tool_result_message,
    user_message,
)


def test_user_and_assistant_helpers():
    user = user_message("hello")
    assert user.role == "user"
    assert user.text() == "hello"

    assistant = assistant_message([TextBlock("hi"), TextBlock("there")])
    assert assistant.role == "assistant"
    assert assistant.text() == "hi\nthere"


def test_message_json_round_trip():
    message = Message("assistant", [
        TextBlock("Let me look."),
        ThinkingBlock("need a file", signature="sig"),
        ToolUseBlock.new("Read", {"file_path": "a.py"}),
    ])
    restored = message_from_dict(json.loads(json.dumps(message_to_dict(message))))
    assert isinstance(restored.content[0], TextBlock)
    assert isinstance(restored.content[1], ThinkingBlock)
    assert restored.content[1].signature == "sig"
    assert isinstance(restored.content[2], ToolUseBlock)
    assert restored.tool_uses()[0].name == "Read"


def test_tool_result_round_trip_with_nested_blocks():
    result = ToolResultBlock("toolu_1", [TextBlock("ok"), ImageBlock("image/png", "AAA")])
    restored = message_from_dict(message_to_dict(Message("user", [result])))
    block = restored.content[0]
    assert isinstance(block, ToolResultBlock)
    assert isinstance(block.content, list)
    assert block.content[1].to_data_url().startswith("data:image/png;base64,")


def test_unknown_block_type_is_tolerated():
    restored = message_from_dict({"role": "user", "content": [{"type": "wat"}]})
    assert isinstance(restored.content[0], TextBlock)


def test_usage_math():
    usage = Usage(input_tokens=10, output_tokens=5,
                  cache_creation_input_tokens=2, cache_read_input_tokens=3)
    assert usage.prompt_tokens == 15
    assert usage.total_tokens == 20
    assert usage.add(Usage(1, 1)).to_dict()["input_tokens"] == 11


def test_extract_text_walks_tool_results():
    message = tool_result_message([
        ToolResultBlock("t1", "stdout"),
        ToolResultBlock("t2", [TextBlock("nested")]),
    ])
    assert extract_text(message.content) == "stdout\nnested"
