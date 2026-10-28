"""Local real-model recovery samples and a paired archive probe; no game implementation here."""

import argparse
import asyncio
import csv
import hashlib
import json
import shutil
import time
from dataclasses import asdict, replace
from pathlib import Path

import evaluation_observer as observer
from verify_capabilities import credentials

from clearact_lite.bootstrap import build_application
from clearact_lite.context.builder import ContextBuilder
from clearact_lite.core.contracts import Event, Message


def save(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def sample(root, name, prompt, prepare, check):
    folder = root / name
    folder.mkdir()
    workspace = folder / "workspace"
    workspace.mkdir()
    prepare(workspace)
    settings = replace(credentials(workspace), run_timeout_seconds=240, request_timeout_seconds=120)
    observer.RECORD, observer.START, observer.ACTIVE = folder, time.perf_counter(), {"task": "parent"}
    provider = observer.ObservedProvider(settings)
    app = build_application(settings, provider)
    try:
        result = await app.run(prompt, on_event=observer.observe)
        save(folder, "run.json", asdict(result))
        save(folder, "report.json", app.report(result.branch_id))
        save(folder, "calls.json", provider.metrics)
        try:
            verdict = check(workspace)
        except Exception as exc:
            verdict = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        save(folder, "independent.json", verdict)
        return {"case": name, "run": asdict(result), "independent": verdict}
    finally:
        await app.aclose()


async def archive_probe(root):
    old = Path("development/records/evals/fps-attempt-20261025-135936")
    report = json.loads((old / "report.json").read_text(encoding="utf-8"))
    events = [Event(**e) for e in report["events"] if e["id"] <= 4757]
    before = [Message.from_dict(m) for m in json.loads((old / "archive-2-projection-after.json").read_text("utf-8"))]
    folder = root / "archive-paired-probe"
    folder.mkdir()
    settings = replace(credentials(folder), max_output_tokens=4096, request_timeout_seconds=120)
    app = build_application(settings)
    try:
        tools = app.runner.registry.specs()
        after, info = ContextBuilder(100096, 16384, 6000, settings.tokenizer_key).build(events, tools)
        save(folder, "candidate-projection.json", [m.to_dict() for m in after])
        save(
            folder,
            "projection-evidence.json",
            {
                "original_getEntities_occurrences": sum(m.content.count("getEntities") for m in before),
                "candidate_getEntities_occurrences": sum(m.content.count("getEntities") for m in after),
                "candidate_budget": info,
                "scope": "Only one next decision per condition; no tools executed; not whole-task acceptance",
            },
        )
        results = []
        for label, messages in [("original", before), ("candidate", after)]:
            case = folder / label
            case.mkdir()
            observer.RECORD, observer.START = case, time.perf_counter()
            provider = observer.ObservedProvider(settings)
            try:
                response = await provider.complete(messages, tools, lambda text: None)
                save(case, "response.json", asdict(response))
                save(case, "calls.json", provider.metrics)
                results.append({"condition": label, "response": asdict(response)})
            except Exception as exc:
                failure = {"condition": label, "error": f"{type(exc).__name__}: {exc}"}
                save(case, "failure.json", failure)
                results.append(failure)
        return results
    finally:
        await app.aclose()


def prepare_data(workspace):
    (workspace / "input.csv").write_text("id,category,amount\na,X,12.5\nb,Y,7\na,X,12.5\nc,X,2.5\n", "utf-8")


def check_data(workspace):
    rows = list(csv.DictReader((workspace / "totals.csv").open(encoding="utf-8-sig", newline="")))
    actual = {row["category"]: float(row["total"]) for row in rows}
    return {"passed": actual == {"X": 15, "Y": 7}, "actual": actual}


def prepare_browser(workspace):
    (workspace / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Ledger</title><h1>账本</h1>'
        "<p>接口在 window.InvoiceBook 中，请用已有接口取出账单。</p><script>"
        'window.InvoiceBook={getEntries:()=>[{id:"a",amount:12.5},{id:"b",amount:7}]};</script>',
        "utf-8",
    )


def check_browser(workspace):
    value = json.loads((workspace / "total.json").read_text("utf-8-sig"))
    return {"passed": value == {"count": 2, "total": 19.5}, "actual": value}


async def main(root, samples_only=False):
    root = root.resolve()
    assert root.is_relative_to(Path.cwd() / "development/records/evals")
    root.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for source in Path("src").rglob("*"):
        if source.is_file() and "__pycache__" not in source.parts:
            target = root / "tested-source" / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            hashes[source.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    save(root, "source-sha256.json", hashes)
    results = {"archive_probe": [] if samples_only else await archive_probe(root)}
    results["samples"] = [
        await sample(
            root,
            "data",
            "按id去重input.csv的账目，每个id只计算一次。按category合计amount，写totals.csv，"
            "列为category,total。按需核对并登记交付，不修改input.csv。",
            prepare_data,
            check_data,
        ),
        await sample(
            root,
            "browser-recovery",
            "打开工作区index.html。先尝试调用window.InvoiceBook.listRows()取账单，"
            "如果失败，自行找出页面已有的有效接口，再用浏览器读取实际账单。写total.json，"
            "只包含count和total两个数字字段，并登记交付。不要改网页来迎合错误的接口。",
            prepare_browser,
            check_browser,
        ),
    ]
    save(root, "summary.json", results)
    save(
        root,
        "source-integrity.json",
        {
            "changed": [
                path for path, digest in hashes.items() if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
            ],
        },
    )
    print(
        json.dumps(
            {
                "samples": [
                    {"case": r["case"], "status": r["run"]["status"], "independent": r["independent"]}
                    for r in results["samples"]
                ]
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--samples-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.record, args.samples_only))
