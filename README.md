# YotCode

一个用 Python 实现的终端 AI 编程助手，拥有和 Claude Code 一样的工作流：精致的 TUI、
流式 Markdown 输出、工具权限控制、项目记忆、会话管理、上下文压缩、子代理、Hooks、
自定义命令、MCP，以及用于脚本的 `-p` 无头模式。

支持 **Anthropic API** 以及任何 **OpenAI 兼容接口**（DeepSeek、通义千问、Kimi、智谱
GLM、Ollama、vLLM 等）。不依赖任何厂商 SDK，只需要 `textual`、`rich`、`httpx`。

```
██╗   ██╗ ██████╗ █████████ ██████╗  ██████╗ ██████╗  ███████╗
╚██╗ ██╔╝██╔═══██╗╚════██╝ ██╔════╝ ██╔═══██╗██╔══██╗ ██╔════╝
 ╚████╔╝ ██║   ██║ ══════╝ ██║      ██║   ██║██║  ██║ █████╗
  ╚██╔╝  ██║   ██║  ═════╝ ██║      ██║   ██║██║  ██║ ██╔══╝
   ██║   ╚██████╔╝   ═══╝ ╚██████╗ ╚██████╔╝██████╔╝ ███████╗
   ╚═╝    ╚═════╝    ═══╝  ╚═════╝  ╚═════╝ ╚═════╝  ╚══════╝
```

## 两个命令：`yot` 和 `yotcode`

安装后系统会提供**两个完全等价的命令**，指向同一个入口，任选其一即可：

| 命令 | 说明 |
| --- | --- |
| `yot` | 简短主命令（推荐） |
| `yotcode` | 完整名称命令 |

两者功能完全一致，例如 `yot --mock` 与 `yotcode --mock` 效果相同。在 Windows 上，
项目根目录还提供了 `yot.cmd` 和 `run.cmd` 启动器，双击即可运行。

## 安装

### 方式一：从 GitHub 远程安装（推荐）

需要 Python 3.10 及以上版本。一条命令即可从远程仓库安装，安装后自动获得 `yot` 和
`yotcode` 两个命令：

```bash
pip install "git+https://github.com/liguoliang10086/YotCode.git"
```

如果本机有多个 Python 版本，请使用对应的 pip，例如：

```bash
python -m pip install "git+https://github.com/liguoliang10086/YotCode.git"
```

安装完成后验证：

```bash
yot --version
# 或
yotcode --version
```

以后升级到最新版本：

```bash
pip install --upgrade "git+https://github.com/liguoliang10086/YotCode.git"
```

> 国内网络较慢时，可以为 pip 指定镜像源（镜像只加速依赖下载，仓库仍从 GitHub 拉取）：
> ```bash
> pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
>   "git+https://github.com/liguoliang10086/YotCode.git"
> ```

### 方式二：克隆源码本地安装（开发用）

```bash
git clone https://github.com/liguoliang10086/YotCode.git
cd YotCode
python -m venv .venv
```

激活虚拟环境：

```bash
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Windows (CMD)
.venv\Scripts\activate.bat
```

然后安装：

```bash
pip install -e .
# 需要跑测试时安装开发依赖
pip install -e ".[dev]"
```

Windows 用户也可以直接双击 `setup.cmd` 一键完成虚拟环境创建和安装，再用 `run.cmd`
启动。

### 离线体验（无需任何 API Key）

```bash
yot --mock
# Windows 也可以：run.cmd --mock
```

## 配置自己的大模型

YotCode 支持两类 provider（供应商）：

- `anthropic`：Anthropic 官方 API（Claude 系列模型）
- `openai`：**OpenAI 兼容接口**，包括 OpenAI、DeepSeek、通义千问、Kimi、智谱 GLM、
  Ollama、vLLM 等

配置有三种方式，**优先级从高到低**：命令行参数 → 环境变量 / `.env` 文件 →
`settings.json`。下面给出具体步骤。

### 第 1 步：准备 API Key

在你选择的服务商官网注册并创建 API Key。常见服务商：

| 服务商 | 需要的环境变量 | 自动识别的 Base URL |
| --- | --- | --- |
| Anthropic | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |
| 通义千问 | `DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | `https://api.moonshot.cn/v1` |
| 智谱 GLM | `ZHIPUAI_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` |

对于上表中的服务商，**只需设置对应的 `*_API_KEY`，Base URL 会自动识别**。

### 第 2 步：把密钥写入 `.env` 文件

在项目根目录创建 `.env` 文件（该文件已被 `.gitignore` 忽略，不会被提交）。参考
`.env.example`，按你的服务商填写其中一组：

```bash
# Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-你的密钥

# 或 DeepSeek（只需这一行，Base URL 自动识别）
DEEPSEEK_API_KEY=sk-你的密钥

# 或 OpenAI
OPENAI_API_KEY=sk-你的密钥
```

你也可以直接用系统环境变量（效果相同，且优先级高于 `.env`）：

```bash
# macOS / Linux
export DEEPSEEK_API_KEY=sk-你的密钥

# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-你的密钥"
# Windows CMD
set DEEPSEEK_API_KEY=sk-你的密钥
```

> 安全提示：不要把 API Key 写进 `settings.json` 或提交到 Git。推荐放在 `.env` 或环境变量中。

### 第 3 步：启动并指定模型

直接用命令行参数指定模型和供应商，最简单：

```bash
# Anthropic Claude
yot --model claude-sonnet-4-5

# DeepSeek
yot --provider openai --model deepseek-chat

# OpenAI
yot --provider openai --model gpt-4.1

# 通义千问
yot --provider openai --model qwen3-coder-plus

# Kimi
yot --provider openai --model kimi-k2-0905-preview

# 智谱 GLM
yot --provider openai --model glm-4.6
```

Windows 上把 `yot` 换成 `run.cmd`，例如 `run.cmd --model deepseek-chat`。

由于上述专用 Key 会触发自动推断，很多情况下**只写模型名就够了**：

```bash
yot --model deepseek-chat
```

如果同时配置了 Anthropic 和其他服务商的 Key，程序默认优先 Anthropic；此时请显式加
`--provider openai`。

### 常见服务商完整示例

**DeepSeek**

```bash
# .env
DEEPSEEK_API_KEY=sk-你的密钥
```
```bash
yot --provider openai --model deepseek-chat
# 推理模型：
yot --provider openai --model deepseek-reasoner
```

**OpenAI**

```bash
# .env
OPENAI_API_KEY=sk-你的密钥
```
```bash
yot --provider openai --model gpt-4.1
```

**通义千问（DashScope）**

```bash
# .env
DASHSCOPE_API_KEY=sk-你的密钥
```
```bash
yot --provider openai --model qwen3-coder-plus
```

**Kimi / Moonshot**

```bash
# .env
MOONSHOT_API_KEY=sk-你的密钥
```
```bash
yot --provider openai --model kimi-k2-0905-preview
```

**智谱 GLM**

```bash
# .env
ZHIPUAI_API_KEY=你的密钥
```
```bash
yot --provider openai --model glm-4.6
```

**Ollama（本地模型，无需 API Key）**

先安装并启动 Ollama，然后拉取模型：

```bash
ollama serve
ollama pull qwen2.5-coder
```

再指定本地地址：

```bash
yot --provider openai --base-url http://localhost:11434/v1 --model qwen2.5-coder
```

### 第 4 步（可选）：写入 `settings.json` 永久保存

如果不想每次都敲参数，可以把配置写进配置文件。配置按以下顺序合并，后面的覆盖前面的：

1. `~/.yotcode/settings.json`（全局）
2. `<项目>/.claude/settings.json`
3. `<项目>/.yotcode/settings.json`（项目级，推荐）
4. `<项目>/.claude/settings.local.json`
5. `<项目>/.yotcode/settings.local.json`

在项目根目录创建 `.yotcode/settings.json`（支持注释的 JSONC），例如 DeepSeek 配置：

```jsonc
{
  "model": "deepseek-chat",
  "provider": "openai",
  "maxTokens": 8192,
  "maxTurns": 60,
  "autoCompact": true,
  "thinking": true,
  "providers": {
    "openai": {
      "baseUrl": "https://api.deepseek.com/v1"
    }
  }
}
```

API Key 仍建议放在 `.env` 中。如果确实要写进配置，可用 `env` 字段：

```jsonc
{
  "env": {
    "DEEPSEEK_API_KEY": "sk-你的密钥"
  }
}
```

也可以从 `examples/` 目录复制现成模板：

```bash
mkdir -p .yotcode
cp examples/settings.deepseek.json .yotcode/settings.json
```

`examples/` 提供 DeepSeek、OpenAI、通义千问、Ollama 的配置示例。

### 第 5 步：验证与临时切换

进入 TUI 后可用以下命令查看和切换模型：

- `/config`：查看当前加载的配置
- `/model`：在界面内切换模型
- `/doctor`：检查环境与配置是否正确

临时用命令行验证（不进入 TUI）：

```bash
yot -p "用一句话介绍你自己"
yot -p --output-format json "列出当前目录的文件"
```

更多 CLI 参数：

```bash
yot [prompt]                  # 交互 TUI（可带初始 prompt）
yot -p "explain this repo"    # 无头模式：打印结果后退出
yot --continue                # 继续最近的会话
yot --resume <session-id>     # 恢复指定会话
yot --permission-mode acceptEdits
yot --mock                    # 离线演示，无需 Key
yot --version
```

## 功能特性

- **流式聊天 TUI** — Markdown、思考块、语法高亮工具卡片、内联 diff、待办面板、
  token/费用状态栏、提示符历史和 `@文件` 补全。
- **真实工具** — `Read`、`Write`、`Edit`、`MultiEdit`、`LS`、`Glob`、`Grep`、
  `Bash`、`BashOutput`、`KillShell`、`WebFetch`、`WebSearch`、`TodoWrite`、`Task`
  （子代理）、`TaskPlan`、`AskUserQuestion`、`ExitPlanMode`、`Skill`，以及 MCP 工具。
- **并发工具执行** — 只读工具和子代理可在同一轮并行运行，有副作用的工具保持严格顺序。
- **权限控制** — `default`、`acceptEdits`、`plan`、`bypassPermissions` 四种模式；
  允许/询问/拒绝规则；`Shift+Tab` 切换模式。
- **会话管理** — 每轮对话以 JSONL 保存到 `~/.yotcode/projects/<slug>/`，支持
  `/resume`、`--continue`、`--resume`、`--list-sessions`、`/export`。
- **上下文管理** — 达到上下文窗口 82% 时自动压缩；`/compact [指令]`。
- **项目记忆** — `CLAUDE.md` / `YOTCODE.md`、父目录、`@imports`、`#笔记` 快捷写法。
- **自定义斜杠命令** — `.yotcode/commands/*.md`。
- **子代理** — 内置 `general-purpose` 与只读 `Explore`，也支持 `.yotcode/agents/*.md`。
- **任务编排** — `TaskPlan` 声明依赖图，调度器并发运行独立任务。
- **检查点与回退** — 每次文件修改前自动快照；`/checkpoints`、`/revert [id]`。
- **技能包** — `.yotcode/skills/<name>/SKILL.md`，按需加载。
- **Hooks** — `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SessionEnd`。
- **MCP** — stdio 服务器，`tools/list` + `tools/call` 代理为 `mcp__server__tool`。
- **无头模式** — `yot -p "…"`、`--output-format json|stream-json`、stdin 输入。

## 键盘快捷键

| 按键 | 作用 |
| --- | --- |
| `Enter` | 发送 |
| `Shift+Enter` / `Ctrl+J` | 换行 |
| `Esc` | 打断当前生成 |
| `Ctrl+C` | 打断 · 清空输入 · 连按两次退出 |
| `Ctrl+D` | 输入为空时退出 |
| `Shift+Tab` | 切换权限模式 |
| `Ctrl+P` | 命令面板 |
| `Ctrl+R` | 恢复会话 |
| `Ctrl+O` | 展开 / 折叠工具输出 |
| `Ctrl+L` | 清屏 |
| `Ctrl+T` | 显示待办 |
| `F1` | 帮助 |
| `Tab` | 接受斜杠命令或 `@文件` 补全 |
| `Up` / `Down` | 提示符历史 / 补全项 |

## 内置命令

`/help` · `/clear` · `/compact` · `/cost` · `/status` · `/doctor` · `/model` ·
`/config` · `/permissions` · `/memory` · `/init` · `/todos` · `/agents` ·
`/skills` · `/checkpoints` · `/revert` · `/mcp` · `/resume` · `/export` ·
`/add-dir` · `/exit`

此外 `!命令` 可直接执行 shell 命令，`#笔记` 可追加到 `CLAUDE.md`。

## 文档

- [中文使用手册](docs/使用手册.md) — 安装、模型配置、TUI 使用、权限、工具、会话、
  Hooks、MCP 和无头模式。
- [中文代码导读](docs/代码导读.md) — 架构与模块说明。
- [配置示例](examples/README.md) — 可直接复制的 `settings.json` 模板。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest
python scripts/smoke.py
python scripts/ui_smoke.py
python scripts/ui_interactions.py
```

## 项目结构

```
yotcode/
  agent.py        agent 循环：流式、工具、权限、压缩、子代理
  cli.py          命令行入口 + 无头运行
  config.py       配置合并、凭据、模型/provider 解析
  permissions.py  允许/询问/拒绝规则引擎
  session.py      JSONL 持久化、恢复、导出
  types.py        消息/块数据模型
  checkpoints.py  文件快照与恢复
  tasks.py        任务依赖图
  prompts.py      系统提示词组装
  memory.py       项目记忆
  hooks.py        生命周期 Hooks
  skills.py       技能包
  subagents.py    内置与自定义子代理
  providers/      anthropic.py · openai_compat.py · mock.py
  tools/          文件 / shell / web / 交互 / 编排工具与 MCP
  ui/             TUI：app.py · modals.py · widgets.py · render.py · theme.tcss
tests/            pytest 测试（离线，无需 API Key）
examples/         DeepSeek / OpenAI / 通义千问 / Ollama 配置示例
docs/             使用手册.md · 代码导读.md
scripts/          冒烟测试脚本
```

## License

MIT
