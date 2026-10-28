"""Versioned application DTOs. Provider wire formats stay inside adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from clearact_lite.core.errors import ProtocolError

SCHEMA_VERSION = 1
type Role = Literal["system", "user", "assistant", "tool"]
type RunStatus = Literal["finished", "stopped", "failed", "context_limit", "output_limit"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Message:
        data = dict(value)
        data["tool_calls"] = tuple(ToolCall(**call) for call in data.get("tool_calls", ()))
        return cls(**data)


def validate_messages(messages: list[Message]) -> None:
    pending: set[str] = set()
    used: set[str] = set()
    for message in messages:
        if message.role not in {"system", "user", "assistant", "tool"}:
            raise ProtocolError("Unknown message role")
        if message.role == "tool":
            if message.tool_calls or message.tool_call_id not in pending:
                raise ProtocolError("Tool result has no matching pending call")
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ProtocolError("Unresolved tool calls before the next message")
        if message.tool_call_id or (message.tool_calls and message.role != "assistant"):
            raise ProtocolError("Tool fields on an invalid role")
        for call in message.tool_calls:
            if not call.id or call.id in used or not call.name or not isinstance(call.arguments, dict):
                raise ProtocolError("Invalid or duplicate tool call")
            pending.add(call.id)
            used.add(call.id)
    if pending:
        raise ProtocolError("Unresolved tool calls")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class TextRange:
    """Character cursor shared by file and history reads; never a byte/line offset."""

    total_characters: int
    offset: int
    next_offset: int | None
    eof: bool
    text: str

    @classmethod
    def read(cls, source: str, offset: int, limit: int) -> TextRange:
        text = source[offset : offset + limit]
        end = min(offset + len(text), len(source))
        eof = end == len(source)
        return cls(len(source), offset, None if eof else end, eof, text)

    def to_tool_text(self) -> str:
        cursor = "null" if self.next_offset is None else str(self.next_offset)
        return (
            f"characters={self.total_characters}; offset={self.offset}; "
            f"next_offset={cursor}; eof={str(self.eof).lower()}\n{self.text}"
        )


@dataclass(frozen=True)
class RequestFootprint:
    """Identity of a measured request, without duplicating its source text."""

    model_key: str
    tools_hash: str
    message_hashes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict) -> RequestFootprint:
        return cls(value["model_key"], value["tools_hash"], tuple(value["message_hashes"]))


@dataclass(frozen=True)
class ModelReply:
    message: Message
    finish_reason: str
    usage: Usage = field(default_factory=Usage)
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    error_kind: str = "unknown"


@dataclass(frozen=True)
class BrowserDiagnostic:
    session_id: str
    sequence: int
    document_id: int
    kind: str
    message: str
    url: str = ""
    resource_type: str = ""
    http_status: int | None = None
    line_number: int | None = None
    column_number: int | None = None
    stack: str = ""
    event_id: int | None = None

    def context_view(self) -> dict[str, Any]:
        data = {key: value for key, value in asdict(self).items() if value not in (None, "")}
        data.pop("stack", None)
        data["message"] = self.message[:600]
        if self.stack or len(self.message) > 600:
            data["full_record_available"] = True
        return data


@dataclass(frozen=True)
class BrowserPageState:
    session_id: str = ""
    document_id: int = 0
    document_ready_state: str = "unknown"
    navigation_status: int | None = None
    errors: tuple[BrowserDiagnostic, ...] = ()
    earlier_errors_omitted: int = 0

    def context_view(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "document_id": self.document_id,
            "document_ready_state": self.document_ready_state,
            "navigation_status": self.navigation_status,
            "errors": [error.context_view() for error in self.errors],
            "earlier_errors_omitted": self.earlier_errors_omitted,
        }


@dataclass(frozen=True)
class RecoveryState:
    """Persisted technical failure episode, not a task acceptance verdict."""

    started_at: float = 0
    failures: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked_rounds: int = 0
    output_tokens: int = 0
    output_estimated: bool = False
    paused: bool = False
    reason: str = ""
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_event_id: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RecoveryState:
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})

    def context_view(self) -> dict[str, Any]:
        value = asdict(self)
        # Full counters stay in the journal; only repeated observations need model attention.
        value["observations"] = {k: v for k, v in self.observations.items() if v["count"] >= 2}
        return value


@dataclass(frozen=True)
class ToolFact:
    name: str
    arguments: dict[str, Any]
    result_excerpt: str
    call_event_id: int
    result_event_id: int
    branch_id: str
    truncated: bool = False


@dataclass(frozen=True)
class Event:
    id: int
    branch_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class BranchInfo:
    id: str
    parent_id: str | None
    title: str
    status: str
    updated_at: str | None


@dataclass(frozen=True)
class RunResult:
    branch_id: str
    status: RunStatus
    answer: str
    model_calls: int
    usage: Usage
    elapsed_seconds: float
    error: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class PlannedFile:
    path: str
    purpose: str


@dataclass(frozen=True)
class PlannedTask:
    id: str
    instruction: str
    files: tuple[PlannedFile, ...]
    depends_on: tuple[str, ...] = ()
    tools: tuple[str, ...] | None = None
    deliverable: str = ""


@dataclass(frozen=True)
class InterfaceContract:
    name: str
    provider_task: str
    signature: str
    consumers: tuple[str, ...] = ()
    invariants: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> InterfaceContract:
        return cls(
            value["name"],
            value["provider_task"],
            value["signature"],
            tuple(value.get("consumers", ())),
            value.get("invariants", ""),
        )


@dataclass(frozen=True)
class HandoffFile:
    path: str
    exists: bool
    bytes: int | None
    sha256: str | None
    before_sha256: str | None = None
    changed: bool | None = None


@dataclass(frozen=True)
class TaskHandoff:
    task_id: str
    branch_id: str
    report_event_id: int
    interfaces: tuple[InterfaceContract, ...] = ()
    interface_source: str = "none"
    unresolved: tuple[str, ...] = ()
    evidence_event_ids: tuple[int, ...] = ()
    checkpoint_event_id: int | None = None
    interface_checkpoint_event_id: int | None = None
    unresolved_checkpoint_event_id: int | None = None
    model_authored: bool = True
    verified: bool = False

    @classmethod
    def from_dict(cls, value: dict) -> TaskHandoff:
        return cls(
            **{**value, "interfaces": tuple(InterfaceContract.from_dict(c) for c in value.get("interfaces", ()))}
        )

    def context_view(self) -> dict:
        data = asdict(self)
        truncated = False
        data["unresolved"] = []
        budget = 850
        for note in self.unresolved:
            if budget <= 0:
                truncated = True
                break
            data["unresolved"].append(note[:budget])
            truncated |= len(note) > budget
            budget -= len(note)
        data["interfaces"] = []
        budget = 1300
        for interface in self.interfaces:
            item = asdict(interface)
            item["signature"] = item["signature"][:400]
            item["invariants"] = item["invariants"][:200]
            if len(str(item)) > budget:
                truncated = True
                break
            data["interfaces"].append(item)
            truncated |= item != asdict(interface)
            budget -= len(str(item))
        data["evidence_event_ids"] = list(self.evidence_event_ids[:12])
        data["context_excerpt"] = truncated or len(self.evidence_event_ids) > 12
        return data


@dataclass(frozen=True)
class TaskPlan:
    interfaces: str
    tasks: tuple[PlannedTask, ...]
    contracts: tuple[InterfaceContract, ...] = ()

    @classmethod
    def from_dict(cls, value: dict) -> TaskPlan:
        return cls(
            value["interfaces"],
            tuple(
                PlannedTask(
                    t["id"],
                    t["instruction"],
                    tuple(PlannedFile(**f) for f in t["files"]),
                    tuple(t.get("depends_on", [])),
                    tuple(t["tools"]) if t.get("tools") is not None else None,
                    t.get("deliverable", ""),
                )
                for t in value["tasks"]
            ),
            tuple(InterfaceContract.from_dict(item) for item in value.get("contracts", ())),
        )
