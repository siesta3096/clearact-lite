"""Small extension points shared by local adapters and future gateways."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from clearact_lite.core.contracts import BranchInfo, Event, Message, ModelReply, ToolResult, ToolSpec

type Emit = Callable[[str, dict[str, Any]], Event]
type Delta = Callable[[str], None]


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        on_delta: Delta,
    ) -> ModelReply: ...


class EventStore(Protocol):
    def append(self, branch_id: str, kind: str, payload: dict[str, Any]) -> Event: ...

    def events(self, branch_id: str) -> list[Event]: ...

    def fork(self, branch_id: str | None = None, at_event: int | None = None) -> str: ...

    def branches(self) -> list[BranchInfo]: ...

    def close(self) -> None: ...


class SnapshotStore(Protocol):
    def write_text(self, path: Path, text: str, emit: Emit) -> str: ...

    def write_bytes(self, path: Path, data: bytes, emit: Emit, *, text_preview: bool = False) -> str: ...

    def restore(self, change: dict[str, Any], emit: Emit) -> None: ...


class ToolContext(Protocol):
    workspace: Path
    branch_id: str
    store: EventStore
    snapshots: SnapshotStore
    emit: Emit


class Tool(Protocol):
    spec: ToolSpec

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...


class AsyncResource(Protocol):
    @property
    def is_open(self) -> bool: ...

    async def aclose(self) -> None: ...


class BrowserPort(AsyncResource, Protocol):
    async def state(self) -> dict: ...

    async def control(self, arguments: dict, context: ToolContext | None = None) -> dict: ...

    async def screenshot(self) -> bytes: ...


class MCPPort(AsyncResource, Protocol):
    def overview(self) -> list[dict]: ...

    async def connect(self, server: str) -> dict: ...

    async def list_tools(self, server: str, offset: int = 0, limit: int = 20) -> dict: ...

    async def call(self, server: str, tool: str, arguments: dict, context: ToolContext) -> tuple[str, bool]: ...

    async def disconnect(self, server: str) -> None: ...

    async def configure(self, servers: dict) -> None: ...


class DelegationPort(Protocol):
    def plan(self, arguments: dict, context: ToolContext) -> dict: ...

    async def execute(self, task_id: str, context: ToolContext) -> dict: ...
