import asyncio
import json
import os
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from clearact_lite.bootstrap import build_application
from clearact_lite.core.ports import ModelProvider
from clearact_lite.gateway.contracts import SettingsRequest
from clearact_lite.integrations.config import normalize_servers
from clearact_lite.providers.openai_compatible import OpenAICompatible
from clearact_lite.settings import Settings
from clearact_lite.storage.snapshots import atomic_write


class GatewayService:
    def __init__(
        self,
        settings: Settings,
        config_path: Path,
        provider_factory: Callable[[Settings], ModelProvider] | None = None,
    ):
        self.config_path = config_path
        self.provider_factory = provider_factory
        self.mcp_path = config_path.with_name("mcp-servers.json")
        self.mcp_configs = (
            normalize_servers(json.loads(self.mcp_path.read_text(encoding="utf-8"))) if self.mcp_path.exists() else {}
        )
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if raw.get("api_key") == "":
                raw["clear_api_key"] = True
            request = SettingsRequest.model_validate(raw)
            settings = self._settings(settings, request)
        self.application = self._build(settings)
        self.task: asyncio.Task | None = None
        self.active_branch: str | None = None
        self.sequence = 0
        self.frames: deque[dict] = deque(maxlen=1000)
        self.changed = asyncio.Event()
        self.last_error: str | None = None
        self.started = asyncio.Event()
        self.closing = False
        self.upload_limit_bytes = int(os.getenv("CLEARACT_UPLOAD_MAX_MB", "64")) * 1024 * 1024
        if self.upload_limit_bytes <= 0:
            raise ValueError("CLEARACT_UPLOAD_MAX_MB must be positive")

    def _build(self, settings: Settings):
        provider = self.provider_factory(settings) if self.provider_factory else None
        return build_application(settings, provider, self.mcp_configs)

    @staticmethod
    def _settings(current: Settings, request: SettingsRequest) -> Settings:
        raw = request.model_dump(exclude={"clear_api_key", "api_key"})
        raw["workspace"] = Path(request.workspace).expanduser().resolve()
        raw["api_key"] = "" if request.clear_api_key else (request.api_key or current.api_key)
        return replace(current, **raw)

    def publish(self, kind: str, payload: dict) -> None:
        self.sequence += 1
        self.frames.append({"sequence": self.sequence, "kind": kind, "payload": payload})
        self.changed.set()

    def require_idle(self) -> None:
        if self.task is not None:
            raise RuntimeError("任务正在运行，请先停止或等待结束")

    def state(self) -> dict:
        settings = self.application.settings
        public = asdict(settings)
        public.pop("api_key")
        public["workspace"] = str(settings.workspace)
        public["api_key_set"] = bool(settings.api_key)
        public.pop("tool_result_chars")
        return {
            "schema_version": 1,
            "settings": public,
            "branches": [asdict(b) for b in self.application.branches()],
            "active_branch": self.active_branch,
            "sequence": self.sequence,
            "last_error": self.last_error,
            "mode": "red",
            "mcp_servers": self.application.mcp_status(),
            "upload_limit_bytes": self.upload_limit_bytes,
        }

    async def configure(self, request: SettingsRequest) -> dict:
        self.require_idle()
        settings = self._settings(self.application.settings, request)
        same_workspace = settings.workspace.resolve() == self.application.settings.workspace.resolve()
        replacement = None if same_workspace else self._build(settings)
        saved = asdict(settings)
        saved.pop("tool_result_chars")
        saved["workspace"] = str(settings.workspace)
        try:
            atomic_write(self.config_path, json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8"))
        except BaseException:
            if replacement:
                await replacement.aclose()
            raise
        if same_workspace:
            provider = self.provider_factory(settings) if self.provider_factory else OpenAICompatible(settings)
            self.application.reconfigure(settings, provider)
        else:
            await self.application.aclose()
            self.application = replacement
        self.last_error = None
        self.publish("state", {"reason": "settings_changed"})
        return self.state()

    def start(self, prompt: str, branch_id: str | None, attachment_ids: list[str] | None = None) -> str:
        self.require_idle()
        if not prompt.strip():
            raise ValueError("请先输入任务")
        for upload_id in attachment_ids or []:
            self.application.uploads.get(upload_id)
        if branch_id:
            self.application.events(branch_id)
        branch_id = branch_id or self.application.fork()
        self.active_branch, self.last_error = branch_id, None
        self.started.clear()

        def observed(event):
            if event.kind == "run.started":
                self.started.set()
            self.publish("event", asdict(event))

        async def execute():
            try:
                await self.application.run(
                    prompt,
                    branch_id,
                    observed,
                    lambda text: self.publish("delta", {"branch_id": branch_id, "text": text}),
                    attachment_ids=attachment_ids,
                )
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.publish("error", {"branch_id": branch_id, "message": self.last_error})
            finally:
                self.active_branch, self.task = None, None
                self.publish("state", {"reason": "run_ended"})

        self.task = asyncio.create_task(execute())
        self.publish("state", {"reason": "run_started", "branch_id": branch_id})
        return branch_id

    async def stop(self) -> bool:
        if self.task is None:
            return False
        task = self.task
        waiter = asyncio.create_task(self.started.wait())
        try:
            await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
            stopped = self.application.stop() if not task.done() else False
            await task
            return stopped
        finally:
            waiter.cancel()

    async def probe(self) -> dict:
        settings = self.application.settings
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                response = await client.get(settings.base_url.rstrip("/") + "/models", headers=headers)
                response.raise_for_status()
                models = [m["id"] for m in response.json().get("data", [])]
                return {
                    "schema_version": 1,
                    "ok": True,
                    "models": models,
                    "message": "模型接口已连接" if settings.model in models else "接口已连接，请确认模型名称",
                }
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return {"schema_version": 1, "ok": False, "models": [], "message": f"连接未通过：{exc}"}

    async def close(self) -> None:
        await self.stop()
        await self.application.aclose()

    async def configure_mcp(self, raw: dict) -> dict:
        self.require_idle()
        configs = normalize_servers(raw, self.mcp_configs)
        atomic_write(self.mcp_path, json.dumps(configs, ensure_ascii=False, indent=2).encode("utf-8"))
        await self.application.configure_mcp(configs)
        self.mcp_configs = configs
        self.publish("state", {"reason": "mcp_configured"})
        return {"schema_version": 1, "servers": self.application.mcp_status()}
