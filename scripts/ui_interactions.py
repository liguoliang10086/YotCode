"""Interaction tests for the TUI (keybindings, completions, commands, resume)."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yotcode.config import build_config
from yotcode.session import Session
from yotcode.ui.app import YotCodeApp
from yotcode.ui.modals import CommandPaletteScreen, HelpScreen, ModelPickerScreen
from yotcode.ui.widgets import InfoPanel, NoticePanel, UserMessage

FAILS: list[str] = []


def check(label: str, condition: bool, extra: str = "") -> None:
    print(("  ok  " if condition else "  FAIL") + f"  {label}" + (f"  ({extra})" if extra else ""))
    if not condition:
        FAILS.append(label)


async def idle(app: YotCodeApp, pilot, timeout: float = 30.0) -> None:
    await pilot.pause()
    deadline = asyncio.get_event_loop().time() + timeout
    while app.busy and asyncio.get_event_loop().time() < deadline:
        await pilot.pause(0.05)
    for _ in range(40):
        await pilot.pause(0.05)
        if not app.busy:
            break


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        config = build_config(root, mock=True)
        app = YotCodeApp(config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)

            # --- permission mode cycle -----------------------------------
            await pilot.press("shift+tab")
            await pilot.pause(0.15)
            check("shift+tab -> acceptEdits", config.permission_mode == "acceptEdits",
                  config.permission_mode)
            await pilot.press("shift+tab")
            await pilot.pause(0.15)
            check("shift+tab -> plan", config.permission_mode == "plan",
                  config.permission_mode)
            await pilot.press("shift+tab")
            await pilot.pause(0.15)
            check("shift+tab -> default", config.permission_mode == "default",
                  config.permission_mode)

            # --- slash completion ---------------------------------------
            area = app.prompt_area()
            area.prompt.focus()
            area.prompt.load_text("/he")
            area.prompt.move_cursor((0, 3))
            await pilot.pause(0.2)
            check("completion list visible", area.completions.display,
                  str(area.completions.option_count))
            await pilot.press("tab")
            await pilot.pause(0.2)
            check("tab completes /help", area.prompt.text.strip() == "/help",
                  repr(area.prompt.text))
            area.prompt.load_text("")
            await pilot.pause(0.1)

            # --- file completion ----------------------------------------
            area.prompt.load_text("@hel")
            area.prompt.move_cursor((0, 4))
            await pilot.pause(0.2)
            check("file completion visible", area.completions.display)
            await pilot.press("tab")
            await pilot.pause(0.2)
            check("tab completes @hello.py", area.prompt.text.strip() == "@hello.py",
                  repr(area.prompt.text))
            area.prompt.load_text("")
            await pilot.pause(0.1)

            # --- command palette ----------------------------------------
            await pilot.press("ctrl+p")
            await pilot.pause(0.2)
            check("palette opens", isinstance(app.screen, CommandPaletteScreen))
            await pilot.press("c", "o", "s", "t")
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.3)
            check("palette prefills prompt", app.prompt_area().prompt.text.strip() == "/cost",
                  repr(app.prompt_area().prompt.text))

            # --- run /cost ----------------------------------------------
            before = len(app.query(InfoPanel))
            app.handle_submit("/cost")
            await pilot.pause(0.5)
            check("/cost renders info panel", len(app.query(InfoPanel)) > before)
            app.prompt_area().prompt.load_text("")
            await pilot.pause(0.1)

            # --- shell shortcut -----------------------------------------
            before = len(app.query(InfoPanel))
            app.handle_submit("!echo hi")
            await pilot.pause(0.6)
            check("!cmd renders shell output", len(app.query(InfoPanel)) > before)

            # --- memory shortcut ----------------------------------------
            app.handle_submit("#remember the milk")
            await pilot.pause(0.3)
            check("memory file updated", (root / "CLAUDE.md").is_file())
            if (root / "CLAUDE.md").is_file():
                check("memory content", "remember the milk" in
                      (root / "CLAUDE.md").read_text(encoding="utf-8"))

            # --- queue while busy ---------------------------------------
            app.handle_submit("hello there")
            await pilot.pause(0.02)
            app.handle_submit("queue this one")
            await pilot.pause(0.1)
            check("second prompt queued", len(app.queue) == 1, str(app.queue))
            await idle(app, pilot, 40)
            await pilot.pause(0.4)
            check("queue drained", not app.queue, str(app.queue))
            check("both prompts have user bubbles",
                  len(app.query(UserMessage)) >= 3, str(len(app.query(UserMessage))))

            # --- interrupt ----------------------------------------------
            app.handle_submit("tell me about this project")
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.pause(0.4)
            await idle(app, pilot, 10)
            check("escape interrupts", not app.busy)

            # --- help modal ---------------------------------------------
            await pilot.press("f1")
            await pilot.pause(0.2)
            check("f1 opens help", isinstance(app.screen, HelpScreen))
            await pilot.press("escape")
            await pilot.pause(0.2)
            check("escape closes help", not isinstance(app.screen, HelpScreen))

            # --- model picker -------------------------------------------
            app.handle_submit("/model")
            await pilot.pause(0.4)
            check("model picker opens", isinstance(app.screen, ModelPickerScreen))
            await pilot.press("enter")
            await pilot.pause(0.6)
            check("model switched", config.model != "mock-claude", config.model)

            # --- export -------------------------------------------------
            app.handle_submit("/export")
            await pilot.pause(0.4)
            exports = list(root.glob("yotcode-session-*.md"))
            check("/export writes markdown", bool(exports), str(exports))

            # --- resume the saved session -------------------------------
            session_id = app.session.meta.id
            loaded = Session.load(config.session_dir, session_id)
            check("session reload has messages",
                  loaded is not None and len(loaded.messages) > 0,
                  str(len(loaded.messages)) if loaded else "none")
            replay_app = YotCodeApp(config, session=loaded)
            async with replay_app.run_test(size=(120, 40)) as pilot2:
                await pilot2.pause(0.8)
                check("resume renders assistant bubbles",
                      len(replay_app.query("AssistantMessage")) >= 2,
                      str(len(replay_app.query("AssistantMessage"))))
                check("resume renders user bubbles",
                      len(replay_app.query(UserMessage)) >= 2,
                      str(len(replay_app.query(UserMessage))))

            # --- new session / clear ------------------------------------
            app.handle_submit("/clear")
            await pilot.pause(0.5)
            check("/clear empties messages", len(app.agent.messages) == 0,
                  str(len(app.agent.messages)))

            # chart of the final screen
            print("session:", session_id)
            Path("scripts/chat-shot.svg").write_text(app.export_screenshot(), encoding="utf-8")

    print("\nFAILURES:", FAILS if FAILS else "none")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
