"""Real local-model three-module handoff exercise; independent checks after the run."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright
from verify_browser_handoff import MeasuredProvider, save
from verify_capabilities import credentials

from clearact_lite.bootstrap import build_application

GOAL = """创建一个离线可用的购物计价页面，验证多模块分工与交接。只实现以下范围：
1. catalog.js提供window.CATALOG数组：{id:'book',label:'书',priceCents:1250}、
   {id:'card',label:'卡片',priceCents:10}。
2. quote.js提供window.computeQuote(lines)，每行{priceCents,quantity}，均为非负整数。
   返回整数分subtotalCents、discountCents、taxCents、shippingCents、totalCents。
   商品小计是各行单价乘数量之和；总数量>=3时，小计打九折，优惠金额按分四舍五入；
   税是优惠后商品金额的13%，按分四舍五入；优惠后商品金额>=5000分免运费，否则599分。
   空购物车或数量全零时，五个返回值均为0。总额=小计-优惠+税+运费。
3. index.html负责组装和DOM交互，依次加载两个模块。商品选择#product（值book/card）、
   数量输入#quantity、添加按钮#add、购物行容器#lines、清空按钮#reset；
   输出#subtotal、#discount、#tax、#shipping、#total显示元，两位小数，不含单位或标签。
   可多次添加商品，清空后金额全部为0.00，不使用网络依赖。
用task_plan列出三个文件各自职责、接口提供者/消费者和依赖，再逐个agent_task委派三个实现任务。
每个任务只写自己的一个文件，UI任务负责最终入口组装。
为重现显式工具选择场景，每个子任务tools只列file_read/file_write/file_edit，
不要把history_read/task_checkpoint写进tools清单；它们应由运行时提供。
在自然交接时用task_checkpoint记录实际接口与未完成项；后继按需取回交接原文。
主代理依据现有证据决定必要检查，登记三个交付文件，如实说明已验证和未验证范围。
"""


def source_hashes(project):
    return {
        str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (project / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


async def independent(workspace, folder):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        result = {"passed": False, "page_errors": errors}
        try:
            await page.goto((workspace / "index.html").as_uri())
            samples = await page.evaluate("""() => [[], [{priceCents:1250,quantity:1}],
                [{priceCents:1250,quantity:3}], [{priceCents:1250,quantity:5}],
                [{priceCents:10,quantity:3}], [{priceCents:1250,quantity:0}]]
                .map(lines => window.computeQuote(lines))""")
            fields = ["subtotalCents", "discountCents", "taxCents", "shippingCents", "totalCents"]
            expected = [
                [0, 0, 0, 0, 0],
                [1250, 0, 163, 599, 2012],
                [3750, 375, 439, 599, 4413],
                [6250, 625, 731, 0, 6356],
                [30, 3, 4, 599, 630],
                [0, 0, 0, 0, 0],
            ]
            result["function_samples"] = samples
            result["function_checks_passed"] = [
                [sample.get(field) for field in fields] == values
                for sample, values in zip(samples, expected, strict=True)
            ]
            await page.locator("#product").select_option("book")
            await page.locator("#quantity").fill("3")
            await page.locator("#add").click()
            result["native_add_outputs"] = [
                (await page.locator("#" + field).inner_text()).strip()
                for field in ["subtotal", "discount", "tax", "shipping", "total"]
            ]
            result["native_add_passed"] = result["native_add_outputs"] == [
                "37.50",
                "3.75",
                "4.39",
                "5.99",
                "44.13",
            ]
            result["native_line_visible"] = bool((await page.locator("#lines").inner_text()).strip())
            await page.locator("#reset").click()
            result["native_reset_outputs"] = [
                (await page.locator("#" + field).inner_text()).strip()
                for field in ["subtotal", "discount", "tax", "shipping", "total"]
            ]
            result["native_reset_passed"] = result["native_reset_outputs"] == ["0.00"] * 5
            result["passed"] = (
                all(result["function_checks_passed"])
                and result["native_add_passed"]
                and result["native_line_visible"]
                and result["native_reset_passed"]
                and not errors
            )
        except Exception as exc:
            result.update(error_type=type(exc).__name__, detail=str(exc))
        finally:
            await page.screenshot(path=str(folder / "independent.png"), full_page=True)
            await browser.close()
        return result


async def main(record):
    project = Path.cwd().resolve()
    record = record.resolve()
    if not record.is_relative_to(project / "development/records/evals"):
        raise ValueError("Record must be inside development/records/evals")
    record.mkdir(parents=True, exist_ok=False)
    workspace = record / "workspace"
    workspace.mkdir()
    settings = replace(credentials(workspace), run_timeout_seconds=600)
    if urlsplit(settings.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This exercise only uses the configured local model")
    save(
        record / "settings.json",
        {key: value for key, value in asdict(settings).items() if key not in {"api_key", "workspace"}},
    )
    source = source_hashes(project)
    save(record / "source-sha256.json", source)
    (record / "goal.txt").write_text(GOAL, encoding="utf-8")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
            response = await client.get(settings.base_url.rstrip("/") + "/models", headers=headers)
            response.raise_for_status()
            if settings.model not in [item["id"] for item in response.json()["data"]]:
                raise ValueError("Configured model is not listed")
    except Exception as exc:
        summary = {"status": "model_unavailable", "error_type": type(exc).__name__, "real_model_calls": 0}
        save(record / "summary.json", summary)
        print(json.dumps(summary), flush=True)
        return
    provider = MeasuredProvider(settings)
    app = build_application(settings, provider)
    branch = app.fork()
    try:
        result = await app.run(GOAL, branch)
        save(record / "run.json", asdict(result))
        report = app.report(branch)
        save(record / "report.json", report)
        assignments = []
        handoffs = []
        for child in report["children"]:
            child_branch = child["branch_id"]
            save(record / ("child-" + child_branch + ".json"), app.report(child_branch))
            assignments.extend(asdict(e) for e in app.events(child_branch) if e.kind == "task.assignment")
            handoffs.append(child.get("handoff", {}))
        save(record / "assignments.json", assignments)
        await app.aclose()
        verified = await independent(workspace, record)
        save(record / "independent.json", verified)
        baseline = {"history_read", "task_checkpoint"}
        restricted = bool(assignments) and all(
            isinstance(e["payload"]["task"].get("tools"), list)
            and not baseline.intersection(e["payload"]["task"]["tools"])
            and set(e["payload"]["effective_tools"]) == set(e["payload"]["task"]["tools"]) | baseline
            for e in assignments
        )
        observed = {
            "three_child_assignments": len(assignments) == 3,
            "baseline_despite_explicit_selection": restricted,
            "structured_interface_handoffs": sum(h.get("interface_source") == "checkpoint" for h in handoffs),
        }
        summary = {
            "status": result.status,
            "elapsed_seconds": result.elapsed_seconds,
            "model_calls": result.model_calls,
            "usage": asdict(result.usage),
            "observed": observed,
            "independent_passed": verified["passed"],
            "full_large_task_validated": False,
            "source_unchanged": source == source_hashes(project),
        }
        summary["passed"] = (
            result.status == "finished"
            and verified["passed"]
            and restricted
            and observed["three_child_assignments"]
            and observed["structured_interface_handoffs"] >= 2
            and summary["source_unchanged"]
        )
        save(record / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        save(record / "calls.json", provider.calls)
        await app.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    asyncio.run(main(parser.parse_args().record))
