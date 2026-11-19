"""回归测试：状态栏多行、输入框焦点错位、鼠标导致的重复提交。

这些测试在无头 Textual 驱动下运行，验证应用层的行为不变量：
- 状态栏始终是唯一一个 Static，内容永远是一行；
- 切换权限模式只更新一次状态栏，不产生重复控件；
- 输入框在挂载/切换模式/鼠标点击后始终保持焦点；
- 鼠标在底部移动/点击不会重新提交消息。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from textual.widgets import Static

from yotcode.config import build_config
from yotcode.ui.app import YotCodeApp
from yotcode.ui.widgets import PromptArea, PromptTextArea, StatusBar


def _make_app(tmp_path: Path) -> YotCodeApp:
    return YotCodeApp(build_config(tmp_path, mock=True))


@pytest.mark.asyncio
async def test_single_status_bar_and_single_line_after_mode_switches(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        assert len(app.query(StatusBar)) == 1
        for _ in range(8):
            app.action_cycle_mode()
            await pilot.pause(0.08)
        bars = app.query(StatusBar)
        assert len(bars) == 1
        line = bars.first().render().plain
        assert "\n" not in line
        assert line.strip()


@pytest.mark.asyncio
async def test_focus_stays_in_prompt_after_mode_switches_and_clicks(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        assert isinstance(app.focused, PromptTextArea)
        for _ in range(5):
            app.action_cycle_mode()
            await pilot.pause(0.08)
        assert isinstance(app.focused, PromptTextArea)
        await pilot.click(None, offset=(2, 2))
        await pilot.pause(0.15)
        assert isinstance(app.focused, PromptTextArea)


@pytest.mark.asyncio
async def test_mouse_events_do_not_resubmit_messages(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        prompt = app.prompt_area().prompt
        prompt.load_text("say hi")
        app.prompt_area()._update_height()
        for x in range(1, 20):
            await pilot.hover(None, offset=(x, 28))
        await pilot.pause(0.2)
        assert len(app.query(".user-message")) == 0
        assert prompt.text == "say hi"


@pytest.mark.asyncio
async def test_send_hint_debounces_rapid_clicks(tmp_path):
    class FakeEvent:
        def stop(self) -> None:
            pass

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        area = app.prompt_area()
        area.prompt.load_text("hello")
        calls: list[str] = []
        original = app.handle_submit

        def spy(text: str) -> None:
            calls.append(text)

        app.handle_submit = spy  # type: ignore[method-assign]
        for _ in range(4):
            area.send.on_click(FakeEvent())  # type: ignore[arg-type]
        assert len(calls) <= 1


@pytest.mark.asyncio
async def test_bottom_guard_spacer_is_last_widget(tmp_path):
    """末尾必须有空行守卫，让 conhost 末行的 wrap-pending 上滚不会污染状态栏。"""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        guard = app.query_one("#bottom-guard", Static)
        assert guard is not None
        assert guard.styles.height.value >= 2
        # guard 必须是 compose 树的最后一个子元素
        children = list(app.screen.children)
        assert children[-1] is guard


@pytest.mark.asyncio
async def test_mode_prefix_keeps_constant_width(tmp_path):
    """切模式时前缀宽度必须恒定，否则 conhost 差量渲染会残留输入框边框。"""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        area = app.prompt_area()
        widths: set[int] = set()
        for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
            area.set_mode(mode)
            await pilot.pause(0.05)
            widths.add(area.chevron.render().cell_length)
        assert len(widths) == 1, f"前缀宽度不一致: {widths}"


@pytest.mark.asyncio
async def test_prompt_height_uses_own_content_width(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        area: PromptArea = app.prompt_area()
        assert area.prompt.styles.height.value == 1
        area.prompt.load_text("x" * 400)
        area._update_height()
        assert 1 <= area.prompt.styles.height.value <= 10
        area.prompt.load_text("")
        area._update_height()
        assert area.prompt.styles.height.value == 1
