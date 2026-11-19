"""Settings loading, provider inference and defaults."""
from __future__ import annotations

import json

from yotcode.config import (
    DEFAULT_ANTHROPIC_MODEL,
    _infer_openai_defaults,
    _strip_json_comments,
    build_config,
    default_shell,
    load_json_file,
    load_settings,
)


def test_strip_json_comments_keeps_urls_in_strings():
    source = '{\n  // comment\n  "url": "https://x/y",\n  /* block */\n  "n": 1\n}'
    stripped = _strip_json_comments(source)
    assert json.loads(stripped) == {"url": "https://x/y", "n": 1}


def test_load_json_file_tolerates_comments(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{\n  // hi\n  "model": "sonnet"\n}\n', encoding="utf-8")
    assert load_json_file(path) == {"model": "sonnet"}


def test_load_settings_merges_project_over_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".yotcode").mkdir(parents=True)
    (project / ".yotcode").mkdir(parents=True)
    monkeypatch.setenv("CCPY_HOME", str(home / ".yotcode"))
    (home / ".yotcode" / "settings.json").write_text(
        json.dumps({"maxTokens": 100, "permissions": {"allow": ["Read"]}}),
        encoding="utf-8")
    (project / ".yotcode" / "settings.json").write_text(
        json.dumps({"maxTokens": 200, "permissions": {"allow": ["Write"]}}),
        encoding="utf-8")
    settings, used = load_settings(project)
    assert settings["maxTokens"] == 200
    assert set(settings["permissions"]["allow"]) == {"Read", "Write"}
    assert len(used) == 2


def test_build_config_resolves_alias_and_provider(tmp_path, monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY",
        "ZHIPUAI_API_KEY", "OPENAI_BASE_URL", "CCPY_MODEL", "CCPY_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    config = build_config(tmp_path, model="sonnet")
    assert config.model == "claude-sonnet-4-5"
    assert config.provider == "anthropic"
    assert config.base_url.startswith("https://api.anthropic.com")
    assert config.context_window == 200_000
    assert not config.provider_ready()


def test_build_config_uses_deepseek_key_shortcut(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    config = build_config(tmp_path, model="deepseek-chat")
    assert config.provider == "openai"
    assert config.api_key == "sk-test"
    assert config.base_url == "https://api.deepseek.com/v1"
    assert config.provider_ready()


def test_explicit_model_wins_over_explicit_provider(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config = build_config(tmp_path, model="deepseek-chat", provider="anthropic")
    assert config.provider == "openai"
    assert config.model == "deepseek-chat"


def test_default_model_when_nothing_requested(tmp_path, monkeypatch):
    for key in ("CCPY_MODEL", "CCPY_PROVIDER", "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    config = build_config(tmp_path)
    assert config.model == DEFAULT_ANTHROPIC_MODEL
    assert config.provider == "anthropic"


def test_infer_openai_defaults():
    assert _infer_openai_defaults("https://api.deepseek.com/v1")[0] == "deepseek-chat"
    assert _infer_openai_defaults("https://dashscope.aliyuncs.com/compatible-mode/v1")[0] == \
        "qwen3-coder-plus"
    assert _infer_openai_defaults("https://api.moonshot.cn/v1")[0] == "kimi-k2-0905-preview"
    assert _infer_openai_defaults("https://open.bigmodel.cn/api/paas/v4")[0] == "glm-4.6"
    assert _infer_openai_defaults(None)[1] == "https://api.openai.com/v1"


def test_default_shell_env_override(monkeypatch):
    monkeypatch.setenv("CCPY_SHELL", "/bin/zsh")
    assert default_shell() == "/bin/zsh"


def test_ccpy_model_and_provider_env_shortcuts(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "CCPY_MODEL", "CCPY_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("CCPY_MODEL", "deepseek-reasoner")
    monkeypatch.setenv("CCPY_PROVIDER", "openai")
    config = build_config(tmp_path)
    assert config.model == "deepseek-reasoner"
    assert config.provider == "openai"


def test_local_openai_endpoint_works_without_key(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "CCPY_MODEL", "CCPY_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    config = build_config(
        tmp_path,
        model="qwen2.5-coder",
        base_url="http://localhost:11434/v1",
    )
    assert config.provider == "openai"
    assert config.base_url == "http://localhost:11434/v1"
    assert config.provider_ready()
