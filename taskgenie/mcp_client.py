"""MCP client: spawns stdio MCP servers and registers their tools as work tools.

Lives for the duration of a TaskGenie run. Owns an AsyncExitStack that holds
each server's stdio + ClientSession contexts; closing the client cleanly
shuts every subprocess down.
"""
from __future__ import annotations

import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from taskgenie.tools.registry import REGISTRY


class MCPClient:
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._registered_tools: list[str] = []
        self._connect_errors: dict[str, str] = {}
        self._closed = False

    @classmethod
    async def from_config(cls, config_path: str | Path) -> "MCPClient":
        client = cls()
        path = Path(config_path)
        if not path.exists():
            return client
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            client._connect_errors["<config>"] = f"invalid JSON: {e}"
            return client
        servers = raw.get("mcpServers", {})
        await client._connect_all(servers)
        return client

    @property
    def registered_tools(self) -> list[str]:
        return list(self._registered_tools)

    @property
    def connect_errors(self) -> dict[str, str]:
        return dict(self._connect_errors)

    async def _connect_all(self, servers: dict[str, Any]) -> None:
        await self._stack.__aenter__()
        for name, spec in servers.items():
            if name.startswith("_") or not isinstance(spec, dict):
                continue
            try:
                await self._connect_server(name, spec)
            except Exception as e:
                self._connect_errors[name] = f"{type(e).__name__}: {e}"

    async def _connect_server(self, name: str, spec: dict[str, Any]) -> None:
        params = StdioServerParameters(
            command=spec["command"],
            args=spec.get("args", []),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_result = await session.list_tools()
        self._sessions[name] = session
        for tool in tools_result.tools:
            self._register_mcp_tool(name, tool)

    def _register_mcp_tool(self, server_name: str, tool: Any) -> None:
        original_name = tool.name
        registered_name = original_name
        if REGISTRY.get(registered_name) is not None:
            # Collision with another tool (built-in or another MCP server) —
            # namespace with the server name.
            registered_name = f"{server_name}__{original_name}"
            if REGISTRY.get(registered_name) is not None:
                self._connect_errors[server_name] = (
                    f"could not register {original_name}: name conflict even after prefixing"
                )
                return

        schema = {
            "type": "function",
            "function": {
                "name": registered_name,
                "description": tool.description or f"(MCP tool from {server_name})",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        }

        async def dispatcher(agent: Any, args: dict[str, Any]) -> str:
            return await self._call_tool(server_name, original_name, args)

        REGISTRY.register(
            schema,
            kind="work",
            version="mcp",
            source=f"mcp:{server_name}",
            dispatcher=dispatcher,
        )
        self._registered_tools.append(registered_name)

    async def _call_tool(
        self, server_name: str, original_name: str, args: dict[str, Any]
    ) -> str:
        session = self._sessions.get(server_name)
        if session is None:
            return f"[MCP_ERROR] no session for server {server_name}"
        try:
            result = await session.call_tool(name=original_name, arguments=args)
        except Exception as e:
            return f"[MCP_ERROR] {server_name}.{original_name}: {type(e).__name__}: {e}"
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
            else:
                parts.append(repr(item))
        body = "\n".join(parts) if parts else "(no content)"
        if result.isError:
            return f"[MCP_ERROR] {body}"
        return body

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in list(self._registered_tools):
            REGISTRY.unregister(name)
        self._registered_tools.clear()
        self._sessions.clear()
        try:
            await self._stack.aclose()
        except Exception:
            # Best-effort shutdown — child processes may already be dying.
            pass
