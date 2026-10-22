"""Lossless archive, bounded factual working index; no summarizer model calls."""

import json

from clearact_lite.core.contracts import Event, Message
from clearact_lite.core.errors import ContextLimit


def memory_index(events: list[Event], cutoff: int) -> str:
    changes = {}
    for event in events:
        if event.kind == "file.written":
            changes[event.payload["path"]] = {
                "path": event.payload["path"],
                "sha256": event.payload["after_hash"],
                "event_id": event.id,
            }
    archived = [
        e for e in events if e.kind == "message" and e.id <= cutoff and e.payload["role"] in {"assistant", "tool"}
    ]
    receipts = []
    for event in archived[-12:]:
        message = Message.from_dict(event.payload)
        if message.tool_calls:
            receipts.append(
                {
                    "event_id": event.id,
                    "tools": [
                        {"name": c.name, "path": c.arguments.get("path"), "task_id": c.arguments.get("task_id")}
                        for c in message.tool_calls
                    ],
                }
            )
        elif message.content:
            receipts.append({"event_id": event.id, "role": message.role, "excerpt": message.content[:240]})
    plans = [e for e in events if e.kind == "task.plan"]
    children = [e for e in events if e.kind == "agent.finished"]
    checkpoints = [e for e in events if e.kind == "task.checkpoint"]
    index = {
        "type": "archived_history_index_not_original_dialogue",
        "archived_through": cutoff,
        "archived_message_count": len(archived),
        "recent_archived_receipts": receipts,
        "recent_file_versions": list(changes.values())[-24:],
        "plan": plans[-1].payload if plans else None,
        "child_results": [{"event_id": e.id, **e.payload} for e in children[-12:]],
        "working_checkpoint": checkpoints[-1].payload if checkpoints else None,
    }
    return (
        "[Context archive index. All original user requirements remain verbatim below. "
        "Older assistant/tool transactions are archived, not deleted. This factual index is not a semantic "
        "summary; omitted decisions or details must be retrieved with history_read. File hashes are recorded "
        "versions, not guarantees about current external state. Read current files before editing.]\n"
        + json.dumps(index, ensure_ascii=False)
    )


def transaction_boundaries(events: list[Event]) -> list[int]:
    boundaries, pending = [], set()
    for event in events:
        if event.kind != "message":
            continue
        message = Message.from_dict(event.payload)
        if message.role == "assistant":
            pending.update(c.id for c in message.tool_calls)
            if not pending:
                boundaries.append(event.id)
        elif message.role == "tool":
            pending.discard(message.tool_call_id)
            if not pending:
                boundaries.append(event.id)
    return boundaries


def prepare_context(builder, events, tools, emit, settings):
    messages, info = builder.build(events, tools, enforce_limit=False)
    if settings and settings.auto_compact and info["estimated_occupancy"] >= settings.compact_threshold:
        cutoff = info["archived_through"]
        candidates = [point for point in transaction_boundaries(events) if point > cutoff]
        chosen = None
        for point in candidates:
            draft = Event(0, "", "context.compacted", {"archived_through": point}, "")
            projected, after = builder.build([*events, draft], tools, enforce_limit=False)
            if after["estimated_input_tokens"] < info["estimated_input_tokens"]:
                chosen = (point, projected, after)
            if chosen and after["estimated_occupancy"] <= settings.compact_target:
                break
        if chosen:
            point, projected, after = chosen
            checkpoint = emit(
                "context.compacted",
                {
                    "archived_through": point,
                    "before_estimated_tokens": info["estimated_input_tokens"],
                    "after_estimated_tokens": after["estimated_input_tokens"],
                    "threshold": settings.compact_threshold,
                    "target": settings.compact_target,
                    "estimator": info["estimator"],
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
