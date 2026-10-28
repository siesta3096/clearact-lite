"""Bounded failure recovery. No model calls, task classification or automatic action replay."""

import hashlib
import json
import time
from dataclasses import asdict, replace

from clearact_lite.core.contracts import RecoveryState, ToolResult
from clearact_lite.core.errors import RecoveryExhausted


def state_from_events(events):
    states = [e for e in events if e.kind == "reliability.updated"]
    return RecoveryState.from_dict(states[-1].payload) if states else RecoveryState()


class Recovery:
    def __init__(self, context, settings):
        self.context, self.settings = context, settings
        self.state = state_from_events(context.store.events(context.branch_id))

    def save(self, **values):
        self.state = replace(self.state, **values)
        self.context.emit("reliability.updated", asdict(self.state))

    def resume_by_user(self):
        if self.state.paused:
            self.context.emit("reliability.resumed", {"previous_state": asdict(self.state), "source": "user_turn"})
            # Only an explicit new user turn opens a fresh budget; failed action signatures remain.
            self.save(
                started_at=time.time(),
                blocked_rounds=0,
                output_tokens=0,
                output_estimated=False,
                paused=False,
                reason="",
                observations={},
            )
        elif not self.state.started_at and self.state.observations:
            self.save(observations={})

    def budget(self):
        s = self.state
        if not s.started_at:
            return
        reason = s.reason if s.paused else ""
        if s.blocked_rounds >= self.settings.recovery_blocked_rounds:
            reason = "No new evidence after the bounded diagnosis opportunities"
        if time.time() - s.started_at >= self.settings.recovery_timeout_seconds:
            reason = "Recovery time budget exhausted"
        if s.output_tokens >= self.settings.recovery_output_tokens:
            reason = "Recovery output budget exhausted"
        if reason:
            if not s.paused:
                self.save(paused=True, reason=reason)
            raise RecoveryExhausted(reason)

    def model_returned(self, reply):
        if self.state.started_at:
            reported = reply.usage.output_tokens
            bound = len(json.dumps(reply.message.to_dict(), ensure_ascii=False).encode("utf-8"))
            self.save(
                output_tokens=self.state.output_tokens + (reported if reported is not None else bound),
                output_estimated=self.state.output_estimated or reported is None,
            )

    def signature(self, call):
        return hashlib.sha256(
            json.dumps([call.name, call.arguments], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def version(self, call, *, include_browser=True):
        # Content hashes, never mtimes. External state is deliberately not claimed to be fully known.
        versions = {}
        events = self.context.store.events(self.context.branch_id)
        paths = {e.payload["path"] for e in events if e.kind in {"file.written", "file.restored"}}
        if isinstance(call.arguments.get("path"), str):
            paths.add(call.arguments["path"])
        for relative in sorted(paths):
            path = (self.context.workspace / relative).resolve()
            if not path.is_relative_to(self.context.workspace.resolve()) or ".clearact" in path.parts:
                continue
            try:
                versions[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            except OSError:
                versions[relative] = "unreadable"
        successful = {
            e.payload["call_id"]
            for e in events
            if e.kind == "tool.returned" and not e.payload.get("is_error") and e.payload.get("executed", True)
        }
        browser_changes = [
            e.id
            for e in events
            if e.kind == "tool.started"
            and e.payload["call_id"] in successful
            and (
                e.payload["name"] == "browser_open"
                or (
                    e.payload["name"] == "browser_action"
                    and e.payload["arguments"].get("action") not in {"tabs", "screenshot", "assert_text"}
                )
            )
        ]
        # Local file changes and explicit browser actions reopen a failed action, not its episode budget.
        if include_browser and call.name.startswith("browser_"):
            versions["browser_change_event"] = max(browser_changes, default=0)
        return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()

    def before(self, call):
        self.budget()
        failure = self.state.failures.get(self.signature(call))
        if not failure or failure["count"] < self.settings.repeated_failure_limit:
            return None
        if failure["kind"] not in {"validation", "deterministic"} or failure["version"] != self.version(call):
            return None
        self.save(blocked_rounds=self.state.blocked_rounds + 1)
        self.context.emit("reliability.blocked", {"call_id": call.id, "failure": failure, "executed": False})
        return ToolResult(
            "Not executed: this unchanged action failed repeatedly. Inspect the error and recorded successful "
            "interfaces, then change the method or relevant state. Another identical blocked choice consumes "
            "the remaining diagnosis budget. Last error: " + failure["error"],
            True,
            "blocked",
        )

    def after(self, call, result, result_event_id):
        signature = self.signature(call)
        if result.is_error:
            version = self.version(call)
            failures = dict(self.state.failures)
            old = failures.get(signature, {})
            error = result.content[:1200]
            count = old.get("count", 0) + 1 if old.get("error") == error and old.get("version") == version else 1
            failures[signature] = {
                "name": call.name,
                "result_event_id": result_event_id,
                "error": error,
                "arguments_event_id": next(
                    e.id
                    for e in reversed(self.context.store.events(self.context.branch_id))
                    if e.kind == "message" and any(c.get("id") == call.id for c in e.payload.get("tool_calls", []))
                ),
                "kind": result.error_kind,
                "version": version,
                "count": count,
            }
            # Preserve original action through its journal reference; do not copy large file bodies here.
            # A single ordinary failure does not put the rest of a long task on a repair deadline.
            started_at = self.state.started_at
            if count >= self.settings.repeated_failure_limit or call.name == "agent_task":
                started_at = started_at or time.time()
            self.save(
                started_at=started_at,
                started_event_id=self.state.started_event_id or (result_event_id if started_at else 0),
                failures=failures,
                blocked_rounds=0 if old and old.get("version") != version else self.state.blocked_rounds,
            )
            if count == self.settings.repeated_failure_limit:
                self.context.emit("reliability.diagnosing", {"failure": failures[signature]})
        elif signature in self.state.failures:
            failures = dict(self.state.failures)
            del failures[signature]
            self.save(failures=failures)
            if not failures:
                self.resolve("Previously failed action now succeeded", [result_event_id])
        if call.name == "task_checkpoint" and not result.is_error and call.arguments.get("resolved"):
            evidence = call.arguments.get("evidence_event_ids", [])
            events = self.context.store.events(self.context.branch_id)
            first_failure = min(
                (f["result_event_id"] for f in self.state.failures.values()),
                default=self.state.started_event_id or result_event_id,
            )
            stale_hashes = {
                item["result_hash"]
                for item in self.state.observations.values()
                if item["count"] >= self.settings.unchanged_result_limit
            }
            valid = [
                e.id
                for e in events
                if first_failure < e.id < result_event_id
                and e.id in evidence
                and e.kind == "tool.returned"
                and not e.payload.get("is_error")
                and e.payload.get("executed", True)
                and hashlib.sha256(e.payload.get("content", "").encode()).hexdigest() not in stale_hashes
            ]
            if valid and call.arguments.get("changed_method"):
                self.resolve("Model reported recovery with tool evidence; not independent acceptance", valid)
        if not result.is_error:
            return self.observe(call, result, result_event_id)
        return None

    def observe(self, call, result, event_id):
        # Only known reading tools and browser evaluation; do not classify opaque MCP side effects.
        browser_eval = call.name == "browser_action" and call.arguments.get("action") == "evaluate"
        if call.name == "browser_action" and call.arguments.get("action") == "wait":
            self.save(observations={k: v for k, v in self.state.observations.items() if k != "browser_evaluate"})
            return None
        if not browser_eval and call.name not in {
            "file_read",
            "history_read",
            "file_list",
            "document_read",
            "browser_read",
        }:
            return None
        # Pagination and distinct files have distinct keys. Browser code may vary while reporting the same evidence.
        key = "browser_evaluate" if browser_eval else self.signature(call)
        result_hash = hashlib.sha256(result.content.encode()).hexdigest()
        version = self.version(call, include_browser=False)
        previous = self.state.observations.get(key, {})
        same = previous.get("result_hash") == result_hash and previous.get("version") == version
        count = previous.get("count", 0) + 1 if same else 1
        observations = dict(self.state.observations)
        observations[key] = {
            "name": call.name,
            "result_hash": result_hash,
            "version": version,
            "count": count,
            "event_id": event_id,
        }
        # Bounded index; history remains complete. An active diagnosis budget is never reset by eviction.
        observations = dict(list(observations.items())[-32:])
        values = {"observations": observations}
        limit = self.settings.unchanged_result_limit
        if count >= limit:
            values.update(
                started_at=self.state.started_at or time.time(),
                started_event_id=self.state.started_event_id or event_id,
            )
            if count > limit:
                values["blocked_rounds"] = self.state.blocked_rounds + 1
        elif previous and not same:
            values["blocked_rounds"] = 0
        self.save(**values)
        if count == limit:
            self.context.emit(
                "reliability.diagnosing",
                {
                    "reason": "unchanged_successful_observation",
                    "result_event_id": event_id,
                    "count": count,
                    "not_a_failure_verdict": True,
                },
            )
        if count >= limit:
            return (
                "[Diagnosis: successful execution has returned the same evidence repeatedly, despite any argument "
                "variations. This is not proof that side effects failed. Change the hypothesis or inspect relevant "
                "state instead of repeating this check. Use task_checkpoint to record the problem, changed method "
                "and success criterion; cite NEW successful evidence to report resolution. Expected asynchronous "
                "waiting can use the explicit browser wait action. Unchanged checks share the same bounded "
                "diagnosis budget; two further unchanged observations will pause this run.]"
            )
        return None

    def resolve(self, reason, evidence):
        self.context.emit("reliability.resolved", {"reason": reason, "evidence_event_ids": evidence})
        self.state = RecoveryState()
        self.context.emit("reliability.updated", asdict(self.state))
