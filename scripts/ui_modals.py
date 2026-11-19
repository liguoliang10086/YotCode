"""Render modal screenshots for visual review."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotcode.agent import PermissionRequest
from yotcode.commands import help_markdown
from yotcode.config import build_config
from yotcode.tools.shell_tools import BashTool
from yotcode.ui.app import YotCodeApp
from yotcode.ui.modals import HelpScreen, ModelPickerScreen, PermissionScreen

ROOT = Path(__file__).resolve().parents[1]


async def shot(app: YotCodeApp, pilot, name: str) -> None:
    await pilot.pause(0.5)
    (ROOT / "scripts" / f"{name}.svg").write_text(
        app.export_screenshot(), encoding="utf-8"
    )
    print("wrote", name)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = build_config(root, mock=True)
        app = YotCodeApp(config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.push_screen(ModelPickerScreen(config.model, mock=True))
            await shot(app, pilot, "modal-model")
            await pilot.press("escape")
            await pilot.pause(0.3)

            request = PermissionRequest(
                tool=BashTool(),
                data={"command": "python -m pytest -q", "description": "Run tests"},
                summary="python -m pytest -q",
                suggestion="Bash(python -m pytest:*)",
                preview=None,
                kind="tool",
                warning="",
            )
            app.push_screen(PermissionScreen(request))
            await shot(app, pilot, "modal-permission")
            await pilot.press("escape")
            await pilot.pause(0.3)

            app.push_screen(HelpScreen(help_markdown(config, {})))
            await shot(app, pilot, "modal-help")
            await pilot.press("escape")
            await pilot.pause(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
