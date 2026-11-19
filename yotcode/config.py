"""配置系统：设置文件合并、环境变量、API Key 与模型/供应商解析。

配置优先级（从低到高，后者覆盖前者）：
  1. 代码内置默认值 DEFAULT_SETTINGS
  2. ~/.yotcode/settings.json
  3. <项目>/.claude/settings.json
  4. <项目>/.yotcode/settings.json
  5. <项目>/.claude/settings.local.json
  6. <项目>/.yotcode/settings.local.json
  7. 命令行参数（--model / --provider / --api-key ...）

凭据查找顺序：真实环境变量 > .env 文件 > settings.json 的 env 字段 >
settings.json 的 providers.*.apiKey。真实环境变量永远优先，避免仓库里的
.env 意外覆盖 shell 中已经导出的值。

OpenAI 兼容供应商（DeepSeek/Qwen/Kimi/GLM/Ollama/vLLM）共用同一个
OpenAIProvider，只是 base_url 与 model 不同；具体推断逻辑见 build_config()。

项目早先叫 claudepy，配置目录是 .claudepy/。改名成 yotcode 后，老目录会被
自动迁移（见 migrate_legacy_dirs），不需要手工搬。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import models
from .console_font import resolve_font_size
from .utils import deep_merge, load_dotenv, read_text

APP_NAME = "yotcode"
# 老版本（claudepy）用的目录名，改名后用于自动迁移
LEGACY_APP_NAME = "claudepy"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4.1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")


def home_dir() -> Path:
    """用户级配置根目录（默认 ~/.yotcode）。

    环境变量按 YOTCODE_HOME → CCPY_HOME → CLAUDEPY_HOME 的顺序取，
    后面两个是老版本的名字，继续认是为了不打断已有用法。
    """
    raw = (os.environ.get("YOTCODE_HOME")
           or os.environ.get("CCPY_HOME")
           or os.environ.get("CLAUDEPY_HOME"))
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / f".{APP_NAME}"


def project_config_dir(cwd: Path) -> Path:
    """项目级配置目录（<项目>/.yotcode）。"""
    return Path(cwd) / f".{APP_NAME}"


def legacy_home_dir() -> Path | None:
    """老版本的用户级配置目录（~/.claudepy），只用于迁移。

    用户显式指定了 HOME（YOTCODE_HOME / CCPY_HOME / CLAUDEPY_HOME）时返回
    None —— 那种情况下家目录不是我们要管的目录，不该动它。
    """
    for key in ("YOTCODE_HOME", "CCPY_HOME", "CLAUDEPY_HOME"):
        if os.environ.get(key):
            return None
    return Path.home() / f".{LEGACY_APP_NAME}"


def legacy_project_config_dir(cwd: Path) -> Path:
    """老版本的项目级配置目录（<项目>/.claudepy），只用于迁移。"""
    return Path(cwd) / f".{LEGACY_APP_NAME}"


def migrate_legacy_dirs(cwd: Path | None = None) -> list[str]:
    """把老版本（claudepy）的配置目录搬到新名字（yotcode）。

    搬的是整个目录，所以 settings.json、commands/、agents/、skills/、
    checkpoints/、会话文件都能原样带过去。

    规则：
      - 新目录已经存在 → 不动（用户可能自己整理过，别覆盖）；
      - 老目录不存在 → 什么都不做；
      - 优先改名（同一磁盘，快且不会留重复文件），
        跨磁盘等失败情况退回复制。

    返回迁移日志（给用户看），失败不影响启动。
    """
    notes: list[str] = []
    pairs: list[tuple[Path, Path]] = []
    try:
        old_home = legacy_home_dir()
        if old_home is not None:
            pairs.append((old_home, home_dir()))
    except Exception:
        pass
    if cwd is not None:
        try:
            pairs.append((legacy_project_config_dir(Path(cwd)),
                          project_config_dir(Path(cwd))))
        except Exception:
            pass

    for old, new in pairs:
        try:
            if not old.is_dir() or new.exists():
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            try:
                old.rename(new)
            except OSError:
                # 跨盘符改名会失败，退回复制
                shutil.copytree(old, new)
            notes.append(f"{old} → {new}")
        except Exception:
            continue
    return notes


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        text, _, _ = read_text(path)
    except OSError:
        return {}
    try:
        data = json.loads(_strip_json_comments(text))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def settings_paths(cwd: Path) -> list[Path]:
    """返回按优先级从低到高排列的设置文件路径。

    后面的文件覆盖前面的标量配置；permissions 的 allow/deny/ask 与 hooks
    列表则在 deep_merge() 中做“追加合并”，所以全局 deny 规则不会被项目配置清掉。
    """
    hd = home_dir()
    paths = [
        hd / "settings.json",
        Path(cwd) / ".claude" / "settings.json",
        project_config_dir(cwd) / "settings.json",
        Path(cwd) / ".claude" / "settings.local.json",
        project_config_dir(cwd) / "settings.local.json",
    ]
    return paths


DEFAULT_SETTINGS: dict[str, Any] = {
    "model": None,
    "provider": None,
    "smallModel": None,
    "maxTokens": 8192,
    "maxTurns": 60,
    "permissionMode": "default",
    "autoCompact": True,
    "compactThreshold": 0.82,
    "shell": None,
    "theme": "yotcode",
    "temperature": None,
    "systemPromptAppend": "",
    "autoApproveEdits": False,
    "thinking": True,
    "permissions": {
        "allow": [],
        "deny": [],
        "ask": [],
        "additionalDirectories": [],
    },
    "hooks": {},
    "mcpServers": {},
    "env": {},
    "providers": {},
    "webSearch": {"provider": None},
    "ui": {
        "showTips": True,
        "showThinking": True,
        "compactOutput": False,
        # 改文件之前是否自动留检查点（/revert 回退要用）。false = 关掉。
        "checkpoints": True,
        # 经典 conhost 的终端字号（像素高）。null = 用程序默认值。
        # 也可以写进 .env：YOTCODE_FONT_SIZE=14
        "fontSize": None,
    },
}
# The "hooks" list-merge in deep_merge only fires for dict values; keep a copy
# of the pristine defaults.
_BASE_SETTINGS = json.loads(json.dumps(DEFAULT_SETTINGS))


def default_shell() -> str:
    # YOTCODE_SHELL 是新名字，CCPY_SHELL / CLAUDEPY_SHELL 是老名字，继续认
    configured = (os.environ.get("YOTCODE_SHELL")
                  or os.environ.get("CCPY_SHELL")
                  or os.environ.get("CLAUDEPY_SHELL"))
    if configured:
        return configured
    if os.name == "nt":
        bash = shutil.which("bash")
        if not bash:
            for candidate in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
            ):
                if os.path.isfile(candidate):
                    bash = candidate
                    break
        if bash:
            return bash
        return "powershell"
    return os.environ.get("SHELL") or "/bin/bash"


@dataclass
class Config:
    cwd: Path
    model: str = DEFAULT_ANTHROPIC_MODEL
    provider: str = "anthropic"
    small_model: str = DEFAULT_ANTHROPIC_MODEL
    max_tokens: int = 8192
    max_turns: int = 200
    permission_mode: str = "default"
    auto_compact: bool = True
    compact_threshold: float = 0.82
    shell: str = ""
    theme: str = "yotcode"
    temperature: float | None = None
    system_prompt_append: str = ""
    thinking: bool = True
    permissions: dict[str, list[str]] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)
    # 经典 conhost 的终端字号；None = 用 console_font.DEFAULT_TARGET。
    font_size: int | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    api_key: str | None = None
    base_url: str = DEFAULT_ANTHROPIC_BASE
    mock: bool = False
    verbose: bool = False
    debug: bool = False
    settings_files: list[str] = field(default_factory=list)
    loaded_settings: dict[str, Any] = field(default_factory=dict)
    web_search_provider: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def spec(self) -> models.ModelSpec | None:
        return models.get_spec(self.model)

    @property
    def context_window(self) -> int:
        return models.context_window(self.model)

    @property
    def home(self) -> Path:
        return home_dir()

    @property
    def project_dir(self) -> Path:
        return project_config_dir(self.cwd)

    @property
    def checkpoints_enabled(self) -> bool:
        """是否在改文件之前自动留检查点（可以用 ui.checkpoints 关掉）。

        关掉以后 /revert 就没东西可退了，但能省下磁盘占用。
        """
        return bool(self.ui.get("checkpoints", True))

    @property
    def session_dir(self) -> Path:
        from .utils import slug_path

        return home_dir() / "projects" / slug_path(self.cwd)

    @property
    def model_label(self) -> str:
        spec = self.spec
        return spec.label if spec else self.model

    def model_context_label(self) -> str:
        spec = self.spec
        return spec.context_label if spec else "?"

    def provider_ready(self) -> bool:
        """是否具备发起模型请求的条件。

        本地推理服务（Ollama / vLLM / LM Studio）通常不需要 API Key，
        因此 localhost、127.0.0.1 等地址即使没有 key 也视为可用。
        """
        if self.mock or self.api_key:
            return True
        base = (self.base_url or "").lower()
        return any(host in base for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

    def ensure_dirs(self) -> None:
        for path in (home_dir(), self.session_dir, home_dir() / "logs"):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def describe(self) -> str:
        return (
            f"model={self.model} provider={self.provider} "
            f"mode={self.permission_mode} cwd={self.cwd}"
        )

    def to_settings_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "permissionMode": self.permission_mode,
            "shell": self.shell,
            "theme": self.theme,
            "maxTokens": self.max_tokens,
            "maxTurns": self.max_turns,
            "autoCompact": self.auto_compact,
            "thinking": self.thinking,
            "permissions": {
                "allow": list(self.permissions.get("allow", [])),
                "deny": list(self.permissions.get("deny", [])),
                "ask": list(self.permissions.get("ask", [])),
                "additionalDirectories": list(
                    self.permissions.get("additionalDirectories", [])
                ),
            },
            "hooks": self.hooks,
            "mcpServers": self.mcp_servers,
        }


def load_settings(cwd: Path) -> tuple[dict[str, Any], list[str]]:
    merged = json.loads(json.dumps(_BASE_SETTINGS))
    used: list[str] = []
    for path in settings_paths(cwd):
        if path.is_file():
            data = load_json_file(path)
            if data:
                merged = deep_merge(merged, data)
                used.append(str(path))
    return merged, used


def _env_value(name: str, settings_env: dict[str, str]) -> str | None:
    return os.environ.get(name) or settings_env.get(name) or None


def _infer_openai_defaults(base_url: str | None) -> tuple[str | None, str]:
    """根据 base_url 猜测该服务商常用的默认模型。

    返回 (建议模型, 规范化后的 base_url)。例如只设置 DEEPSEEK_API_KEY 时，
    会得到 ("deepseek-chat", "https://api.deepseek.com/v1")，用户无需再写 --model。
    未知服务商返回 (None, base)，由 DEFAULT_OPENAI_MODEL 兜底。
    """
    base = (base_url or "").rstrip("/")
    lowered = base.lower()
    if not base:
        return None, DEFAULT_OPENAI_BASE
    if "deepseek" in lowered:
        return DEFAULT_DEEPSEEK_MODEL, base
    if "dashscope" in lowered or "aliyuncs" in lowered:
        return "qwen3-coder-plus", base
    if "moonshot" in lowered:
        return "kimi-k2-0905-preview", base
    if "zhipu" in lowered or "bigmodel" in lowered:
        return "glm-4.6", base
    if "localhost" in lowered or "127.0.0.1" in lowered:
        return None, base
    return None, base


def build_config(
    cwd: Path,
    *,
    model: str | None = None,
    provider: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    max_turns: int | None = None,
    mock: bool = False,
    verbose: bool = False,
    debug: bool = False,
    api_key: str | None = None,
    base_url: str | None = None,
    system_prompt_append: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> Config:
    cwd = Path(cwd).resolve()
    settings, used_files = load_settings(cwd)

    # .env 文件：先项目根目录，再 ~/.yotcode/.env。
    # 只补充真实环境变量里不存在的键，绝不覆盖 shell 中已经导出的值。
    dotenv: dict[str, str] = {}
    for dotenv_path in (Path(cwd) / ".env", home_dir() / ".env"):
        if dotenv_path.is_file():
            for key, value in load_dotenv(dotenv_path).items():
                if key not in os.environ:
                    dotenv[key] = value
    settings_env = {**dotenv, **(settings.get("env") or {}), **(env_overrides or {})}

    providers_cfg = settings.get("providers") or {}

    # ---- 凭据与 API 地址 ----------------------------------------------
    # 不同厂商共用 OpenAI 协议，但 key 的环境变量名不同。
    # 只设置 DEEPSEEK_API_KEY 也能工作：下面会自动补上 DeepSeek 的 base_url。
    anthropic_cfg = providers_cfg.get("anthropic") or {}
    openai_cfg = providers_cfg.get("openai") or {}

    anthropic_key = (
        api_key
        if provider == "anthropic" and api_key
        else _env_value("ANTHROPIC_API_KEY", settings_env)
        or _env_value("ANTHROPIC_AUTH_TOKEN", settings_env)
        or anthropic_cfg.get("apiKey")
    )
    openai_key = (
        api_key
        if provider == "openai" and api_key
        else _env_value("CCPY_API_KEY", settings_env)
        or _env_value("OPENAI_API_KEY", settings_env)
        or _env_value("DEEPSEEK_API_KEY", settings_env)
        or _env_value("DASHSCOPE_API_KEY", settings_env)
        or _env_value("MOONSHOT_API_KEY", settings_env)
        or _env_value("ZHIPUAI_API_KEY", settings_env)
        or openai_cfg.get("apiKey")
    )

    # --base-url 是“无论最终选中哪个供应商都用这个地址”，因此这里直接用它
    # 覆盖对应分支；否则只传 --base-url --model 时地址会被悄悄忽略。
    an_base = (
        base_url
        or _env_value("ANTHROPIC_BASE_URL", settings_env)
        or anthropic_cfg.get("baseUrl")
        or DEFAULT_ANTHROPIC_BASE
    )
    oa_base = (
        base_url
        or _env_value("OPENAI_BASE_URL", settings_env)
        or openai_cfg.get("baseUrl")
    )
    if not oa_base and _env_value("DEEPSEEK_API_KEY", settings_env):
        oa_base = "https://api.deepseek.com/v1"
    elif not oa_base and _env_value("DASHSCOPE_API_KEY", settings_env):
        oa_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    elif not oa_base and _env_value("MOONSHOT_API_KEY", settings_env):
        oa_base = "https://api.moonshot.cn/v1"
    elif not oa_base and _env_value("ZHIPUAI_API_KEY", settings_env):
        oa_base = "https://open.bigmodel.cn/api/paas/v4"
    oa_base = oa_base or DEFAULT_OPENAI_BASE
    oa_suggested_model, oa_base = _infer_openai_defaults(oa_base)

    # 供应商解析顺序：CLI --provider > CCPY_PROVIDER > settings.provider，
    # 其次根据模型名猜测（deepseek-* / gpt-* → openai），最后看哪个 key 存在。
    # 注意：同时配置 Anthropic 和 OpenAI 的 key 时默认会选 Anthropic；
    # 想用 GPT/DeepSeek 请显式传 --provider openai 或 --model。
    provider_name = (
        provider
        or _env_value("CCPY_PROVIDER", settings_env)
        or settings.get("provider")
        or ""
    ).strip() or None
    requested_model = (
        model
        or _env_value("CCPY_MODEL", settings_env)
        or settings.get("model")
        or ""
    ).strip() or None
    if provider_name not in (None, "anthropic", "openai"):
        provider_name = None
    if not provider_name and requested_model:
        provider_name = models.guess_provider(requested_model)
    if not provider_name and base_url and "anthropic" not in base_url.lower():
        # 显式给了非 Anthropic 的地址（localhost / vLLM / 自建网关）时，
        # 默认按 OpenAI 兼容协议处理，否则没有 key 的 Ollama 会被误判成 Anthropic。
        provider_name = "openai"
    if not provider_name:
        if anthropic_key:
            provider_name = "anthropic"
        elif openai_key:
            provider_name = "openai"
        else:
            provider_name = "anthropic"

    if provider_name == "anthropic":
        key = api_key or anthropic_key
        base = an_base.rstrip("/")
        default_model = DEFAULT_ANTHROPIC_MODEL
        spec_provider = "anthropic"
    else:
        key = api_key or openai_key
        base = oa_base.rstrip("/")
        default_model = oa_suggested_model or DEFAULT_OPENAI_MODEL
        spec_provider = "openai"

    chosen_spec = models.get_spec(requested_model)
    if requested_model and chosen_spec and chosen_spec.provider != spec_provider:
        # 显式模型优先于猜测的供应商：--model deepseek-chat --provider anthropic
        # 最终仍会走 OpenAI 兼容协议，因为模型目录里 deepseek 属于 openai。
        spec_provider = chosen_spec.provider
        provider_name = chosen_spec.provider
        if provider_name == "anthropic":
            key = key or anthropic_key
            base = an_base.rstrip("/")
        else:
            key = key or openai_key
            base = oa_base.rstrip("/")
    resolved_model = models.resolve_model_id(requested_model) or requested_model or default_model

    small_model = settings.get("smallModel") or (
        "claude-haiku-4-5" if provider_name == "anthropic" else resolved_model
    )

    perm_mode = (
        permission_mode
        or settings.get("permissionMode")
        or "default"
    )
    if perm_mode not in PERMISSION_MODES:
        perm_mode = "default"

    permissions = json.loads(json.dumps(settings.get("permissions") or {}))
    permissions.setdefault("allow", [])
    permissions.setdefault("deny", [])
    permissions.setdefault("ask", [])
    permissions.setdefault("additionalDirectories", [])

    # ---- 终端字号（经典 conhost）-----------------------------------------
    # 优先级：真实环境变量 > .env / settings.json 的 env > ui.fontSize。
    # 注意 .env 里的值只存在于 settings_env，不会进入 os.environ，
    # 所以这里必须显式解析，否则 .env 里写 YOTCODE_FONT_SIZE 完全无效。
    # （老名字 CLAUDEPY_FONT_SIZE 仍然认）
    font_size = (
        resolve_font_size(_env_value("YOTCODE_FONT_SIZE", settings_env))
        or resolve_font_size(_env_value("CLAUDEPY_FONT_SIZE", settings_env))
        or resolve_font_size((settings.get("ui") or {}).get("fontSize"))
    )

    cfg = Config(
        cwd=cwd,
        model=resolved_model,
        provider=provider_name,
        small_model=small_model,
        max_tokens=int(settings.get("maxTokens") or 8192),
        max_turns=int(max_turns or settings.get("maxTurns") or 60),
        permission_mode=perm_mode,
        auto_compact=bool(settings.get("autoCompact", True)),
        compact_threshold=float(settings.get("compactThreshold") or 0.82),
        shell=settings.get("shell") or default_shell(),
        theme=settings.get("theme") or "yotcode",
        temperature=settings.get("temperature"),
        system_prompt_append=(
            system_prompt_append
            if system_prompt_append is not None
            else (settings.get("systemPromptAppend") or "")
        ),
        thinking=bool(settings.get("thinking", True)),
        permissions=permissions,
        hooks=settings.get("hooks") or {},
        mcp_servers=settings.get("mcpServers") or {},
        env=settings_env,
        ui=settings.get("ui") or {},
        font_size=font_size,
        allowed_tools=allowed_tools or [],
        disallowed_tools=disallowed_tools or [],
        api_key=key,
        base_url=base,
        mock=mock,
        verbose=verbose,
        debug=debug,
        settings_files=used_files,
        loaded_settings=settings,
        web_search_provider=(settings.get("webSearch") or {}).get("provider"),
        extra=settings,
    )
    if not cfg.mock and not cfg.api_key:
        # Leave config usable so the UI can show a friendly setup screen.
        cfg.mock = mock
    if cfg.mock:
        cfg.provider = "mock"
        if not requested_model:
            cfg.model = "mock-claude"
        cfg.base_url = "mock://local"
        cfg.api_key = ""
    cfg.ensure_dirs()
    return cfg
