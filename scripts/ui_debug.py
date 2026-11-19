from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotcode.config import build_config
from yotcode.ui.app import YotCodeApp


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("print(1)\n", encoding="utf-8")
        app = YotCodeApp(build_config(root, mock=True))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.handle_submit("list files")
            for i in range(40):
                await pilot.pause(0.1)
                if not app.busy:
                    break
            await pilot.pause(1.0)
            print("busy", app.busy)
            children = list(app.transcript().children)
            print("children:", [type(w).__name__ for w in children])
            for w in children:
                print(type(w).__name__, "display", w.display, "size", w.size, "region", w.region)
            print("messages:", [(m.role, [type(b).__name__ for b in m.content]) for m in app.agent.messages])
            print("assistant widget count:", len(app.query("AssistantMessage")))
            svg = app.export_screenshot()
            Path("scripts/debug-shot.svg").write_text(svg, encoding="utf-8")


asyncio.run(main())
