"""
handlers/mcp_bridge.py — MCP (Model Context Protocol) 客户端集成

为 EvoCLI Python Soul 提供：
1. mcp.list_servers   — 列出已注册 MCP server
2. mcp.load_tools     — 连接 MCP server，将工具注入 LLM function calling
3. mcp.call_tool      — 调用指定 MCP 工具
4. mcp.server_tools   — 获取某 server 的工具列表（用于 agent 动态更新）
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("evocli.mcp_bridge")

_MCP_CONFIG = Path.home() / ".evocli" / "mcp_servers.json"

# Module-level MCP tool registry: mcp_tool_name -> (program, args, tool_schema)
_mcp_tools: dict[str, dict] = {}   # "mcp_filesystem_read_file" -> {"server": "filesystem", "name": "read_file", "schema": {...}}
_mcp_servers: dict[str, dict] = {} # "filesystem" -> {"program": "npx", "args": [...]}


def load_mcp_config() -> list[dict]:
    if not _MCP_CONFIG.exists():
        return []
    try:
        with open(_MCP_CONFIG) as f:
            return json.load(f)
    except Exception as e:
        import logging
        logging.getLogger("evocli.mcp_bridge").debug("Failed to load MCP config from %s: %s", _MCP_CONFIG, e)
        return []


class McpClientProcess:
    """Light Python MCP stdio client (pure asyncio, no external deps)."""
    def __init__(self, program: str, args: list[str]):
        self.program = program
        self.args = args
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.program, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "evocli", "version": "0.1.0"}
        })
        await self._notify("notifications/initialized", {})

    async def _read_loop(self) -> None:
        while self._proc and self._proc.stdout:
            try:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                msg = json.loads(line.decode())
                if "id" in msg and msg["id"] in self._pending:
                    self._pending.pop(msg["id"]).set_result(msg)
            except Exception as e:
                # Log JSON decode errors at debug level; connection drops or empty lines are normal.
                import logging as _log
                _log.getLogger("evocli.mcp_bridge").debug("MCP read_loop parse error: %s", e)
                pass

    async def _send(self, method: str, params: dict) -> Any:
        req_id = self._next_id; self._next_id += 1
        req = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        # asyncio.get_running_loop() is the correct API inside an async method.
        # get_event_loop() is deprecated in Python 3.10+ and raises RuntimeError in 3.12+.
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        if self._proc and self._proc.stdin:
            self._proc.stdin.write((req + "\n").encode())
            await self._proc.stdin.drain()
        try:
            resp = await asyncio.wait_for(fut, timeout=15.0)
            if "error" in resp:
                raise RuntimeError(f"MCP error: {resp['error']}")
            return resp.get("result")
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP request timed out: {method}")

    async def _notify(self, method: str, params: dict) -> None:
        notif = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        if self._proc and self._proc.stdin:
            self._proc.stdin.write((notif + "\n").encode())
            await self._proc.stdin.drain()

    async def list_tools(self) -> list[dict]:
        result = await self._send("tools/list", {})
        return result.get("tools", []) if result else []

    async def call_tool(self, name: str, arguments: dict) -> Any:
        return await self._send("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._proc:
            self._proc.terminate()


async def _load_server_tools(server_name: str, program: str, args: list[str]) -> list[dict]:
    """Connect to MCP server and fetch tool list. Returns tool definitions."""
    client = McpClientProcess(program, args)
    try:
        await asyncio.wait_for(client.connect(), timeout=10.0)
        tools = await client.list_tools()
        return tools
    except Exception as e:
        log.warning("MCP server '%s' unreachable: %s", server_name, e)
        return []
    finally:
        await client.close()


async def initialize_mcp_tools() -> dict[str, dict]:
    """Load all MCP server tool definitions into the registry. Call on startup."""
    global _mcp_tools, _mcp_servers
    servers = load_mcp_config()
    _mcp_servers = {s["name"]: s for s in servers}
    _mcp_tools.clear()
    
    for server in servers:
        name = server["name"]
        program = server["program"]
        args = server.get("args", [])
        log.info("Loading MCP server: %s", name)
        tools = await _load_server_tools(name, program, args)
        for tool in tools:
            tool_key = f"mcp_{name}_{tool['name'].replace('-', '_')}"
            _mcp_tools[tool_key] = {
                "server": name,
                "name": tool["name"],
                "description": tool.get("description", ""),
                "schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                "program": program,
                "args": args,
            }
        log.info("Loaded %d tools from MCP server '%s'", len(tools), name)
    return _mcp_tools


def get_mcp_tool_definitions() -> list[dict]:
    """Return OpenAI function-calling format definitions for all MCP tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": key,
                "description": f"[MCP:{info['server']}] {info['description']}",
                "parameters": info["schema"],
            }
        }
        for key, info in _mcp_tools.items()
    ]


async def call_mcp_tool(tool_key: str, arguments: dict) -> Any:
    """Execute an MCP tool call by spawning a short-lived client connection."""
    info = _mcp_tools.get(tool_key)
    if not info:
        raise ValueError(f"Unknown MCP tool: {tool_key}")
    client = McpClientProcess(info["program"], info["args"])
    try:
        await asyncio.wait_for(client.connect(), timeout=10.0)
        result = await client.call_tool(info["name"], arguments)
        return result
    finally:
        await client.close()


def register(router) -> None:
    router.add("mcp.list_servers",  handle_mcp_list_servers)
    router.add("mcp.load_tools",    handle_mcp_load_tools)
    router.add("mcp.call_tool",     handle_mcp_call_tool)
    router.add("mcp.server_tools",  handle_mcp_server_tools)


async def handle_mcp_list_servers(req_id: str, params: dict, send, state) -> None:
    try:
        servers = load_mcp_config()
        await send.response(req_id, {
            "servers": [{"name": s["name"], "program": s["program"], "args": s.get("args", [])} for s in servers],
            "tools_loaded": len(_mcp_tools),
        })
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_mcp_load_tools(req_id: str, params: dict, send, state) -> None:
    try:
        tools = await initialize_mcp_tools()
        await send.response(req_id, {
            "loaded": len(tools),
            "tool_names": list(tools.keys()),
        })
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_mcp_call_tool(req_id: str, params: dict, send, state) -> None:
    try:
        tool_key  = params.get("tool", "")
        arguments = params.get("arguments", {})
        result    = await call_mcp_tool(tool_key, arguments)
        await send.response(req_id, {"result": result})
    except Exception as e:
        await send.error(req_id, -32603, str(e))


async def handle_mcp_server_tools(req_id: str, params: dict, send, state) -> None:
    try:
        server_name = params.get("server", "")
        defs = [
            {"key": k, "server": v["server"], "name": v["name"], "description": v["description"]}
            for k, v in _mcp_tools.items()
            if v["server"] == server_name or not server_name
        ]
        await send.response(req_id, {"tools": defs})
    except Exception as e:
        await send.error(req_id, -32603, str(e))
