"""Local-only parameter comparisons, capacity probes and original full-task evaluation."""

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from verify_capabilities import credentials

from clearact_lite.bootstrap import build_application
from clearact_lite.core.contracts import Message, ToolCall
from clearact_lite.providers.openai_compatible import OpenAICompatible
from clearact_lite.runtime.runner import SYSTEM

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_HASHES = {
    path.relative_to(SOURCE_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (SOURCE_ROOT / "src").rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
}

COUNTER_GOAL = """创建一个完成度良好的中文计数工作台，实际创建 index.html、style.css、app.js。
先用 task_plan 明确这三个文件的职责和 DOM 接口，再用 agent_task 分配编写（每个任务只写分配文件）。
界面有 id=count 的计数显示、id=increment 的加一按钮、id=reset 的重置按钮。
初始值为0；加一后立即更新并保存 localStorage；刷新保留值；重置保存0。
响应式支持手机390px宽和电脑1280px宽，无横向溢出，按钮可见可点击，界面中文。
每个文件使用独立 CSS/JS，DOM接口保持一致。主代理整合后实际用浏览器点击、刷新、重置检查。
生成 README.md 说明入口和已检查/未完成项，登记交付所有四个文件。不要把代码写在最终回答中。
尽量用完整的小调用，长文件分段写入，每段目标不超过 {chunk} 个输出token。保留工作断点。"""


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def counter_acceptance(workspace):
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width, height in [(1280, 820), (390, 844)]:
                context = await browser.new_context(viewport={"width": width, "height": height})
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto((workspace / "index.html").as_uri())
                    await page.locator("#reset").click()
                    await page.wait_for_function("document.querySelector('#count').textContent.trim()==='0'")
                    await page.locator("#increment").click(click_count=3, delay=50)
                    await page.wait_for_function("document.querySelector('#count').textContent.trim()==='3'")
                    await page.reload()
                    await page.wait_for_function("document.querySelector('#count').textContent.trim()==='3'")
                    await page.locator("#reset").click()
                    await page.reload()
                    await page.wait_for_function("document.querySelector('#count').textContent.trim()==='0'")
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors, errors
                    await page.screenshot(path=str(workspace.parent / f"counter-{width}.png"))
                    results.append({"width": width, "passed": True, "page_errors": errors})
                finally:
                    await context.close()
        finally:
            await browser.close()
    return results


class FirstOutputLimited:
    """Induce one real output limit after a completed write, then use normal configuration."""

    def __init__(self, settings):
        self.settings, self.calls, self.induced = settings, 0, False

    async def complete(self, messages, tools, on_delta):
        self.calls += 1
        completed_write = any(message.role == "tool" and message.content.startswith("Written ") for message in messages)
        induce = completed_write and not self.induced
        if induce:
            self.induced = True
        settings = replace(self.settings, max_output_tokens=1) if induce else self.settings
        return await OpenAICompatible(settings).complete(messages, tools, on_delta)


async def run_case(root, base, *, output, threshold, chunk, large=False, induced=False, seed=False):
    root.mkdir(parents=True)
    settings = replace(
        base,
        workspace=root / "workspace",
        max_output_tokens=output,
        compact_threshold=threshold,
        compact_target=0.6,
        request_timeout_seconds=360,
        run_timeout_seconds=900 if large else 300,
    )
    app = build_application(settings, FirstOutputLimited(settings) if induced else None)
    result = {
        "provider": "real_local_model",
        "source_sha256_at_process_import": SOURCE_HASHES,
        "model": settings.model,
        "passed": False,
        "parameters": {
            "output": output,
            "threshold": threshold,
            "target": 0.6,
            "chunk_instruction": chunk,
            "context": settings.context_tokens,
            "child_concurrency": 1,
        },
        "induced_output_limit_after_completed_write": 1 if induced else None,
        "seeded_archival_fixture": seed,
    }
    try:
        branch = app.fork()
        if seed:
            emit = app._emit(branch)
            emit(
                "message",
                Message(
                    "system",
                    SYSTEM + f" Workspace root: {settings.workspace}. "
                    "Use workspace-relative paths. Archival fixture; previous unrelated file work is complete.",
                ).to_dict(),
            )
            emit("message", Message("user", "Previous fixture: keep seed.txt unchanged during the new task.").to_dict())
            text = "archival-fixture-line\n" * 2800
            emit(
                "message",
                Message(
                    "assistant", tool_calls=(ToolCall("seed-write", "file_write", {"path": "seed.txt", "text": text}),)
                ).to_dict(),
            )
            app.snapshots.write_text(settings.workspace / "seed.txt", text, emit)
            emit("message", Message("tool", "Seed file completed.", tool_call_id="seed-write").to_dict())
        goal = (
            (Path(__file__).resolve().parents[1] / "fixtures/large-visual-novel.txt").read_text(encoding="utf-8")
            if large
            else COUNTER_GOAL.format(chunk=chunk)
        )
        (root / "goal.txt").write_text(goal, encoding="utf-8")

        def observed(event):
            if event.kind in {"context.compacted", "capacity.continuation", "agent.started", "agent.finished"}:
                print(root.name, event.kind, event.payload.get("task_id", ""), flush=True)

        run = await app.run(goal, branch, on_event=observed)
        result["run"] = asdict(run)
        report = app.report(branch)
        save(root / "report.json", report)
        all_events = list(app.events(branch))
        for child in report["children"]:
            save(root / f"child-{child['branch_id']}.json", app.report(child["branch_id"]))
            all_events.extend(app.events(child["branch_id"]))
        result["compactions"] = [e.payload for e in app.events(branch) if e.kind == "context.compacted"]
        result["children"] = len(report["children"])
        result["capacity_continuations"] = len([e for e in all_events if e.kind == "capacity.continuation"])
        if induced:
            result["capacity_resume_observed"] = result["capacity_continuations"] > 0
        if large:
            result["generation_finished"] = run.status == "finished"
            result["independent_acceptance"] = "pending"
            result["passed"] = None
        else:
            assert run.status == "finished", asdict(run)
            assert result["children"] > 0, "Model did not delegate"
            assert all(
                (settings.workspace / name).is_file() for name in ["index.html", "style.css", "app.js", "README.md"]
            )
            result["independent_checks"] = await counter_acceptance(settings.workspace)
            assert all(check["passed"] for check in result["independent_checks"])
            result["passed"] = True
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await app.aclose()
        save(root / "evaluation.json", result)
        print(
            root.name,
            "RESULT",
            result.get("passed"),
            result.get("run", {}).get("usage"),
            result.get("error", ""),
            flush=True,
        )
    return result


async def boundary(root, base):
    root.mkdir(parents=True)
    result = {"provider": "real_local_model", "context": base.context_tokens, "requests": []}
    settings = replace(base, max_output_tokens=8, request_timeout_seconds=180)
    for index in range(8):
        start = time.monotonic()
        reply = await OpenAICompatible(settings).complete(
            [
                Message("system", "Return only OK. Ignore filler."),
                Message("user", "Filler:\n" + "x " * 16000 + f"\nRequest {index}. Return OK."),
            ],
            [],
            lambda text: None,
        )
        result["requests"].append(
            {
                "index": index,
                "usage": asdict(reply.usage),
                "finish_reason": reply.finish_reason,
                "answer": reply.message.content,
                "seconds": round(time.monotonic() - start, 3),
                "raw_response": reply.raw_response,
            }
        )
        save(root / "boundary.json", result)
    values = [r["usage"]["input_tokens"] for r in result["requests"]]
    result["cumulative_input_tokens"] = sum(values) if all(v is not None for v in values) else None
    result["cumulative_over_100k_with_each_request_below_capacity"] = (
        (sum(values) > 100000 and all(v < base.context_tokens for v in values))
        if all(v is not None for v in values)
        else None
    )
    concurrency_results = []

    async def throughput_call(index):
        start = time.monotonic()
        response = await OpenAICompatible(replace(settings, max_output_tokens=256)).complete(
            [
                Message("system", "Follow the requested output format."),
                Message(
                    "user",
                    "x " * 16000 + f"\nRun {index}: list integers 1 through 100 separated by commas, no explanation.",
                ),
            ],
            [],
            lambda text: None,
        )
        return {"usage": asdict(response.usage), "seconds": round(time.monotonic() - start, 3)}

    for round_index, concurrency in enumerate([1, 2, 2, 1]):
        start = time.monotonic()
        if concurrency == 1:
            runs = [await throughput_call(f"{round_index}-{index}") for index in range(2)]
        else:
            runs = await asyncio.gather(*(throughput_call(f"{round_index}-{index}") for index in range(2)))
        concurrency_results.append(
            {
                "concurrency": concurrency,
                "seconds": round(time.monotonic() - start, 3),
                "runs": runs,
                "scope": "independent inference requests, not a full parallel child-agent task",
            }
        )
        result["concurrency_probe"] = concurrency_results
        save(root / "boundary.json", result)
    try:
        reply = await OpenAICompatible(settings).complete(
            [Message("system", "Return OK."), Message("user", "x " * 110000 + "Return OK.")], [], lambda text: None
        )
        result["over_window_probe"] = {
            "behavior": "service_returned_response",
            "usage": asdict(reply.usage),
            "finish_reason": reply.finish_reason,
            "raw_response": reply.raw_response,
            "interpretation": "Check reported input for service-side truncation; not an Agent compaction",
        }
    except Exception as exc:
        result["over_window_probe"] = {
            "behavior": "service_rejected_or_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "raw_response": getattr(exc, "raw_response", None),
        }
    after = await OpenAICompatible(settings).complete([Message("user", "Reply exactly OK.")], [], lambda text: None)
    result["short_request_after_overflow_without_reload"] = {
        "answer": after.message.content,
        "finish_reason": after.finish_reason,
        "usage": asdict(after.usage),
        "raw_response": after.raw_response,
    }
    save(root / "boundary.json", result)
    print("BOUNDARY", result["cumulative_input_tokens"], result["over_window_probe"]["behavior"], flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["matrix", "large", "boundary", "resume"], default="matrix")
    parser.add_argument("--output", type=int, default=16384)
    parser.add_argument("--induce-after-write", action="store_true")
    args = parser.parse_args()
    root = (
        Path(__file__).resolve().parents[1]
        / "records/evals"
        / datetime.now().strftime(f"long-{args.mode}-%Y%m%d-%H%M%S")
    )
    root.mkdir(parents=True)
    base = credentials(root / "unused")
    print("RECORDS:", root, flush=True)
    if args.mode == "boundary":
        await boundary(root / "capacity", base)
    elif args.mode == "large":
        await run_case(
            root / "original-game",
            base,
            output=args.output,
            threshold=0.9,
            chunk=2048,
            large=True,
            induced=args.induce_after_write,
        )
    elif args.mode == "resume":
        await run_case(
            root / "real-output-resume", base, output=args.output, threshold=0.9, chunk=2048, induced=True, seed=True
        )
    else:
        results = []
        for output, threshold, chunk in [(8192, 0.9, 1024), (16384, 0.9, 2048), (32768, 0.9, 2048), (16384, 0.8, 2048)]:
            results.append(
                await run_case(
                    root / f"out-{output}-threshold-{threshold}",
                    base,
                    output=output,
                    threshold=threshold,
                    chunk=chunk,
                    seed=True,
                )
            )
            save(root / "matrix.json", results)


if __name__ == "__main__":
    asyncio.run(main())
