"""YotCode —— 用 Python 重写的 Claude Code 风格终端 AI 编程助手。

代码阅读顺序建议：
  1. cli.py          程序入口：解析参数，决定进入 TUI 还是无头 -p 模式
  2. config.py       配置从哪来、API Key 怎么找、模型/供应商怎么定
  3. agent.py        核心循环：请求模型 → 执行工具 → 回填结果 → 继续
  4. providers/      各家 API 的流式协议适配（Anthropic / OpenAI 兼容）
  5. tools/          文件、Shell、网络、待办、子代理、MCP 等工具
  6. permissions.py  工具调用前的 allow / ask / deny 判断
  7. session.py      JSONL 会话持久化与恢复
  8. ui/             Textual 终端界面（app / widgets / modals / render）

命令行入口是 `yot`（老名字 claudepy / ccpy 仍然可用）。

更完整的架构说明见 docs/代码导读.md。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
