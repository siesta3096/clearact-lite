"""Actual Edge UI; protocol test double, never counted as a real model result."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, expect
from verify_frontend import LocalServer, send, state, wait_finished

from clearact_lite.core.contracts import Message, ModelReply, ToolCall, Usage
from clearact_lite.gateway.server import create_app
from clearact_lite.settings import Settings


class LongProvider:
    parent = 0
    child = 0

    async def complete(self, messages, tools, on_delta):
        is_child = any("You are a delegated child worker" in m.content for m in messages if m.role == "user")
        if is_child:
            self.child += 1
            action = ("file_write", {"path": "chapter.txt", "text": "子代理写入"}) if self.child == 1 else None
            if not action:
                return ModelReply(Message("assistant", "子代理完成，主代理需整合"), "stop", Usage(100, 10))
            call_id = "child-file"
        else:
            self.parent += 1
            if self.parent == 1:
                return ModelReply(Message("assistant", "未完成片段"), "length", Usage(100, 10), {"induced": True})
            actions = [
                ("file_write", {"path": "fixture.txt", "text": "归档样例" * 1000}),
                (
                    "task_plan",
                    {
                        "interfaces": "chapter.txt 为 UTF-8 章节文本",
                        "tasks": [
                            {
                                "id": "chapter",
                                "instruction": "写一个章节",
                                "files": [{"path": "chapter.txt", "purpose": "独立章节"}],
                            }
                        ],
                    },
                ),
                ("agent_task", {"task_id": "chapter"}),
                (
                    "file_edit",
                    {
                        "path": "chapter.txt",
                        "mode": "append",
                        "text": "，主代理整合",
                        "expected_bytes": len("子代理写入".encode()),
                    },
                ),
                ("artifact_register", {"path": "chapter.txt"}),
            ]
            index = self.parent - 2
            if index >= len(actions):
                return ModelReply(Message("assistant", "已整合，记录完整保留"), "stop", Usage(100, 10))
            action = actions[index]
            call_id = f"parent-{self.parent}"
        # Deliberately simulate near-capacity reported input. These are protocol
        # fixture values, not a claim about an actual model/tokenizer.
        usage = Usage(
            24000 if not is_child and self.parent >= 3 else 100, 4000 if not is_child and self.parent == 2 else 10
        )
        return ModelReply(Message("assistant", tool_calls=(ToolCall(call_id, *action),)), "tool_calls", usage)


async def main():
    root = Path(__file__).resolve().parents[1] / "records/evals" / datetime.now().strftime("long-ui-%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    settings = Settings(root / "workspace", max_output_tokens=4096)
    app = create_app(settings, root / "settings.json", lambda _: LongProvider())
    result = {"provider": "protocol_test_double", "passed": False, "checks": []}
    print("RECORDS:", root, flush=True)
    async with LocalServer(app) as server, async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1500, "height": 1000}, accept_downloads=True)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(server.url)
            await expect(page.locator("#send")).to_be_enabled()
            await page.locator("#settings-trigger").click()
            await page.locator("#settings-form summary").click()
            await page.locator('input[name="compact_threshold"]').fill("0.85")
            await page.locator("#save-settings").click()
            await expect(page.locator("#settings-dialog")).not_to_be_visible()
            current = await state(page)
            assert current["settings"]["compact_threshold"] == 0.85 and current["settings"]["auto_compact"] is True
            result["checks"].append("settings_roundtrip_with_boolean_and_threshold")
            await send(page, "请按文件计划调用子代理，分段整合并交付章节")
            await wait_finished(page)
            current = await state(page)
            assert len(current["branches"]) == 1
            branch = current["branches"][0]["id"]
            report = await page.evaluate("id=>fetch('/api/branches/'+id).then(r=>r.json())", branch)
            assert report["runs"][-1]["status"] == "finished"
            assert report["runs"][-1]["model_calls"] == 9
            assert any(e["kind"] == "context.compacted" for e in report["events"])
            assert any(e["kind"] == "capacity.continuation" for e in report["events"])
            assert (root / "workspace/chapter.txt").read_text(encoding="utf-8") == "子代理写入，主代理整合"
            result["checks"].append("capacity_continue_compact_delegate_and_integrate")
            await page.locator("#inspector-toggle").click()
            await expect(page.get_by_text("子代理结果", exact=False).first).to_be_visible()
            child = report["children"][0]
            child_record = await page.evaluate("id=>fetch('/api/branches/'+id).then(r=>r.json())", child["branch_id"])
            assert any(e["kind"] == "message" and e["payload"]["role"] == "assistant" for e in child_record["events"])
            await page.screenshot(path=str(root / "plan-and-children.png"))
            result["checks"].append("child_history_access_and_single_parent_topic")
            await page.locator("#tab-changes").click()
            child_change = report["child_changes"][0]
            parent_change = [
                e for e in report["events"] if e["kind"] == "file.written" and e["payload"]["path"] == "chapter.txt"
            ][-1]
            for change in [parent_change, child_change]:
                detail = page.locator(f'.record[data-event-id="{change["id"]}"]')
                await detail.locator("summary").click()
                async with page.expect_response(lambda response: response.url.endswith("/restore")) as response:
                    await detail.locator(".restore-button").click()
                assert (await response.value).status == 200
            assert not (root / "workspace/chapter.txt").exists()
            result["checks"].append("parent_and_child_change_preview_and_restore")
            result["page_errors"] = errors
            assert not errors, errors
            result["passed"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            await page.screenshot(path=str(root / "failure.png"))
            raise
        finally:
            (root / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.close()
    print("LONG_UI:", result["passed"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
