import json

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.core.ports import MCPPort, ToolContext
from clearact_lite.tools.files import object_schema


class ListMCPTools:
    spec = ToolSpec(
        "mcp_list_tools",
        "Without server, list configured MCP servers. With server, connect and "
        "return paginated tool names, descriptions and schemas. No extra approval layer.",
        object_schema(
            {
                "server": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            [],
        ),
    )

    def __init__(self, manager: MCPPort):
        self.manager = manager

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if arguments.get("server"):
            data = await self.manager.list_tools(
                arguments["server"], arguments.get("offset", 0), arguments.get("limit", 20)
            )
        else:
            data = {
                "servers": [
                    {k: s[k] for k in ("name", "enabled", "transport", "connected", "tool_count", "error")}
                    for s in self.manager.overview()
                ]
            }
        return ToolResult(json.dumps(data, ensure_ascii=False))


class CallMCPTool:
    spec = ToolSpec(
        "mcp_call_tool",
        "Call a configured MCP server tool with its schema-valid arguments. "
        "Inspect mcp_list_tools first when its schema is unknown. External side effects are recorded, "
        "but cannot be undone by file snapshots.",
        object_schema(
            {
                "server": {"type": "string"},
                "tool": {"type": "string"},
                "arguments": {"type": "object", "additionalProperties": True},
            },
            ["server", "tool", "arguments"],
        ),
    )

    def __init__(self, manager: MCPPort):
        self.manager = manager

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        text, error = await self.manager.call(arguments["server"], arguments["tool"], arguments["arguments"], context)
        return ToolResult(text, is_error=error)
