import asyncio
import json
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from clearact_lite.core.errors import ClearActError
from clearact_lite.core.ports import ModelProvider
from clearact_lite.gateway.contracts import (
    BrowserRequest,
    ForkRequest,
    MCPConnectRequest,
    MCPRequest,
    RestoreRequest,
    RunRequest,
    SettingsRequest,
)
from clearact_lite.gateway.service import GatewayService
from clearact_lite.settings import Settings
from clearact_lite.tools.browser import ActBrowser

ASSETS = Path(__file__).resolve().parents[1] / "web"


def create_app(
    settings: Settings,
    config_path: Path,
    provider_factory: Callable[[Settings], ModelProvider] | None = None,
) -> FastAPI:
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = GatewayService(settings, config_path, provider_factory)
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"error": "Only local gateway hosts are accepted"}, status_code=403)
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "Cross-origin access is disabled"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-clearact-token", ""), token):
                return JSONResponse({"error": "页面已失效，请刷新后重试"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"schema_version": 1, "error": "参数不完整或格式不正确"}, status_code=422)

    async def expected_error(request: Request, exc: Exception):
        code = 409 if isinstance(exc, RuntimeError | ClearActError) else 400
        return JSONResponse({"schema_version": 1, "error": str(exc)}, status_code=code)

    for error_type in (ValueError, RuntimeError, ClearActError, OSError):
        app.add_exception_handler(error_type, expected_error)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((ASSETS / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token))

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/api/state")
    async def state():
        return app.state.service.state()

    @app.post("/api/settings")
    async def configure(body: SettingsRequest):
        return await app.state.service.configure(body)

    @app.post("/api/model/probe")
    async def probe():
        return await app.state.service.probe()

    @app.post("/api/runs", status_code=202)
    async def run(body: RunRequest):
        branch = app.state.service.start(body.prompt, body.branch_id, body.attachment_ids)
        return {"schema_version": 1, "branch_id": branch}

    @app.post("/api/stop")
    async def stop():
        return {"schema_version": 1, "stopped": await app.state.service.stop()}

    @app.post("/api/uploads", status_code=201)
    async def upload(file: UploadFile):
        service = app.state.service
        service.require_idle()
        data = bytearray()
        try:
            while chunk := await file.read(1024 * 1024):
                data.extend(chunk)
                if len(data) > service.upload_limit_bytes:
                    return JSONResponse({"schema_version": 1, "error": "文件超过当前上传大小上限"}, status_code=413)
            info = service.application.upload(file.filename or "file", bytes(data))
            return {"schema_version": 1, "file": info}
        finally:
            await file.close()

    @app.get("/api/browser/state")
    async def browser_state():
        return {"schema_version": 1, **await app.state.service.application.browser_state()}

    @app.post("/api/browser/action")
    async def browser_action(body: BrowserRequest):
        from jsonschema import Draft202012Validator, ValidationError

        try:
            Draft202012Validator(ActBrowser.spec.parameters).validate(body.arguments)
        except ValidationError:
            raise ValueError("浏览器参数格式不正确") from None
        service = app.state.service
        branch = service.active_branch or body.branch_id
        try:
            result = await service.application.browser_control(body.arguments, branch)
        except Exception as exc:
            raise ValueError(f"浏览器操作未完成：{type(exc).__name__}: {exc}") from None
        service.publish("browser", {"reason": "user_action"})
        return {"schema_version": 1, **result}

    @app.get("/api/browser/image")
    async def browser_image():
        return Response(await app.state.service.application.browser_image(), media_type="image/jpeg")

    @app.get("/api/mcp")
    async def mcp_status():
        return {"schema_version": 1, "servers": app.state.service.application.mcp_status()}

    @app.post("/api/mcp")
    async def configure_mcp(body: MCPRequest):
        return await app.state.service.configure_mcp(body.servers)

    @app.post("/api/mcp/{server:path}/connect")
    async def connect_mcp(server: str, body: MCPConnectRequest):
        service = app.state.service
        service.require_idle()
        if not body.disconnect:
            await service.application.mcp_connect(server, disconnect=True)
        result = await service.application.mcp_connect(server, body.disconnect)
        service.publish("state", {"reason": "mcp_connection_changed"})
        return {"schema_version": 1, **result}

    @app.get("/api/branches/{branch_id}")
    async def report(branch_id: str):
        return app.state.service.application.report(branch_id)

    @app.post("/api/branches/{branch_id}/fork")
    async def fork(branch_id: str, body: ForkRequest):
        service = app.state.service
        service.require_idle()
        branch = service.application.fork(branch_id, body.at_event)
        service.publish("state", {"reason": "branch_created"})
        return {"schema_version": 1, "branch_id": branch}

    @app.post("/api/branches/{branch_id}/restore")
    async def restore(branch_id: str, body: RestoreRequest):
        service = app.state.service
        service.require_idle()
        service.application.restore(branch_id, body.event_id)
        service.publish("state", {"reason": "file_restored"})
        return {"schema_version": 1, "restored": True}

    @app.get("/api/branches/{branch_id}/files/{event_id}")
    async def download(branch_id: str, event_id: int):
        name, content = app.state.service.application.artifact(branch_id, event_id)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
            },
        )

    @app.get("/api/stream")
    async def stream(request: Request, after: int = 0):
        service = app.state.service
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            pass

        async def frames():
            cursor = after
            while not service.closing and not await request.is_disconnected():
                service.changed.clear()
                ready = [frame for frame in service.frames if frame["sequence"] > cursor]
                for frame in ready:
                    cursor = frame["sequence"]
                    yield f"id: {cursor}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n"
                if not ready:
                    try:
                        await asyncio.wait_for(service.changed.wait(), timeout=10)
                    except TimeoutError:
                        yield ": heartbeat\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app


def serve(settings: Settings, port: int = 8875, open_browser: bool = False) -> None:
    import socket
    import threading
    import webbrowser

    import uvicorn

    if not 1 <= port <= 65535:
        raise ValueError("端口应在 1 到 65535 之间")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        app = create_app(settings, Path.cwd() / ".clearact" / "web-settings.json")
        url = f"http://127.0.0.1:{port}"
        print(f"ClearAct Lite: {url}\n模型与工作区可在页面设置中查看。\n按 Ctrl+C 停止服务。", flush=True)
        if open_browser:

            def ready():
                import time
                import urllib.request

                for _ in range(100):
                    try:
                        with urllib.request.urlopen(url, timeout=0.3) as response:
                            if response.status == 200:
                                webbrowser.open(url)
                                return
                    except OSError:
                        time.sleep(0.1)

            threading.Thread(target=ready, daemon=True).start()

        class LocalGatewayServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                service = getattr(app.state, "service", None)
                if service is not None:
                    service.closing = True
                    service.changed.set()
                super().handle_exit(sig, frame)

        server = LocalGatewayServer(
            uvicorn.Config(
                app,
                log_level="warning",
                lifespan="on",
                ws="none",
                timeout_graceful_shutdown=5,
            )
        )
        server.run(sockets=[sock])
        if not server.started:
            raise RuntimeError("工作台未能启动，请检查上方错误和本机配置")
    finally:
        sock.close()
