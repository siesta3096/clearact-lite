"""Native loss/restart and aiming inputs on an already frozen FPS copy; never sets game state."""

import argparse
import asyncio
import json
import math
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from verify_frontend import LocalServer
from verify_tactical_fps import SNAPSHOT, digest_files


async def main(args):
    workspace, output = args.workspace.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = digest_files(workspace)
    data = {
        "scope": "Native keyboard/mouse only; bot aims using read-only enemy positions, including occluded enemies. "
        "This is not human play, fair combat balance or line-of-sight acceptance. No health/position/round setters.",
        "snapshots": {}, "checks": {}, "page_errors": [],
    }

    def save():
        (output / "combat.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    app = FastAPI()
    app.mount("/", StaticFiles(directory=workspace, html=True))
    try:
        async with LocalServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda e: data["page_errors"].append(str(e)))
                await page.goto(server.url, wait_until="load")
                await page.locator("#btn-start").click()
                await page.keyboard.down("w")
                await page.wait_for_timeout(700)
                await page.keyboard.up("w")
                data["snapshots"]["started"] = await page.evaluate(SNAPSHOT)
                await page.wait_for_function("window.Game?.Round?.state==='lost'", timeout=40000)
                data["snapshots"]["natural_loss"] = await page.evaluate(SNAPSHOT)
                data["checks"]["natural_loss"] = data["snapshots"]["natural_loss"]["health"] == 0
                await page.keyboard.press("Escape", delay=100)
                await page.locator("#btn-restart").click(timeout=6000)
                await page.wait_for_timeout(300)
                state = await page.evaluate(SNAPSHOT)
                data["snapshots"]["restarted"] = state
                data["checks"]["native_restart"] = state["health"] == 100 and state["round"]["phase"] == "active"
                save()
                await page.keyboard.press("2", delay=100)
                cursor = [720.0, 450.0]
                await page.mouse.move(*cursor)
                await page.wait_for_timeout(60)
                data["aiming_trace"] = []
                for step in range(45):
                    state = await page.evaluate(SNAPSHOT)
                    if state["round"]["phase"] != "active":
                        break
                    targets = [e for e in state["enemies"] if e.get("alive") and e.get("position")]
                    if not targets:
                        break
                    target = targets[0]["position"]
                    pos = state["position"]
                    dx, dy, dz = target[0] - pos[0], target[1] + 0.5 - pos[1], target[2] - pos[2]
                    yaw, pitch = math.atan2(-dx, -dz), math.atan2(dy, math.hypot(dx, dz))
                    rotation = state["camera"]["rotation"]
                    difference = (rotation[1] - yaw + math.pi) % (2 * math.pi) - math.pi
                    # Game's documented mouse scale is .002 radians/pixel. Actual camera is observed after input.
                    x, y = difference / 0.002, (rotation[0] - pitch) / 0.002
                    cursor[0] += x
                    cursor[1] += y
                    await page.mouse.move(*cursor, steps=4)
                    await page.mouse.down()
                    await page.wait_for_timeout(200)
                    await page.mouse.up()
                    after = await page.evaluate(SNAPSHOT)
                    data["aiming_trace"].append({"step": step, "before": state, "after": after})
                    if after["ammo"] == 0 and after["reserve"] > 0:
                        await page.keyboard.press("r", delay=100)
                        await page.wait_for_timeout(2700)
                final = await page.evaluate(SNAPSHOT)
                data["snapshots"]["after_combat"] = final
                data["checks"]["native_shots_damaged_enemy"] = any(
                    any(e.get("health", 100) < 100 for e in row["after"]["enemies"])
                    for row in data["aiming_trace"]
                )
                data["checks"]["native_win"] = final["round"]["phase"] == "won"
                await page.screenshot(path=str(output / "after-combat.png"))
            finally:
                await browser.close()
    except Exception as exc:
        data["evaluation_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        data["source_unchanged"] = digest_files(workspace) == before
        save()
        print(json.dumps({"checks": data["checks"], "error": data.get("evaluation_error")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
