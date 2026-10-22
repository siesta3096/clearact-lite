"""Real Edge UI verification. Scripted protocol checks and real-model checks are reported separately."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from clearact_lite.core.contracts import Message, ModelReply, ToolCall, Usage
from clearact_lite.gateway.server import create_app
from clearact_lite.settings import Settings


class BrowserTestProvider:
    async def complete(self, messages, tools, on_delta):
        await asyncio.sleep(0.05)
        last_user = max(i for i, m in enumerate(messages) if m.role == "user")
        prompt = messages[last_user].content
        if "等待停止" in prompt:
            on_delta("正在等待用户停止")
            await asyncio.Event().wait()
        step = sum(m.role == "tool" for m in messages[last_user:])
        actions = [
            ("file_write", {"path": "result.txt", "text": "修改后的内容" if "修改" in prompt else "初始内容"}),
            ("file_read", {"path": "result.txt"}),
            ("artifact_register", {"path": "result.txt"}),
        ]
        if step < len(actions):
            name, arguments = actions[step]
            return ModelReply(
                Message("assistant", tool_calls=(ToolCall(uuid4().hex, name, arguments),)), "tool_calls", Usage(100, 10)
            )
        on_delta("文件已写入，")
        await asyncio.sleep(0.08)
        on_delta("读取核对并登记交付。")
        return ModelReply(Message("assistant", "文件已写入，读取核对并登记交付。"), "stop", Usage(100, 10))


def local_settings(root: Path, reference: Path | None) -> Settings:
    settings = Settings.from_env(root)
    if reference is None:
        return settings
    config = json.loads(reference.read_text(encoding="utf-8-sig"))
    values = dict(os.environ)
    dotenv = reference.with_name(".env")
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    def expand(value):
        return re.sub(r"\$\{([A-Z0-9_]+)(?::([^}]*))?\}", lambda m: values.get(m[1], m[2] or ""), value)

    from urllib.parse import urlsplit

    matches = [
        p
        for p in config["profiles"].values()
        if urlsplit(expand(p.get("baseUrl", ""))).hostname in {"localhost", "127.0.0.1"}
        and urlsplit(expand(p.get("baseUrl", ""))).port == 1235
    ]
    if len(matches) != 1:
        raise ValueError("No unique matching localhost profile; no model evaluation started")
    profile = matches[0]
    from dataclasses import replace

    return replace(settings, api_key=expand(profile.get("apiKey", "")) or values.get(profile.get("apiKeyEnv", ""), ""))


class LocalServer:
    def __init__(self, app):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="on", ws="none", timeout_graceful_shutdown=5)
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    async def __aenter__(self):
        self.thread.start()
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        if (await client.get(self.url)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *args):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 15)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Owned test gateway did not stop")


async def state(page):
    return await page.evaluate("fetch('/api/state').then(r => r.json())")


async def wait_finished(page, timeout=120000):
    async with asyncio.timeout(timeout / 1000):
        while (await state(page))["active_branch"]:
            await asyncio.sleep(0.1)
    await expect(page.locator("#stage")).to_be_hidden(timeout=5000)
    await expect(page.locator("#send")).to_be_enabled(timeout=5000)


async def send(page, prompt):
    await page.locator("#goal").fill(prompt)
    await page.locator("#send").click()
    await expect(page.locator("#conversation")).to_be_visible()


async def verify_scripted(root: Path, browser):
    workspace = root / "scripted-workspace"
    config = root / "scripted-settings.json"
    app = create_app(Settings(workspace), config, lambda _: BrowserTestProvider())
    result = {"provider": "scripted_protocol_test_double", "checks": [], "passed": False}
    async with LocalServer(app) as server:
        context = await browser.new_context(viewport={"width": 1440, "height": 960}, accept_downloads=True)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(server.url)
            await expect(page.locator("#connection span")).to_have_text("本地网关已连接")
            await page.screenshot(path=str(root / "desktop-welcome.png"))
            result["checks"].append("desktop_welcome")
            await page.locator("#settings-trigger").click()
            await expect(page.locator("#settings-dialog")).to_be_visible()
            await expect(page.locator('input[name="model"]')).to_have_value("qwen/qwen3.8-27b")
            await page.locator("#save-settings").click()
            await expect(page.locator("#settings-dialog")).to_be_hidden()
            assert config.exists()
            result["checks"].append("settings_save")
            await send(page, "创建文件并交付")
            await wait_finished(page)
            await expect(page.locator("#run-summary")).to_contain_text("本轮已结束")
            assert (workspace / "result.txt").read_text(encoding="utf-8") == "初始内容"
            await expect(page.locator(".delivery b")).to_have_text("result.txt")
            async with page.expect_download() as downloaded:
                await page.locator(".delivery button").click()
            download = await downloaded.value
            await download.save_as(root / "downloaded-result.txt")
            assert (root / "downloaded-result.txt").read_text(encoding="utf-8") == "初始内容"
            result["checks"].append("run_stream_and_download")
            await send(page, "修改文件并交付")
            await wait_finished(page)
            assert (workspace / "result.txt").read_text(encoding="utf-8") == "修改后的内容"
            await page.locator("#inspector-toggle").click()
            await page.locator("#tab-changes").click()
            changes = page.locator("#change-records details")
            await changes.nth(1).locator("summary").click()
            await expect(changes.nth(1).locator("pre")).to_contain_text("初始内容")
            await page.screenshot(path=str(root / "desktop-changes.png"))
            await changes.nth(1).locator(".restore-button").click()
            await expect(page.locator("#notice")).to_contain_text("已恢复文件")
            assert (workspace / "result.txt").read_text(encoding="utf-8") == "初始内容"
            result["checks"].append("followup_diff_and_restore")
            await page.locator("#fork-topic").click()
            await expect(page.locator("#topic-count")).to_have_text("2")
            await expect(page.locator("#topic-subtitle")).to_contain_text("历史分支")
            await page.reload()
            await expect(page.locator("#topic-count")).to_have_text("2")
            result["checks"].append("fork_and_reload_history")
            await send(page, "等待停止")
            await expect(page.locator("#stage")).to_be_visible()
            await page.locator("#stop-run").click()
            await wait_finished(page)
            await expect(page.locator("#run-summary")).to_contain_text("已停止")
            result["checks"].append("user_stop")
            await page.locator("#theme-toggle").click()
            await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            await page.screenshot(path=str(root / "desktop-dark.png"))
            result["checks"].append("dark_theme")
            result["page_errors"] = errors
            assert not errors
            mobile = await browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, device_scale_factor=1
            )
            mobile_page = await mobile.new_page()
            await mobile_page.goto(server.url)
            await expect(mobile_page.locator("#send")).to_be_enabled()
            await mobile_page.locator("#menu-toggle").click()
            await mobile_page.locator("#new-topic").click()
            await expect(mobile_page.locator("#welcome")).to_be_visible()
            assert await mobile_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await mobile_page.screenshot(path=str(root / "mobile-welcome.png"))
            await mobile_page.locator("#choose-workspace").click()
            await expect(mobile_page.locator("#workspace-dialog")).to_be_visible()
            await mobile_page.locator("#workspace-input").fill(str(root / "mobile-workspace"))
            await mobile_page.locator('#workspace-form button[type="submit"]').click()
            await expect(mobile_page.locator("#workspace-dialog")).to_be_hidden()
            assert (await state(mobile_page))["settings"]["workspace"] == str((root / "mobile-workspace").resolve())
            result["checks"].append("mobile_layout_and_workspace_switch")
            await mobile.close()
            result["passed"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            if not page.is_closed():
                await page.screenshot(path=str(root / "scripted-failure.png"))
            raise
        finally:
            (root / "scripted-ui.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await context.close()


async def verify_real(root: Path, browser, reference: Path | None):
    workspace = root / "real-workspace"
    workspace.mkdir()
    inputs = [
        [{"category": "A", "amount": 10}, {"category": "B", "amount": 7}],
        [{"category": "A", "amount": 2.5}, {"category": "C", "amount": 4}],
        [{"category": "B", "amount": 3}, {"category": "C", "amount": 6}],
        [{"category": "A", "amount": 2.5}, {"category": "B", "amount": -2}],
    ]
    original = {}
    for index, rows in enumerate(inputs):
        path = workspace / f"input_{index}.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        original[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    settings = local_settings(workspace, reference)
    app = create_app(settings, root / "real-settings.json")
    result = {
        "provider": "real_local_model",
        "model": settings.model,
        "passed": False,
        "large_task_equivalent_verified": False,
    }
    async with LocalServer(app) as server:
        context = await browser.new_context(viewport={"width": 1440, "height": 960}, accept_downloads=True)
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(server.url)
            await expect(page.locator("#send")).to_be_enabled()
            print("REAL_UI: starting multi-file task", flush=True)
            await send(
                page,
                "读取 input_0.json、input_1.json、input_2.json、input_3.json，按 category 汇总 amount。"
                "只新增 summary.json、report.md、dashboard.html 三个文件，保持四个输入文件不变。"
                "summary.json 必须是类别名对应数值以及 total 对应总数的 JSON 对象。"
                "report.md 用中文说明每类合计和总计。dashboard.html 是可离线打开的完整网页，"
                "在表格里展示同样结果，不使用外部资源。生成后读取核对三个输出并登记交付，简短回复。",
            )
            await wait_finished(page, 180000)
            branch = (await state(page))["branches"][0]["id"]
            report = await page.evaluate("id => fetch('/api/branches/'+id).then(r=>r.json())", branch)
            (root / "real-initial-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            assert report["runs"][-1]["status"] == "finished", report["runs"][-1]
            assert json.loads((workspace / "summary.json").read_text(encoding="utf-8")) == {
                "A": 15,
                "B": 8,
                "C": 10,
                "total": 33,
            }
            for name, value in original.items():
                assert hashlib.sha256((workspace / name).read_bytes()).hexdigest() == value
            assert {a["path"] for a in report["artifacts"]} == {"summary.json", "report.md", "dashboard.html"}
            await expect(page.locator(".delivery")).to_have_count(3)
            await page.locator("#inspector-toggle").click()
            await page.screenshot(path=str(root / "real-task.png"))
            print("REAL_UI: initial files checked; starting follow-up", flush=True)
            await send(
                page,
                "继续：只在 report.md 末尾追加一行“复核完成”，保留原正文和其他所有文件不变，"
                "读取核对后重新登记交付 report.md。",
            )
            await wait_finished(page, 120000)
            report = await page.evaluate("id => fetch('/api/branches/'+id).then(r=>r.json())", branch)
            (root / "real-followup-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            assert report["runs"][-1]["status"] == "finished"
            assert (workspace / "report.md").read_text(encoding="utf-8").rstrip().endswith("复核完成")
            result["runs"] = report["runs"]
            result["page_errors"] = errors
            preview = await context.new_page()
            await preview.goto((workspace / "dashboard.html").as_uri())
            await expect(preview.locator("table")).to_be_visible()
            body = await preview.locator("body").inner_text()
            assert all(value in body for value in ["15", "8", "10", "33"])
            await preview.screenshot(path=str(root / "real-dashboard.png"))
            assert not errors
            result["passed"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            await page.screenshot(path=str(root / "real-failure.png"))
            raise
        finally:
            (root / "real-ui.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await context.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--reference-config", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "records/evals" / datetime.now().strftime("web-ui-%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    print(f"UI_RECORDS: {root}", flush=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            if args.real_model:
                await verify_real(root, browser, args.reference_config)
            else:
                await verify_scripted(root, browser)
        finally:
            await browser.close()
    print("UI_CHECKS: passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
