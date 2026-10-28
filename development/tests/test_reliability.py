"""Failure recovery regressions use protocol doubles, never claimed as real-model acceptance."""

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from test_foundation import ScriptedProvider, reply
from test_long_work import PLAN

from clearact_lite.bootstrap import build_application
from clearact_lite.context.compaction import memory_index
from clearact_lite.core.contracts import Message, RecoveryState, ToolCall, ToolResult, ToolSpec, validate_messages
from clearact_lite.runtime.reliability import state_from_events
from clearact_lite.settings import Settings


class Action:
    spec = ToolSpec("action", "protocol regression action", {"type": "object"})

    def __init__(self, error=None):
        self.calls = []
        self.error = error or TypeError("missing method")

    async def execute(self, arguments, context):
        self.calls.append(arguments)
        if arguments.get("method") != "correct":
            raise self.error
        return ToolResult("actual successful result")


def failed_call(index, name="action", arguments=None):
    return reply(calls=[ToolCall(f"fail-{index}", name, arguments or {"method": "missing"})])


def test_repeated_failure_stops_tools_and_model_decisions_and_closes_batch(tmp_path):
    provider = ScriptedProvider(
        [failed_call(i) for i in range(3)]
        + [
            reply(
                calls=[
                    ToolCall("four", "action", {"method": "missing"}),
                    ToolCall("skip", "file_write", {"path": "never.txt", "text": "never"}),
                ]
            ),
            reply("MUST NOT be requested"),
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    action = Action()
    app.runner.registry.tools["action"] = action
    try:
        result = asyncio.run(app.run("complete work"))
        assert result.status == "stopped" and result.error.startswith("Recovery paused:")
        assert len(action.calls) == 2 and result.model_calls == 4
        assert not (tmp_path / "never.txt").exists()
        events = app.events(result.branch_id)
        validate_messages([Message.from_dict(e.payload) for e in events if e.kind == "message"])
        assert len([e for e in events if e.kind == "reliability.blocked"]) == 2
        assert any(e.kind == "task.checkpoint" and e.payload["source"] == "runtime" for e in events)
        skipped = next(e for e in events if e.kind == "tool.returned" and e.payload["call_id"] == "skip")
        assert skipped.payload["executed"] is False
        # Explicit user resumption opens diagnosis budget, but cannot erase the unchanged failures.
        app.runner.provider = ScriptedProvider([failed_call(10), failed_call(11)])
        second = asyncio.run(app.run("continue diagnosing", result.branch_id))
        assert second.status == "stopped" and len(action.calls) == 2 and second.model_calls == 2
        assert any(e.kind == "reliability.resumed" for e in app.events(result.branch_id))
    finally:
        app.close()


def test_new_method_and_evidence_checkpoint_can_recover_without_forced_review(tmp_path):
    class Provider:
        step = 0

        async def complete(self, messages, tools, on_delta):
            self.step += 1
            if self.step <= 2:
                return failed_call(self.step)
            if self.step == 3:
                return reply(calls=[ToolCall("fixed", "action", {"method": "correct"})])
            if self.step == 4:
                receipt_id = int(messages[-1].content.split("event_id=")[1].split(";")[0])
                return reply(
                    calls=[
                        ToolCall(
                            "checkpoint",
                            "task_checkpoint",
                            {
                                "state": "resolved",
                                "next_steps": "deliver",
                                "changed_method": "Use actual existing method",
                                "success_criterion": "method returns result",
                                "resolved": True,
                                "evidence_event_ids": [receipt_id],
                            },
                        )
                    ]
                )
            return reply("Completed using the changed method")

    app = build_application(Settings(tmp_path), Provider())
    action = Action()
    app.runner.registry.tools["action"] = action
    try:
        result = asyncio.run(app.run("work"))
        assert result.status == "finished" and len(action.calls) == 3 and result.model_calls == 5
        assert not state_from_events(app.events(result.branch_id)).started_at
        assert any(e.kind == "reliability.resolved" for e in app.events(result.branch_id))
    finally:
        app.close()


def test_successful_pagination_and_bounded_transient_retries_are_not_blocked(tmp_path):
    (tmp_path / "data.txt").write_text("abcdefgh", encoding="utf-8")
    provider = ScriptedProvider(
        [failed_call(i) for i in range(3)]
        + [failed_call(i + 3, "file_read", {"path": "data.txt", "offset": i * 2, "limit": 2}) for i in range(4)]
        + [reply("Read all pages; network still unavailable")]
    )
    app = build_application(Settings(tmp_path), provider)
    action = Action(TimeoutError("temporarily unavailable"))
    app.runner.registry.tools["action"] = action
    try:
        result = asyncio.run(app.run("read"))
        assert result.status == "finished" and len(action.calls) == 3
        assert not any(e.kind == "reliability.blocked" for e in app.events(result.branch_id))
    finally:
        app.close()


@pytest.mark.parametrize("budget", ["time", "output"])
def test_recovery_budget_survives_compaction_and_output_continuation(tmp_path, budget):
    provider = ScriptedProvider([failed_call(1), failed_call(2), reply("partial", finish="length"), reply("never")])
    settings = replace(Settings(tmp_path), recovery_output_tokens=1 if budget == "output" else 32768)
    app = build_application(settings, provider)
    app.runner.registry.tools["action"] = Action()
    try:
        branch = app.fork()
        if budget == "time":
            app._emit(branch)("reliability.updated", asdict(RecoveryState(started_at=1)))
        app._emit(branch)("context.compacted", {"archived_through": 0, "index_as_of": 0})
        result = asyncio.run(app.run("work", branch))
        assert result.status == "stopped"
        assert result.model_calls == (0 if budget == "time" else 3)
        if budget == "output":
            assert any(e.kind == "capacity.continuation" for e in app.events(branch))
        state = state_from_events(app.events(branch))
        assert state.paused
        assert "recovery_state" in memory_index(app.events(branch), 0)
    finally:
        app.close()


def test_archiving_keeps_successful_browser_value_past_many_file_reads(tmp_path):
    app = build_application(Settings(tmp_path))
    try:
        branch = app.fork()
        emit = app._emit(branch)
        for index in range(40):
            call = ToolCall(
                str(index),
                "browser_action" if index == 0 else "file_read",
                {"action": "evaluate", "value": "Game.AI.getEntities().length"}
                if index == 0
                else {"path": "a.js", "offset": index * 200, "limit": 200},
            )
            emit("message", Message("assistant", tool_calls=(call,)).to_dict())
            emit("tool.returned", {"call_id": call.id, "content": "3", "is_error": False})
            emit("message", Message("tool", "3", tool_call_id=call.id).to_dict())
        index = memory_index(app.events(branch), max(e.id for e in app.events(branch)))
        assert "Game.AI.getEntities().length" in index and '"action": "evaluate"' in index
        assert len(index) < 12000
    finally:
        app.close()


def test_child_recovery_pause_hands_unfinished_work_back_to_parent(tmp_path):
    class Provider:
        child_calls = 0
        parent_calls = 0

        async def complete(self, messages, tools, on_delta):
            if any("You are a delegated child worker" in m.content for m in messages):
                self.child_calls += 1
                return failed_call(self.child_calls, "file_read", {"path": "missing.txt"})
            self.parent_calls += 1
            if self.parent_calls == 1:
                return reply(calls=[ToolCall("plan", "task_plan", PLAN)])
            if self.parent_calls == 2:
                return reply(calls=[ToolCall("child", "agent_task", {"task_id": "story"})])
            data = json.loads(messages[-1].content.split("\n[Tool receipt:")[0].removeprefix("[Tool error]\n"))
            assert data["result"]["status"] == "stopped" and data["working_state"]["recovery"]["paused"]
            return reply("Child could not finish; parent has the failure and chooses next steps")

    app = build_application(Settings(tmp_path), Provider())
    try:
        result = asyncio.run(app.run("work"))
        assert result.status == "finished" and result.model_calls == 7
        assert app.report(result.branch_id)["children"][0]["result"]["status"] == "stopped"
    finally:
        app.close()


def test_changed_browser_state_reopens_failed_action_without_resetting_episode(tmp_path):
    class BrowserAction:
        spec = ToolSpec("browser_action", "browser state regression", {"type": "object"})
        actual_calls = 0
        ready = False

        async def execute(self, arguments, context):
            self.actual_calls += 1
            if arguments["value"] == "prepare":
                self.ready = True
                return ToolResult("state changed")
            if not self.ready:
                raise TypeError("method not yet present")
            return ToolResult("actual success")

    args = {"action": "evaluate", "value": "method()"}
    provider = ScriptedProvider(
        [
            failed_call(1, "browser_action", args),
            failed_call(2, "browser_action", args),
            failed_call(3, "browser_action", args),
            failed_call(4, "browser_action", {"action": "evaluate", "value": "prepare"}),
            failed_call(5, "browser_action", args),
            reply("done"),
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    action = BrowserAction()
    app.runner.registry.tools["browser_action"] = action
    try:
        result = asyncio.run(app.run("work"))
        assert result.status == "finished" and action.actual_calls == 4
        assert len([e for e in app.events(result.branch_id) if e.kind == "reliability.blocked"]) == 1
        assert not state_from_events(app.events(result.branch_id)).started_at
    finally:
        app.close()


def test_single_ordinary_missing_file_does_not_start_long_task_repair_timer(tmp_path):
    provider = ScriptedProvider(
        [
            failed_call(1, "file_read", {"path": "new.txt"}),
            failed_call(2, "file_write", {"path": "new.txt", "text": "new"}),
            reply("done"),
        ]
    )
    app = build_application(Settings(tmp_path, recovery_timeout_seconds=0.001), provider)
    try:
        result = asyncio.run(app.run("create new file"))
        assert result.status == "finished" and (tmp_path / "new.txt").exists()
        assert not state_from_events(app.events(result.branch_id)).started_at
    finally:
        app.close()


def test_successful_checks_with_varying_arguments_cannot_loop_on_same_evidence(tmp_path):
    class Observe:
        spec = ToolSpec("browser_action", "observation", {"type": "object"})
        calls = 0

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult('{"pending":true,"count":0}')

    provider = ScriptedProvider(
        [
            reply(calls=[ToolCall(str(i), "browser_action", {"action": "evaluate", "value": f"probe({i / 1000})"})])
            for i in range(10)
        ]
    )
    app = build_application(Settings(tmp_path), provider)
    observe = Observe()
    app.runner.registry.tools["browser_action"] = observe
    try:
        result = asyncio.run(app.run("check result"))
        assert result.status == "stopped" and observe.calls == result.model_calls == 6
        assert "Diagnosis:" in provider.requests[4][-1].content
        events = app.events(result.branch_id)
        assert any(e.kind == "reliability.diagnosing" for e in events)
        validate_messages([Message.from_dict(e.payload) for e in events if e.kind == "message"])
        assert state_from_events(events).paused
    finally:
        app.close()


def test_explicit_waiting_and_distinct_file_reads_are_not_unchanged_check_loops(tmp_path):
    class Observe:
        spec = ToolSpec("browser_action", "observation", {"type": "object"})

        async def execute(self, arguments, context):
            return ToolResult("pending" if arguments["action"] == "evaluate" else "waited")

    responses = []
    for i in range(7):
        (tmp_path / f"{i}.txt").write_text("same content", "utf-8")
        responses.extend(
            [
                reply(calls=[ToolCall(f"a{i}", "browser_action", {"action": "evaluate", "value": "poll()"})]),
                reply(calls=[ToolCall(f"w{i}", "browser_action", {"action": "wait", "milliseconds": 10})]),
                reply(calls=[ToolCall(f"f{i}", "file_read", {"path": f"{i}.txt"})]),
            ]
        )
    app = build_application(Settings(tmp_path), ScriptedProvider([*responses, reply("done")]))
    app.runner.registry.tools["browser_action"] = Observe()
    try:
        result = asyncio.run(app.run("wait for async results and read different files"))
        assert result.status == "finished" and result.model_calls == 22
        assert not state_from_events(app.events(result.branch_id)).started_at
    finally:
        app.close()


def test_real_failed_game_observations_replay_stop_within_budget(tmp_path):
    from pathlib import Path

    report_file = Path(__file__).parents[1] / (
        "records/evals/reliability-repair-20261025-153523/fps-retry-2026-10-27/report.json"
    )
    if not report_file.exists():
        pytest.skip("Local real failure replay fixture is intentionally not uploaded")
    events = json.loads(report_file.read_text("utf-8"))["events"]
    sources = [
        e
        for e in events
        if e["kind"] == "tool.started" and "position.set(0,1.7,-19.9999" in str(e["payload"].get("arguments"))
    ]
    results = {e["payload"]["call_id"]: e["payload"] for e in events if e["kind"] == "tool.returned"}
    unchanged = [e for e in sources if '"ammo\\":0' in results.get(e["payload"]["call_id"], {}).get("content", "")]
    assert len(unchanged) >= 6

    class Replay:
        spec = ToolSpec("browser_action", "recorded real results; not a real model", {"type": "object"})

        async def execute(self, arguments, context):
            source = next(e for e in unchanged if e["payload"]["arguments"] == arguments)
            payload = results[source["payload"]["call_id"]]
            return ToolResult(payload["content"], payload["is_error"])

    provider = ScriptedProvider(
        [reply(calls=[ToolCall(str(i), "browser_action", e["payload"]["arguments"])]) for i, e in enumerate(unchanged)]
    )
    app = build_application(Settings(tmp_path), provider)
    app.runner.registry.tools["browser_action"] = Replay()
    try:
        result = asyncio.run(app.run("continue check"))
        assert result.status == "stopped" and result.model_calls == 6
    finally:
        app.close()
