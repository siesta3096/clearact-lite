"""Regression for the real evaluator's synchronous-stop/await-bool shutdown failure."""

import asyncio
import json

from evaluate_tactical_fps import watch_manual_stop

from clearact_lite.bootstrap import build_application
from clearact_lite.settings import Settings


def test_manual_evaluator_stop_completes_watcher_and_records_result(tmp_path):
    async def scenario():
        class WaitingProvider:
            def __init__(self):
                self.started = asyncio.Event()

            async def complete(self, messages, tools, on_delta):
                self.started.set()
                await asyncio.Event().wait()

        provider = WaitingProvider()
        app = build_application(Settings(tmp_path / "workspace"), provider)
        run_task = asyncio.create_task(app.run("Evaluator stop regression; no real model."))
        try:
            await asyncio.wait_for(provider.started.wait(), 2)
            (tmp_path / "manual-stop.txt").write_text("Manual test stop", encoding="utf-8")
            watcher = asyncio.create_task(watch_manual_stop(app, tmp_path, run_task))
            result = await asyncio.wait_for(run_task, 2)
            await asyncio.wait_for(watcher, 2)
            assert result.status == "stopped"
            record = json.loads((tmp_path / "evaluator-stop.json").read_text(encoding="utf-8"))
            assert record["automatic"] is False
            assert record["method"] == "Application.stop"
            assert app.report(result.branch_id)["runs"][-1]["status"] == "stopped"
        finally:
            if not run_task.done():
                app.stop()
                await asyncio.gather(run_task, return_exceptions=True)
            await app.aclose()

    asyncio.run(scenario())
