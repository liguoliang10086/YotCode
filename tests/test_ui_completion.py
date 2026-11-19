"""斜杠命令补全面板：回车应该是"接受补全"，而不是把输入发出去。"""
from __future__ import annotations

from pathlib import Path

from yotcode.config import Config
from yotcode.ui.app import YotCodeApp
from yotcode.ui.widgets import PromptArea


def _app(tmp_path: Path) -> YotCodeApp:
    return YotCodeApp(Config(cwd=tmp_path, mock=True))


async def test_enter_accepts_completion_instead_of_sending(tmp_path):
    """输入 / 后按回车：补全成命令，但**不**提交。"""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        area = app.query_one(PromptArea)
        area.prompt.insert("/")
        await pilot.pause()

        assert area.completions.display, "输入 / 应该弹出命令列表"
        assert area.completions.option_count > 0

        await pilot.press("enter")
        await pilot.pause()

        # 补全生效：输入框里变成了某个命令
        assert area.prompt.text.startswith("/"), area.prompt.text
        assert area.prompt.text.strip() != "/", "回车应该补全出命令"
        # 关键：没有提交出去
        assert not app.busy, "回车不应该把内容发送出去"
        assert area.prompt.text.strip(), "提交后输入框会被清空，这里应该还留着"


async def test_tab_also_accepts_completion(tmp_path):
    """Tab 同样是接受补全（老行为不变）。"""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        area = app.query_one(PromptArea)
        area.prompt.insert("/")
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()

        assert area.prompt.text.strip() != "/"
        assert not app.busy


async def test_enter_without_completion_sends(tmp_path):
    """没有补全面板时，回车照常提交。"""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        area = app.query_one(PromptArea)
        area.prompt.insert("hello")
        await pilot.pause()

        assert not area.completions.display

        await pilot.press("enter")
        await pilot.pause()

        # 内容被提交走了，输入框清空
        assert area.prompt.text.strip() == ""
