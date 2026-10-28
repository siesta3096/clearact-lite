"""Model-authored file plans and serial child runners with independent histories."""

import json
from dataclasses import asdict

from clearact_lite.context.builder import ContextBuilder
from clearact_lite.context.compaction import tool_facts
from clearact_lite.core.contracts import HandoffFile, InterfaceContract, TaskHandoff, TaskPlan, ToolResult
from clearact_lite.runtime.reliability import state_from_events
from clearact_lite.runtime.runner import ExecutionContext, Runner
from clearact_lite.storage.snapshots import digest, workspace_path
from clearact_lite.tools.registry import ToolRegistry

FILE_TOOLS = {"file_list", "file_read", "file_write", "file_edit", "document_read", "history_read", "task_checkpoint"}


def file_receipt(workspace, name, before=None):
    path = workspace_path(workspace, name)
    content = path.read_bytes() if path.is_file() else None
    sha = digest(content) if content is not None else None
    return asdict(
        HandoffFile(
            name, content is not None, len(content) if content is not None else None, sha, before, sha != before
        )
    )


def report_excerpt(text):
    if len(text) <= 1600:
        return text
    return text[:1000] + "\n[Report middle omitted; full text at report_event_id]\n" + text[-500:]


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
        if len({item.name for item in plan.contracts}) != len(plan.contracts):
            raise ValueError("Plan contract names must be unique")
        for item in plan.contracts:
            if item.provider_task not in ids or any(consumer not in ids for consumer in item.consumers):
                raise ValueError("Contract provider/consumers must reference planned tasks")
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
        return {
            "plan_event_id": event.id,
            "file_count": len({f.path for t in plan.tasks for f in t.files}),
            "ready_task_ids": [t.id for t in plan.tasks if not t.depends_on],
            **value,
        }

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
        latest = {
            e.payload["task_id"]: e.payload
            for e in events
            if e.kind == "agent.finished" and e.payload["plan_event_id"] == plan_event.id
        }
        done = {key for key, receipt in latest.items() if receipt["result"]["status"] == "finished"}
        if not set(task.depends_on) <= done:
            raise ValueError("Planned dependencies have not finished; model must decide how to proceed")
        handoffs, related = [], []
        assigned = {workspace_path(context.workspace, f.path) for f in task.files}
        current_versions = {}
        for producer, receipt in latest.items():
            if producer == task_id:
                continue
            basis = []
            if producer in task.depends_on:
                basis.append("explicit_dependency")
            if any(workspace_path(context.workspace, f["path"]) in assigned for f in receipt["files"]):
                basis.append("shared_files")
            if any(c.provider_task == producer and task_id in c.consumers for c in plan.contracts):
                basis.append("contract_provider")
            if not basis:
                continue
            versions = []
            for item in receipt["files"]:
                path = item["path"]
                if path not in current_versions:
                    current_versions[path] = file_receipt(context.workspace, path)["sha256"]
                versions.append(
                    {
                        **item,
                        "current_sha256": current_versions[path],
                        "matches_handoff": current_versions[path] == item["sha256"],
                    }
                )
            value = {
                "task_id": producer,
                "branch_id": receipt["branch_id"],
                "basis": basis,
                "handoff": receipt.get("handoff"),
                "files": versions,
                "report": receipt["result"]["answer"],
                "status": receipt["result"]["status"],
                "acceptance": receipt["acceptance"],
            }
            (handoffs if producer in task.depends_on else related).append(value)
        before_files = {f.path: file_receipt(context.workspace, f.path)["sha256"] for f in task.files}
        branch = context.store.fork()

        def emit(kind, payload):
            event = context.store.append(branch, kind, payload)
            if kind in {
                "model.started",
                "tool.started",
                "file.written",
                "context.compacted",
                "capacity.continuation",
                "reliability.diagnosing",
                "reliability.blocked",
                "reliability.paused",
            }:
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
        emit(
            "task.assignment",
            {
                "plan_event_id": plan_event.id,
                "task": asdict(task),
                "handoff_branches": [h["branch_id"] for h in handoffs + related],
            },
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
            ContextBuilder(
                settings.context_tokens, settings.max_output_tokens, settings.tool_result_chars, settings.tokenizer_key
            ),
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
        if previous:
            prior_state = state_from_events(context.store.events(previous[-1].payload["branch_id"]))
            if prior_state.started_at:
                emit("reliability.updated", asdict(prior_state))
        prompt = (
            "You are a delegated child worker with an independent history. The parent's original requirements "
            "are reference constraints; execute ONLY the assigned task below, not the entire project. "
            "Read current workspace files as needed; file_write/file_edit can write only assigned paths. "
            "Use project_files to see what OTHER tasks implement; do not duplicate their assets or logic inline. "
            "Finish only the assigned deliverable/increment, then hand back its interfaces and unresolved work. "
            "Do not expand the assignment into subsequent content/assets/polish tasks. Dependency handoffs are "
            "reports and file coordinates, not new project-wide work. Project-wide browser acceptance and delivery are "
            "the parent's responsibility unless your assigned task explicitly includes them and provides those tools. "
            "Use manageable complete chunks. Report actual changes, checks, unresolved work concisely. "
            "For interface handoffs, task_checkpoint can record actual interfaces and unresolved items. "
            "Planned contracts are intentions, not proof of exports. Check file versions before relying on reports; "
            "history_read can read handed-off branches using their branch_id and event_id. "
            "The parent will integrate; a normal finish is not independent acceptance.\n"
            + json.dumps(
                {
                    "original_requirements": requirements,
                    "shared_interfaces": plan.interfaces,
                    "planned_contracts": [
                        asdict(c) for c in plan.contracts if c.provider_task == task_id or task_id in c.consumers
                    ],
                    "assigned_task": asdict(task),
                    "project_files": [
                        {"task_id": planned.id, "files": [asdict(f) for f in planned.files]} for planned in plan.tasks
                    ],
                    "dependency_handoffs": handoffs,
                    "related_handoffs": related,
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
            prompt,
            ExecutionContext(context.workspace, branch, context.store, context.snapshots, emit),
            allow_recovery_resume=False,
        )
        files = [file_receipt(context.workspace, item.path, before_files[item.path]) for item in task.files]
        child_events = context.store.events(branch)
        checkpoints = [e for e in child_events if e.kind == "task.checkpoint"]
        checkpoint = checkpoints[-1] if checkpoints else None
        data = checkpoint.payload if checkpoint else {}
        reports = [e for e in child_events if e.kind == "run.ended"]
        interface_checkpoint = next((e for e in reversed(checkpoints) if "interfaces" in e.payload), None)
        unresolved_checkpoint = next((e for e in reversed(checkpoints) if "unresolved" in e.payload), None)
        contracts = (
            tuple(InterfaceContract.from_dict(c) for c in interface_checkpoint.payload["interfaces"])
            if interface_checkpoint
            else ()
        )
        if not interface_checkpoint:
            contracts = tuple(c for c in plan.contracts if c.provider_task == task_id)
        unresolved = tuple(unresolved_checkpoint.payload["unresolved"]) if unresolved_checkpoint else ()
        if not unresolved_checkpoint and result.status != "finished" and data.get("next_steps"):
            unresolved = (data["next_steps"],)
        valid_ids = {e.id for e in child_events}
        handoff = TaskHandoff(
            task_id,
            branch,
            reports[-1].id,
            interfaces=contracts,
            interface_source="checkpoint" if interface_checkpoint else "plan" if contracts else "none",
            unresolved=unresolved,
            evidence_event_ids=tuple(i for i in data.get("evidence_event_ids", ()) if i in valid_ids),
            checkpoint_event_id=checkpoint.id if checkpoint else None,
            interface_checkpoint_event_id=interface_checkpoint.id if interface_checkpoint else None,
            unresolved_checkpoint_event_id=unresolved_checkpoint.id if unresolved_checkpoint else None,
        )
        handoff_event = emit("task.handoff", asdict(handoff))
        receipt = {
            "task_id": task_id,
            "branch_id": branch,
            "plan_event_id": plan_event.id,
            "handoff": {"event_id": handoff_event.id, **handoff.context_view()},
            "result": {**asdict(result), "answer": report_excerpt(result.answer)},
            "files": files,
            "acceptance": "not_independently_verified",
            "deliverable": task.deliverable,
            "working_state": {
                "successful_tool_facts": tool_facts(context.store.events(branch)),
                "recovery": state_from_events(context.store.events(branch)).context_view(),
                "checkpoints": [
                    {"event_id": e.id, **e.payload} for e in context.store.events(branch) if e.kind == "task.checkpoint"
                ][-2:],
                "report_reference": {"branch_id": branch, "kind": "run.ended", "event_id": reports[-1].id},
                "review": "Parent decides focused checks; reuse valid evidence; unresolved work is not acceptance",
            },
        }
        done.discard(task_id)
        if result.status == "finished":
            done.add(task_id)
        receipt["ready_task_ids"] = [t.id for t in plan.tasks if t.id not in done and set(t.depends_on) <= done]
        receipt["pending_task_ids"] = [t.id for t in plan.tasks if t.id not in done]
        context.emit("agent.finished", receipt)
        if result.status == "stopped" and not (result.error or "").startswith("Recovery paused:"):
            import asyncio

            raise asyncio.CancelledError("Child stopped; parent also stopped")
        return receipt
