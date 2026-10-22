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


@dataclass(frozen=True)
class TaskPlan:
    interfaces: str
    tasks: tuple[PlannedTask, ...]

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
                )
                for t in value["tasks"]
            ),
        )
