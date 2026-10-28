"""Stable entry point for CLI, future UI, and external integrations."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict

from clearact_lite.context.builder import ContextBuilder
from clearact_lite.core.contracts import BranchInfo, Event, RunResult
from clearact_lite.core.ports import BrowserPort, Delta, EventStore, MCPPort, ModelProvider, SnapshotStore
from clearact_lite.runtime.runner import ExecutionContext, Runner
from clearact_lite.settings import Settings
from clearact_lite.storage.lease import workspace_lease
from clearact_lite.storage.snapshots import digest, workspace_path
from clearact_lite.storage.uploads import UploadStore
from clearact_lite.tools.registry import ToolRegistry


class Application:
    def __init__(
        self,
        settings: Settings,
        provider: ModelProvider,
        store: EventStore,
        snapshots: SnapshotStore,
        tools: ToolRegistry,
        browser: BrowserPort | None = None,
        mcp: MCPPort | None = None,
    ):
        self.settings, self.store, self.snapshots = settings, store, snapshots
        self.runner = Runner(
            provider,
            tools,
            ContextBuilder(
                settings.context_tokens,
                settings.max_output_tokens,
                settings.tool_result_chars,
                settings.tokenizer_key,
            ),
            settings.run_timeout_seconds,
            settings,
        )
        self._task: asyncio.Task | None = None
        self._browser, self._mcp, self._tools = browser, mcp, tools
        self.uploads = UploadStore(settings.workspace)

    def _emit(self, branch_id: str, listener: Callable[[Event], None] | None = None):
        def emit(kind: str, payload: dict) -> Event:
            event = self.store.append(branch_id, kind, payload)
            if listener:
                try:
                    listener(event)
                except Exception as exc:
                    self.store.append(branch_id, "observer.failed", {"type": type(exc).__name__})
            return event

        return emit

    async def run(
        self,
        prompt: str,
        branch_id: str | None = None,
        on_event: Callable[[Event], None] | None = None,
        on_delta: Delta | None = None,
        attachment_ids: list[str] | None = None,
    ) -> RunResult:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        if self._task is not None:
            raise RuntimeError("This application already has an active run")
        attachments = [self.uploads.get(upload_id) for upload_id in attachment_ids or []]
        with workspace_lease(self.settings.workspace / ".clearact"):
            branch_id = branch_id or self.store.fork()
            context = ExecutionContext(
                self.settings.workspace.resolve(),
                branch_id,
                self.store,
                self.snapshots,
                self._emit(branch_id, on_event),
            )
            context.emit(
                "run.configuration",
                {
                    "model": self.settings.model,
                    "base_url": self.settings.base_url,
                    "context_tokens": self.settings.context_tokens,
                    "max_output_tokens": self.settings.max_output_tokens,
                    "temperature": self.settings.temperature,
                    "reasoning_effort": self.settings.reasoning_effort,
                    "request_timeout_seconds": self.settings.request_timeout_seconds,
                    "run_timeout_seconds": self.settings.run_timeout_seconds,
                    "auto_compact": self.settings.auto_compact,
                    "compact_threshold": self.settings.compact_threshold,
                    "compact_target": self.settings.compact_target,
                    "auto_continue": self.settings.auto_continue,
                },
            )
            if attachments:
                context.emit("attachment.linked", {"files": attachments, "prompt": prompt})
                references = [{k: f[k] for k in ("name", "path", "bytes", "mime_type")} for f in attachments]
                prompt += "\n\n[Attached workspace files; read as needed using file_read/document_read]\n"
                prompt += json.dumps(references, ensure_ascii=False)
            self._task = asyncio.create_task(self.runner.run(prompt, context, on_delta))
            try:
                return await self._task
            finally:
                self._task = None

    def stop(self) -> bool:
        if self._task is None:
            return False
        self._task.cancel()
        return True

    def events(self, branch_id: str) -> list[Event]:
        return self.store.events(branch_id)

    def fork(self, branch_id: str | None = None, at_event: int | None = None) -> str:
        with workspace_lease(self.settings.workspace / ".clearact"):
            return self.store.fork(branch_id, at_event)

    def branches(self) -> list[BranchInfo]:
        return [
            branch
            for branch in self.store.branches()
            if not any(e.kind == "agent.identity" for e in self.events(branch.id))
        ]

    def artifact(self, branch_id: str, event_id: int) -> tuple[str, bytes]:
        event = next((e for e in self.events(branch_id) if e.id == event_id), None)
        if event is None or event.kind != "artifact.registered":
            raise ValueError("File is not registered for delivery in this branch")
        path = workspace_path(self.settings.workspace, event.payload["path"])
        content = path.read_bytes()
        if digest(content) != event.payload["sha256"]:
            raise ValueError("File changed after registration; inspect or register its current version")
        return path.name, content

    def restore(self, branch_id: str, change_event: int) -> None:
        with workspace_lease(self.settings.workspace / ".clearact"):
            event = next((e for e in self.events(branch_id) if e.id == change_event), None)
            if event is None or event.kind != "file.written":
                raise ValueError("Restore requires a file.written event in this branch")
            self.snapshots.restore(event.payload, self._emit(branch_id))

    def report(self, branch_id: str) -> dict:
        events = self.events(branch_id)
        return {
            "schema_version": 1,
            "branch_id": branch_id,
            "runs": [e.payload for e in events if e.kind == "run.ended"],
            "artifacts": [e.payload for e in events if e.kind == "artifact.registered"],
            "events": [asdict(e) for e in events],
            "children": [e.payload for e in events if e.kind == "agent.finished"],
            "child_changes": [
                asdict(child_event)
                for event in events
                if event.kind == "agent.started"
                for child_event in self.events(event.payload["branch_id"])
                if child_event.kind in {"file.written", "file.restored"}
            ],
        }

    def close(self) -> None:
        if self._task is not None:
            raise RuntimeError("Stop and await the active run before closing")
        if any(resource.is_open for resource in self._tools.resources):
            raise RuntimeError("Use await Application.aclose() to close active browser/MCP sessions")
        self.store.close()

    async def aclose(self) -> None:
        if self._task is not None:
            raise RuntimeError("Stop and await the active run before closing")
        await self._tools.aclose()
        self.store.close()

    def upload(self, name: str, content: bytes) -> dict:
        if self._task is not None:
            raise RuntimeError("任务运行中，请停止或等待后上传文件")
        with workspace_lease(self.settings.workspace / ".clearact"):
            return self.uploads.add(name, content)

    def reconfigure(self, settings: Settings, provider: ModelProvider) -> None:
        if self._task is not None or settings.workspace.resolve() != self.settings.workspace.resolve():
            raise RuntimeError("Reconfiguration requires an idle application in the same workspace")
        self.settings = settings
        self.runner.provider = provider
        self.runner.context_builder = ContextBuilder(
            settings.context_tokens,
            settings.max_output_tokens,
            settings.tool_result_chars,
            settings.tokenizer_key,
        )
        self.runner.timeout_seconds = settings.run_timeout_seconds
        self.runner.settings = settings

    async def browser_state(self) -> dict:
        return await self._browser.state() if self._browser else {"ready": False}

    async def browser_control(self, arguments: dict, branch_id: str | None = None) -> dict:
        if self._browser is None:
            raise ValueError("Browser capability is not installed")
        if branch_id:
            self.events(branch_id)
        result = await self._browser.control(arguments)
        if branch_id:
            self._emit(branch_id)("browser.manual", {"arguments": arguments, "result": result})
        return result

    async def browser_image(self) -> bytes:
        if self._browser is None:
            raise ValueError("Browser capability is not installed")
        return await self._browser.screenshot()

    def mcp_status(self) -> list[dict]:
        return self._mcp.overview() if self._mcp else []

    async def mcp_connect(self, server: str, disconnect: bool = False) -> dict:
        if self._mcp is None:
            raise ValueError("MCP capability is not installed")
        if disconnect:
            await self._mcp.disconnect(server)
            return {"disconnected": True}
        return await self._mcp.connect(server)

    async def configure_mcp(self, servers: dict) -> None:
        if self._task is not None or self._mcp is None:
            raise RuntimeError("MCP configuration requires an idle application")
        await self._mcp.configure(servers)
