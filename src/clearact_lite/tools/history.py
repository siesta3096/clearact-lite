import json

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.core.ports import ToolContext
from clearact_lite.tools.files import object_schema


class ReadHistory:
    spec = ToolSpec(
        "history_read",
        "Read an original event with character pagination. Optional branch_id may reference a child "
        "delegated by this branch. Omit event_id to list event IDs/kinds for the selected history.",
        object_schema(
            {
                "event_id": {"type": "integer", "minimum": 1},
                "branch_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            [],
        ),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        branch = arguments.get("branch_id", context.branch_id)
        current = context.store.events(context.branch_id)
        if branch != context.branch_id and branch not in {
            e.payload["branch_id"] for e in current if e.kind == "agent.started"
        }:
            return ToolResult("History must belong to this branch or its delegated child", True)
        events = context.store.events(branch)
        if "event_id" in arguments:
            event = next((e for e in events if e.id == arguments["event_id"]), None)
            if event is None:
                return ToolResult("Event not found in this branch", is_error=True)
            content = (
                event.payload.get("content")
                if event.kind == "message" and not event.payload.get("tool_calls")
                else None
            )
            if content is None:
                content = json.dumps(event.payload, ensure_ascii=False)
        else:
            content = json.dumps([{"id": e.id, "kind": e.kind, "role": e.payload.get("role")} for e in events])
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 6000)
        return ToolResult(f"characters={len(content)}; offset={offset}\n" + content[offset : offset + limit])
