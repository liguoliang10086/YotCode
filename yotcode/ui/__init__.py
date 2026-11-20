"""YotCode 的终端界面（基于 Textual 框架）。

Textual terminal UI for YotCode.

这里只对外导出一个 YotCodeApp —— 整个 TUI 的"总指挥"。
cli.py 在交互模式下创建它并调用 run()，它负责画界面、处理按键、
把用户输入转给 Agent、再把 Agent 的流式输出渲染到屏幕上。
"""
from __future__ import annotations

from .app import YotCodeApp

__all__ = ["YotCodeApp"]
