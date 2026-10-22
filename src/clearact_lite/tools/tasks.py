import json

from clearact_lite.core.contracts import ToolResult, ToolSpec
from clearact_lite.core.ports import DelegationPort, ToolContext
from clearact_lite.tools.files import object_schema


class PlanTask:
    spec = ToolSpec(
        "task_plan",
        "Save a model-authored file plan before delegating multi-file work. Specify every file's "
        "responsibility, shared interfaces, and task dependencies. Optional task tools selects child capabilities; "
        "default is file tools/history/checkpoint. Add browser/MCP tools explicitly when needed. "
        "Parent integrates/tests/delivers the complete project. Replacing a plan preserves older versions.",
        object_schema(
            {
                "interfaces": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": object_schema(
                        {
                            "id": {"type": "string", "minLength": 1},
                            "instruction": {"type": "string", "minLength": 1},
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "items": object_schema(
                                    {"path": {"type": "string"}, "purpose": {"type": "string"}}, ["path", "purpose"]
                                ),
                            },
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "tools": {"type": ["array", "null"], "items": {"type": "string"}},
                        },
                        ["id", "instruction", "files"],
                    ),
                },
            },
            ["interfaces", "tasks"],
        ),
    )

    def __init__(self, manager: DelegationPort):
        self.manager = manager

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(json.dumps(self.manager.plan(arguments, context), ensure_ascii=False))


class DelegateTask:
    spec = ToolSpec(
        "agent_task",
        "Execute a planned task in an independent child context, serially on the local model. "
        "Parent receives a short report and file hashes; full child messages stay in its branch. "
        "You must integrate/test the project yourself. Child may continue from existing files after interruption.",
        object_schema({"task_id": {"type": "string"}}, ["task_id"]),
    )

    def __init__(self, manager: DelegationPort):
        self.manager = manager

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        result = await self.manager.execute(arguments["task_id"], context)
        return ToolResult(json.dumps(result, ensure_ascii=False), result["result"]["status"] != "finished")


class SaveCheckpoint:
    spec = ToolSpec(
        "task_checkpoint",
        "Save a concise working checkpoint: current state, unresolved work and next steps. "
        "Preserved through context compaction. This is model-authored state, not verified task acceptance.",
        object_schema({"state": {"type": "string"}, "next_steps": {"type": "string"}}, ["state", "next_steps"]),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        event = context.emit("task.checkpoint", {**arguments, "source": "model", "verified": False})
        return ToolResult(f"Working checkpoint recorded at event {event.id}")
