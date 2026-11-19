"""Headless UI smoke test: run the app with the mock provider."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotcode.agent import PermissionRequest
from yotcode.config import build_config
from yotcode.permissions import PermissionDecision
from yotcode.tools.shell_tools import BashTool
from yotcode.ui.app import YotCodeApp


async def wait_idle(app: YotCodeApp, pilot, timeout: float = 20.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    await pilot.pause()
    while app.busy and asyncio.get_event_loop().time() < deadline:
        await pilot.pause(0.05)
    await pilot.pause(0.2)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        config = build_config(root, mock=True)
        app = YotCodeApp(config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            print("mounted widgets:", len(app.query("*")))
            print("screen:", type(app.screen).__name__)
            # 1. plain prompt -> mock agent should call LS
            app.handle_submit("list the files please")
            await wait_idle(app, pilot, 25)
            from yotcode.ui.widgets import ToolCard, AssistantMessage, UserMessage
            print("tool cards:", len(app.query(ToolCard)))
            print("assistant blocks:", len(app.query(AssistantMessage)))
            print("user blocks:", len(app.query(UserMessage)))
            print("busy:", app.busy)
            # 2. todo prompt
            app.handle_submit("add a todo for tests")
            await wait_idle(app, pilot, 25)
            print("todos:", app.agent.todos)
            # 3. slash commands
            app.handle_submit("/cost")
            await pilot.pause(0.4)
            app.handle_submit("/status")
            await pilot.pause(0.4)
            app.handle_submit("/model")
            await pilot.pause(0.3)
            from yotcode.ui.modals import ModelPickerScreen
            print("model picker:", isinstance(app.screen, ModelPickerScreen))
            await pilot.press("escape")
            await pilot.pause(0.2)
            print("after escape:", type(app.screen).__name__)
            # 4. permission dialog
            request = PermissionRequest(
                tool=BashTool(),
                data={"command": "echo hi"},
                summary="echo hi",
                suggestion="Bash(echo:*)",
                preview=None,
                kind="tool",
                warning="",
            )
            result_box: list[PermissionDecision] = []

            async def ask() -> None:
                result_box.append(await app.ask_permission(request))

            app.run_worker(ask(), group="test", exclusive=True)
            await pilot.pause(0.4)
            print("permission screen:", type(app.screen).__name__)
            await pilot.press("a")
            await pilot.pause(0.3)
            print("decision:", result_box[0] if result_box else None)
            # 5. help
            app.action_help()
            await pilot.pause(0.3)
            print("help screen:", type(app.screen).__name__)
            await pilot.press("escape")
            await pilot.pause(0.2)
            # save screenshot
            out = Path(__file__).resolve().parents[1] / "scripts" / "ui-shot.svg"
            try:
                svg = app.export_screenshot()
                out.write_text(svg, encoding="utf-8")
                print("screenshot:", out)
            except Exception as exc:
                print("screenshot failed:", exc)
        print("session:", app.session.meta.id, "messages:", len(app.session.messages))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
