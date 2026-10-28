"""Protocol fixtures exercise real runners/storage, not real model acceptance."""

import asyncio
import json

import pytest
from test_foundation import reply
from test_long_work import execution

from clearact_lite.bootstrap import build_application
from clearact_lite.context.compaction import memory_index
from clearact_lite.core.contracts import InterfaceContract, TaskHandoff, TaskPlan, ToolCall
from clearact_lite.settings import Settings
from clearact_lite.storage.snapshots import digest


def plan():
    return {
        "interfaces": "producer exports a value for consumer",
        "contracts": [
            {
                "name": "value",
                "provider_task": "producer",
                "signature": "planned value:number",
                "consumers": ["consumer"],
                "invariants": "nonnegative",
            }
        ],
        "tasks": [
            {"id": "producer", "instruction": "export value", "files": [{"path": "api.js", "purpose": "API"}]},
            {"id": "consumer", "instruction": "use value", "files": [{"path": "use.js", "purpose": "consumer"}]},
        ],
    }


@pytest.mark.parametrize("invalid", ["duplicate", "provider", "consumer"])
def test_contract_references_are_structural_and_legacy_plans_remain_valid(tmp_path, invalid):
    value = plan()
    old = {key: value[key] for key in ("interfaces", "tasks")}
    assert TaskPlan.from_dict(old).contracts == ()
    app = build_application(Settings(tmp_path))
    ctx = execution(app, app.fork())

    async def scenario():
        good = await app.runner.registry.execute("task_plan", value, ctx)
        assert not good.is_error
        assert json.loads(good.content)["ready_task_ids"] == ["producer", "consumer"]
        assert TaskPlan.from_dict(value).tasks[1].depends_on == ()  # No automatic scheduling.
        if invalid == "duplicate":
            value["contracts"].append(dict(value["contracts"][0]))
        elif invalid == "provider":
            value["contracts"][0]["provider_task"] = "absent"
        else:
            value["contracts"][0]["consumers"] = ["absent"]
        bad = await app.runner.registry.execute("task_plan", value, ctx)
        assert bad.is_error
        assert len([e for e in app.events(ctx.branch_id) if e.kind == "task.plan"]) == 1

    try:
        asyncio.run(scenario())
    finally:
        app.close()


def test_related_handoff_preserves_actual_contracts_unresolved_report_tail_versions_and_history(tmp_path):
    class Provider:
        calls = 0
        consumers = 0
        producer = 0
        payloads = []

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            prompt = next(m.content for m in messages if m.role == "user")
            payload = json.loads(prompt[prompt.index('{"original_requirements"') :])
            task = payload["assigned_task"]["id"]
            if task == "producer":
                self.producer += 1
                if self.producer == 1:
                    return reply(
                        calls=[ToolCall("write", "file_write", {"path": "api.js", "text": "export const value=7;"})]
                    )
                if self.producer == 2:
                    return reply(
                        calls=[
                            ToolCall(
                                "checkpoint",
                                "task_checkpoint",
                                {
                                    "state": "API implemented",
                                    "next_steps": "Integrate consumer",
                                    "interfaces": [
                                        {
                                            "name": "value",
                                            "provider_task": "producer",
                                            "signature": "actual value:number=7",
                                            "consumers": ["consumer"],
                                        }
                                    ],
                                    "unresolved": ["Consumer integration still pending"],
                                },
                            )
                        ]
                    )
                return reply(
                    "Report start\n" + "implementation detail " * 600 + "\nTAIL: units are seconds, not milliseconds"
                )
            self.consumers += 1
            self.payloads.append(payload)
            handed = payload["related_handoffs"][0]
            assert payload["dependency_handoffs"] == []
            assert handed["handoff"]["interface_source"] == "checkpoint"
            assert handed["handoff"]["interfaces"][0]["signature"] == "actual value:number=7"
            assert handed["handoff"]["unresolved"] == ["Consumer integration still pending"]
            return reply("Read handoff, no changes needed")

    provider = Provider()
    app = build_application(Settings(tmp_path), provider)
    ctx = execution(app, app.fork())

    async def scenario():
        await app.runner.registry.execute("task_plan", plan(), ctx)
        first = await app.runner.registry.execute("agent_task", {"task_id": "producer"}, ctx)
        receipt = json.loads(first.content)
        assert receipt["files"][0]["changed"] and receipt["files"][0]["before_sha256"] is None
        assert "TAIL: units" in receipt["result"]["answer"]
        child = receipt["branch_id"]
        raw = app.events(child)
        full = next(e for e in raw if e.id == receipt["handoff"]["report_event_id"])
        assert len(full.payload["answer"]) > 10000
        assert "Consumer integration still pending" in memory_index(raw, full.id)
        await app.runner.registry.execute("agent_task", {"task_id": "consumer"}, ctx)
        before = provider.payloads[-1]["related_handoffs"][0]
        assert before["files"][0]["matches_handoff"] is True
        # Parent edit invalidates the provider's source version; no hidden re-delegation.
        await app.runner.registry.execute("file_write", {"path": "api.js", "text": "export const value=8;"}, ctx)
        last = await app.runner.registry.execute("agent_task", {"task_id": "consumer"}, ctx)
        after = provider.payloads[-1]["related_handoffs"][0]
        assert after["files"][0]["matches_handoff"] is False
        assert after["files"][0]["current_sha256"] == digest(b"export const value=8;")
        consumer_ctx = execution(app, json.loads(last.content)["branch_id"])
        accessible = await app.runner.registry.execute(
            "history_read", {"branch_id": child, "event_id": full.id}, consumer_ctx
        )
        assert not accessible.is_error and "Report start" in accessible.content
        forbidden = await app.runner.registry.execute("history_read", {"branch_id": ctx.branch_id}, consumer_ctx)
        assert forbidden.is_error
        assert provider.calls == 5  # Three provider requests and one per consumer; no extra reviewer.
        archived = memory_index(app.events(ctx.branch_id), 0)
        assert "Consumer integration still pending" in archived

    try:
        asyncio.run(scenario())
    finally:
        app.close()


def test_handoff_excerpt_keeps_full_raw_record_and_truthful_truncation():
    handoff = TaskHandoff(
        "a", "branch", 1, interfaces=(InterfaceContract("api", "a", "s" * 500),), unresolved=("u" * 1000, "later")
    )
    short = handoff.context_view()
    assert short["context_excerpt"] is True
    assert len(short["unresolved"][0]) == 850 and len(handoff.unresolved[0]) == 1000
    assert len(short["interfaces"][0]["signature"]) == 400
    assert TaskHandoff("a", "b", 1).context_view()["context_excerpt"] is False


def test_checkpoint_rejects_other_task_provider(tmp_path):
    app = build_application(Settings(tmp_path))
    ctx = execution(app, app.fork())
    ctx.emit("task.assignment", {"task": {"id": "assigned"}})

    async def scenario():
        return await app.runner.registry.execute(
            "task_checkpoint",
            {
                "state": "ready",
                "next_steps": "integrate",
                "interfaces": [{"name": "x", "provider_task": "other", "signature": "x()"}],
            },
            ctx,
        )

    try:
        assert asyncio.run(scenario()).is_error
        assert not any(e.kind == "task.checkpoint" for e in app.events(ctx.branch_id))
    finally:
        app.close()


def test_new_failed_attempt_replaces_earlier_success_in_pending_and_dependency_state(tmp_path):
    class Provider:
        calls = 0

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            if self.calls == 1:
                return reply("First attempt ended normally")
            raise RuntimeError("New attempt failed")

    app = build_application(Settings(tmp_path), Provider())
    ctx = execution(app, app.fork())

    async def scenario():
        value = plan()
        value["tasks"][1]["depends_on"] = ["producer"]
        await app.runner.registry.execute("task_plan", value, ctx)
        first = await app.runner.registry.execute("agent_task", {"task_id": "producer"}, ctx)
        assert json.loads(first.content)["pending_task_ids"] == ["consumer"]
        failed = await app.runner.registry.execute("agent_task", {"task_id": "producer"}, ctx)
        assert failed.is_error
        assert json.loads(failed.content)["pending_task_ids"] == ["producer", "consumer"]
        blocked = await app.runner.registry.execute("agent_task", {"task_id": "consumer"}, ctx)
        assert blocked.is_error and "dependencies have not finished" in blocked.content
        assert len([e for e in app.events(ctx.branch_id) if e.kind == "agent.finished"]) == 2

    try:
        asyncio.run(scenario())
    finally:
        app.close()


@pytest.mark.parametrize("declared", [False, True])
def test_shared_file_or_planned_contract_handoffs_do_not_claim_actual_interfaces(tmp_path, declared):
    class Provider:
        calls = 0

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            if self.calls == 1:
                return reply("Export report without structured checkpoint")
            prompt = next(m.content for m in messages if m.role == "user")
            payload = json.loads(prompt[prompt.index('{"original_requirements"') :])
            handed = payload["related_handoffs"][0]
            assert handed["basis"] == ["contract_provider" if declared else "shared_files"]
            assert handed["handoff"]["interface_source"] == ("plan" if declared else "none")
            assert handed["handoff"]["verified"] is False
            return reply("Related handoff available")

    app = build_application(Settings(tmp_path), Provider())
    ctx = execution(app, app.fork())

    async def scenario():
        value = plan()
        if not declared:
            value.pop("contracts")
            value["tasks"][1]["files"] = [{"path": "api.js", "purpose": "next increment"}]
        await app.runner.registry.execute("task_plan", value, ctx)
        await app.runner.registry.execute("agent_task", {"task_id": "producer"}, ctx)
        result = await app.runner.registry.execute("agent_task", {"task_id": "consumer"}, ctx)
        assert not result.is_error

    try:
        asyncio.run(scenario())
    finally:
        app.close()
