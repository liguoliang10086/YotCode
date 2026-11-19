"""首次运行引导：生成带注释的用户级配置模板 + 配置命令行 PATH。

设计目标：用户执行 pip install 之后，
1. 第一次运行 yot 时，自动在 ~/.yotcode/ 下生成全注释的 .env 与 settings.json，
   用户想用哪个参数就删掉对应的注释符号，不必从零记忆格式。
2. 提供 `yot --setup-path`（或 python -m yotcode --setup-path）把 yot / yotcode
   所在目录写入用户级 PATH，解决 pip 装完后"命令找不到"的问题。

所有写入都遵循"文件已存在则绝不覆盖"原则，避免破坏用户的真实配置。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import home_dir

# ---------------------------------------------------------------------------
# 带注释的模板内容
# ---------------------------------------------------------------------------

# .env 模板：所有变量都注释掉，用户按需取消注释。
# 变量清单来自代码中实际读取的名字（config.py / apilog.py / console_font.py 等）。
ENV_TEMPLATE = """\
# ============================================================================
# YotCode 全局环境变量配置（~/.yotcode/.env）
# ----------------------------------------------------------------------------
# 所有行默认都是注释。想启用哪一项，就把行首的 # 删掉并填上你自己的值。
# 真实环境变量 > 本文件 > settings.json 的 env 字段。
# 类别说明：provider 只有两类——anthropic（Claude 官方）与 openai（所有兼容接口）。
# ============================================================================

# ---- A 类：Anthropic 官方 API（provider = anthropic）-----------------------
# 只需 API Key，Base URL 默认 https://api.anthropic.com，一般不用填。
# ANTHROPIC_API_KEY=sk-ant-你的密钥
# ANTHROPIC_BASE_URL=https://api.anthropic.com
# ANTHROPIC_AUTH_TOKEN=你的授权令牌        # 也可替代 API_KEY

# ---- B 类：OpenAI 兼容接口（provider = openai）-----------------------------
# 各家 Key 只需设置其中一个；对应的 Base URL 会自动识别，无需手填。
# OPENAI_API_KEY=sk-你的密钥
# OPENAI_BASE_URL=https://api.openai.com/v1  # 自建服务/网关才需要手填
# DEEPSEEK_API_KEY=sk-你的密钥               # 自动 https://api.deepseek.com/v1
# DASHSCOPE_API_KEY=sk-你的密钥              # 通义千问，自动兼容模式地址
# MOONSHOT_API_KEY=sk-你的密钥               # Kimi，自动地址
# ZHIPUAI_API_KEY=你的密钥                   # 智谱 GLM，自动地址
# CCPY_API_KEY=sk-你的密钥                   # 通用 openai key（旧变量名）

# ---- 模型 / 供应商 / shell（可选）------------------------------------------
# CCPY_MODEL=claude-sonnet-4-5               # 默认模型，可被 settings.json 覆盖
# CCPY_PROVIDER=anthropic                    # anthropic 或 openai
# CCPY_SHELL=bash                            # 指定 Bash 工具使用的 shell

# ---- 联网搜索的密钥（可选）--------------------------------------------------
# BRAVE_API_KEY=你的密钥
# TAVILY_API_KEY=你的密钥

# ---- YotCode 高级选项（可选）------------------------------------------------
# YOTCODE_HOME=~/.yotcode                    # 全局配置目录，可改到别处
# YOTCODE_SHELL=bash                         # 同 CCPY_SHELL，新变量名
# YOTCODE_FONT_SIZE=14                       # 经典 conhost 终端字号（像素高）
# YOTCODE_API_LOG=~/.yotcode/logs/api.log    # API 请求日志文件路径
"""

# settings.json 模板：JSONC（支持 // 注释），所有字段都注释掉。
SETTINGS_TEMPLATE = """\
{
  // ==========================================================================
  // YotCode 全局配置（~/.yotcode/settings.json）——支持 // 与 /* */ 注释
  // 所有字段默认注释。想启用哪个就删掉它前面的 // 并按需要改值。
  // 没写的字段使用内置默认值。项目级配置放在 <项目>/.yotcode/settings.json。
  // ==========================================================================

  // ---- 模型与供应商（provider 只有 anthropic / openai 两类）---------------
  // "provider": "anthropic",          // Claude 官方
  // "provider": "openai",             // OpenAI / DeepSeek / 千问 / Kimi / GLM / Ollama
  // "model": "claude-sonnet-4-5",     // 主模型名
  // "model": "deepseek-chat",         // openai 类示例
  // "smallModel": "claude-sonnet-4-5",// 轻量任务模型，省略则同主模型
  // "maxTokens": 8192,                // 单次回复最大 token 数
  // "maxTurns": 60,                   // 一轮输入内最多工具调用轮数
  // "temperature": 0.7,               // 采样温度，省略用服务端默认

  // ---- API 地址（Key 放 .env，这里一般只放 baseUrl）-----------------------
  // anthropic 类默认 https://api.anthropic.com，通常不用填 baseUrl。
  // openai 类各家地址不同，多数由 *_API_KEY 自动识别；自建/Ollama 才需手填：
  // "providers": {
  //   "anthropic": {
  //     "baseUrl": "https://api.anthropic.com"
  //   },
  //   "openai": {
  //     "baseUrl": "https://api.deepseek.com/v1"
  //   }
  // },

  // ---- 行为开关 ------------------------------------------------------------
  // "permissionMode": "default",      // default | acceptEdits | plan | bypassPermissions
  // "autoCompact": true,              // 上下文快满时自动压缩
  // "compactThreshold": 0.82,         // 触发压缩的上下文占比 0~1
  // "thinking": true,                 // 是否展示思考过程
  // "autoApproveEdits": false,        // 是否自动批准文件编辑
  // "shell": "bash",                  // Bash 工具使用的 shell
  // "theme": "yotcode",               // 主题名
  // "systemPromptAppend": "",         // 追加到系统提示词末尾

  // ---- 权限规则（列表会与项目级配置合并，全局 deny 不会被清掉）------------
  // "permissions": {
  //   "allow": ["Bash(npm test:*)", "Read(./src/**)"],
  //   "ask":   ["Bash(git push:*)"],
  //   "deny":  ["Read(./.env)"],
  //   "additionalDirectories": ["../shared-lib"]
  // },

  // ---- 钩子 / MCP / 联网搜索 ----------------------------------------------
  // "hooks": {
  //   "PreToolUse": [
  //     { "matcher": "Bash", "command": "python .yotcode/check_command.py" }
  //   ]
  // },
  // "mcpServers": {
  //   "filesystem": {
  //     "command": "npx",
  //     "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
  //   }
  // },
  // "webSearch": { "provider": "brave" },

  // ---- 环境变量（可放 Key，但别提交到 Git）--------------------------------
  // "env": {
  //   "DEEPSEEK_API_KEY": "sk-示例0000000000000000000000000000"
  // },

  // ---- 界面细节 ------------------------------------------------------------
  // "ui": {
  //   "showTips": true,
  //   "showThinking": true,
  //   "compactOutput": false,
  //   "checkpoints": true,
  //   "fontSize": 14
  // }
}
"""


def ensure_user_templates() -> list[str]:
    """首次运行时生成全注释的 ~/.yotcode/.env 与 settings.json。

    只在目标文件不存在时创建，绝不覆盖用户已有配置。返回本次创建的文件路径
    列表，供调用方打印提示。目录不存在时一并创建。
    """
    created: list[str] = []
    home = home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return created

    for filename, content in (
        (".env", ENV_TEMPLATE),
        ("settings.json", SETTINGS_TEMPLATE),
    ):
        target = home / filename
        if target.exists():
            continue
        try:
            target.write_text(content, encoding="utf-8")
            created.append(str(target))
        except OSError:
            pass
    return created


# ---------------------------------------------------------------------------
# PATH 配置
# ---------------------------------------------------------------------------

def _scripts_dir() -> Path:
    """返回当前 Python 环境安装 console script 的目录。"""
    try:
        import sysconfig
        path = sysconfig.get_path("scripts")
        if path:
            return Path(path)
    except Exception:
        pass
    return Path(sys.executable).parent


def _already_on_path(directory: Path) -> bool:
    wanted = str(directory).lower().rstrip("\\/")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and str(entry).lower().rstrip("\\/") == wanted:
            return True
    return False


def _add_path_windows(directory: Path) -> tuple[bool, str]:
    """把目录写入 Windows 用户级 PATH（注册表 HKCU\\Environment），并广播变更。"""
    try:
        import winreg
    except ImportError:
        return False, "无法导入 winreg（非 Windows 环境）"

    dir_str = str(directory)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        current = ""

    parts = [p for p in current.split(";") if p]
    if any(p.lower().rstrip("\\/") == dir_str.lower().rstrip("\\/") for p in parts):
        return False, "已在用户 PATH 中，无需重复添加"

    parts.append(dir_str)
    new_value = ";".join(parts)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_value)
        finally:
            winreg.CloseKey(key)
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"

    # 广播环境变量变更，让新开的终端立即看到（已开的窗口仍需重开）
    try:
        import ctypes
        from ctypes import wintypes
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment",
            0x0002, 5000, ctypes.byref(result),
        )
    except Exception:
        pass
    return True, "已写入用户 PATH，请重新打开终端后生效"


def _add_path_posix(directory: Path) -> tuple[bool, str]:
    """把 export PATH 行追加到用户的 shell 启动文件。"""
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    candidates = []
    if "zsh" in shell:
        candidates = [home / ".zshrc", home / ".zprofile"]
    elif "bash" in shell:
        candidates = [home / ".bashrc", home / ".bash_profile", home / ".profile"]
    else:
        candidates = [home / ".profile", home / ".bashrc"]

    line = f'export PATH="{directory}:$PATH"  # YotCode CLI'
    for rc in candidates:
        if not rc.exists():
            continue
        try:
            text = rc.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "YotCode CLI" in text and str(directory) in text:
            return False, f"已在 {rc} 中配置，无需重复添加"
        try:
            with rc.open("a", encoding="utf-8") as fh:
                fh.write("\n" + line + "\n")
            return True, f"已追加到 {rc}，请重新打开终端或执行 source {rc}"
        except OSError:
            continue
    return False, "未找到可写的 shell 启动文件，请手动把脚本目录加入 PATH"


def setup_path() -> tuple[bool, str, Path]:
    """把 yot / yotcode 所在目录加入用户级 PATH。返回 (是否改动, 说明, 目录)。"""
    directory = _scripts_dir()
    if os.name == "nt":
        changed, message = _add_path_windows(directory)
    else:
        changed, message = _add_path_posix(directory)
    return changed, message, directory
