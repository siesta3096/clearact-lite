"""One real FPS attempt through Application; no source changes or acceptance feedback during execution."""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import evaluation_observer as observer
from verify_capabilities import credentials

from clearact_lite.bootstrap import build_application
from clearact_lite.storage.snapshots import atomic_write


def save(record, name, value):
    atomic_write(record / name, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


async def watch_manual_stop(app, record, run_task):
    while not run_task.done():
        marker = record / "manual-stop.txt"
        if marker.exists():
            save(
                record,
                "evaluator-stop.json",
                {
                    "automatic": False,
                    "method": "Application.stop",
                    "reason": marker.read_text(encoding="utf-8-sig"),
                },
            )
            app.stop()
            return
        await asyncio.sleep(0.5)


async def main(args):
    root = Path.cwd().resolve()
    record = args.record.resolve()
    workspace = args.workspace.resolve()
    assert record.is_relative_to(root / "development/records/evals")
    record.mkdir(parents=True, exist_ok=True)
    if not args.branch:
        assert not list(workspace.iterdir()), "Workspace changed after preflight; do not overwrite it."
    assert not (record / "run.json").exists() and not (record / "live.json").exists()
    if not (record / "before-source-sha256.json").exists():
        save(
            record,
            "before-source-sha256.json",
            {
                p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (root / "src").rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            },
        )
        save(
            record,
            "workspace-before.json",
            {"workspace": str(workspace), "empty": not bool(args.branch), "continuation_branch": args.branch},
        )
    before = json.loads((record / "before-source-sha256.json").read_text(encoding="utf-8"))
    assert all(hashlib.sha256((root / p).read_bytes()).hexdigest() == digest for p, digest in before.items())
    observer_path = Path(observer.__file__).resolve()
    observer.RECORD = record
    observer.WORKSPACE = workspace
    observer.START = time.perf_counter()
    observer.ACTIVE = {"task": "parent"}
    config_path = root / ".clearact/web-settings.json"
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    base = credentials(workspace)
    settings = replace(
        base,
        run_timeout_seconds=args.timeout,
        auto_compact=raw.get("auto_compact", True),
        compact_threshold=raw.get("compact_threshold", 0.9),
        compact_target=raw.get("compact_target", 0.6),
        auto_continue=raw.get("auto_continue", True),
        tool_result_chars=raw.get("tool_result_chars", 6000),
        repeated_failure_limit=raw.get("repeated_failure_limit", 2),
        recovery_blocked_rounds=raw.get("recovery_blocked_rounds", 2),
        recovery_timeout_seconds=raw.get("recovery_timeout_seconds", 600),
        recovery_output_tokens=raw.get("recovery_output_tokens", 32768),
        unchanged_result_limit=raw.get("unchanged_result_limit", 4),
    )
    public = {k: v for k, v in asdict(settings).items() if k != "api_key"}
    public["workspace"] = str(workspace)
    save(
        record,
        "configuration.json",
        {
            "settings": public,
            "saved_settings_modified": False,
            "runtime_source_sha256": before,
            "observer_source": observer_path.relative_to(root).as_posix(),
            "observer_sha256": hashlib.sha256(observer_path.read_bytes()).hexdigest(),
            "cloud_used": False,
            "agent_runtime_modified": False,
            "original_goal": "development/fixtures/large-tactical-fps.txt",
            "measurement": "Existing passive SSE observer and whole-machine load sampling; no extra model calls.",
            "acceptance": (
                "Independent actual-input browser checks after run; no forced game state or acceptance feedback."
            ),
        },
    )
    prompt = (root / "development/fixtures/large-tactical-fps.txt").read_text(encoding="utf-8")
    prompt = prompt.replace("E:\\game", str(workspace))
    (record / "goal.txt").write_text(prompt, encoding="utf-8")
    if args.branch:
        prompt = (
            "继续当前分支中尚未完成的原始任务，原要求不变。上一轮由测试人员停止，完整历史和文件仍在。"
            "根据现有记录和实际状态决定如何从断点推进，不重放已完成的外部动作。"
        )
        (record / "continuation-prompt.txt").write_text(prompt, encoding="utf-8")
    provider = observer.ObservedProvider(settings)
    app = build_application(settings, provider)
    branch = args.branch or app.fork()
    if args.branch:
        assert app.events(branch) and not any(e.kind == "agent.identity" for e in app.events(branch))
    save(
        record,
        "live.json",
        {
            "branch_id": branch,
            "status": "running",
            "workspace": str(workspace),
            "started_utc": datetime.now(UTC).isoformat(),
        },
    )
    stop = asyncio.Event()
    sampler = asyncio.create_task(observer.telemetry(stop))
    watcher = None
    run = None

    try:
        run_task = asyncio.create_task(app.run(prompt, branch, on_event=observer.observe))
        watcher = asyncio.create_task(watch_manual_stop(app, record, run_task))
        run = await run_task
        save(record, "run.json", asdict(run))
    finally:
        stop.set()
        await sampler
        if watcher:
            try:
                await watcher
            except Exception as exc:
                save(record, "watcher-error.json", {"type": type(exc).__name__, "detail": str(exc)})
        report = app.report(branch)
        save(record, "report.json", report)
        for child in report["children"]:
            save(record, "child-" + child["branch_id"] + ".json", app.report(child["branch_id"]))
        save(record, "calls.json", provider.metrics)
        try:
            await app.aclose()
        finally:
            files = {
                p.relative_to(workspace).as_posix(): {
                    "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in workspace.rglob("*")
                if p.is_file() and ".clearact" not in p.relative_to(workspace).parts
            }
            save(record, "generated-files.json", files)
            changed = [
                p for p, digest in before.items() if hashlib.sha256((root / p).read_bytes()).hexdigest() != digest
            ]
            save(
                record,
                "live.json",
                {
                    "branch_id": branch,
                    "status": run.status if run else "harness_error",
                    "workspace": str(workspace),
                    "elapsed_seconds": observer.relative_time(),
                    "runtime_source_changed": changed,
                    "files": files,
                },
            )
    print("DONE", run.status if run else "harness_error", "RECORDS", record, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("E:/game"),
        help="Explicit isolated game directory; must be empty for a fresh run",
    )
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--branch", help="Explicitly continue an existing parent branch, without clearing its files")
    asyncio.run(main(parser.parse_args()))
