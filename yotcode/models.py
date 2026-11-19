"""模型目录：上下文窗口、价格、别名和供应商归属。

价格单位是“美元 / 百万 token”，仅用于界面上估算成本，不是账单依据；
厂商调价后可以直接改这里。aliases 让用户能用 sonnet、deepseek、r1 等
短名字，resolve_model_id() 会统一成正式模型 ID。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelSpec:
    id: str
    provider: str  # "anthropic" | "openai"
    label: str
    context: int = 128_000
    max_output: int = 8192
    input_price: float = 0.0
    output_price: float = 0.0
    cache_write_price: float = 0.0
    cache_read_price: float = 0.0
    thinking: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def context_label(self) -> str:
        if self.context >= 1_000_000:
            return f"{self.context / 1_000_000:g}M"
        return f"{self.context // 1000}k"


MODELS: dict[str, ModelSpec] = {}


def _add(spec: ModelSpec) -> None:
    MODELS[spec.id] = spec


# -- Anthropic -------------------------------------------------------------
_add(ModelSpec(
    "claude-opus-4-5", "anthropic", "Claude Opus 4.5",
    context=200_000, max_output=32_000,
    input_price=5.0, output_price=25.0, cache_write_price=6.25, cache_read_price=0.5,
    thinking=True, aliases=("opus", "opus-4.5", "claude-opus-4-5-20251101"),
))
_add(ModelSpec(
    "claude-sonnet-4-5", "anthropic", "Claude Sonnet 4.5",
    context=200_000, max_output=64_000,
    input_price=3.0, output_price=15.0, cache_write_price=3.75, cache_read_price=0.3,
    thinking=True, aliases=("sonnet", "sonnet-4.5", "claude-sonnet-4-5-20250929"),
))
_add(ModelSpec(
    "claude-haiku-4-5", "anthropic", "Claude Haiku 4.5",
    context=200_000, max_output=32_000,
    input_price=1.0, output_price=5.0, cache_write_price=1.25, cache_read_price=0.1,
    thinking=True, aliases=("haiku", "haiku-4.5"),
))
_add(ModelSpec(
    "claude-3-7-sonnet-20250219", "anthropic", "Claude Sonnet 3.7",
    context=200_000, max_output=64_000,
    input_price=3.0, output_price=15.0, cache_write_price=3.75, cache_read_price=0.3,
    thinking=True, aliases=("sonnet-3.7",),
))
_add(ModelSpec(
    "claude-3-5-haiku-latest", "anthropic", "Claude Haiku 3.5",
    context=200_000, max_output=8192,
    input_price=0.8, output_price=4.0, cache_write_price=1.0, cache_read_price=0.08,
    aliases=("haiku-3.5",),
))

# -- DeepSeek --------------------------------------------------------------
_add(ModelSpec(
    "deepseek-chat", "openai", "DeepSeek V3.2",
    context=128_000, max_output=8192,
    input_price=0.28, output_price=0.42, cache_read_price=0.028,
    aliases=("deepseek", "deepseek-v3"),
))
_add(ModelSpec(
    "deepseek-reasoner", "openai", "DeepSeek R1 (reasoner)",
    context=128_000, max_output=65_536,
    input_price=0.55, output_price=2.19, cache_read_price=0.14,
    thinking=True, aliases=("deepseek-r1",),
))

# -- OpenAI ---------------------------------------------------------------
_add(ModelSpec(
    "gpt-4.1", "openai", "GPT-4.1",
    context=1_000_000, max_output=32_768,
    input_price=2.0, output_price=8.0, cache_read_price=0.5,
))
_add(ModelSpec(
    "gpt-4.1-mini", "openai", "GPT-4.1 mini",
    context=1_000_000, max_output=32_768,
    input_price=0.4, output_price=1.6, cache_read_price=0.1,
))
_add(ModelSpec(
    "gpt-4o", "openai", "GPT-4o",
    context=128_000, max_output=16_384,
    input_price=2.5, output_price=10.0, cache_read_price=1.25,
))
_add(ModelSpec(
    "gpt-4o-mini", "openai", "GPT-4o mini",
    context=128_000, max_output=16_384,
    input_price=0.15, output_price=0.6, cache_read_price=0.075,
))
_add(ModelSpec(
    "o3", "openai", "OpenAI o3",
    context=200_000, max_output=100_000,
    input_price=2.0, output_price=8.0, cache_read_price=0.5, thinking=True,
))
_add(ModelSpec(
    "o4-mini", "openai", "OpenAI o4-mini",
    context=200_000, max_output=100_000,
    input_price=1.1, output_price=4.4, cache_read_price=0.275, thinking=True,
))

# -- Chinese providers (OpenAI compatible) --------------------------------
_add(ModelSpec(
    "qwen3-coder-plus", "openai", "Qwen3 Coder Plus",
    context=256_000, max_output=32_768,
    input_price=1.0, output_price=5.0,
))
_add(ModelSpec(
    "qwen-max", "openai", "Qwen Max",
    context=32_000, max_output=8192,
    input_price=1.6, output_price=6.4,
))
_add(ModelSpec(
    "kimi-k2-0905-preview", "openai", "Kimi K2",
    context=256_000, max_output=16_384,
    input_price=0.6, output_price=2.5,
))
_add(ModelSpec(
    "glm-4.6", "openai", "GLM-4.6",
    context=200_000, max_output=16_384,
    input_price=0.6, output_price=2.2,
))

ALIASES: dict[str, str] = {}
for _spec in MODELS.values():
    for _alias in _spec.aliases:
        ALIASES[_alias.lower()] = _spec.id


def resolve_model_id(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip()
    if key in MODELS:
        return key
    return ALIASES.get(key.lower())


def get_spec(name: str | None) -> ModelSpec | None:
    resolved = resolve_model_id(name)
    return MODELS.get(resolved) if resolved else None


def guess_provider(name: str | None) -> str | None:
    spec = get_spec(name)
    if spec:
        return spec.provider
    if not name:
        return None
    lowered = name.lower()
    if "claude" in lowered:
        return "anthropic"
    if any(k in lowered for k in ("gpt", "o1", "o3", "o4")):
        return "openai"
    return None


def context_window(name: str | None, default: int = 128_000) -> int:
    spec = get_spec(name)
    return spec.context if spec else default


def max_output_tokens(name: str | None, default: int = 8192) -> int:
    spec = get_spec(name)
    return spec.max_output if spec else default


def cost_for_usage(name: str | None, usage) -> float:
    spec = get_spec(name)
    if not spec:
        return 0.0
    million = 1_000_000
    return (
        usage.input_tokens * spec.input_price
        + usage.output_tokens * spec.output_price
        + usage.cache_creation_input_tokens * spec.cache_write_price
        + usage.cache_read_input_tokens * spec.cache_read_price
    ) / million


def list_models(provider: str | None = None) -> list[ModelSpec]:
    specs = [s for s in MODELS.values() if provider is None or s.provider == provider]
    return sorted(specs, key=lambda s: (s.provider, s.label))
