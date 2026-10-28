"""Working-context behavior; protocol doubles are not real-model acceptance."""

import asyncio
import json
from dataclasses import asdict

import pytest
from test_foundation import reply
from test_long_work import execution

from clearact_lite.bootstrap import build_application
from clearact_lite.context.budget import fingerprint
from clearact_lite.context.builder import ContextBuilder
from clearact_lite.context.compaction import prepare_context
from clearact_lite.core.contracts import Event, Message, TaskPlan, ToolCall, ToolSpec, Usage, validate_messages
from clearact_lite.settings import Settings


def record_measurement(emit, messages, info, response, usage):
    projected = emit("context.projected", info)
    emit(
        "model.returned",
        {
            "context_event_id": projected.id,
            "response_message_hash": fingerprint(response.to_dict()),
            "usage": asdict(usage),
        },
    )
    emit("message", response.to_dict())


def test_reported_request_and_completion_replace_whole_request_byte_guess():
    events = []

    def emit(kind, payload):
        event = Event(len(events) + 1, "branch", kind, payload, "")
        events.append(event)
        return event

    builder = ContextBuilder(100096, 16384, 6000, "model-A")
    emit("message", Message("system", "Implement the requested files").to_dict())
    emit("message", Message("user", "Keep both endings").to_dict())
    messages, info = builder.build(events, [])
    response = Message(
        "assistant",
        tool_calls=(
            ToolCall(
                "write",
                "file_write",
                {
                    "path": "art.svg",
                    "text": '<path fill="#abcdef" d="M1 2"/>' * 2400,
                },
            ),
        ),
    )
    record_measurement(emit, messages, info, response, Usage(17035, 15633))
    emit("message", Message("tool", '{"bytes":72000}', tool_call_id="write").to_dict())
    projected, measured = builder.build(events, [])
    assert measured["unmeasured_byte_bound"] > 80000
    assert 32700 < measured["estimated_input_tokens"] < 36000
    assert measured["estimated_occupancy"] < 0.55
    assert measured["estimator"] == "reported_input_plus_bounded_delta"
    validate_messages(projected)
    for key, tools in [("model-B", []), ("model-A", [ToolSpec("changed", "changed", {})])]:
        # Model/schema changes must not reuse another request's measurement.
        _, unknown = ContextBuilder(100096, 16384, 6000, key).build(events, tools, enforce_limit=False)
        assert unknown["estimator"] == "utf8_bound_without_matching_measurement"


def test_measurement_survives_one_shot_capacity_notice_disappearing():
    events = []

    def emit(kind, payload):
        event = Event(len(events) + 1, "branch", kind, payload, "")
        events.append(event)
        return event

    builder = ContextBuilder(100096, 16384, 6000)
    emit("message", Message("user", "Continue the file").to_dict())
    emit("capacity.continuation", {"instruction": "Continue from the completed action"})
    messages, info = builder.build(events, [])
    assert info["transient_tail_hash"]
    response = Message(
        "assistant",
        tool_calls=(
            ToolCall(
                "write",
                "file_write",
                {
                    "path": "x",
                    "text": "source " * 4000,
                },
            ),
        ),
    )
    record_measurement(emit, messages, info, response, Usage(3000, 7000))
    emit("message", Message("tool", "written", tool_call_id="write").to_dict())
    following, estimate = builder.build(events, [])
    assert not estimate["transient_tail_hash"]
    assert estimate["estimator"] == "reported_input_plus_bounded_delta"
    assert 10000 < estimate["estimated_input_tokens"] < 12000
    assert len(following) == 3


def test_unknown_output_is_bounded_instead_of_treated_as_zero():
    events = []

    def emit(kind, payload):
        event = Event(len(events) + 1, "branch", kind, payload, "")
        events.append(event)
        return event

    builder = ContextBuilder(32768, 1024, 6000)
    emit("message", Message("user", "Write source").to_dict())
    messages, info = builder.build(events, [])
    record_measurement(emit, messages, info, Message("assistant", "source " * 1000), Usage(100, None))
    _, result = builder.build(events, [])
    assert result["estimated_input_tokens"] > 7000


def test_archive_preserves_last_write_and_recent_read_even_above_target(tmp_path):
    settings = Settings(tmp_path, context_tokens=32000, max_output_tokens=1024, compact_target=0.2)
    app = build_application(settings)
    branch = app.fork()
    ctx = execution(app, branch)
    emit = app._emit(branch)
    emit("message", Message("system", "system").to_dict())
    emit("message", Message("user", "Keep original requirements and both endings").to_dict())
    emit("task.checkpoint", {"state": "starting empty", "next_steps": "Implement core"})

    async def transaction(call):
        emit("message", Message("assistant", tool_calls=(call,)).to_dict())
        result = await app.runner.registry.execute(call.name, call.arguments, ctx)
        assert not result.is_error
        emit("message", Message("tool", result.content, tool_call_id=call.id).to_dict())

    async def scenario():
        await transaction(ToolCall("old", "file_write", {"path": "old.txt", "text": "old source " * 4000}))
        await transaction(ToolCall("active", "file_write", {"path": "core.js", "text": "ACTIVE " * 1200}))
        await transaction(ToolCall("recent", "file_read", {"path": "core.js", "offset": 100, "limit": 200}))

    try:
        asyncio.run(scenario())
        before = app.events(branch)
        builder = ContextBuilder(32000, 1024, 6000, settings.tokenizer_key)
        messages, info = prepare_context(builder, before, [], emit, settings)
        calls = [c.id for m in messages for c in m.tool_calls]
        assert "old" not in calls and set(calls) == {"active", "recent"}
        validate_messages(messages)
        archived = [e for e in app.events(branch) if e.kind == "context.compacted"][-1]
        assert archived.payload["target_reached"] is False
        assert 0.2 < info["estimated_occupancy"] < 0.9
        assert "Read current files before editing" not in messages[0].content
        assert '"bytes": 8400' in messages[0].content and "newer_file_event_ids" in messages[0].content
        assert app.events(branch)[: len(before)] == before
        rebuilt, _ = builder.build(app.events(branch), [])
        assert rebuilt == messages
        response = Message("assistant", "Continue the assigned increment")
        record_measurement(emit, messages, info, response, Usage(7000, 10))
        emit("message", Message("user", "Continue").to_dict())
        following, measured = builder.build(app.events(branch), [])
        assert following[0].content == messages[0].content
        assert measured["estimator"] == "reported_input_plus_bounded_delta"
    finally:
        app.close()


def test_reported_near_capacity_triggers_archive_without_losing_active_write(tmp_path):
    settings = Settings(tmp_path, context_tokens=16000, max_output_tokens=1000)
    app = build_application(settings)
    branch = app.fork()
    emit = app._emit(branch)
    ctx = execution(app, branch)
    builder = ContextBuilder(16000, 1000, 6000, settings.tokenizer_key)
    emit("message", Message("user", "Keep the runnable core").to_dict())

    async def scenario():
        for name, size in [("old", 8000), ("core", 500)]:
            call = ToolCall(name, "file_write", {"path": name + ".js", "text": name * size})
            emit("message", Message("assistant", tool_calls=(call,)).to_dict())
            result = await app.runner.registry.execute(call.name, call.arguments, ctx)
            emit("message", Message("tool", result.content, tool_call_id=name).to_dict())

    try:
        asyncio.run(scenario())
        messages, info = builder.build(app.events(branch), [], enforce_limit=False)
        record_measurement(emit, messages, info, Message("assistant", "Core is ready"), Usage(14000, 20))
        messages, _ = prepare_context(builder, app.events(branch), [], emit, settings)
        archived = [e for e in app.events(branch) if e.kind == "context.compacted"][-1]
        assert archived.payload["estimator"] == "reported_input_plus_bounded_delta"
        assert [c.id for m in messages for c in m.tool_calls] == ["core"]
        assert any(m.content == "Core is ready" for m in messages)
        validate_messages(messages)
    finally:
        app.close()


@pytest.mark.parametrize("absolute", [False, True])
def test_directory_root_and_restore_publish_consistent_file_metadata(tmp_path, absolute):
    app = build_application(Settings(tmp_path))
    branch = app.fork()
    ctx = execution(app, branch)

    async def scenario():
        for text in ["原文", "改文"]:
            result = await app.runner.registry.execute("file_write", {"path": "x.txt", "text": text}, ctx)
            assert not result.is_error
        changes = [e for e in app.events(branch) if e.kind == "file.written"]
        assert changes[-1].payload["bytes"] == 6 and changes[-1].payload["text_tail"] == "改文"
        app.restore(branch, changes[-1].id)
        restored = app.events(branch)[-1]
        assert restored.payload["bytes"] == 6 and restored.payload["text_tail"] == "原文"
        listed = await app.runner.registry.execute("file_list", {"path": str(tmp_path) if absolute else "."}, ctx)
        assert not listed.is_error and json.loads(listed.content)["entries"][0]["bytes"] == 6

    try:
        asyncio.run(scenario())
    finally:
        app.close()


def test_task_deliverable_and_dependency_handoff_bound_child_work(tmp_path):
    plan = {
        "interfaces": "window.CORE.ready is true; story.js adds window.STORY",
        "tasks": [
            {
                "id": "core",
                "instruction": "Write core only",
                "deliverable": "Runnable empty core",
                "files": [{"path": "core.js", "purpose": "Core"}],
            },
            {
                "id": "story",
                "instruction": "Write data only",
                "deliverable": "Story data for existing core",
                "files": [{"path": "story.js", "purpose": "Data"}],
                "depends_on": ["core"],
            },
        ],
    }
    assert TaskPlan.from_dict(plan).tasks[0].deliverable == "Runnable empty core"

    class Provider:
        parent = 0
        children = {}

        async def complete(self, messages, tools, on_delta):
            prompt = next(
                (m.content for m in messages if m.role == "user" and "delegated child worker" in m.content), None
            )
            if prompt:
                payload = json.loads(prompt[prompt.index('{"original_requirements"') :])
                task = payload["assigned_task"]["id"]
                self.children[task] = self.children.get(task, 0) + 1
                if task == "story":
                    handoff = payload["dependency_handoffs"][0]
                    assert handoff["files"][0]["path"] == "core.js"
                    assert "CORE.ready" in handoff["report"]
                if self.children[task] == 1:
                    return reply(calls=[ToolCall(task, "file_write", {"path": task + ".js", "text": task})])
                return reply("CORE.ready is the exported core interface" if task == "core" else "story ready")
            self.parent += 1
            if self.parent == 1:
                return reply(calls=[ToolCall("plan", "task_plan", plan)])
            if self.parent == 2:
                return reply(calls=[ToolCall("delegate-core", "agent_task", {"task_id": "core"})])
            if self.parent == 3:
                receipt = json.loads(messages[-1].content)
                assert receipt["ready_task_ids"] == ["story"] and receipt["pending_task_ids"] == ["story"]
                return reply(calls=[ToolCall("delegate-story", "agent_task", {"task_id": "story"})])
            return reply("integrated")

    app = build_application(Settings(tmp_path), Provider())
    try:
        result = asyncio.run(app.run("Build runnable core, then story"))
        assert result.status == "finished"
        children = app.report(result.branch_id)["children"]
        assert len(children) == 2 and children[-1]["pending_task_ids"] == []
    finally:
        app.close()


@pytest.mark.parametrize("archived", [False, True])
def test_manual_restore_is_visible_to_the_next_model_projection(tmp_path, archived):
    app = build_application(Settings(tmp_path))
    branch = app.fork()
    emit = app._emit(branch)
    builder = ContextBuilder(32768, 1024, 6000)
    emit("message", Message("system", "Continue from current files").to_dict())
    emit("message", Message("user", "Keep the restored version").to_dict())
    try:
        for text in ["restored source", "later source"]:
            app.snapshots.write_text(tmp_path / "active.js", text, emit)
        written = [e for e in app.events(branch) if e.kind == "file.written"][-1]
        if archived:
            emit("context.compacted", {"archived_through": written.id, "index_as_of": written.id})
        before, _ = builder.build(app.events(branch), [])
        app.restore(branch, written.id)
        after, _ = builder.build(app.events(branch), [])
        assert "restored source" in after[0].content and "file.restored" in after[0].content
        assert after != before
        stable, _ = builder.build(app.events(branch), [])
        assert stable == after
        validate_messages(after)
    finally:
        app.close()


def test_file_and_history_share_character_cursor_and_explicit_eof(tmp_path):
    app = build_application(Settings(tmp_path))
    branch = app.fork()
    ctx = execution(app, branch)
    content = "中文🌟abc"
    (tmp_path / "unicode.txt").write_text(content, encoding="utf-8")
    event = app._emit(branch)("message", Message("user", content).to_dict())

    async def scenario():
        for name, args in [("file_read", {"path": "unicode.txt"}), ("history_read", {"event_id": event.id})]:
            first = await app.runner.registry.execute(name, {**args, "offset": 0, "limit": 3}, ctx)
            assert first.content.endswith("\n中文🌟")
            assert "characters=6" in first.content and "next_offset=3; eof=false" in first.content
            end = await app.runner.registry.execute(name, {**args, "offset": 3, "limit": 100}, ctx)
            assert "next_offset=null; eof=true" in end.content and end.content.endswith("\nabc")
            beyond = await app.runner.registry.execute(name, {**args, "offset": 900, "limit": 100}, ctx)
            assert "next_offset=null; eof=true" in beyond.content and beyond.content.endswith("\n")

    try:
        asyncio.run(scenario())
    finally:
        app.close()
