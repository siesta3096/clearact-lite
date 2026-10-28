"""Lossless archive, bounded factual working index; no summarizer model calls."""

import json
from dataclasses import asdict

from clearact_lite.core.contracts import (
    BrowserDiagnostic,
    Event,
    Message,
    RecoveryState,
    TaskHandoff,
    ToolFact,
)
from clearact_lite.core.errors import ContextLimit


def tool_facts(events: list[Event]) -> list[dict]:
    """Bounded, verbatim successful receipts, balanced by tool/action; no inferred API facts."""
    calls, results, buckets = {}, {}, {}
    for event in events:
        if event.kind == "tool.returned":
            results[event.payload["call_id"]] = event
    for event in events:
        if event.kind != "message":
            continue
        message = Message.from_dict(event.payload)
        for call in message.tool_calls:
            calls[call.id] = (event, call)
        if message.role != "tool" or message.tool_call_id not in calls:
            continue
        receipt = results.get(message.tool_call_id)
        if receipt and (receipt.payload.get("is_error") or not receipt.payload.get("executed", True)):
            continue
        if not receipt and message.content.startswith(("[Tool error]", "Execution interrupted")):
            continue
        source, call = calls[message.tool_call_id]
        args, truncated = {}, len(message.content) > 240
        for key, value in call.arguments.items():
            if key in {"text", "old_text", "new_text"} and call.name in {"file_write", "file_edit"}:
                truncated = True
                continue
            encoded = json.dumps(value, ensure_ascii=False)
            if len(encoded) > 768:
                args[key] = {"prefix": encoded[:512], "suffix": encoded[-256:], "truncated": True}
                truncated = True
            else:
                args[key] = value
        fact = ToolFact(
            call.name,
            args,
            message.content[:240],
            source.id,
            receipt.id if receipt else event.id,
            event.branch_id,
            truncated,
        )
        key = (call.name, str(call.arguments.get("action", "")))
        buckets[key] = [*buckets.get(key, []), asdict(fact)][-2:]
    return sorted([fact for bucket in buckets.values() for fact in bucket], key=lambda fact: fact["result_event_id"])[
        -16:
    ]


def memory_index(events: list[Event], cutoff: int, retained_ids=()) -> str:
    changes = {}
    browser_errors = {}
    latest_tail = None
    for event in events:
        if event.kind == "browser.diagnostic":
            data = {**event.payload, "event_id": event.id}
            diagnostic = BrowserDiagnostic(**data)
            key = (diagnostic.session_id, diagnostic.document_id, diagnostic.kind, diagnostic.message, diagnostic.url)
            browser_errors[key] = diagnostic.context_view()
        if event.kind in {"file.written", "file.restored"}:
            changes[event.payload["path"]] = {
                "path": event.payload["path"],
                "sha256": event.payload.get("after_hash", event.payload.get("restored_hash")),
                "bytes": event.payload.get("bytes"),
                "event_id": event.id,
                "operation": event.kind,
            }
            latest_tail = {"path": event.payload["path"], "event_id": event.id, "text": event.payload.get("text_tail")}
    archived = [
        e
        for e in events
        if e.kind == "message"
        and e.id <= cutoff
        and e.id not in retained_ids
        and e.payload["role"] in {"assistant", "tool"}
    ]
    receipts = []
    for event in archived[-12:]:
        message = Message.from_dict(event.payload)
        if message.tool_calls:
            receipts.append(
                {
                    "event_id": event.id,
                    "note": message.content[:400],
                    "tools": [
                        {
                            "name": c.name,
                            **{
                                k: c.arguments[k]
                                for k in ("path", "task_id", "mode", "offset", "limit")
                                if k in c.arguments
                            },
                        }
                        for c in message.tool_calls
                    ],
                }
            )
        elif message.content:
            receipts.append({"event_id": event.id, "role": message.role, "excerpt": message.content[:240]})
    plans = [e for e in events if e.kind == "task.plan"]
    children = [e for e in events if e.kind == "agent.finished"]
    checkpoints = [e for e in events if e.kind == "task.checkpoint"]
    assignments = [e for e in events if e.kind == "task.assignment"]
    checkpoint = checkpoints[-1] if checkpoints else None
    recoveries = [e for e in events if e.kind == "reliability.updated"]
    handoffs = [e for e in events if e.kind == "task.handoff"]
    index = {
        "type": "archived_history_index_not_original_dialogue",
        "archived_through": cutoff,
        "archived_message_count": len(archived),
        "recent_archived_receipts": receipts,
        "recent_browser_errors_observed_not_current_health": list(browser_errors.values())[-4:],
        "successful_tool_facts": tool_facts(events),
        "recovery_state": RecoveryState.from_dict(recoveries[-1].payload).context_view()
        if recoveries
        else RecoveryState().context_view(),
        "recent_file_versions": list(changes.values())[-24:],
        "latest_file_tail": latest_tail,
        "assigned_increment": assignments[-1].payload if assignments else None,
        "plan": plans[-1].payload if plans else None,
        "child_results": [{"event_id": e.id, **e.payload} for e in children[-12:]],
        "recent_task_handoffs": [
            {"event_id": e.id, **TaskHandoff.from_dict(e.payload).context_view()} for e in handoffs[-3:]
        ],
        "working_checkpoint": {
            "event_id": checkpoint.id,
            "model_authored": checkpoint.payload,
            "newer_file_event_ids": [v["event_id"] for v in changes.values() if v["event_id"] > checkpoint.id],
        }
        if checkpoint
        else None,
    }
    heading = (
        "[Context archive index. Older assistant/tool transactions are archived, not deleted. "
        if cutoff
        else "[Working state index after explicit workspace restore. "
    )
    return (
        heading + "All original user requirements remain verbatim below. This factual index is not a semantic "
        "summary. Full text is available through history_read when needed. File versions and write positions "
        "are recorded facts; a model-authored checkpoint may predate the listed file events. "
        "This index does not add a task or request another review.]\n" + json.dumps(index, ensure_ascii=False)
    )


def transaction_groups(events: list[Event]) -> list[list[Event]]:
    groups, current, pending = [], [], set()
    for event in events:
        if event.kind != "message":
            continue
        message = Message.from_dict(event.payload)
        if message.role == "assistant":
            current = [event]
            pending.update(c.id for c in message.tool_calls)
            if not pending:
                groups.append(current)
        elif message.role == "tool":
            current.append(event)
            pending.discard(message.tool_call_id)
            if not pending:
                groups.append(current)
    return groups


def transaction_boundaries(events: list[Event]) -> list[int]:
    return [group[-1].id for group in transaction_groups(events)]


def prepare_context(builder, events, tools, emit, settings):
    messages, info = builder.build(events, tools, enforce_limit=False)
    if settings and settings.auto_compact and info["estimated_occupancy"] >= settings.compact_threshold:
        cutoff = info["archived_through"]
        groups = transaction_groups(events)
        protected = set(e.id for e in groups[-1]) if groups else set()
        writes = [e for e in events if e.kind in {"file.written", "file.restored"}]
        if writes and writes[-1].kind == "file.written":
            written = writes[-1]
            for group in groups:
                if group[0].id < written.id < group[-1].id:
                    protected.update(e.id for e in group)
                    break
        candidates = [group[-1].id for group in groups if group[-1].id > cutoff]
        chosen = None
        as_of = max((e.id for e in events), default=0)
        for point in candidates:
            draft = Event(
                0,
                "",
                "context.compacted",
                {"archived_through": point, "index_as_of": as_of, "retained_message_ids": sorted(protected)},
                "",
            )
            projected, after = builder.build([*events, draft], tools, enforce_limit=False)
            if after["unmeasured_byte_bound"] < info["unmeasured_byte_bound"] and (
                chosen is None or after["estimated_input_tokens"] < chosen[2]["estimated_input_tokens"]
            ):
                chosen = (point, projected, after)
            if chosen and after["estimated_occupancy"] <= settings.compact_target:
                break
        if chosen:
            point, projected, after = chosen
            checkpoint = emit(
                "context.compacted",
                {
                    "archived_through": point,
                    "index_as_of": as_of,
                    "retained_message_ids": sorted(protected),
                    "before_estimated_tokens": info["estimated_input_tokens"],
                    "after_estimated_tokens": after["estimated_input_tokens"],
                    "threshold": settings.compact_threshold,
                    "target": settings.compact_target,
                    "estimator": info["estimator"],
                    "after_estimator": after["estimator"],
                    "target_reached": after["estimated_occupancy"] <= settings.compact_target,
                    "working_set_preserved": True,
                    "method": "complete_transaction_archive_with_factual_index",
                    "pinned_user_event_ids": [
                        e.id for e in events if e.kind == "message" and e.payload["role"] == "user"
                    ],
                },
            )
            messages, info = projected, {**after, "checkpoint_event_id": checkpoint.id}
    if info["estimated_input_tokens"] > info["input_allowance"]:
        raise ContextLimit(
            "User requirements and working context do not fit after available compaction; history preserved"
        )
    return messages, info
