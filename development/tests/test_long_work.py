import asyncio
import json

import pytest
from test_foundation import ScriptedProvider, reply

from clearact_lite.bootstrap import build_application
from clearact_lite.context.builder import ContextBuilder
from clearact_lite.context.compaction import prepare_context
from clearact_lite.core.contracts import Message, ModelReply, ToolCall, Usage, validate_messages
from clearact_lite.core.errors import ContextLimit
from clearact_lite.runtime.runner import ExecutionContext
from clearact_lite.settings import Settings


def execution(app, branch):
    return ExecutionContext(app.settings.workspace, branch, app.store, app.snapshots, app._emit(branch))


def test_90_percent_compaction_preserves_requirements_raw_calls_and_fork(tmp_path):
    app = build_application(Settings(tmp_path, context_tokens=16000, max_output_tokens=1000))
    try:
        branch = app.fork()
        emit = app._emit(branch)
        emit("message", Message("system", "system").to_dict())
        emit("message", Message("user", "Keep ORIGINAL-29, mobile layout and both endings").to_dict())
        emit(
            "message",
            Message(
                "assistant",
                tool_calls=(ToolCall("big", "file_write", {"path": "source.js", "text": "archived-source-" * 1000}),),
            ).to_dict(),
        )
        emit("message", Message("tool", "written", tool_call_id="big").to_dict())
        emit("message", Message("assistant", "Source completed; next integrate both endings").to_dict())
        emit("task.checkpoint", {"state": "source implemented", "next_steps": "Test both endings", "verified": False})
        original = app.events(branch)
        fork = app.fork(branch)
        messages, info = prepare_context(ContextBuilder(16000, 1000, 6000), original, [], emit, app.settings)
        assert info["estimated_occupancy"] < 0.9
        assert any(m.content == "Keep ORIGINAL-29, mobile layout and both endings" for m in messages)
        assert not any(m.tool_calls for m in messages)
        assert "Test both endings" in messages[0].content
        validate_messages(messages)
        assert messages[0].role == "system" and sum(m.role == "system" for m in messages) == 1
        assert app.events(fork) == original and app.events(branch)[: len(original)] == original
        archived = original[2]
        ctx = execution(app, branch)
        full = asyncio.run(app.runner.registry.execute("history_read", {"event_id": archived.id}, ctx))
        assert "archived-source-" in full.content and "tool_calls" in full.content
        rebuilt, _ = ContextBuilder(16000, 1000, 6000).build(app.events(branch), [])
        assert rebuilt == messages
    finally:
        app.close()


def test_compaction_does_not_remove_user_text_to_force_fit(tmp_path):
    app = build_application(Settings(tmp_path, context_tokens=4096, max_output_tokens=1024))
    try:
        branch = app.fork()
        app._emit(branch)("message", Message("user", "required " * 2000).to_dict())
        with pytest.raises(ContextLimit):
            prepare_context(ContextBuilder(4096, 1024, 6000), app.events(branch), [], app._emit(branch), app.settings)
        assert len(app.events(branch)[0].payload["content"]) == 18000
    finally:
        app.close()


def test_output_continuation_uses_complete_file_checkpoint_without_replaying_partial(tmp_path):
    provider = ScriptedProvider(
        [
            reply(calls=[ToolCall("first", "file_write", {"path": "long.txt", "text": "A"})]),
            ModelReply(
                Message(
                    "assistant",
                    tool_calls=(ToolCall("partial", "file_write", {"path": "bad.txt", "text": "MUST NOT BE WRITTEN"}),),
                ),
                "length",
                Usage(10, 2),
                {"truncated": True},
            ),
            reply(
                calls=[
                    ToolCall(
                        "append", "file_edit", {"path": "long.txt", "mode": "append", "expected_bytes": 1, "text": "B"}
                    )
                ]
            ),
            reply("done"),
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    try:
        result = asyncio.run(app.run("Write a long file"))
        assert result.status == "finished" and result.model_calls == 4 and result.usage == Usage(40, 8)
        assert (tmp_path / "long.txt").read_text() == "AB" and not (tmp_path / "bad.txt").exists()
        assert "Automatic capacity continuation" in provider.requests[2][-1].content
        assert any(e.kind == "capacity.continuation" for e in app.events(result.branch_id))
        assert any(
            e.kind == "model.returned" and e.payload["raw_response"] == {"truncated": True}
            for e in app.events(result.branch_id)
        )
    finally:
        app.close()


def test_chunk_offsets_prevent_duplicate_append_and_snapshot_restore(tmp_path):
    app = build_application(Settings(tmp_path))
    branch = app.fork()
    ctx = execution(app, branch)

    async def scenario():
        first = await app.runner.registry.execute(
            "file_edit", {"path": "x", "mode": "append", "text": "中", "expected_bytes": 0}, ctx
        )
        assert not first.is_error and json.loads(first.content)["bytes"] == 3
        duplicate = await app.runner.registry.execute(
            "file_edit", {"path": "x", "mode": "append", "text": "中", "expected_bytes": 0}, ctx
        )
        assert duplicate.is_error and (tmp_path / "x").read_text(encoding="utf-8") == "中"
        second = await app.runner.registry.execute(
            "file_edit", {"path": "x", "mode": "append", "text": "文", "expected_bytes": 3}, ctx
        )
        assert not second.is_error
        changed = [e for e in app.events(branch) if e.kind == "file.written"][-1]
        app.restore(branch, changed.id)
        assert (tmp_path / "x").read_text(encoding="utf-8") == "中"

    try:
        asyncio.run(scenario())
    finally:
        app.close()


PLAN = {
    "interfaces": "story.js exports window.STORY; engine.js reads it",
    "tasks": [
        {
            "id": "story",
            "instruction": "Write only story.js",
            "files": [{"path": "story.js", "purpose": "Narrative data"}],
            "depends_on": [],
        }
    ],
}


@pytest.mark.parametrize("explicit_tools", [False, True])
def test_child_history_is_independent_parent_usage_and_changes_are_visible(tmp_path, explicit_tools):
    plan = json.loads(json.dumps(PLAN))
    if explicit_tools:
        plan["tasks"][0]["tools"] = ["file_write", "history_read", "browser_read"]

    class Provider:
        calls = 0
        parent_step = 0
        child_step = 0
        requests = []

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            self.requests.append(messages)
            child = any("You are a delegated child worker" in m.content for m in messages if m.role == "user")
            if child:
                self.child_step += 1
                assert "ORIGINAL-29" in messages[-1].content or self.child_step > 1
                assert "PARENT-PRIVATE-SOURCE" not in json.dumps([m.to_dict() for m in messages])
                assert "agent_task" not in [t.name for t in tools]
                assert ("browser_read" in [t.name for t in tools]) == explicit_tools
                assert "project_files" in messages[1].content
                return (
                    reply(
                        calls=[
                            ToolCall("child-write", "file_write", {"path": "story.js", "text": "window.STORY = {};"})
                        ]
                    )
                    if self.child_step == 1
                    else reply("story ready")
                )
            self.parent_step += 1
            if self.parent_step == 1:
                return reply(
                    calls=[
                        ToolCall(
                            "parent-write", "file_write", {"path": "private.js", "text": "PARENT-PRIVATE-SOURCE" * 1000}
                        )
                    ]
                )
            if self.parent_step == 2:
                return reply(calls=[ToolCall("plan", "task_plan", plan)])
            if self.parent_step == 3:
                return reply(calls=[ToolCall("delegate", "agent_task", {"task_id": "story"})])
            return reply("parent integrated")

    provider = Provider()
    app = build_application(Settings(tmp_path, context_tokens=100096), provider)
    try:
        result = asyncio.run(app.run("ORIGINAL-29 needs a game"))
        assert result.status == "finished" and result.model_calls == 6 and result.usage == Usage(60, 12)
        assert len(app.branches()) == 1
        report = app.report(result.branch_id)
        child = report["children"][0]
        assert child["result"]["model_calls"] == 2 and report["child_changes"][0]["payload"]["path"] == "story.js"
        parent_messages = [e for e in report["events"] if e["kind"] == "message"]
        assert not any("window.STORY = {};" in json.dumps(e["payload"]) for e in parent_messages)
        ctx = execution(app, result.branch_id)
        index = asyncio.run(app.runner.registry.execute("history_read", {"branch_id": child["branch_id"]}, ctx))
        assert "message" in index.content
    finally:
        app.close()


def test_stop_propagates_from_child_and_usage_is_not_reported_as_zero(tmp_path):
    entered = asyncio.Event()

    class Provider:
        calls = 0

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            if self.calls == 1:
                return reply(calls=[ToolCall("plan", "task_plan", PLAN)])
            if self.calls == 2:
                return reply(calls=[ToolCall("child", "agent_task", {"task_id": "story"})])
            entered.set()
            await asyncio.Event().wait()

    app = build_application(Settings(tmp_path), Provider())

    async def scenario():
        running = asyncio.create_task(app.run("make a game"))
        await entered.wait()
        app.stop()
        result = await asyncio.wait_for(running, 2)
        assert result.status == "stopped" and result.model_calls == 3
        assert result.usage == Usage(None, None)
        assert app.report(result.branch_id)["children"][0]["result"]["status"] == "stopped"

    try:
        asyncio.run(scenario())
    finally:
        app.close()


def test_cumulative_usage_does_not_become_context_occupancy(tmp_path):
    provider = ScriptedProvider([reply("ok", usage=Usage(30000, 1)) for _ in range(5)])
    app = build_application(Settings(tmp_path, context_tokens=100096), provider)
    try:
        branch = None
        for _ in range(5):
            result = asyncio.run(app.run("Short request", branch))
            branch = result.branch_id
            assert result.status == "finished"
        assert sum(run["usage"]["input_tokens"] for run in app.report(branch)["runs"]) == 150000
        assert not any(e.kind == "context.compacted" for e in app.events(branch))
    finally:
        app.close()
