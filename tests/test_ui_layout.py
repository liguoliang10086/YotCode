"""终端缩放后的布局：输入框和状态栏不能重叠、不能跑出屏幕。"""
from __future__ import annotations

from pathlib import Path

from yotcode.config import Config
from yotcode.ui.app import YotCodeApp


def _app(tmp_path: Path) -> YotCodeApp:
    return YotCodeApp(Config(cwd=tmp_path, mock=True))


async def test_resize_keeps_layout_consistent(tmp_path):
    """反复改窗口大小，底部布局都要保持整齐。

    以前的问题是 app._size、screen 高度、终端缓冲区三者各说各话，
    拖一下窗口，底部就会叠行。
    """
    app = _app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for width, height in ((100, 30), (60, 20), (120, 40), (80, 24), (100, 30)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()

            screen = app.screen
            row = app.query_one("#prompt-row").region
            status = app.query_one("#status").region
            row_bottom = row.y + row.height
            status_bottom = status.y + status.height

            assert row_bottom <= status.y, (
                f"{width}x{height}: 输入框压到了状态栏 "
                f"(prompt-row.bottom={row_bottom}, status.top={status.y})"
            )
            assert status_bottom <= screen.size.height, (
                f"{width}x{height}: 状态栏超出屏幕 "
                f"(status.bottom={status_bottom}, screen={screen.size.height})"
            )


async def test_input_height_follows_width(tmp_path):
    """窗口变窄后，长文本的输入框要变高（否则内容会被裁掉）。"""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        area = app.query_one("#prompt")
        area.text = "x" * 200
        await pilot.pause()
        tall = area.size.height

        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        await pilot.pause()
        assert area.size.height > tall, "变窄后输入框应该变高"
