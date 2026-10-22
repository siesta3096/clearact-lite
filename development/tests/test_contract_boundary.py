"""Dependency boundaries and persisted public envelope are architectural regression checks."""

import ast
from dataclasses import asdict
from pathlib import Path

from clearact_lite.core.contracts import SCHEMA_VERSION, Event, Message, ToolCall


def test_shared_core_does_not_import_adapters_or_runtime():
    root = Path(__file__).resolve().parents[2] / "src" / "clearact_lite" / "core"
    assert root.is_dir(), "Contract test must inspect the real core directory"
    forbidden = {
        "runtime",
        "providers",
        "storage",
        "tools",
        "application",
        "bootstrap",
        "cli",
        "gateway",
        "web",
        "integrations",
    }
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if name.startswith("clearact_lite."):
                    assert name.split(".")[1] not in forbidden, (path.name, name)


def test_message_round_trip_and_event_envelope():
    message = Message(
        "assistant", "text", (ToolCall("a", "file_read", {"path": "x"}),), reasoning_content="provider trace"
    )
    assert Message.from_dict(message.to_dict()) == message
    envelope = asdict(Event(1, "branch", "message", message.to_dict(), "2026-10-22T00:00:00Z"))
    assert set(envelope) == {"id", "branch_id", "kind", "payload", "created_at", "schema_version"}
    assert envelope["schema_version"] == SCHEMA_VERSION == 1
