"""Generate deterministic screenshots for the redesigned TUI.

This is a development helper, not part of the test suite. It mounts sample
messages/tool cards directly so the visual design can be reviewed without
calling a real model.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotcode.config import build_config
from yotcode.tools import ToolResult
from yotcode.tools.file_tools import EditTool, ReadTool
from yotcode.tools.shell_tools import BashTool
from yotcode.types import ToolUseBlock
from yotcode.ui.app import YotCodeApp
from yotcode.ui.widgets import AssistantMessage, ToolCard

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        config = build_config(root, mock=True)
        app = YotCodeApp(config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)

            app.add_user(
                "Add a DeepSeek provider example to the docs and run the tests."
            )

            assistant = AssistantMessage(
                "I'll update the provider documentation, then run the test suite.\n\n"
                "**Plan**\n"
                "1. Add a DeepSeek section with environment variables and `settings.json`.\n"
                "2. Verify the example configs against the real CLI.\n"
                "3. Run the test suite."
            )
            app.transcript().mount(assistant)
            assistant.start()
            await pilot.pause(0.3)
            await assistant.finish()

            read_block = ToolUseBlock(
                "tool-read", "Read", {"file_path": "docs/使用手册.md"}
            )
            read_card = ToolCard(read_block, ReadTool())
            app.transcript().mount(read_card)
            read_card.set_result(
                ToolResult(
                    "1→# 使用手册\n"
                    "2→\n"
                    "3→## 安装\n"
                    "4→\n"
                    "5→```bash\n"
                    "6→pip install -e .\n"
                    "7→```"
                ),
                0.4,
            )

            before = "## 配置\n\n- Anthropic\n"
            after = (
                "## 配置\n\n"
                "- Anthropic\n"
                "- DeepSeek（OpenAI 兼容）\n"
                "- Ollama（本地，无需 API key）\n"
            )
            edit_block = ToolUseBlock(
                "tool-edit",
                "Edit",
                {
                    "file_path": "docs/使用手册.md",
                    "old_string": "- Anthropic\n",
                    "new_string": after,
                },
            )
            edit_card = ToolCard(
                edit_block, EditTool(),
                diff=("docs/使用手册.md", before, after),
            )
            app.transcript().mount(edit_card)
            edit_card.set_result(
                ToolResult("Updated docs/使用手册.md with 2 additions."), 0.8
            )

            bash_block = ToolUseBlock(
                "tool-bash", "Bash",
                {"command": "python -m pytest -q", "description": "Run tests"},
            )
            bash_card = ToolCard(bash_block, BashTool())
            app.transcript().mount(bash_card)
            bash_card.set_result(
                ToolResult("........................................................\n"
                           "72 passed in 6.41s"),
                2.6,
            )

            app.update_todos([
                {"content": "阅读现有文档结构", "status": "completed",
                 "activeForm": "阅读现有文档结构"},
                {"content": "补充 DeepSeek / Ollama 示例", "status": "in_progress",
                 "activeForm": "补充示例"},
                {"content": "运行测试并检查手册链接", "status": "pending",
                 "activeForm": "运行测试"},
            ])
            app.add_info("Status", app._status_report())
            app.add_info("Usage and cost", app._cost_report())
            app.prompt_area()._set_text("Also add an Ollama example")
            await pilot.pause(0.5)

            app.transcript().scroll_home(animate=False)
            await pilot.pause(0.3)
            (ROOT / "scripts" / "preview-top.svg").write_text(
                app.export_screenshot(), encoding="utf-8"
            )

            app.transcript().scroll_end(animate=False)
            await pilot.pause(0.3)
            (ROOT / "scripts" / "preview-bottom.svg").write_text(
                app.export_screenshot(), encoding="utf-8"
            )
            print("wrote preview-top.svg and preview-bottom.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
