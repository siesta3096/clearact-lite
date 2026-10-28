import json
from dataclasses import asdict

from clearact_lite.core.contracts import InterfaceContract, ToolResult, ToolSpec
from clearact_lite.core.ports import DelegationPort, ToolContext
from clearact_lite.tools.files import object_schema

INTERFACE_SCHEMA = object_schema(
    {
        "name": {"type": "string", "minLength": 1},
        "provider_task": {"type": "string", "minLength": 1},
        "signature": {"type": "string", "minLength": 1},
        "consumers": {"type": "array", "items": {"type": "string"}},
        "invariants": {"type": "string"},
    },
    ["name", "provider_task", "signature"],
)


class PlanTask:
    spec = ToolSpec(
        "task_plan",
        "Plan bounded implementation increments for a multi-part deliverable. Specify file responsibilities, "
        "shared interfaces (exports, arguments/returns, units, ownership and callers), dependencies, "
        "focused verification and each increment's concrete deliverable. The first increment is a "
        "minimal runnable core, followed by separate content/assets/polish work. Do not assign the whole "
        "application to one child. Dependent increments may edit the same file. "
        "Optional task tools selects child capabilities; "
        "default is file tools/history/checkpoint. Add browser/MCP tools explicitly when needed. "
        "Optional contracts declares named provider/consumer interfaces without changing dependencies. "
        "Parent integrates/tests/delivers the complete project. Replacing a plan preserves older versions.",
        object_schema(
            {
                "interfaces": {"type": "string"},
                "contracts": {"type": "array", "items": INTERFACE_SCHEMA},
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": object_schema(
                        {
                            "id": {"type": "string", "minLength": 1},
                            "instruction": {"type": "string", "minLength": 1},
                            "deliverable": {"type": "string"},
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
        "Parent receives interfaces, unresolved work, file changes and report references; "
        "full child messages stay in its branch. "
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
        "Optional interfaces records actual exported signatures using the plan contract shape; unresolved lists "
        "remaining work. Preserved through context compaction. Model-authored, not verified acceptance.",
        object_schema(
            {
                "state": {"type": "string"},
                "next_steps": {"type": "string"},
                "problem": {"type": "string"},
                "changed_method": {"type": "string"},
                "success_criterion": {"type": "string"},
                "resolved": {"type": "boolean"},
                "interfaces": {"type": "array", "items": INTERFACE_SCHEMA},
                "unresolved": {"type": "array", "items": {"type": "string"}},
                "evidence_event_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            },
            ["state", "next_steps"],
        ),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        value = dict(arguments)
        if "interfaces" in value:
            interfaces = tuple(InterfaceContract.from_dict(item) for item in value["interfaces"])
            assignments = [e for e in context.store.events(context.branch_id) if e.kind == "task.assignment"]
            if assignments and any(item.provider_task != assignments[-1].payload["task"]["id"] for item in interfaces):
                raise ValueError("Checkpoint interface provider must be the assigned task")
            value["interfaces"] = [asdict(item) for item in interfaces]
        event = context.emit("task.checkpoint", {**value, "source": "model", "verified": False})
        return ToolResult(f"Working checkpoint recorded at event {event.id}")
