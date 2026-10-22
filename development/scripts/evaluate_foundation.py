"""Small real local-model smoke only; saves success/failure and original events in isolation."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from clearact_lite.bootstrap import build_application
from clearact_lite.settings import Settings


async def evaluate(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    app = build_application(Settings.from_env(root))
    try:
        first = await app.run(
            "在当前工作目录创建 proof.txt，内容必须精确为 clearact-lite-ok，不含换行。"
            "读取核对后登记交付此文件，简短报告结果。"
        )
        report = app.report(first.branch_id)
        read_calls = [
            e for e in report["events"] if e["kind"] == "tool.started" and e["payload"]["name"] == "file_read"
        ]
        delivered = any(a["path"] == "proof.txt" for a in report["artifacts"])
        report["evaluation"] = {
            "scope": "small_file_smoke_only",
            "model": app.settings.model,
            "base_url": app.settings.base_url,
            "reasoning_effort": app.settings.reasoning_effort,
            "max_output_tokens": app.settings.max_output_tokens,
            "temperature": app.settings.temperature,
            "content_exact": (root / "proof.txt").read_bytes() == b"clearact-lite-ok"
            if (root / "proof.txt").exists()
            else False,
            "large_task_verified": False,
            "read_tool_used": bool(read_calls),
            "file_registered": delivered,
        }
        report["evaluation"]["passed"] = (
            first.status == "finished" and report["evaluation"]["content_exact"] and bool(read_calls) and delivered
        )
        (root / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"root": str(root), "runs": report["runs"], "evaluation": report["evaluation"]},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        app.close()


if __name__ == "__main__":
    path = (
        Path(__file__).resolve().parents[1] / "records" / "evals" / datetime.now().strftime("foundation-%Y%m%d-%H%M%S")
    )
    asyncio.run(evaluate(path))
