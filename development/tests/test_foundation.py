import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest

from clearact_lite.bootstrap import build_application
from clearact_lite.context.builder import ContextBuilder
from clearact_lite.core.contracts import Message, ModelReply, ToolCall, Usage, validate_messages
from clearact_lite.core.errors import ContextLimit, ProtocolError, WorkspaceConflict
from clearact_lite.providers.openai_compatible import OpenAICompatible
from clearact_lite.settings import Settings
from clearact_lite.storage.lease import workspace_lease
from clearact_lite.storage.sqlite import SQLiteStore


class ScriptedProvider:
    """A protocol test double, never a real-model evaluation."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def complete(self, messages, tools, on_delta):
        self.requests.append(messages)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        on_delta(reply.message.content)
        return reply


DEFAULT_USAGE = Usage(10, 2)


def reply(content="done", calls=(), usage=DEFAULT_USAGE, finish=None):
    return ModelReply(Message("assistant", content, tuple(calls)), finish or ("tool_calls" if calls else "stop"), usage)


def test_write_deliver_continue_and_branch_preserve_history(tmp_path):
    provider = ScriptedProvider(
        [
            reply("", [ToolCall("a", "file_write", {"path": "out.txt", "text": "first"})]),
            reply("", [ToolCall("b", "artifact_register", {"path": "out.txt"})]),
            reply(),
            reply("follow-up"),
            reply("separate branch"),
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    try:
        first = asyncio.run(app.run("write and deliver"))
        assert first.status == "finished"
        assert first.model_calls == 3 and first.usage == Usage(30, 6)
        assert (tmp_path / "out.txt").read_text() == "first"
        assert app.report(first.branch_id)["artifacts"][0]["bytes"] == 5
        initial = app.events(first.branch_id)
        fork = app.fork(first.branch_id)
        asyncio.run(app.run("continue", first.branch_id))
        assert app.events(fork) == initial
        asyncio.run(app.run("another direction", fork))
        assert "continue" not in [m.content for m in provider.requests[-1]]
        assert "write and deliver" in [m.content for m in provider.requests[-1]]
    finally:
        app.close()


def test_snapshot_restore_and_conflict_are_explicit(tmp_path):
    path = tmp_path / "out.txt"
    path.write_bytes(b"original\r\n")
    app = build_application(
        Settings(tmp_path),
        ScriptedProvider(
            [
                reply("", [ToolCall("a", "file_write", {"path": "out.txt", "text": "new"})]),
                reply(),
            ]
        ),
    )
    try:
        result = asyncio.run(app.run("change"))
        change = next(e for e in app.events(result.branch_id) if e.kind == "file.written")
        assert "original" in change.payload["diff"]
        path.write_text("manual edit")
        with pytest.raises(WorkspaceConflict):
            app.restore(result.branch_id, change.id)
        assert path.read_text() == "manual edit"
        path.write_text("new")
        app.restore(result.branch_id, change.id)
        assert path.read_bytes() == b"original\r\n"
        assert change in app.events(result.branch_id)
    finally:
        app.close()


def test_new_file_restore_and_unknown_usage(tmp_path):
    app = build_application(
        Settings(tmp_path),
        ScriptedProvider(
            [
                reply("", [ToolCall("a", "file_write", {"path": "new.txt", "text": "x"})], Usage()),
                reply(),
            ]
        ),
    )
    try:
        result = asyncio.run(app.run("write"))
        assert result.usage.input_tokens is None and result.usage.output_tokens is None
        change = next(e for e in app.events(result.branch_id) if e.kind == "file.written")
        app.restore(result.branch_id, change.id)
        assert not (tmp_path / "new.txt").exists()
    finally:
        app.close()


def test_projection_preserves_requirements_and_original_tool_result(tmp_path):
    store = SQLiteStore(tmp_path / "history.sqlite3")
    branch = store.fork()
    try:
        messages = [
            Message("system", "system rules"),
            Message("user", "must keep my requirements"),
            Message("assistant", tool_calls=(ToolCall("a", "read", {}),)),
            Message("tool", "X" * 10000, tool_call_id="a"),
        ]
        for message in messages:
            store.append(branch, "message", message.to_dict())
        projected, info = ContextBuilder(32768, 8192, 512).build(store.events(branch), [])
        assert projected[1] == messages[1]
        assert "history_read event_id=4" in projected[-1].content
        assert len(store.events(branch)[-1].payload["content"]) == 10000
        assert info["excerpted_events"] == [4]
        with pytest.raises(ContextLimit):
            ContextBuilder(200, 100, 512).build(store.events(branch), [])
    finally:
        store.close()


@pytest.mark.parametrize(
    "calls",
    [
        [ToolCall("same", "file_write", {}), ToolCall("same", "file_write", {})],
        [ToolCall("", "file_write", {})],
    ],
)
def test_invalid_call_transaction_never_executes(tmp_path, calls):
    app = build_application(Settings(tmp_path), ScriptedProvider([reply("", calls)]))
    try:
        result = asyncio.run(app.run("write"))
        assert result.status == "failed"
        assert not any(e.kind == "tool.started" for e in app.events(result.branch_id))
    finally:
        app.close()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "out.txt"},
        {"path": "out.txt", "text": 1},
        {"path": "../outside.txt", "text": "escape"},
        {"path": ".clearact/history.sqlite3", "text": "corrupt"},
    ],
)
def test_invalid_tool_arguments_and_workspace_boundaries(tmp_path, arguments):
    app = build_application(
        Settings(tmp_path),
        ScriptedProvider(
            [
                reply("", [ToolCall("a", "file_write", arguments)]),
                reply("reported error"),
            ]
        ),
    )
    try:
        result = asyncio.run(app.run("write"))
        results = [e for e in app.events(result.branch_id) if e.kind == "tool.returned"]
        assert results[0].payload["is_error"] is True
        assert not (tmp_path / "out.txt").exists()
    finally:
        app.close()


def test_output_truncation_never_executes_partial_calls(tmp_path):
    app = build_application(
        Settings(tmp_path, auto_continue=False),
        ScriptedProvider(
            [
                reply("partial", [ToolCall("a", "file_write", {"path": "x", "text": "x"})], finish="length"),
            ]
        ),
    )
    try:
        result = asyncio.run(app.run("write"))
        assert result.status == "output_limit"
        assert not (tmp_path / "x").exists()
        assert result.model_calls == 1
    finally:
        app.close()


def test_timeout_is_not_automatically_retried(tmp_path):
    class WaitingProvider:
        calls = 0

        async def complete(self, messages, tools, on_delta):
            self.calls += 1
            on_delta("partial output")
            await asyncio.Event().wait()

    provider = WaitingProvider()
    app = build_application(replace(Settings(tmp_path), run_timeout_seconds=0.02), provider)
    try:
        result = asyncio.run(app.run("wait"))
        assert result.status == "failed" and provider.calls == 1
        assert result.usage.input_tokens is None
        assert any(e.kind == "model.partial" for e in app.events(result.branch_id))
    finally:
        app.close()


def test_stop_closes_pending_transaction_without_replay(tmp_path):
    entered = asyncio.Event()

    class WaitingTool:
        from clearact_lite.core.contracts import ToolSpec

        spec = ToolSpec("wait", "wait", {"type": "object"})

        async def execute(self, arguments, context):
            entered.set()
            await asyncio.Event().wait()

    provider = ScriptedProvider(
        [
            reply("", [ToolCall("a", "wait", {}), ToolCall("b", "file_write", {"path": "x", "text": "x"})]),
            reply("next user turn"),
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    app.runner.registry.tools["wait"] = WaitingTool()

    async def scenario():
        task = asyncio.create_task(app.run("wait"))
        await entered.wait()
        assert app.stop()
        first = await task
        assert first.status == "stopped"
        ledger = [Message.from_dict(e.payload) for e in app.events(first.branch_id) if e.kind == "message"]
        validate_messages(ledger)
        assert len([m for m in ledger if m.role == "tool"]) == 2
        second = await app.run("continue", first.branch_id)
        assert second.status == "finished"
        assert not (tmp_path / "x").exists()

    try:
        asyncio.run(scenario())
    finally:
        app.close()


def test_fork_refuses_incomplete_tool_transaction(tmp_path):
    store = SQLiteStore(tmp_path / "history.sqlite3")
    try:
        branch = store.fork()
        store.append(branch, "message", Message("user", "test").to_dict())
        event = store.append(branch, "message", Message("assistant", tool_calls=(ToolCall("a", "x", {}),)).to_dict())
        with pytest.raises(ProtocolError):
            store.fork(branch, event.id)
        clean = store.fork(branch, 1)
        assert len(store.events(clean)) == 1
    finally:
        store.close()


def test_observer_failure_does_not_repeat_tools(tmp_path):
    app = build_application(Settings(tmp_path), ScriptedProvider([reply()]))

    def broken(event):
        raise ValueError("broken UI")

    try:
        result = asyncio.run(app.run("hello", on_event=broken))
        assert result.status == "finished" and result.model_calls == 1
        assert any(e.kind == "observer.failed" for e in app.events(result.branch_id))
    finally:
        app.close()


def test_writer_lock_prevents_two_writers(tmp_path):
    with workspace_lease(tmp_path):
        with pytest.raises(OSError), workspace_lease(tmp_path):
            pytest.fail("Second writer acquired the lease")
    with workspace_lease(tmp_path):
        pass


def test_future_database_version_requires_migration(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = SQLiteStore(path)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE metadata SET version=99")
    with pytest.raises(ProtocolError):
        SQLiteStore(path)


def test_stream_adapter_assembles_tool_arguments_and_reports_usage(tmp_path):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "hello",
                            "tool_calls": [
                                {"index": 0, "id": "a", "function": {"name": "file_read", "arguments": '{"path":'}}
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 3}},
        ]
        content = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatible(Settings(tmp_path), httpx.MockTransport(handler))
    deltas = []
    result = asyncio.run(provider.complete([Message("user", "read")], [], deltas.append))
    assert result.message.tool_calls == (ToolCall("a", "file_read", {"path": "x"}),)
    assert result.usage == Usage(17, 3) and deltas == ["hello"]


def test_stream_without_finish_reason_fails(tmp_path):
    provider = OpenAICompatible(
        Settings(tmp_path),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"unfinished"}}]}\n\n',
            )
        ),
    )
    with pytest.raises(ProtocolError):
        asyncio.run(provider.complete([Message("user", "hello")], [], lambda text: None))


def test_malformed_stream_frame_is_preserved_in_failure(tmp_path):
    provider = OpenAICompatible(
        Settings(tmp_path),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="data: {not-json}\n\n",
            )
        ),
    )
    app = build_application(Settings(tmp_path), provider)
    try:
        result = asyncio.run(app.run("hello"))
        assert result.status == "failed" and result.model_calls == 1
        failure = next(e for e in app.events(result.branch_id) if e.kind == "model.invalid_response")
        assert failure.payload["raw_response"]["chunks"][0]["invalid_data"] == "{not-json}"
    finally:
        app.close()


def test_unsupported_finish_reason_never_executes_tools(tmp_path):
    app = build_application(
        Settings(tmp_path),
        ScriptedProvider(
            [
                reply("", [ToolCall("a", "file_write", {"path": "x", "text": "x"})], finish="content_filter"),
            ]
        ),
    )
    try:
        result = asyncio.run(app.run("write"))
        assert result.status == "failed" and not (tmp_path / "x").exists()
    finally:
        app.close()
