"""Replay the frozen first-archive failure with reported usage; never call a model."""

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from clearact_lite.context.budget import fingerprint
from clearact_lite.context.builder import ContextBuilder
from clearact_lite.context.compaction import prepare_context
from clearact_lite.core.contracts import Event, Message, validate_messages
from clearact_lite.settings import Settings
from clearact_lite.tools.documents import ReadDocument
from clearact_lite.tools.files import EditFile, ListFiles, ReadFile, WriteFile
from clearact_lite.tools.history import ReadHistory
from clearact_lite.tools.tasks import SaveCheckpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Frozen game-load record directory")
    args = parser.parse_args()
    output = (
        Path(__file__).resolve().parents[1] / "records/evals" / datetime.now().strftime("repair-replay-%Y%m%d-%H%M%S")
    )
    output.mkdir(parents=True)
    raw = json.loads(next(args.source.glob("child-*.json")).read_text(encoding="utf-8"))
    original = [Event(**e) for e in raw["events"]]
    archive = next(e for e in original if e.kind == "context.compacted")
    settings = Settings(output / "unused", context_tokens=100096, max_output_tokens=16384)
    builder = ContextBuilder(100096, 16384, 6000, settings.tokenizer_key)
    tools = [
        t.spec
        for t in [ReadFile(), WriteFile(), EditFile(), SaveCheckpoint(), ReadHistory(), ListFiles(), ReadDocument()]
    ]
    replayed, projection = [], None
    for index, event in enumerate(original):
        if event.id >= archive.id:
            break
        if event.kind == "context.projected":
            _, info = builder.build(replayed, tools, enforce_limit=False)
            event = replace(event, payload=info)
            projection = event.id
        elif event.kind == "model.returned":
            response = next(
                e for e in original[index + 1 :] if e.kind == "message" and e.payload["role"] == "assistant"
            )
            event = replace(
                event,
                payload={
                    **event.payload,
                    "context_event_id": projection,
                    "response_message_hash": fingerprint(Message.from_dict(response.payload).to_dict()),
                },
            )
        replayed.append(event)
    emitted = []

    def emit(kind, payload):
        event = Event(replayed[-1].id + len(emitted) + 1, "replay", kind, payload, "")
        emitted.append(event)
        return event

    result = {"provider": "offline replay of recorded real-model usage, no new inference", "passed": False}
    try:
        messages, info = prepare_context(builder, replayed, tools, emit, settings)
        writes = [c for m in messages for c in m.tool_calls if c.name in {"file_write", "file_edit"}]
        validate_messages(messages)
        assert not emitted, "The frozen half-full request must not be archived"
        assert len(writes) == 3 and 0.49 < info["estimated_occupancy"] < 0.6
        result.update(
            passed=True, original_archive=archive.payload, repaired_projection=info, retained_write_calls=len(writes)
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    (output / "replay.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPLAY", result["passed"], output, result.get("repaired_projection", {}).get("estimated_input_tokens"))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
