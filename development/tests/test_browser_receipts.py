"""Actual Edge + local HTTP evidence; no model or browser protocol doubles."""

import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from test_long_work import execution
from verify_frontend import LocalServer

from clearact_lite.bootstrap import build_application
from clearact_lite.context.compaction import memory_index
from clearact_lite.settings import Settings


def test_startup_errors_resources_full_history_and_navigation_are_observable(tmp_path):
    fixture = FastAPI()

    @fixture.get("/")
    async def health():
        return {"ready": True}

    @fixture.get("/broken")
    async def broken():
        return HTMLResponse("""<!doctype html><title>Broken fixture</title><p>partially usable</p>
        <script src="/missing-engine.js?token=synthetic-private-value"></script>
        <script src="http://127.0.0.1:1/unreachable.js"></script>
        <script>console.error('initial console error');throw new Error('original boot failure');</script>""")

    @fixture.get("/clean")
    async def clean():
        return HTMLResponse("<!doctype html><title>Clean fixture</title><p>clean</p>")

    async def scenario():
        app = build_application(Settings(tmp_path))
        branch = app.fork()
        ctx = execution(app, branch)
        try:
            async with LocalServer(fixture) as server:
                result = await app.runner.registry.execute("browser_open", {"url": server.url + "/broken"}, ctx)
                assert not result.is_error  # A usable page can still have original application errors.
                await app.runner.registry.execute("browser_action", {"action": "wait", "milliseconds": 100}, ctx)
                state = await app.browser_state()
                diagnostics = state["diagnostics"]
                errors = diagnostics["errors"]
                kinds = {error["kind"] for error in errors}
                assert kinds >= {"javascript_error", "console_error", "http_error", "resource_failed"}
                assert diagnostics["document_ready_state"] == "complete" and diagnostics["navigation_status"] == 200
                assert "original boot failure" in json.dumps(errors)
                missing = next(error for error in errors if error.get("http_status") == 404)
                assert missing["url"].endswith("/missing-engine.js") and "token=" not in missing["url"]
                events = app.events(branch)
                for error in errors:
                    original = next(e for e in events if e.id == error["event_id"])
                    assert original.kind == "browser.diagnostic"
                script_error = next(
                    e for e in events if e.kind == "browser.diagnostic" and e.payload["kind"] == "javascript_error"
                )
                assert "original boot failure" in script_error.payload["stack"]
                # Evaluation failure must carry the already-captured startup evidence as well.
                failed = await app.runner.registry.execute(
                    "browser_action",
                    {"action": "evaluate", "value": "missingRuntimeMethod()"},
                    ctx,
                )
                assert failed.is_error and "original boot failure" in failed.content
                assert "Browser diagnostics" in failed.content
                old_document = diagnostics["document_id"]
                clean_result = await app.runner.registry.execute(
                    "browser_action",
                    {"action": "navigate", "url": server.url + "/clean"},
                    ctx,
                )
                clean_state = json.loads(clean_result.content)["diagnostics"]
                assert clean_state["document_id"] != old_document and clean_state["errors"] == []
                assert any(e.kind == "browser.diagnostic" for e in app.events(branch))
                assert "original boot failure" in memory_index(app.events(branch), 0)
                raw = [
                    json.loads(line)
                    for line in (tmp_path / ".clearact/browser/diagnostics.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                assert any("original boot failure" in error["stack"] for error in raw)
        finally:
            await app.aclose()

    asyncio.run(scenario())


def test_repeated_error_occurrences_do_not_fabricate_new_evidence_and_tabs_are_separate(tmp_path):
    async def scenario():
        app = build_application(Settings(tmp_path))
        branch = app.fork()
        ctx = execution(app, branch)
        try:
            await app.runner.registry.execute("browser_open", {"url": "about:blank"}, ctx)
            call = {"action": "evaluate", "value": "console.error('repeat-me'); 42"}
            first = await app.runner.registry.execute("browser_action", call, ctx)
            for _ in range(10):
                same = await app.runner.registry.execute("browser_action", call, ctx)
                assert same.content == first.content
            events = [e for e in app.events(branch) if e.kind == "browser.diagnostic"]
            assert len(events) == 11
            source = json.loads(first.content)["diagnostics"]
            assert len(source["errors"]) == 1
            await app.runner.registry.execute("browser_action", {"action": "new_tab"}, ctx)
            assert (await app.browser_state())["diagnostics"]["errors"] == []
            await app.runner.registry.execute("browser_action", {"action": "switch_tab", "index": 0}, ctx)
            assert (await app.browser_state())["diagnostics"] == source
        finally:
            await app.aclose()

    asyncio.run(scenario())
