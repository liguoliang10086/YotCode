"""Provider factory."""
from __future__ import annotations

import os
from pathlib import Path

from ..apilog import ApiLogger
from ..config import Config
from ..httplog import HttpLogger
from .anthropic import AnthropicProvider
from .base import (
    Interrupted,
    ModelError,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseReady,
    TurnDone,
)
from .mock import MockProvider
from .openai_compat import OpenAIProvider

__all__ = [
    "Provider",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolUseReady",
    "TurnDone",
    "ModelError",
    "Interrupted",
    "create_provider",
]


# 同一个进程里所有 provider 共用一套 logger，日志始终追加不清空。
_shared_logger: ApiLogger | None = None
_shared_http_logger: HttpLogger | None = None
_shared_logger_path: Path | None = None


def _shared_loggers(config: Config) -> tuple[ApiLogger | None, HttpLogger | None]:
    """返回进程级共享的 (正文日志, HTTP 报文日志)，按环境变量开关。"""
    global _shared_logger, _shared_http_logger, _shared_logger_path
    log_setting = (os.environ.get("YOTCODE_API_LOG")
                   or os.environ.get("CLAUDEPY_API_LOG")
                   or "1").strip()
    if log_setting.lower() in ("0", "false", "off", "no"):
        return None, None
    log_path = (
        Path(log_setting).expanduser()
        if log_setting != "1"
        else config.project_dir / "api-log.txt"
    )
    if _shared_logger is None or _shared_logger_path != log_path:
        _shared_logger = ApiLogger(log_path)
        _shared_logger.clear()      # 追加一条"新会话开始"分隔，不清空历史
        # HTTP 原始报日志文件与正文日志同目录，固定名 api-http.txt
        http_path = log_path.parent / "api-http.txt"
        _shared_http_logger = HttpLogger(http_path)
        _shared_http_logger.clear()  # 同上：追加分隔标记，不清空历史
        _shared_logger_path = log_path
    return _shared_logger, _shared_http_logger


def create_provider(config: Config) -> Provider:
    provider: Provider
    if config.mock:
        provider = MockProvider(
            model=config.model if config.model else "mock-claude",
            max_tokens=config.max_tokens,
        )
    elif config.provider == "anthropic":
        provider = AnthropicProvider(
            api_key=config.api_key or "",
            base_url=config.base_url,
            model=config.model,
            max_tokens=config.max_tokens,
        )
    else:
        provider = OpenAIProvider(
            api_key=config.api_key or "",
            base_url=config.base_url,
            model=config.model,
            max_tokens=config.max_tokens,
        )

    logger, http_logger = _shared_loggers(config)
    if logger:
        provider.set_logger(logger)
    if http_logger:
        provider._http_logger = http_logger
    return provider
