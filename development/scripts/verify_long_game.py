"""Independent actual clicks for this batch's generated STORY-array visual novel.

Reads game state for assertions only; never changes story/state or forces an ending.
An incompatible generated game must get a new adapter, not a fabricated pass.
"""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from verify_frontend import LocalServer


def routes(nodes, start="start"):
    table = {node["id"]: node for node in nodes}
    result = []

    def walk(node_id, path, choices):
        if node_id in path:
            raise ValueError("Story contains a cycle; this adapter needs explicit cycle handling")
        node = table[node_id]
        path = [*path, node_id]
        if node.get("ending"):
            result.append(
                {
                    "nodes": path,
                    "choices": choices,
                    "ending": node["ending"],
                    "text_characters": sum(len(table[item].get("text", "")) for item in path),
                }
            )
        elif node.get("choices"):
            for index, choice in enumerate(node["choices"]):
                walk(choice["next"], path, [*choices, index])
        elif isinstance(node.get("next"), str):
            walk(node["next"], path, choices)
        else:
            raise ValueError(f"Non-ending dead end or unsupported routing: {node_id}")

    walk(start, [], [])
    return result


async def play_object_route(browser, url, node_table, route, width, output, route_index):
    context = await browser.new_context(
        viewport={"width": width, "height": 844 if width == 390 else 820},
        is_mobile=width == 390,
        has_touch=width == 390,
    )
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    await page.add_init_script("""window.__audioProbe={contexts:0,oscillators:0};
        if(window.AudioContext){const Base=window.AudioContext;window.AudioContext=class extends Base{
        constructor(...args){super(...args);window.__audioProbe.contexts++}
        createOscillator(...args){window.__audioProbe.oscillators++;return super.createOscillator(...args)}}} """)

    async def act(selector):
        locator = page.locator(selector)
        if width == 390:
            await locator.tap()
        else:
            await locator.click()

    result = {
        "width": width,
        "route": route_index,
        "expected": route,
        "passed": False,
        "input": "actual_touch" if width == 390 else "mouse_and_keyboard",
        "checks": [],
    }
    try:
        await page.goto(url)
        await act("#btn-start")
        await page.locator("#game-screen").wait_for(state="visible", timeout=4000)
        choice_index, saved = 0, False
        for step, node_id in enumerate(route["nodes"]):
            node = node_table[node_id]
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if node.get("choices") or node.get("ending"):
                # Real typewriter callback creates choices/end overlay: wait, never force it.
                selector = "#choices" if node.get("choices") else "#btn-end-restart"
                await page.locator(selector).wait_for(state="visible", timeout=20000)
                assert await page.locator("#text-content").inner_text() == node.get("text", "")
                if node.get("ending"):
                    result["actual_ending"] = node_id
                    break
                if width == 390:
                    await page.locator(".choice-btn").nth(route["choices"][choice_index]).tap()
                else:
                    await page.locator(".choice-btn").nth(route["choices"][choice_index]).click()
                choice_index += 1
                continue
            if await page.locator("#text-content").inner_text() != node.get("text", ""):
                await act("#text-box")
            await page.wait_for_function(
                "text=>document.querySelector('#text-content').textContent===text", arg=node.get("text", "")
            )
            await page.locator("#advance-indicator.show").wait_for(state="visible")
            assert await page.locator("#bg-layer svg").count() == 1
            if node.get("char"):
                assert await page.locator("#char-layer svg").count() == 1
                assert not await page.locator("#char-layer svg text").count(), "Placeholder portrait in actual scene"
            if step >= len(route["nodes"]) // 2 and not saved:
                await act("#btn-save")
                snapshot = await page.evaluate("JSON.parse(localStorage.getItem('vn_save_v1'))")
                assert snapshot["node"] == node_id
                await act("#text-box")
                await act("#btn-sound-game")
                await act("#btn-load-game")
                await page.wait_for_function(
                    "text=>document.querySelector('#text-content').textContent===text", arg=node.get("text", "")
                )
                await page.locator("#advance-indicator.show").wait_for(state="visible")
                assert await page.locator("#btn-sound-game").inner_text() == ("🔊" if snapshot["sound"] else "🔇")
                saved = True
                result["checks"].append("actual_save_load_body_and_sound")
                await page.screenshot(path=str(output / f"route-{width}-{route_index}-body.png"))
            if width == 1280 and step % 2:
                await page.keyboard.press("Space")
            else:
                await act("#text-box")
        assert saved and choice_index == len(route["choices"])
        result["audio_probe"] = await page.evaluate("window.__audioProbe")
        await page.screenshot(path=str(output / f"route-{width}-{route_index}-ending.png"))
        await act("#btn-end-restart")
        await page.locator("#title-screen").wait_for(state="visible")
        await act("#btn-start")
        await page.locator("#game-screen").wait_for(state="visible")
        await act("#btn-restart")
        await page.locator("#title-screen").wait_for(state="visible")
        assert not errors, errors
        result["checks"].extend(["reachable_ending", "restart", "no_horizontal_overflow", "actual_svg_assets"])
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        await page.screenshot(path=str(output / f"route-{width}-{route_index}-failure.png"))
    finally:
        result["page_errors"] = errors
        await context.close()
    return result


async def complete_normal_text(page, node):
    if await page.evaluate("state.typing"):
        await page.locator("#dialogue-box").click()
    await page.wait_for_function(
        "expected=>document.querySelector('#dialogue-text').textContent===expected", arg=node["text"]
    )


async def play_route(browser, url, node_table, route, width, output, route_index):
    context = await browser.new_context(viewport={"width": width, "height": 844 if width == 390 else 820})
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    result = {"width": width, "route": route_index, "expected": route, "passed": False, "checks": []}
    try:
        await page.goto(url)
        await page.locator("#btn-start").click()
        before = await page.evaluate("state.soundOn")
        await page.locator("#btn-sound").click()
        assert await page.evaluate("state.soundOn") != before
        await page.locator("#btn-sound").click()
        choice_index, saved = 0, False
        for step, node_id in enumerate(route["nodes"]):
            node = node_table[node_id]
            await page.wait_for_function("id=>state.nodeId===id", arg=node_id)
            assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if node.get("ending"):
                # Let typewriter callbacks run naturally for choices/endings.
                await page.locator("#ending-screen").wait_for(state="visible", timeout=20000)
                assert (await page.locator("#ending-title").inner_text()).strip()
                result["actual_ending"] = await page.locator("#ending-title").inner_text()
                break
            if node.get("choices"):
                await page.locator("#choices-panel").wait_for(state="visible", timeout=20000)
                await page.locator(".choice-btn").nth(route["choices"][choice_index]).click()
                choice_index += 1
                continue
            await complete_normal_text(page, node)
            if node.get("char"):
                await page.wait_for_function(
                    "(()=>{const i=document.querySelector('#char-portrait');return i.complete&&i.naturalWidth>0})()"
                )
            if step >= len(route["nodes"]) // 2 and not saved:
                snapshot = await page.evaluate(
                    "({id:state.nodeId,flags:state.flags,text:state.fullText,sound:state.soundOn})"
                )
                await page.locator("#btn-save").click()
                await page.keyboard.press("Space")
                await page.wait_for_function("id=>state.nodeId!==id", arg=node_id)
                await page.locator("#btn-sound").click()
                await page.locator("#btn-load").click()
                await page.wait_for_function("id=>state.nodeId===id", arg=node_id)
                await page.wait_for_function(
                    "text=>document.querySelector('#dialogue-text').textContent===text", arg=snapshot["text"]
                )
                await page.wait_for_function("state.typing===false")
                restored = await page.evaluate(
                    "({id:state.nodeId,flags:state.flags,text:state.fullText,sound:state.soundOn})"
                )
                assert restored == snapshot
                result["checks"].append("actual_save_load_body_flags_sound")
                saved = True
                await page.screenshot(path=str(output / f"route-{width}-{route_index}-body.png"))
            if step % 2:
                await page.keyboard.press("Space")
            else:
                await page.locator("#dialogue-box").click()
        assert saved and choice_index == len(route["choices"])
        await page.screenshot(path=str(output / f"route-{width}-{route_index}-ending.png"))
        await page.locator("#btn-ending-restart").click()
        await page.wait_for_function("state.nodeId==='start'")
        await page.locator("#btn-restart").click()
        await page.locator("#title-screen").wait_for(state="visible")
        await page.locator("#btn-start").click()
        await page.wait_for_function("state.nodeId==='start'")
        result["checks"].extend(
            ["mouse_and_keyboard", "reachable_ending", "restart", "no_horizontal_overflow", "loaded_portraits"]
        )
        assert not errors, errors
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        await page.screenshot(path=str(output / f"route-{width}-{route_index}-failure.png"))
    finally:
        result["page_errors"] = errors
        await context.close()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    source = args.workspace.resolve()
    root = source.parent / datetime.now().strftime("independent-%Y%m%d-%H%M%S")
    root.mkdir()
    fixture = root / "game-copy"
    shutil.copytree(source, fixture, ignore=shutil.ignore_patterns(".clearact", "browser-captures"))
    result = {
        "provider": "independent_actual_edge",
        "source": str(source),
        "passed": False,
        "source_hashes": {
            p.relative_to(fixture).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in fixture.rglob("*")
            if p.is_file()
        },
        "routes": [],
    }
    print("RECORDS:", root, flush=True)
    web = FastAPI()
    web.mount("/", StaticFiles(directory=fixture, html=True))
    try:
        async with LocalServer(web) as server, async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page()
                await page.goto(server.url)
                raw_story = await page.evaluate("window.STORY || (typeof STORY!=='undefined'?STORY:null)")
                if isinstance(raw_story, dict) and isinstance(raw_story.get("nodes"), dict):
                    nodes = [
                        dict(node, id=key, ending=key if node.get("end") else None)
                        for key, node in raw_story["nodes"].items()
                    ]
                    generated_routes = routes(nodes, raw_story.get("start", "start"))
                    raw_art = await page.evaluate("window.ART")
                    character_ids = sorted({n["char"] for n in nodes if n.get("char")})
                    art = {
                        "backgrounds": list(raw_art["bg"]),
                        "characters": {
                            name: [key for key in raw_art["char"] if key.startswith(name + "_")]
                            for name in character_ids
                        },
                    }
                    player = play_object_route
                    result["mobile_scope"] = "actual touch input in mobile browser context"
                elif isinstance(raw_story, list):
                    nodes = raw_story
                    generated_routes = routes(nodes)
                    art = await page.evaluate(
                        "({backgrounds:Object.keys(BG),characters:Object.fromEntries(Object.entries(CHAR).map(([k,v])=>[k,Object.keys(v)]))})"
                    )
                    player = play_route
                    result["mobile_scope"] = "mobile viewport with mouse input"
                else:
                    script_paths = await page.locator("script[src]").evaluate_all(
                        "nodes=>nodes.map(n=>n.getAttribute('src'))"
                    )
                    missing = [
                        name
                        for name in script_paths
                        if not name.startswith(("http:", "https:")) and not (fixture / name).is_file()
                    ]
                    if missing:
                        raise ValueError(f"Generated game is incomplete: missing scripts {missing}; no routes tested")
                    raise ValueError("Unsupported generated story structure; adapter required")
                await page.close()
                result["story"] = {
                    "nodes": len(nodes),
                    "choice_nodes": sum(bool(node.get("choices")) for node in nodes),
                    "endings": sorted({str(node["ending"]) for node in nodes if node.get("ending")}),
                    "route_text_characters": [route["text_characters"] for route in generated_routes],
                    "duration": "text length only; no human ten-minute playtime measurement",
                }
                result["art"] = art
                for width in [1280, 390]:
                    for index, route in enumerate(generated_routes):
                        played = await player(
                            browser, server.url, {n["id"]: n for n in nodes}, route, width, root, index
                        )
                        result["routes"].append(played)
                        print("ROUTE", width, index, played["passed"], played.get("error", ""), flush=True)
                        (root / "independent.json").write_text(
                            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                source_text = "\n".join(
                    p.read_text(encoding="utf-8") for p in fixture.rglob("*") if p.suffix in {".html", ".js"}
                )
                result["required_checks"] = {
                    "all_reachable_routes": all(route["passed"] for route in result["routes"]),
                    "at_least_three_choice_points": result["story"]["choice_nodes"] >= 3,
                    "at_least_two_endings": len(result["story"]["endings"]) >= 2,
                    "three_backgrounds": len(art["backgrounds"]) >= 3,
                    "three_characters_with_two_expressions": len(art["characters"]) >= 3
                    and all(len(v) >= 2 for v in art["characters"].values()),
                    "audio_implementation_present": bool(
                        re.search(r"AudioContext|webkitAudioContext|new\s+Audio\s*\(|<audio", source_text)
                    ),
                }
                result["audio_scope"] = (
                    "Audio API/source presence plus real toggle/state checks; not a claim of listening verification"
                )
                result["passed"] = all(result["required_checks"].values())
            finally:
                await browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (root / "independent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("INDEPENDENT", result["passed"], result.get("required_checks"), result.get("error", ""), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
