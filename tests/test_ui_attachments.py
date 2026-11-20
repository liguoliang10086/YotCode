"""TUI 附件交互回归测试。

重点覆盖 Textual 8.x 下的鼠标点击：Click 事件只有 widget 属性、没有 target，
PromptArea.on_click 若访问 event.target 会在输入区任何点击时崩溃退出。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual import events

from yotcode.config import build_config
from yotcode.types import ImageBlock, TextBlock
from yotcode.ui.app import YotCodeApp
from yotcode.ui.widgets import PromptArea, PromptTextArea


def _make_app(tmp_path: Path) -> YotCodeApp:
    return YotCodeApp(build_config(tmp_path, mock=True))


@pytest.mark.asyncio
async def test_clicking_attachment_chip_removes_it_without_crash(tmp_path):
    """真实鼠标点击附件筹码：不能崩溃，并正确移除该附件。"""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        area: PromptArea = app.prompt_area()
        area.add_attachments([ImageBlock("image/png", "AAAA", filename="shot.png")])
        await pilot.pause(0.15)
        assert len(list(app.query(".attachment-chip"))) == 1

        # 真实点击（不是构造 FakeEvent）：会走到 PromptArea.on_click。
        await pilot.click(".attachment-chip")
        await pilot.pause(0.15)
        assert area.attachments == []
        assert len(list(app.query(".attachment-chip"))) == 0


@pytest.mark.asyncio
async def test_clicking_plain_input_area_does_not_crash(tmp_path):
    """空附件时点击输入区任意位置也不能因 event.target 崩溃。"""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        # 输入区内的多次点击；修复前这里会抛 AttributeError: Click has no attribute target
        for x in range(30, 90, 10):
            await pilot.click(None, offset=(x, 27))
        await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_multiple_chips_remove_correct_index(tmp_path):
    """多个附件时，点击第二个筹码应只移除第二个。"""
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        area: PromptArea = app.prompt_area()
        area.add_attachments([
            ImageBlock("image/png", "AAAA", filename="first.png"),
            TextBlock('<file path="a.py">\nprint(1)\n</file>'),
        ])
        await pilot.pause(0.15)
        chips = list(app.query(".attachment-chip"))
        assert len(chips) == 2

        # 点击第二个筹码（文本文件附件）
        await pilot.click(chips[1])
        await pilot.pause(0.15)
        assert len(area.attachments) == 1
        assert isinstance(area.attachments[0], ImageBlock)


@pytest.mark.asyncio
async def test_ctrl_v_key_reads_clipboard_image(tmp_path):
    """终端里 Ctrl+V 截图只发 Key 事件（无 Paste 事件），应主动读剪贴板图片为附件。"""
    app = _make_app(tmp_path)
    fake = ImageBlock("image/png", "BBBB")
    with patch("yotcode.attachments.clipboard_image_block", return_value=fake):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            area: PromptArea = app.prompt_area()
            # 真实按键：终端里 Ctrl+V 的剪贴板图片不会转成 Paste 事件
            await pilot.press("ctrl+v")
            await pilot.pause(0.2)
            assert len(area.attachments) == 1
            assert isinstance(area.attachments[0], ImageBlock)


@pytest.mark.asyncio
async def test_ctrl_v_without_image_falls_through(tmp_path):
    """Ctrl+V 但剪贴板没有图片时不应拦截，交给默认粘贴流程。"""
    app = _make_app(tmp_path)
    with patch("yotcode.attachments.clipboard_image_block", return_value=None):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            area: PromptArea = app.prompt_area()
            await pilot.press("ctrl+v")
            await pilot.pause(0.2)
            assert area.attachments == []


@pytest.mark.asyncio
async def test_plain_text_paste_does_not_read_clipboard(tmp_path):
    """粘贴普通文本时应保持默认行为，不误读剪贴板里的残留图片。"""
    app = _make_app(tmp_path)
    with patch(
        "yotcode.attachments.clipboard_image_block",
        side_effect=AssertionError("不应读取剪贴板"),
    ):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            area: PromptArea = app.prompt_area()
            prompt: PromptTextArea = area.prompt
            prompt.post_message(events.Paste("hello world"))
            await pilot.pause(0.2)
            assert area.attachments == []
            assert "hello world" in prompt.text
