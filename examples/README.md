# YotCode 配置示例

这些文件是**可直接改名的 settings.json 片段**，不是完整的配置系统。复制到项目里即可：

```bat
mkdir .yotcode
copy examples\settings.deepseek.json .yotcode\settings.json
```

然后在项目根目录创建 `.env`，写入对应的 Key：

```dotenv
DEEPSEEK_API_KEY=sk-...
```

## 文件说明

| 文件 | 用途 | 需要的环境变量 |
|---|---|---|
| `settings.deepseek.json` | DeepSeek V3 / R1 | `DEEPSEEK_API_KEY` |
| `settings.openai.json` | OpenAI GPT-4.1 / GPT-4o | `OPENAI_API_KEY` |
| `settings.qwen.json` | 通义千问 Qwen3 Coder | `DASHSCOPE_API_KEY` |
| `settings.ollama.json` | 本地 Ollama | 不需要 |
| `skills/release/SKILL.md` | 示例技能包（发布流程） | 不需要 |

## 示例技能包

`skills/release/SKILL.md` 是一个可以直接复制的 Skill 模板。用法：

```bat
mkdir .yotcode\skills
copy /r examples\skills\release .yotcode\skills\release
```

重启程序后，在 TUI 里输入 `/skills` 就能看到它；模型遇到"发版""打 tag"这类任务时
会自动调用 `Skill` 工具把正文读进来。

Skill 的完整说明见[中文使用手册](../docs/使用手册.md#13-skill-技能包)。

## 注意事项

- **不要提交 API Key**。把 Key 放在 `.env` 或系统环境变量里，`.gitignore` 已经忽略 `.env`。
- 项目里的配置优先级高于用户目录；`settings.local.json` 又高于 `settings.json`。
- `settings.json` 支持 `//` 和 `/* */` 注释（JSONC）。
- 想临时切换模型，不必改文件：`run.cmd --model gpt-4o --provider openai`。
- 同时配置 Anthropic 和 OpenAI Key 时，默认选 Anthropic；使用 GPT / DeepSeek 请显式写 `"provider": "openai"`。
