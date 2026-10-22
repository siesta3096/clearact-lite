import asyncio
import json
import re
from contextlib import asynccontextmanager

import httpx

from clearact_lite.core.contracts import Message, ModelReply, ToolCall, Usage
from clearact_lite.gateway.server import create_app
from clearact_lite.settings import Settings


class FileProvider:
    """HTTP integration double; not a real-model evaluation."""

    def __init__(self):
        self.index = 0

    async def complete(self, messages, tools, on_delta):
        self.index += 1
        call_id = f"call-{self.index}"
        if messages[-1].role == "user":
            call = ToolCall(call_id, "file_write", {"path": "result.txt", "text": "gateway-proof"})
        elif "Written" in messages[-1].content:
            call = ToolCall(call_id, "artifact_register", {"path": "result.txt"})
        else:
            on_delta("已完成")
            return ModelReply(Message("assistant", "已完成"), "stop", Usage(12, 3))
        return ModelReply(Message("assistant", tool_calls=(call,)), "tool_calls", Usage(12, 3))


@asynccontextmanager
async def gateway(tmp_path, provider=None, config=None):
    settings = Settings(tmp_path / "workspace", api_key="test-only-secret")
    app = create_app(settings, config or tmp_path / "config.json", lambda settings: provider or FileProvider())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            page = await client.get("/")
            token = re.search(r'name="clearact-token" content="([^"]+)"', page.text)[1]
            client.headers["X-ClearAct-Token"] = token
            yield client, app


async def wait_idle(client):
    async with asyncio.timeout(3):
        while (await client.get("/api/state")).json()["active_branch"]:
            await asyncio.sleep(0.005)


def test_http_run_report_download_fork_and_restore(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (client, app):
            started = await client.post("/api/runs", json={"prompt": "write and deliver"})
            assert started.status_code == 202
            branch = started.json()["branch_id"]
            await wait_idle(client)
            report = (await client.get(f"/api/branches/{branch}")).json()
            assert report["runs"][-1]["status"] == "finished"
            assert report["runs"][-1]["model_calls"] == 3
            registered = next(e for e in report["events"] if e["kind"] == "artifact.registered")
            download = await client.get(f"/api/branches/{branch}/files/{registered['id']}")
            assert download.content == b"gateway-proof"
            assert "attachment" in download.headers["content-disposition"]
            fork = await client.post(f"/api/branches/{branch}/fork", json={})
            assert fork.status_code == 200
            assert len((await client.get("/api/state")).json()["branches"]) == 2
            change = next(e for e in report["events"] if e["kind"] == "file.written")
            restored = await client.post(f"/api/branches/{branch}/restore", json={"event_id": change["id"]})
            assert restored.json()["restored"] is True
            assert not (tmp_path / "workspace/result.txt").exists()
            assert any(f["kind"] == "delta" for f in app.state.service.frames)
            assert all("schema_version" in f["payload"] for f in app.state.service.frames if f["kind"] == "event")

    asyncio.run(scenario())


def test_settings_preserve_clear_and_never_echo_secret(tmp_path):
    async def scenario():
        config = tmp_path / "config.json"
        async with gateway(tmp_path, config=config) as (client, app):
            state = (await client.get("/api/state")).json()
            assert "test-only-secret" not in json.dumps(state)
            assert state["settings"]["api_key_set"]
            payload = dict(state["settings"])
            payload.pop("api_key_set")
            payload["api_key"] = "new-test-secret"
            saved = await client.post("/api/settings", json=payload)
            assert saved.status_code == 200 and "new-test-secret" not in saved.text
            payload["api_key"] = ""
            await client.post("/api/settings", json=payload)
            assert app.state.service.application.settings.api_key == "new-test-secret"
            payload["clear_api_key"] = True
            await client.post("/api/settings", json=payload)
            assert not (await client.get("/api/state")).json()["settings"]["api_key_set"]
        async with gateway(tmp_path, config=config) as (client, app):
            assert not (await client.get("/api/state")).json()["settings"]["api_key_set"]

    asyncio.run(scenario())


def test_busy_stop_and_settings_conflict(tmp_path):
    class WaitingProvider:
        async def complete(self, messages, tools, on_delta):
            on_delta("partial")
            await asyncio.Event().wait()

    async def scenario():
        async with gateway(tmp_path, WaitingProvider()) as (client, app):
            started = await client.post("/api/runs", json={"prompt": "wait"})
            branch = started.json()["branch_id"]
            assert (await client.post("/api/runs", json={"prompt": "second"})).status_code == 409
            state = (await client.get("/api/state")).json()
            payload = state["settings"]
            payload.pop("api_key_set")
            assert (await client.post("/api/settings", json=payload)).status_code == 409
            stopped = await client.post("/api/stop", json={})
            assert stopped.json()["stopped"]
            assert (await client.get(f"/api/branches/{branch}")).json()["runs"][-1]["status"] == "stopped"

    asyncio.run(scenario())


def test_gateway_rejects_cross_origin_host_and_missing_token(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (client, app):
            assert (
                await client.post("/api/runs", json={"prompt": "x"}, headers={"Origin": "https://elsewhere.example"})
            ).status_code == 403
            assert (await client.get("/api/state", headers={"Host": "attacker.example"})).status_code == 403
            client.headers.pop("X-ClearAct-Token")
            assert (await client.post("/api/runs", json={"prompt": "x"})).status_code == 403

    asyncio.run(scenario())


def test_download_rejects_unregistered_or_changed_files(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (client, app):
            started = await client.post("/api/runs", json={"prompt": "write"})
            branch = started.json()["branch_id"]
            await wait_idle(client)
            report = (await client.get(f"/api/branches/{branch}")).json()
            registered = next(e for e in report["events"] if e["kind"] == "artifact.registered")
            (tmp_path / "workspace/result.txt").write_text("manual edit")
            assert (await client.get(f"/api/branches/{branch}/files/{registered['id']}")).status_code == 400
            assert (await client.get(f"/api/branches/{branch}/files/1")).status_code == 400
            change = next(e for e in report["events"] if e["kind"] == "file.written")
            assert (
                await client.post(f"/api/branches/{branch}/restore", json={"event_id": change["id"]})
            ).status_code == 409

    asyncio.run(scenario())


def test_invalid_settings_hide_submitted_credentials(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as (client, app):
            state = (await client.get("/api/state")).json()["settings"]
            state.pop("api_key_set")
            state["api_key"] = "never-echo-me"
            state["base_url"] = "bad-url"
            response = await client.post("/api/settings", json=state)
            assert response.status_code == 422
            assert "never-echo-me" not in response.text
            assert app.state.service.application.settings.api_key == "test-only-secret"

    asyncio.run(scenario())
