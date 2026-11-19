"""工具协议：内置工具与 MCP 代理工具都实现同一个 Tool 接口。

一个工具需要提供：
  name / description / input_schema   -> 发给模型看的元数据
  run(data, ctx)                      -> 真正执行，返回 ToolResult
  summary(data)                       -> UI 卡片上显示的一行摘要
  get_diff(data, ctx)                 -> 可选，权限弹窗里预览文件改动
  read_only / requires_permission     -> 权限系统据此快速判断

ToolContext 携带当前工作目录、配置、取消事件、后台 Shell 管理器和 Agent
引用；工具不应该自己保存全局状态，否则子代理之间会互相污染。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..agent import Agent
    from ..config import Config


@dataclass
class ToolResult:
    """工具执行完的返回值。content 可以是纯文字，也可以是内容块列表。

    is_error=True 表示执行失败（比如文件不存在），Agent 会照样把错误
    告诉大模型，让它自己决定怎么补救。meta 放额外信息给 UI 用。
    """
    content: str | list[Any]
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # 每个实例独立的空字典

    @property
    def text(self) -> str:
        """把 content 统一转成纯文字，方便显示和拼接。property 让它能像属性一样用：result.text"""
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
            elif getattr(block, "type", "") == "image":
                parts.append("[image]")
        return "\n".join(parts)


@dataclass
class ToolContext:
    """工具执行时的"上下文"：工具干活需要的环境信息都打包在这里传进去。

    cwd = 当前目录；config = 配置；cancel = 取消信号；
    state = 工具之间共享的小抽屉（比如后台 shell 管理器就存在这里）。
    """
    cwd: Path
    config: "Config"
    cancel: asyncio.Event
    session_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    agent: "Agent | None" = None

    def check_cancel(self) -> None:
        # 用户在界面上按了打断键就抛出异常，让工具尽快停下来
        if self.cancel.is_set():
            from ..providers.base import Interrupted

            raise Interrupted()

    def resolve(self, raw: str) -> Path:
        from ..utils import expand_path

        path = expand_path(raw, self.cwd)
        return path

    @property
    def shells(self) -> Any:
        if "shells" not in self.state:
            from .shell_tools import ShellManager

            self.state["shells"] = ShellManager(self.config.home / "tmp")
        return self.state["shells"]


class Tool(ABC):
    """所有工具的"模板"（抽象基类）。每个具体工具（Read、Bash 等）都继承它。

    ABC = Abstract Base Class，abstractmethod 标记的方法必须由子类实现。
    这样 Agent 不需要知道工具有多少种，只要按统一方式调用 run() 即可 ——
    这就是所谓的"插件式架构"。

    下面的类属性是给不同对象看的：
      name / description / input_schema → 发给大模型，告诉它工具怎么用
      read_only / requires_permission / dangerous → 给权限系统判断要不要问用户
      parallel_safe → 给 Agent 判断这个工具能不能和别的工具并发执行
    """
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    read_only: bool = False
    requires_permission: bool = True
    dangerous: bool = False
    hidden: bool = False
    # parallel_safe = True 表示这个工具可以在同一轮里和别的工具「并发」执行。
    #
    # 为什么要区分：模型经常一轮里同时请求几个互不相关的调用
    # （一次读 5 个文件、同时开 3 个子代理探索不同目录）。
    # 如果一个个排队等，这些操作的总耗时就是各自耗时之和；
    # 只要它们互不干扰、也不需要弹权限窗，并发执行能把总耗时压到接近最慢那一个。
    #
    # 判定标准（保守起见，两条都要满足才标 True）：
    #   1) 不修改任何共享状态，也不依赖别的工具的执行结果；
    #   2) 不需要弹权限对话框（弹窗一次只能弹一个，并发弹会乱）。
    #
    # 注意：即使标了 True，Agent 返回给模型的 tool_result 顺序仍然和模型
    # 请求的顺序一致（Anthropic 协议要求两者一一对应），并发只是内部提速。
    parallel_safe: bool = False

    # -- metadata ----------------------------------------------------------
    def spec(self) -> dict[str, Any]:
        """把工具的元数据打包成字典，发给大模型当"工具说明书" """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def summary(self, data: dict[str, Any]) -> str:
        for key in ("file_path", "path", "pattern", "command", "url", "query",
                    "description", "shell_id", "bash_id"):
            value = data.get(key)
            if value:
                text = str(value).replace("\n", " ")
                return text if len(text) <= 90 else text[:87] + "..."
        return ""

    def validate(self, data: dict[str, Any]) -> str | None:
        schema = self.input_schema or {}
        for key in schema.get("required", []):
            if key not in data or data[key] in (None, ""):
                return f"Missing required parameter: {key}"
        return None

    def get_diff(self, data: dict[str, Any], ctx: ToolContext) -> tuple[str, str, str] | None:
        """Return (path, before, after) so the UI can show a diff preview."""
        return None

    def touched_paths(self, data: dict[str, Any], ctx: ToolContext) -> list[Path]:
        """这次调用会改动哪些文件？返回空列表表示"不知道"。

        Agent 在执行工具**之前**会用它来决定给哪些文件留检查点
        （见 yotcode/checkpoints.py），这样 AI 改错了可以用 /revert 退回去。

        只读工具不用重写这个方法；会改文件的工具（Write / Edit / MultiEdit）
        应该返回它将要写入的路径。
        """
        return []

    # -- execution ---------------------------------------------------------
    @abstractmethod
    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...

    # -- helpers for subclasses -------------------------------------------
    @staticmethod
    def error(message: str, **meta: Any) -> ToolResult:
        return ToolResult(message, is_error=True, meta=meta)

    @staticmethod
    def ok(content: str | list[Any], **meta: Any) -> ToolResult:
        return ToolResult(content, meta=meta)
