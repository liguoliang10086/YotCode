"""Agent 主循环的回归测试。

这里放"以前真的炸过"的坑，防止再犯：
工具执行完要回填结果给模型，这段路径上有个变量必须先初始化。
"""
from __future__ import annotations

from yotcode.agent import Agent
from yotcode.config import Config
from yotcode.providers.mock import MockProvider
from yotcode.tools import create_tools


async def test_run_with_tool_call_returns_normally(tmp_path):
    """模型请求工具 → 执行 → 回填结果 → 再问一次模型 → 正常收尾。

    回归点：工具跑完后要判断"要不要立刻收尾"，那个标志位以前没初始化，
    走到那行就是 UnboundLocalError，AI 一调工具就崩。
    """
    provider = MockProvider(api_key="", base_url="mock://local", model="mock-claude")
    config = Config(cwd=tmp_path, permission_mode="bypassPermissions")
    agent = Agent(config, provider, create_tools())

    text = await agent.run("列出当前目录的文件")

    assert text, "应该返回模型的最终回答"
    # 工具结果确实回填进了历史
    tool_results = [
        block
        for message in agent.session.messages
        for block in message.content
        if block.__class__.__name__ == "ToolResultBlock"
    ]
    assert tool_results, "工具结果应该被回填"
