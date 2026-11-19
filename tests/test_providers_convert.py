"""Provider message conversion (Anthropic native and OpenAI-compatible)."""
from __future__ import annotations

import json

from yotcode.providers.anthropic import convert_messages as to_anthropic
from yotcode.providers.openai_compat import OpenAIProvider
from yotcode.providers.openai_compat import convert_messages as to_openai
from yotcode.types import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_result_message,
    user_message,
)


def conversation() -> list[Message]:
    return [
        user_message("read a.py"),
        assistant_message([
            TextBlock("Reading."),
            ToolUseBlock(id="tool_1", name="Read", input={"file_path": "a.py"}),
        ]),
        tool_result_message([
            ToolResultBlock("tool_1", "1\tprint('hi')"),
        ]),
    ]


def test_openai_tool_call_and_result_shape():
    converted = to_openai(conversation())
    assert [m["role"] for m in converted] == ["user", "assistant", "tool"]
    assistant = converted[1]
    assert assistant["content"] == "Reading."
    call = assistant["tool_calls"][0]
    assert call["id"] == "tool_1"
    assert call["function"]["name"] == "Read"
    assert json.loads(call["function"]["arguments"]) == {"file_path": "a.py"}
    assert converted[2]["tool_call_id"] == "tool_1"


def test_openai_skips_empty_assistant_messages():
    converted = to_openai([assistant_message([]), user_message("hello")])
    assert converted == [{"role": "user", "content": "hello"}]


def test_anthropic_converts_blocks_and_merges_consecutive_roles():
    converted = to_anthropic(conversation())
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assistant = converted[1]
    kinds = [b["type"] for b in assistant["content"]]
    assert kinds == ["text", "tool_use"]
    assert assistant["content"][1]["input"] == {"file_path": "a.py"}
    result = converted[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["content"] == "1\tprint('hi')"
    assert "is_error" not in result


def test_anthropic_marks_tool_errors():
    converted = to_anthropic([tool_result_message([
        ToolResultBlock("tool_1", "boom", is_error=True),
    ])])
    assert converted[0]["content"][0]["is_error"] is True


def test_anthropic_drops_unsigned_thinking():
    converted = to_anthropic([assistant_message([
        ThinkingBlock("internal monologue"),
        TextBlock("visible"),
    ])])
    kinds = [b["type"] for b in converted[0]["content"]]
    assert kinds == ["text"]


def test_deepseek_reasoner_uses_max_tokens_not_completion_tokens():
    provider = OpenAIProvider(
        api_key="test",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-reasoner",
    )
    body = provider._body(
        system="", messages=[user_message("hi")], tools=[],
        max_tokens=2048, temperature=0.7, stream=False,
    )
    assert body["max_tokens"] == 2048
    assert "max_completion_tokens" not in body
    assert "temperature" not in body


def test_openai_o3_uses_max_completion_tokens():
    provider = OpenAIProvider(
        api_key="test", base_url="https://api.openai.com/v1", model="o3"
    )
    body = provider._body(
        system="", messages=[user_message("hi")], tools=[],
        max_tokens=4096, temperature=0.7, stream=False,
    )
    assert body["max_completion_tokens"] == 4096
    assert "max_tokens" not in body
    assert "temperature" not in body


def test_anthropic_keeps_signed_thinking():
    converted = to_anthropic([assistant_message([
        ThinkingBlock("internal monologue", signature="sig"),
        TextBlock("visible"),
    ])])
    kinds = [b["type"] for b in converted[0]["content"]]
    assert kinds == ["thinking", "text"]
    assert converted[0]["content"][0]["signature"] == "sig"
