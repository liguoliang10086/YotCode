"""Model catalogue lookups and pricing."""
from __future__ import annotations

from yotcode import models
from yotcode.types import Usage


def test_alias_resolution():
    assert models.resolve_model_id("sonnet") == "claude-sonnet-4-5"
    assert models.resolve_model_id("DEEPSEEK") == "deepseek-chat"
    assert models.resolve_model_id("sonnet-3.7") == "claude-3-7-sonnet-20250219"
    assert models.resolve_model_id("nope") is None


def test_guess_provider():
    assert models.guess_provider("claude-opus-4-5") == "anthropic"
    assert models.guess_provider("some-claude-proxy") == "anthropic"
    assert models.guess_provider("gpt-4o") == "openai"
    assert models.guess_provider("o4-mini") == "openai"
    assert models.guess_provider(None) is None


def test_context_and_output_defaults():
    assert models.context_window("sonnet") == 200_000
    assert models.context_window("unknown-model", default=42) == 42
    assert models.max_output_tokens("gpt-4.1") == 32_768


def test_cost_uses_per_million_pricing():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = models.cost_for_usage("claude-sonnet-4-5", usage)
    assert round(cost, 6) == 18.0
    assert models.cost_for_usage("not-a-model", usage) == 0.0


def test_list_models_filters_provider():
    openai_models = models.list_models("openai")
    assert openai_models
    assert all(spec.provider == "openai" for spec in openai_models)
