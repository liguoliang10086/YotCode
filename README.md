# YotCode

一个用 Python 实现的终端 AI 编程助手，拥有和 Claude Code 一样的工作流：精致的 TUI、
流式 Markdown 输出、工具权限控制、项目记忆、会话管理、上下文压缩、子代理、Hooks、
自定义命令、MCP，以及用于脚本的 `-p` 无头模式。

支持 **Anthropic API** 以及任何 **OpenAI 兼容接口**（DeepSeek、通义千问、Kimi、智谱
GLM、Ollama、vLLM 等）。不依赖任何厂商 SDK，只需要 `textual`、`rich`、`httpx`。

```
██╗   ██╗ ██████╗ ████████╗ ██████╗  ██████╗ ██████╗  ███████╗
╚██╗ ██╔╝██╔═══██╗╚══██╔══╝██╔════╝ ██╔═══██╗██╔══██╗ ██╔════╝
 ╚████╔╝ ██║   ██║   ██║   ██║      ██║   ██║██║  ██║ █████╗
  ╚██╔╝  ██║   ██║   ██║   ██║      ██║   ██║██║  ██║ ██╔══╝
   ██║   ╚██████╔╝   ██║   ╚██████╗ ╚██████╔╝██████╔╝ ███████╗
   ╚═╝    ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝ ╚═════╝  ╚══════╝
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

#### 找不到 `yot` 命令？配置 PATH

`pip install` 会把 `yot` / `yotcode` 装到当前 Python 环境的 `Scripts`（Windows）或
`bin`（macOS/Linux）目录。如果这个目录不在 PATH，直接敲 `yot` 会提示"不是内部或
外部命令"。用下面任一方式解决：

```bash
# 方式 1：让程序自动把命令目录写入"用户级 PATH"（推荐，一次即可）
python -m yotcode --setup-path
# 或：yot --setup-path（前提是 yot 已经能运行）

# 方式 2：一键安装脚本（安装 + PATH + 生成模板，全帮你做）
python install.py
# 已经 pip 装过、只想配 PATH：python install.py --path-only
```

`--setup-path` 会修改**用户级** PATH（Windows 写入注册表 HKCU，macOS/Linux 追加到
shell 启动文件），**不需要管理员权限**，也**不会改动系统级 PATH**。执行后需要
**重新打开终端**才会生效。

> 如果你不想改 PATH，也可以直接用 `python -m yotcode` 来运行，效果完全一样：
> `python -m yotcode --version`、`python -m yotcode --model deepseek-chat`。

#### 首次运行：自动生成配置模板

第一次运行 `yot`（或执行 `python -m yotcode --setup`）时，程序会自动在全局配置目录
创建两个**带完整注释的模板文件**：

- `~/.yotcode/.env` —— 所有环境变量，全部注释掉
- `~/.yotcode/settings.json` —— 所有配置参数，全部注释掉

你只要把想用的那一行前面的 `#`（或 `//`）删掉、填上自己的值即可，不用从零记格式。
已存在的文件**绝不会被覆盖**。想随时查看这份指引：

```bash
python -m yotcode --setup
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

YotCode 把模型供应商归为**两大类**（配置字段 `provider` 只有这两个取值），
理解它们的区别是配置的关键：

| provider | 适用对象 | API Key 环境变量 | Base URL |
| --- | --- | --- | --- |
| `anthropic` | Anthropic 官方（Claude 系列） | `ANTHROPIC_API_KEY` | 默认 `https://api.anthropic.com`，一般不用填 |
| `openai` | 所有 **OpenAI 兼容接口** | 见下方各家专用变量 | 多数自动识别，自建服务 / Ollama 需手填 |

**如何区分**：判断标准是"接口协议"，不是"厂商名字"。
- Anthropic 自家的 Claude API → 用 `anthropic`
- 其他全部（OpenAI 官方、DeepSeek、通义千问、Kimi、智谱 GLM、Ollama、vLLM、
  自建网关）→ 都用 `openai`，因为它们兼容 OpenAI 的 `/v1/chat/completions` 协议

配置有三种方式，**优先级从高到低**：命令行参数 → 环境变量 / `.env` 文件 →
`settings.json`。

### 两类 API 的配置示例（假例子，照格式填空即可）

下面用**虚构的 Key** 演示两类配置长什么样。把 `sk-示例...` 换成你自己的真实 Key。

**A 类 · `anthropic`（Anthropic 官方 Claude）**

`.env` 文件（项目根目录或 `~/.yotcode/.env`）：
```bash
ANTHROPIC_API_KEY=sk-ant-示例0000000000000000000000000000
```

`settings.json`：
```jsonc
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "maxTokens": 8192
}
```

启动：只配了 Anthropic Key 时直接 `yot` 即可（会自动选 anthropic）。

**B 类 · `openai`（OpenAI 兼容接口，以 DeepSeek 为例）**

`.env` 文件：
```bash
DEEPSEEK_API_KEY=sk-示例0000000000000000000000000000
```

`settings.json`：
```jsonc
{
  "provider": "openai",
  "model": "deepseek-chat",
  "providers": {
    "openai": {
      "baseUrl": "https://api.deepseek.com/v1"
    }
  }
}
```

启动：`yot --model deepseek-chat`

> **两类 Key 不要混用**：`ANTHROPIC_API_KEY` 只服务于 anthropic 类；各家专用 Key
> （`DEEPSEEK_API_KEY` 等）只服务于 openai 类。若两类 Key 同时存在，程序默认选
> anthropic，此时想走 openai 类请显式加 `--provider openai`。

#### 为什么 B 类示例有 `baseUrl`，A 类却没有？

因为两类对地址的需求不同：

- **`anthropic` 类**：官方接口地址是固定的 `https://api.anthropic.com`，程序已经内置为
  默认值。所以上面的 A 类示例**不需要写 `baseUrl`**，只填 Key 就能用。
  只有当你用**自建的 Anthropic 兼容网关 / 代理**时，才需要配置 `baseUrl`：

  ```jsonc
  {
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "providers": {
      "anthropic": { "baseUrl": "https://你的网关地址/v1" }
    }
  }
  ```
  或环境变量 `ANTHROPIC_BASE_URL=https://你的网关地址/v1`。

- **`openai` 类**：这类接口由很多不同厂商提供，地址各不相同。好消息是——用各家专用
  Key（`DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 等）时，程序会**自动识别**对应的
  `baseUrl`，所以也可以不写。但如果你用**通用 Key**（`OPENAI_API_KEY` / `CCPY_API_KEY`）
  或**自建服务**（Ollama / vLLM / 网关），就必须显式给出 `baseUrl`，否则不知道往哪发请求。

  地址也可以放 `.env`：`OPENAI_BASE_URL=https://api.deepseek.com/v1`，
  或命令行 `--base-url https://api.deepseek.com/v1`。

一句话总结：**anthropic 默认地址固定、基本不用填；openai 视情况，专用 Key 自动识别、
通用 Key / 自建服务必须填**。两类的 `baseUrl` 都能写在 `settings.json` 的
`providers.<类别>.baseUrl` 里，也能用环境变量，效果等价。

### 第 1 步：准备 API Key

在你选择的服务商官网注册并创建 API Key。常见服务商与对应的环境变量：

| 类别 | 服务商 | Key 环境变量 | 自动识别的 Base URL |
| --- | --- | --- | --- |
| anthropic | Anthropic | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` |
| openai | OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| openai | DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |
| openai | 通义千问 | `DASHSCOPE_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| openai | Kimi / Moonshot | `MOONSHOT_API_KEY` | `https://api.moonshot.cn/v1` |
| openai | 智谱 GLM | `ZHIPUAI_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` |

对于上表中的服务商，**只需设置对应的 `*_API_KEY`，Base URL 会自动识别**，无需手填。

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

1. `~/.yotcode/settings.json`（全局，对所有项目生效）
2. `<项目>/.yotcode/settings.json`（项目级，推荐）
3. `<项目>/.yotcode/settings.local.json`（项目本地，通常不提交到 Git）

> `<项目>` 指的是你运行 `yot` 时的当前目录。全局配置放一次，所有项目都能用；
> 项目级配置只影响当前项目，且会覆盖全局里相同的字段。命令行参数优先级最高。

#### 全局配置例子（假例子）

放在 `~/.yotcode/settings.json`，对所有项目生效。下面是一个"内容大概长这样"的完整示例，
字段都能按需删减，没写的用默认值：

```jsonc
{
  // ---- 模型与供应商 ----
  "model": "deepseek-chat",        // 主模型名
  "provider": "openai",            // 两大类之一：anthropic 或 openai
  "smallModel": "deepseek-chat",   // 轻量任务用的模型，省略则同主模型
  "maxTokens": 8192,               // 单次回复最大 token 数
  "maxTurns": 60,                  // 一轮对话最多工具调用轮数

  // ---- 行为开关 ----
  "permissionMode": "default",     // 权限模式，见下方参数表
  "autoCompact": true,             // 上下文快满时自动压缩
  "compactThreshold": 0.82,        // 触发压缩的上下文占比
  "thinking": true,                // 是否展示模型思考过程
  "theme": "yotcode",              // 主题名

  // ---- API Key 地址（Key 本身放 .env，这里只放地址）----
  "providers": {
    "openai": {
      "baseUrl": "https://api.deepseek.com/v1"
    }
  },

  // ---- 权限规则 ----
  "permissions": {
    "allow": ["Bash(npm test:*)", "Read(./src/**)"],
    "ask":   ["Bash(git push:*)"],
    "deny":  ["Read(./.env)"]
  }
}
```

对应的全局密钥文件 `~/.yotcode/.env`（假例子）：

```bash
# openai 类：DeepSeek
DEEPSEEK_API_KEY=sk-示例0000000000000000000000000000

# 如果是 anthropic 类，则改成这一行（二选一，别混）
# ANTHROPIC_API_KEY=sk-ant-示例0000000000000000000000000000
```

#### 项目级配置例子（假例子）

在项目根目录建 `.yotcode/settings.json`，只需写要**覆盖全局**的字段。例如某项目想换成
Claude、并额外禁止读密钥文件：

```jsonc
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "permissions": {
    "deny": ["Read(./secrets/*)"]
  }
}
```

> API Key 建议始终放 `.env` 或环境变量。若确实要写进 `settings.json`，可用 `env` 字段
> （注意别提交到 Git）：
> ```jsonc
> { "env": { "DEEPSEEK_API_KEY": "sk-示例0000000000000000000000000000" } }
> ```

#### 全部可配置参数一览

`settings.json` 支持以下字段（均为可选，省略则用默认值）：

| 参数 | 类型 / 默认 | 作用 |
| --- | --- | --- |
| `model` | 字符串 / `null` | 主模型名，如 `claude-sonnet-4-5`、`deepseek-chat` |
| `provider` | `anthropic`\|`openai` / `null` | 供应商类别，见上文两类说明 |
| `smallModel` | 字符串 / `null` | 轻量任务用的模型，省略则同主模型 |
| `maxTokens` | 整数 / `8192` | 单次回复允许的最大 token 数 |
| `maxTurns` | 整数 / `60` | 一轮用户输入内最多进行多少轮工具调用 |
| `permissionMode` | 字符串 / `default` | 权限模式：`default`、`acceptEdits`、`plan`、`bypassPermissions` |
| `autoCompact` | 布尔 / `true` | 上下文接近上限时是否自动压缩历史 |
| `compactThreshold` | 浮点 / `0.82` | 触发自动压缩的上下文占比（0~1） |
| `thinking` | 布尔 / `true` | 是否展示模型的思考内容 |
| `temperature` | 浮点 / `null` | 采样温度，省略则用服务端默认 |
| `shell` | 字符串 / `null` | 指定 Bash 工具使用的 shell，省略则自动探测 |
| `theme` | 字符串 / `yotcode` | 界面主题名 |
| `systemPromptAppend` | 字符串 / `""` | 追加到系统提示词末尾的内容 |
| `autoApproveEdits` | 布尔 / `false` | 是否自动批准文件编辑类操作 |
| `permissions` | 对象 | 权限规则，含 `allow`/`deny`/`ask`/`additionalDirectories` 四个字符串数组 |
| `hooks` | 对象 / `{}` | 生命周期钩子，按事件名配置命令列表 |
| `mcpServers` | 对象 / `{}` | MCP 服务器配置，工具会以 `mcp__服务名__工具名` 形式加入 |
| `env` | 对象 / `{}` | 注入到环境变量的键值对，可放 API Key（别提交） |
| `providers` | 对象 / `{}` | 各供应商的 `apiKey` 与 `baseUrl`（`anthropic` / `openai` 两个子对象） |
| `webSearch` | 对象 | 联网搜索配置，含 `provider` 字段 |
| `ui` | 对象 | 界面细节，见下表 |

`ui` 子字段：

| 参数 | 类型 / 默认 | 作用 |
| --- | --- | --- |
| `ui.showTips` | 布尔 / `true` | 是否显示提示文案 |
| `ui.showThinking` | 布尔 / `true` | 是否在界面显示思考块 |
| `ui.compactOutput` | 布尔 / `false` | 是否紧凑显示工具输出 |
| `ui.checkpoints` | 布尔 / `true` | 改文件前是否自动留检查点（供 `/revert` 回退） |
| `ui.fontSize` | 整数 / `null` | 经典 conhost 下的终端字号（像素高），也可写 `.env` 的 `YOTCODE_FONT_SIZE` |

也可以从 `examples/` 目录复制现成模板：

```bash
mkdir -p .yotcode
cp examples/settings.deepseek.json .yotcode/settings.json
```

`examples/` 提供 DeepSeek、OpenAI、通义千问、Ollama 的配置示例，以及两个"全参数带注释"
的参考模板：`.env.full.example`（项目根目录）和 `examples/settings.full.example.json`。

### 全注释模板（首次运行自动生成，想用哪个就取消注释）

首次运行 `yot` 时会在 `~/.yotcode/` 自动生成下面两个文件（已存在则不覆盖）。
所有项都注释着，用户想用哪个就删掉行首的注释符号。#/.env 用 `#`，settings.json 用 `//`。

**`~/.yotcode/.env`（全注释示例）**

```bash
# ---- A 类：Anthropic 官方 API（provider = anthropic）----
# ANTHROPIC_API_KEY=sk-ant-你的密钥
# ANTHROPIC_BASE_URL=https://api.anthropic.com        # 自建网关才需要
# ANTHROPIC_AUTH_TOKEN=你的授权令牌

# ---- B 类：OpenAI 兼容接口（provider = openai）----
# OPENAI_API_KEY=sk-你的密钥
# OPENAI_BASE_URL=https://api.openai.com/v1           # 通用 Key / 自建服务才需要
# DEEPSEEK_API_KEY=sk-你的密钥                        # 自动识别 baseUrl
# DASHSCOPE_API_KEY=sk-你的密钥                       # 通义千问，自动识别
# MOONSHOT_API_KEY=sk-你的密钥                        # Kimi，自动识别
# ZHIPUAI_API_KEY=你的密钥                            # 智谱 GLM，自动识别
# CCPY_API_KEY=sk-你的密钥                            # 通用 openai key（旧名）

# ---- 模型 / 供应商 / shell ----
# CCPY_MODEL=claude-sonnet-4-5
# CCPY_PROVIDER=anthropic                             # 或 openai
# CCPY_SHELL=bash

# ---- 联网搜索 ----
# BRAVE_API_KEY=你的密钥
# TAVILY_API_KEY=你的密钥

# ---- YotCode 高级选项 ----
# YOTCODE_HOME=~/.yotcode
# YOTCODE_SHELL=bash
# YOTCODE_FONT_SIZE=14
# YOTCODE_API_LOG=~/.yotcode/logs/api.log
```

**`~/.yotcode/settings.json`（全注释示例，支持 JSONC）**

```jsonc
{
  // ---- 模型与供应商（provider 只有 anthropic / openai 两类）----
  // "provider": "anthropic",
  // "provider": "openai",
  // "model": "claude-sonnet-4-5",
  // "model": "deepseek-chat",
  // "smallModel": "claude-sonnet-4-5",
  // "maxTokens": 8192,
  // "maxTurns": 60,
  // "temperature": 0.7,

  // ---- API 地址：anthropic 默认固定；openai 专用 Key 自动识别 ----
  // "providers": {
  //   "anthropic": { "baseUrl": "https://api.anthropic.com" },
  //   "openai":    { "baseUrl": "https://api.deepseek.com/v1" }
  // },

  // ---- 行为开关 ----
  // "permissionMode": "default",
  // "autoCompact": true,
  // "compactThreshold": 0.82,
  // "thinking": true,
  // "autoApproveEdits": false,
  // "shell": "bash",
  // "theme": "yotcode",
  // "systemPromptAppend": "",

  // ---- 权限规则 ----
  // "permissions": {
  //   "allow": ["Bash(npm test:*)", "Read(./src/**)"],
  //   "ask":   ["Bash(git push:*)"],
  //   "deny":  ["Read(./.env)"],
  //   "additionalDirectories": ["../shared-lib"]
  // },

  // ---- 钩子 / MCP / 联网搜索 ----
  // "hooks": {
  //   "PreToolUse": [
  //     { "matcher": "Bash", "command": "python .yotcode/check_command.py" }
  //   ]
  // },
  // "mcpServers": {
  //   "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
  // },
  // "webSearch": { "provider": "brave" },

  // ---- 环境变量（可放 Key，但别提交 Git）----
  // "env": { "DEEPSEEK_API_KEY": "sk-示例0000000000000000000000000000" },

  // ---- 界面细节 ----
  // "ui": {
  //   "showTips": true,
  //   "showThinking": true,
  //   "compactOutput": false,
  //   "checkpoints": true,
  //   "fontSize": 14
  // }
}
```

> 提示：全注释的 `settings.json` 是合法的（程序会剥离 `//` 注释后按 JSON 解析，
> 结果相当于空配置 `{}`），所以首次生成后即使什么都不改，程序也能正常启动。

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
yot --resume                  # 弹出会话选择器（不带 id 时，类似 claude -r）
yot --resume <session-id>     # 按 id 精确恢复指定会话
yot --image shot.png -p "解释这张图"  # 附加图片（可重复传）
yot --file main.py -p "审查这个文件"  # 附加文本文件（可重复传）
yot --permission-mode acceptEdits
yot --mock                    # 离线演示，无需 Key
yot --setup-path              # 把命令目录加入用户 PATH（解决"找不到命令"）
yot --setup                   # 打印首次配置指引 + 生成全注释模板
yot --version
```

### 发送图片和文件给大模型

除了文字，你还可以把截图、图片和文件一起发给模型。四种入口最终都会变成一条
"带附件"的用户消息：

| 入口 | 操作 | 适用场景 |
| --- | --- | --- |
| 命令行参数 | `yot -p "解释这张图" --image shot.png` | 无头模式 / 脚本 |
| 剪贴板截图 | 输入框按 `Ctrl+Shift+V`（或 `Ctrl+Alt+V`） | 截图后直接发给模型 |
| `@文件` 智能识别 | 输入 `@图片.png` 或 `@代码.py` | 在消息里引用文件 |
| 粘贴 / 拖放路径 | 把文件路径粘贴或拖进输入框 | 从资源管理器复制文件 |

**1. 命令行附加（`--image` / `--file`）**

两个参数都可重复传多次，程序按扩展名自动分辨：图片（`png/jpg/jpeg/gif/webp/bmp/tif/tiff`）
读成图片块，其他文件读成文本块：

```bash
yot -p "这两张图有什么区别？" --image before.png --image after.png
yot -p "审查这个文件的改动" --file src/main.py
yot --image shot.png          # 交互 TUI 里带着图片启动
```

**2. 剪贴板截图（`Ctrl+Shift+V`）**

截图到剪贴板后，在输入框按 `Ctrl+Shift+V`（部分终端用 `Ctrl+Alt+V`）即可附加。
终端里的普通 `Ctrl+V` 只会传文本、无法携带位图，所以使用独立按键。
Pillow 已作为项目必需依赖，`pip install` 后即可直接使用，无需额外安装：
若当前环境读取失败会给出友好提示，不影响其他附件方式。

**3. `@文件` 智能识别**

输入 `@` 后的补全方式和以前一样；发送时 Agent 按扩展名智能处理：

- 图片：作为真正的图片发送，模型能"看到"画面内容；
- 文本：沿用原来的 `<file>` 包装，提示词保持一致；
- 其他二进制：给出"这是二进制文件"的说明文本，避免乱码。

```text
看看这张截图里哪里错了 @./screenshots/error.png
参考一下这个模块 @yotcode/agent.py
```

**4. 粘贴 / 拖放文件路径**

从文件管理器复制文件（或把文件拖进终端窗口），输入框会自动把路径识别成附件，
不会把一长串路径文本发给模型。已附加的图片/文件显示为输入框下方的筹码，
点击 `×` 可在发送前移除。

**限制与提示**

- 单个文件上限 5MB（与内置 `Read` 工具读图片一致），一次最多 10 个附件；
- 文本附件超过 6 万字符会截断；
- 当前模型不支持图片时会打印警告，仍会发送，由模型端决定如何响应；
- 附件随会话一起存入 JSONL，恢复后仍可回放；
- 图片以 base64 进入请求体，超大图片会增加 token 消耗与费用。

### 恢复会话：`--resume` 选择器

`--resume` 现在支持不带 id，体验类似 `claude -r`：

```bash
yot --resume                  # 弹出会话选择器，输入序号恢复
yot --resume <session-id>     # 带 id 时按 id 精确恢复（可跨项目）
yot --continue                # 直接继续当前目录最近的会话
```

不带 id 时，终端会列出当前目录的会话（日期、标题、轮数），输入序号回车确认。
这样即使是几天前的会话也能从列表里找回来，而不再只能取最近一条。
TUI 里按 `Ctrl+R` 或输入 `/resume` 打开的是图形化选择器，两者都可以使用。

## 功能特性

- **流式聊天 TUI** — Markdown、思考块、语法高亮工具卡片、内联 diff、待办面板、
  token/费用状态栏、提示符历史和 `@文件` 补全。
- **真实工具** — `Read`、`Write`、`Edit`、`MultiEdit`、`LS`、`Glob`、`Grep`、
  `Bash`、`BashOutput`、`KillShell`、`WebFetch`、`WebSearch`、`TodoWrite`、`Task`
  （子代理）、`TaskPlan`、`AskUserQuestion`、`ExitPlanMode`、`Skill`，以及 MCP 工具。
- **并发工具执行** — 只读工具和子代理可在同一轮并行运行，有副作用的工具保持严格顺序。
- **权限控制** — `default`、`acceptEdits`、`plan`、`bypassPermissions` 四种模式；
  允许/询问/拒绝规则；`Shift+Tab` 切换模式。
- **图片 / 文件附件** — `--image` / `--file` 命令行附加、剪贴板截图（`Ctrl+Shift+V`）、
  `@图片` 智能识别、粘贴/拖放路径；附件显示为可移除筹码，随会话持久化并可在恢复后回放。
- **会话管理** — 每轮对话以 JSONL 保存到 `~/.yotcode/projects/<slug>/`，支持
  `/resume`、`--continue`、`--resume`（不带 id 弹选择器）、`--list-sessions`、`/export`。
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
| `Ctrl+Shift+V` / `Ctrl+Alt+V` | 从剪贴板附加截图 |
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
  cli.py          命令行入口 + 无头运行（含 --setup-path / --setup）
  config.py       配置合并、凭据、模型/provider 解析
  attachments.py  图片/文件附件转换：路径/剪贴板 → 内容块，大小与类型校验
  setup.py        首次运行模板生成 + PATH 配置逻辑
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
install.py        跨平台一键安装脚本（安装 + PATH + 生成模板）
tests/            pytest 测试（离线，无需 API Key）
examples/         DeepSeek / OpenAI / 通义千问 / Ollama 配置示例 + settings.full.example.json
.env.full.example 全量环境变量模板（首次运行自动生成的同款）
docs/             使用手册.md · 代码导读.md
scripts/          冒烟测试脚本
```

## License

MIT
