"""Minimal MCP (Model Context Protocol) stdio client.

Supports tools/list and tools/call over newline-delimited JSON-RPC, which is
all YotCode needs to consume most MCP servers.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..config import project_config_dir
from .base import Tool, ToolContext, ToolResult


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, config: dict[str, Any], cwd: Path) -> None:
        self.name = name
        self.config = config
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.tools: list[dict[str, Any]] = []
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.error: str | None = None
        self.log_path = project_config_dir(cwd) / f"mcp-{name}.log"

    async def start(self, timeout: float = 25.0) -> None:
        command = self.config.get("command")
        if not command:
            raise MCPError("stdio MCP server requires a 'command'")
        args = [str(a) for a in (self.config.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        exe = shutil.which(command) or command
        self.process = await asyncio.create_subprocess_exec(
            exe, *args,
            cwd=str(self.cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._initialize(), timeout=timeout)
        except Exception as exc:
            await self.close()
            raise MCPError(f"MCP server '{self.name}' failed to initialize: {exc}") from exc

    async def _initialize(self) -> None:
        result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "yotcode", "version": "0.1.0"},
        })
        await self._notify("notifications/initialized", {})
        listed = await self._request("tools/list", {})
        self.tools = list(listed.get("tools") or [])
        self.server_info = result.get("serverInfo") or {}

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if "id" in message and message["id"] in self._pending:
                    future = self._pending.pop(message["id"])
                    if future.done():
                        continue
                    if "error" in message:
                        err = message["error"]
                        future.set_exception(
                            MCPError(err.get("message") if isinstance(err, dict) else str(err))
                        )
                    else:
                        future.set_result(message.get("result") or {})
        except Exception as exc:
            self.error = str(exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPError("MCP server disconnected"))

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as log:
                while True:
                    line = await self.process.stderr.readline()
                    if not line:
                        break
                    log.write(line)
        except Exception:
            pass

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise MCPError("MCP server is not running")
        data = json.dumps(message, ensure_ascii=False) + "\n"
        self.process.stdin.write(data.encode("utf-8"))
        await self.process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any],
                       timeout: float = 60.0) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        })
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MCPError(f"MCP request '{method}' timed out") from exc

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 180.0) -> ToolResult:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=timeout
        )
        parts: list[str] = []
        for item in result.get("content") or []:
            kind = item.get("type")
            if kind == "text":
                parts.append(item.get("text") or "")
            elif kind == "image":
                parts.append(f"[image: {item.get('mimeType', 'image')}]")
            elif kind == "resource":
                resource = item.get("resource") or {}
                parts.append(str(resource.get("text") or resource.get("uri") or "[resource]"))
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        text = "\n".join(parts) or "(no content)"
        return ToolResult(text, is_error=bool(result.get("isError")))

    async def close(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass


class MCPTool(Tool):
    """A remote MCP tool exposed through the normal Tool interface."""

    def __init__(self, client: MCPClient, definition: dict[str, Any]) -> None:
        self.client = client
        self.remote_name = definition.get("name") or "tool"
        self.name = f"mcp__{client.name}__{self.remote_name}".replace("-", "_")[:64]
        self.description = (
            f"[MCP:{client.name}] "
            + (definition.get("description") or self.remote_name)
        )
        schema = definition.get("inputSchema") or {"type": "object", "properties": {}}
        self.input_schema = schema if isinstance(schema, dict) else {"type": "object"}
        annotations = definition.get("annotations") or {}
        self.read_only = bool(annotations.get("readOnlyHint"))

    def summary(self, data: dict[str, Any]) -> str:
        if not data:
            return self.remote_name
        preview = json.dumps(data, ensure_ascii=False)
        return preview[:90] + ("..." if len(preview) > 90 else "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            return await self.client.call_tool(self.remote_name, data)
        except Exception as exc:
            return self.error(f"MCP tool call failed: {exc}")


class MCPManager:
    def __init__(self, servers: dict[str, Any], cwd: Path) -> None:
        self.servers = servers or {}
        self.cwd = cwd
        self.clients: dict[str, MCPClient] = {}
        self.errors: dict[str, str] = {}

    async def connect_all(self, notify=None) -> list[MCPTool]:
        tools: list[MCPTool] = []
        for name, config in self.servers.items():
            if not isinstance(config, dict) or not config.get("command"):
                self.errors[name] = "missing command (only stdio servers are supported)"
                continue
            client = MCPClient(name, config, self.cwd)
            try:
                await client.start()
                self.clients[name] = client
                for definition in client.tools:
                    tools.append(MCPTool(client, definition))
                if notify:
                    notify(f"MCP server '{name}' connected ({len(client.tools)} tools)", "info")
            except Exception as exc:
                self.errors[name] = str(exc)
                if notify:
                    notify(f"MCP server '{name}': {exc}", "error")
        return tools

    async def close(self) -> None:
        for client in self.clients.values():
            await client.close()
        self.clients.clear()
