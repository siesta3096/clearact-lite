import asyncio
import base64
import json
import mimetypes
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator

from clearact_lite.core.ports import ToolContext
from clearact_lite.integrations.config import normalize_servers


@dataclass
class Connection:
    ready: asyncio.Future
    stop: asyncio.Event
    task: asyncio.Task | None = None
    session: object | None = None
    definitions: list | None = None
    error: str | None = None


class MCPManager:
    """The task that opens an SDK context is also the task that closes it."""

    def __init__(self, workspace: Path, servers: dict | None = None):
        self.workspace = workspace
        self.servers = normalize_servers(servers or {})
        self.connections: dict[str, Connection] = {}

    @property
    def is_open(self) -> bool:
        return any(c.task and not c.task.done() for c in self.connections.values())

    def overview(self) -> list[dict]:
        result = []
        for name, config in self.servers.items():
            connection = self.connections.get(name)
            public = {k: v for k, v in config.items() if k not in {"env", "headers"}}
            public.update(
                {
                    "name": name,
                    "env_keys": list(config["env"]),
                    "header_keys": list(config["headers"]),
                    "connected": bool(connection and connection.session),
                    "tool_count": len(connection.definitions or []) if connection else 0,
                    "error": connection.error if connection else None,
                }
            )
            result.append(public)
        return result

    async def _own(self, name: str, connection: Connection) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        config = self.servers[name]
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(config["connect_timeout"]):
                    if config["transport"] == "stdio":
                        logdir = self.workspace / ".clearact" / "mcp"
                        logdir.mkdir(parents=True, exist_ok=True)
                        log = stack.enter_context((logdir / f"{uuid4().hex}.stderr.log").open("w", encoding="utf-8"))
                        streams = await stack.enter_async_context(
                            stdio_client(
                                StdioServerParameters(
                                    command=config["command"], args=config["args"], env=config["env"]
                                ),
                                errlog=log,
                            )
                        )
                    else:
                        if config["transport"] == "streamable_http":
                            client = await stack.enter_async_context(
                                httpx.AsyncClient(
                                    headers=config["headers"],
                                    trust_env=False,
                                    timeout=httpx.Timeout(config["connect_timeout"], read=config["call_timeout"]),
                                )
                            )
                            streams = await stack.enter_async_context(
                                streamable_http_client(
                                    config["url"],
                                    http_client=client,
                                )
                            )
                        else:
                            streams = await stack.enter_async_context(
                                sse_client(
                                    config["url"],
                                    headers=config["headers"],
                                    timeout=config["connect_timeout"],
                                    sse_read_timeout=config["call_timeout"],
                                )
                            )
                    session = await stack.enter_async_context(
                        ClientSession(
                            streams[0],
                            streams[1],
                            read_timeout_seconds=timedelta(seconds=config["call_timeout"]),
                        )
                    )
                    await session.initialize()
                    definitions, cursor = [], None
                    while True:
                        page = await session.list_tools(cursor=cursor)
                        definitions.extend(page.tools)
                        cursor = page.nextCursor
                        if not cursor:
                            break
                    for tool in definitions:
                        Draft202012Validator.check_schema(tool.inputSchema)
                    connection.session, connection.definitions = session, definitions
                    if not connection.ready.done():
                        connection.ready.set_result(True)
                await connection.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            connection.error = f"{type(exc).__name__}：连接未完成，请检查地址、命令、鉴权和服务状态"
            if not connection.ready.done():
                connection.ready.set_result(False)
        finally:
            connection.session = None
            if not connection.ready.done():
                connection.ready.set_result(False)

    async def connect(self, server: str) -> dict:
        if server not in self.servers:
            raise ValueError("MCP server is not configured")
        if not self.servers[server]["enabled"]:
            raise ValueError("MCP server is disabled in your configuration")
        connection = self.connections.get(server)
        if connection is None:
            connection = Connection(asyncio.get_running_loop().create_future(), asyncio.Event())
            self.connections[server] = connection
            connection.task = asyncio.create_task(self._own(server, connection))
        try:
            ok = await asyncio.shield(connection.ready)
        except asyncio.CancelledError:
            # A cancelled connection attempt is not left running behind the user's stop.
            if not connection.ready.done() and connection.task:
                connection.task.cancel()
                await asyncio.gather(connection.task, return_exceptions=True)
                self.connections.pop(server, None)
            raise
        if not ok or connection.session is None:
            raise RuntimeError(connection.error or "MCP session disconnected; reconnect explicitly")
        return next(s for s in self.overview() if s["name"] == server)

    async def list_tools(self, server: str, offset: int = 0, limit: int = 20) -> dict:
        await self.connect(server)
        tools = self.connections[server].definitions
        return {
            "server": server,
            "total": len(tools),
            "offset": offset,
            "tools": [
                {"name": t.name, "description": t.description or "", "parameters": t.inputSchema}
                for t in tools[offset : offset + limit]
            ],
        }

    async def call(self, server: str, tool: str, arguments: dict, context: ToolContext) -> tuple[str, bool]:
        await self.connect(server)
        connection = self.connections[server]
        definition = next((t for t in connection.definitions if t.name == tool), None)
        if definition is None:
            raise ValueError("Unknown MCP tool; use mcp_list_tools to inspect this server")
        Draft202012Validator(definition.inputSchema).validate(arguments)
        result = await connection.session.call_tool(
            tool,
            arguments,
            read_timeout_seconds=timedelta(seconds=self.servers[server]["call_timeout"]),
        )
        raw = result.model_dump(mode="json", by_alias=True)
        event = context.emit("mcp.response", {"server": server, "tool": tool, "response": raw})
        texts, files = [], []
        for index, block in enumerate(raw.get("content", [])):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") in {"image", "audio"} and block.get("data"):
                suffix = mimetypes.guess_extension(block.get("mimeType", "")) or ".bin"
                path = Path("mcp-results") / uuid4().hex / f"result-{index}{suffix}"
                data = base64.b64decode(block["data"], validate=True)
                context.snapshots.write_bytes(context.workspace / path, data, context.emit)
                files.append(path.as_posix())
            else:
                texts.append(json.dumps(block, ensure_ascii=False))
        return json.dumps(
            {
                "server": server,
                "tool": tool,
                "text": "\n".join(texts),
                "structured_content": raw.get("structuredContent"),
                "files": files,
                "raw_event_id": event.id,
            },
            ensure_ascii=False,
        ), bool(raw.get("isError"))

    async def disconnect(self, server: str) -> None:
        connection = self.connections.pop(server, None)
        if connection is not None and connection.task is not None:
            if not connection.ready.done():
                connection.task.cancel()
            else:
                connection.stop.set()
            await asyncio.gather(connection.task, return_exceptions=True)

    async def configure(self, servers: dict) -> None:
        normalized = normalize_servers(servers, self.servers)
        await self.aclose()
        self.servers = normalized

    async def aclose(self) -> None:
        for name in list(self.connections):
            await self.disconnect(name)
