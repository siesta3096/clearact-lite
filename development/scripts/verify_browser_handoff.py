"""Isolated real local-model exercises; independent Edge checks, no production patches during runs."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from verify_capabilities import credentials

from clearact_lite.bootstrap import build_application
from clearact_lite.providers.openai_compatible import OpenAICompatible


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class MeasuredProvider:
    def __init__(self, settings):
        self.provider = OpenAICompatible(settings)
        self.calls = []

    async def complete(self, messages, tools, on_delta):
        value = await self.provider.complete(messages, tools, on_delta)
        self.calls.append(asdict(value.usage))
        return value


async def independent(workspace, target, mode):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto((workspace / target).as_uri())
            if mode == "startup":
                await page.locator("#quantity").fill("3")
                await page.locator("#calculate").click()
                result = await page.locator("#total").inner_text()
                return {"passed": result == "37.50" and not errors, "actual_total": result, "page_errors": errors}
            samples = await page.evaluate("""() => [window.computeTotal([{price:12.5,quantity:3}]),
                window.computeTotal([{price:0.1,quantity:3},{price:0.2,quantity:1}])]""")
            await page.locator("#quantity").fill("4")
            await page.locator("#calculate").click()
            result = await page.locator("#total").inner_text()
            return {
                "passed": samples == [37.5, 0.5] and result == "50.00" and not errors,
                "samples": samples,
                "actual_total": result,
                "page_errors": errors,
            }
        except Exception as exc:
            return {"passed": False, "type": type(exc).__name__, "detail": str(exc), "page_errors": errors}
        finally:
            await page.screenshot(path=str(workspace.parent / "independent.png"), full_page=True)
            await browser.close()


async def exercise(root, mode):
    folder = root / mode
    workspace = folder / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    settings = replace(credentials(workspace), run_timeout_seconds=360, request_timeout_seconds=180)
    public = {key: value for key, value in asdict(settings).items() if key not in {"api_key", "workspace"}}
    save(folder / "settings.json", public)
    provider = MeasuredProvider(settings)
    app = build_application(settings, provider)
    if mode == "startup":
        (workspace / "index.html").write_text(
            """<!doctype html><meta charset="utf-8"><title>本地计价</title>
<label>数量<input id="quantity" type="number" value="1"></label><button id="calculate">计算</button>
<output id="total">加载中</output><script src="engine.js"></script><script>
document.querySelector('#calculate').onclick=()=>{document.querySelector('#total').textContent=
window.computeTotal([{price:12.5,quantity:Number(document.querySelector('#quantity').value)}]).toFixed(2)};
document.querySelector('#calculate').click();</script>""",
            encoding="utf-8",
        )
        prompt = (
            "工作区已有一个计价页面index.html，但打开后停在加载中。先用浏览器打开它，依据原始错误回执定位，"
            "修复缺失的运行能力，保留原页面输入和按钮。单价12.5，数量可修改，点击计算显示两位小数总价。"
            "不要用占位常量掩盖错误。完成后用真实页面操作验证数量3显示37.50，登记交付文件并如实说明结果。"
        )
    else:
        prompt = (
            "创建一个简单的计价页面以验证模块交接。请用task_plan分成两个实现任务并逐个用agent_task执行："
            "engine任务只写api.js，提供window.computeTotal(items)，items是price、quantity数组，返回总价数字，"
            "按分四舍五入；ui任务只写index.html，引用api.js，数量输入#quantity、计算按钮#calculate、"
            "结果#total，单价12.5，结果两位小数。用计划的可选contracts声明engine为提供者、ui为消费者。"
            "子代理用task_checkpoint的interfaces和unresolved交回真实接口与待办；主代理依据交接集成验证。"
            "不要生成额外功能。主代理实际操作数量4得到50.00后登记文件交付，如实报告尚未验证的内容。"
        )
    (folder / "goal.txt").write_text(prompt, encoding="utf-8")
    branch = app.fork()
    try:
        result = await app.run(prompt, branch)
        save(folder / "run.json", asdict(result))
        report = app.report(branch)
        save(folder / "report.json", report)
        for child in report["children"]:
            save(folder / ("child-" + child["branch_id"] + ".json"), app.report(child["branch_id"]))
        events = app.events(branch)
        observed = {
            "startup_errors_recorded": sum(e.kind == "browser.diagnostic" for e in events),
            "structured_handoffs": sum(
                bool(child.get("handoff", {}).get("interfaces")) for child in report["children"]
            ),
            "model_claim_is_independent_acceptance": False,
        }
        await app.aclose()
        verified = await independent(workspace, "index.html", mode)
        feature_used = (
            observed["startup_errors_recorded"] > 0 if mode == "startup" else observed["structured_handoffs"] >= 1
        )
        summary = {
            "mode": mode,
            "status": result.status,
            "model_calls": result.model_calls,
            "usage": asdict(result.usage),
            "elapsed_seconds": result.elapsed_seconds,
            "observed": observed,
            "independent": verified,
            "passed": result.status == "finished" and verified["passed"] and feature_used,
            "full_large_task_validated": False,
        }
        save(folder / "acceptance.json", summary)
        return summary
    finally:
        save(folder / "calls.json", provider.calls)
        if app.store is not None:
            try:
                await app.aclose()
            except Exception:
                pass


async def main(record):
    project = Path.cwd().resolve()
    record = record.resolve()
    assert record.is_relative_to(project / "development/records/evals")
    record.mkdir(parents=True, exist_ok=True)
    source = {
        str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (project / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    save(record / "source-sha256.json", source)
    settings = credentials(record)
    async with httpx.AsyncClient(timeout=15) as client:
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        response = await client.get(settings.base_url.rstrip("/") + "/models", headers=headers)
        response.raise_for_status()
        assert settings.model in [model["id"] for model in response.json()["data"]]
    results = []
    for mode in ["startup", "handoff"]:
        results.append(await exercise(record, mode))
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        save(record / "summary.json", results)
    assert all(hashlib.sha256((project / path).read_bytes()).hexdigest() == sha for path, sha in source.items())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    asyncio.run(main(parser.parse_args().record))
