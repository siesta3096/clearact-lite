"""Model-authored file plans and serial child runners with independent histories."""

import json
from dataclasses import asdict

from clearact_lite.context.builder import ContextBuilder
from clearact_lite.core.contracts import TaskPlan, ToolResult
from clearact_lite.runtime.runner import ExecutionContext, Runner
from clearact_lite.storage.snapshots import digest, workspace_path
from clearact_lite.tools.registry import ToolRegistry

FILE_TOOLS = {"file_list", "file_read", "file_write", "file_edit", "document_read", "history_read", "task_checkpoint"}


class AssignedFileTool:
    def __init__(self, tool, files):
        self.spec, self.tool, self.files = tool.spec, tool, files

    async def execute(self, arguments, context):
        path = workspace_path(context.workspace, arguments["path"])
        if path not in self.files:
            return ToolResult("File is not assigned to this child; report the required plan change to the parent", True)
        return await self.tool.execute(arguments, context)


class DelegationManager:
    def __init__(self):
        self.runner = None

    def bind(self, runner):
        self.runner = runner

    def plan(self, arguments, context):
        plan = TaskPlan.from_dict(arguments)
        ids = [t.id for t in plan.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("Plan task IDs must be unique")
        for task in plan.tasks:
            if task.tools is not None and set(task.tools) - (
                self.runner.registry.tools.keys() - {"task_plan", "agent_task"}
            ):
                raise ValueError("Child tool list contains an unavailable tool")
            for item in task.files:
                workspace_path(context.workspace, item.path)
            if any(dependency not in ids or dependency == task.id for dependency in task.depends_on):
                raise ValueError("Invalid task dependency")
        completed = set()
        while len(completed) < len(ids):
            ready = {t.id for t in plan.tasks if set(t.depends_on) <= completed} - completed
            if not ready:
                raise ValueError("Task dependency cycle")
            completed.update(ready)
        value = asdict(plan)
        event = context.emit("task.plan", value)
        return {"plan_event_id": event.id, "file_count": len({f.path for t in plan.tasks for f in t.files}), **value}

    async def execute(self, task_id, context):
        events = context.store.events(context.branch_id)
        plans = [e for e in events if e.kind == "task.plan"]
        if not plans:
            raise ValueError("Use task_plan before agent_task")
        plan_event = plans[-1]
        plan = TaskPlan.from_dict(plan_event.payload)
        task = next((t for t in plan.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError("Unknown planned task")
        done = {
            e.payload["task_id"]
            for e in events
            if e.kind == "agent.finished"
            and e.payload["plan_event_id"] == plan_event.id
            and e.payload["result"]["status"] == "finished"
        }
        if not set(task.depends_on) <= done:
            raise ValueError("Planned dependencies have not finished; model must decide how to proceed")
        branch = context.store.fork()

        def emit(kind, payload):
            event = context.store.append(branch, kind, payload)
            if kind in {"model.started", "tool.started", "file.written", "context.compacted", "capacity.continuation"}:
                context.emit(
                    "agent.progress",
                    {
                        "branch_id": branch,
                        "task_id": task_id,
                        "kind": kind,
                        "child_event_id": event.id,
                        "name": payload.get("name"),
                        "path": payload.get("path"),
                    },
                )
            return event

        emit(
            "agent.identity",
            {"parent_branch_id": context.branch_id, "task_id": task_id, "plan_event_id": plan_event.id},
        )
        context.emit(
            "agent.started",
            {
                "branch_id": branch,
                "task_id": task_id,
                "plan_event_id": plan_event.id,
                "files": [asdict(f) for f in task.files],
            },
        )
        assigned = {workspace_path(context.workspace, f.path) for f in task.files}
        tools = []
        available = set(task.tools) if task.tools is not None else FILE_TOOLS
        for tool in self.runner.registry.tools.values():
            if tool.spec.name not in available or tool.spec.name in {"task_plan", "agent_task"}:
                continue
            tools.append(AssignedFileTool(tool, assigned) if tool.spec.name in {"file_write", "file_edit"} else tool)
        settings = self.runner.settings
        child = Runner(
            self.runner.provider,
            ToolRegistry(tools),
            ContextBuilder(settings.context_tokens, settings.max_output_tokens, settings.tool_result_chars),
            settings.run_timeout_seconds,
            settings,
        )
        requirements = [e.payload["content"] for e in events if e.kind == "message" and e.payload["role"] == "user"]
        previous = [
            e
            for e in events
            if e.kind == "agent.started"
            and e.payload["task_id"] == task_id
            and e.payload["plan_event_id"] == plan_event.id
        ]
        prompt = (
            "You are a delegated child worker with an independent history. The parent's original requirements "
            "are reference constraints; execute ONLY the assigned task below, not the entire project. "
            "Read current workspace files as needed; file_write/file_edit can write only assigned paths. "
            "Use project_files to see what OTHER tasks implement; do not duplicate their assets or logic inline. "
            "Complete only your assigned file-writing scope. Project-wide browser acceptance and delivery are "
            "the parent's responsibility unless your assigned task explicitly includes them and provides those tools. "
            "Use manageable complete chunks. Report actual changes, checks, unresolved work concisely. "
            "The parent will integrate; a normal finish is not independent acceptance.\n"
            + json.dumps(
                {
                    "original_requirements": requirements,
                    "shared_interfaces": plan.interfaces,
                    "assigned_task": asdict(task),
                    "project_files": [
                        {"task_id": planned.id, "files": [asdict(f) for f in planned.files]} for planned in plan.tasks
                    ],
                    "previous_child_branches": [e.payload["branch_id"] for e in previous],
                    "previous_child_checkpoints": [
                        event.payload
                        for old in previous
                        for event in context.store.events(old.payload["branch_id"])
                        if event.kind == "task.checkpoint"
                    ][-3:],
                },
                ensure_ascii=False,
            )
        )
        result = await child.run(
            prompt, ExecutionContext(context.workspace, branch, context.store, context.snapshots, emit)
        )
        files = []
        for item in task.files:
            path = workspace_path(context.workspace, item.path)
            content = path.read_bytes() if path.is_file() else None
            files.append(
                {
                    "path": item.path,
                    "exists": content is not None,
                    "bytes": len(content) if content is not None else None,
                    "sha256": digest(content) if content is not None else None,
                }
            )
        receipt = {
            "task_id": task_id,
            "branch_id": branch,
            "plan_event_id": plan_event.id,
            "result": {**asdict(result), "answer": result.answer[:1600]},
            "files": files,
            "acceptance": "not_independently_verified",
        }
        context.emit("agent.finished", receipt)
        if result.status == "stopped":
            import asyncio

            raise asyncio.CancelledError("Child stopped; parent also stopped")
        return receipt
