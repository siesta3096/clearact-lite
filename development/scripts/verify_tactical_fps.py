"""Independent real-input FPS checks on a frozen copy; never repairs the generated game."""

import argparse
import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from verify_frontend import LocalServer

SNAPSHOT = """() => {
  const g=window.Game||{}, p=g.Player, w=p?.getWeapon?.()||g.Weapons?.current;
  const camera=g.Engine?.getCamera?.()||g.Core?.camera;
  const read=(fn)=>{try{return fn()}catch(e){return {read_error:String(e)}}};
  return {
    position:read(()=>p?.getPosition?.()?.toArray?.()||p?.position?.toArray?.()||null),
    health:read(()=>p?.getHealth?.()??p?.health??null),
    alive:read(()=>p?.isAlive?.()??(typeof p?.health==='number'?p.health>0:null)),
    ammo:read(()=>p?.getAmmo?.()??w?.getAmmo?.()??g.Weapons?.ammo??null),
    reserve:read(()=>w?.getReserve?.()??g.Weapons?.reserve??null),
    weapon:read(()=>w?.name??w?.getName?.()??null),
    reload_time_raw:read(()=>w?.getReloadTime?.()??w?.reloadTime??null),
    camera:camera?{rotation:[camera.rotation.x,camera.rotation.y,camera.rotation.z],
                   position:camera.position.toArray()}:null,
    round:read(()=>({phase:g.Round?.getPhase?.()??g.Round?.state??null,over:g.Round?.isOver?.()??null,
                    score:g.Round?.getScore?.()??{player:g.Round?.playerScore,enemy:g.Round?.enemyScore},
                    time:g.Round?.getRoundTime?.()??g.Round?.timeLeft??null})),
    enemies:read(()=>(g.AI?.getEntities?.()||g.AI?.enemies||[]).map(a=>({health:a.getHealth?.()??a.health,
      alive:a.isAlive?.()??a.alive,position:a.getPosition?.()?.toArray?.()||a.mesh?.position?.toArray?.(),
      team:a.getTeam?.()??a.team}))),
    pointer_lock:document.pointerLockElement?.id||null,
    audio:window.__acceptanceAudio||null
  };
}"""


def digest_files(folder):
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.rglob("*")
        if p.is_file() and ".clearact" not in p.relative_to(folder).parts
    }


async def main(args):
    source, output = args.workspace.resolve(), args.output.resolve()
    output.mkdir(parents=True)
    original_hashes = digest_files(source)
    frozen = output / "game-copy"
    shutil.copytree(source, frozen, ignore=shutil.ignore_patterns(".clearact"))
    assert digest_files(frozen) == original_hashes
    result = {
        "scope": "Frozen copy; trusted browser inputs and read-only state observations; no forced game transitions.",
        "this_benchmark_model_generation_concurrent": False,
        "other_model_clients": "not controlled or measured",
        "reload_time_unit": args.reload_time_unit,
        "source_sha256": original_hashes,
        "checks": {},
        "snapshots": {},
        "page_errors": [],
        "console_errors": [],
        "network_errors": [],
        "unverified": ["CSGO-level asset quality, multiplayer, content scale and balance require separate assessment"],
        "csgo_quality_target_established": False,
    }

    def save():
        (output / "independent.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def clean_url(url):
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))

    app = FastAPI()
    app.mount("/", StaticFiles(directory=frozen, html=True))
    try:
        async with LocalServer(app) as server, async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            try:
                result["browser_version"] = browser.version
                context = await browser.new_context(viewport={"width": 1440, "height": 900})
                page = await context.new_page()
                page.on("pageerror", lambda e: result["page_errors"].append(str(e)))
                page.on("console", lambda m: result["console_errors"].append(m.text) if m.type == "error" else None)
                page.on(
                    "requestfailed",
                    lambda r: result["network_errors"].append({"url": clean_url(r.url), "failure": r.failure}),
                )
                await page.add_init_script("""window.__acceptanceAudio={contexts:0,oscillators:0};
                  if(window.AudioContext){const Base=window.AudioContext;window.AudioContext=class extends Base{
                    constructor(...a){super(...a);window.__acceptanceAudio.contexts++}
                    createOscillator(...a){window.__acceptanceAudio.oscillators++;return super.createOscillator(...a)}
                  }}""")
                await page.goto(server.url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1500)
                result["page_title"] = await page.title()
                result["dom_text_before"] = (await page.locator("body").inner_text())[:8000]
                await page.screenshot(path=str(output / "menu.png"))
                result["module_state"] = await page.evaluate("""() => ({three:typeof window.THREE,
                  modules:Object.keys(window.Game||{}),
                  scene:Boolean(window.Game?.Engine?.getScene?.()||window.Game?.Core?.scene)})""")
                ready = result["module_state"]["three"] != "undefined" and result["module_state"]["scene"]
                result["checks"]["boot_3d"] = ready and not result["page_errors"]
                if not result["checks"]["boot_3d"]:
                    result["unverified"].append("Controls and gameplay unavailable because 3D bootstrap failed")
                    return
                result["snapshots"]["before_start"] = await page.evaluate(SNAPSHOT)
                await page.locator("#btn-start").click(timeout=5000)
                await page.wait_for_timeout(300)
                started = await page.evaluate(SNAPSHOT)
                result["snapshots"]["started"] = started
                result["checks"]["pointer_lock"] = bool(started["pointer_lock"])
                result["checks"]["started_alive"] = started["alive"] is True
                await page.keyboard.down("w")
                await page.wait_for_timeout(700)
                await page.keyboard.up("w")
                moved = await page.evaluate(SNAPSHOT)
                result["snapshots"]["moved"] = moved
                a, b = started["position"], moved["position"]
                result["checks"]["keyboard_movement"] = bool(
                    a and b and sum((x - y) ** 2 for x, y in zip(a, b, strict=False)) > 0.01
                )
                await page.mouse.move(720, 450)
                aimed_before = await page.evaluate(SNAPSHOT)
                await page.mouse.move(870, 490, steps=5)
                await page.wait_for_timeout(150)
                aimed = await page.evaluate(SNAPSHOT)
                result["snapshots"]["aimed_before"] = aimed_before
                result["snapshots"]["aimed"] = aimed
                a, b = aimed_before["camera"], aimed["camera"]
                result["checks"]["mouse_aim"] = bool(
                    a and b and sum(abs(x - y) for x, y in zip(a["rotation"], b["rotation"], strict=False)) > 0.001
                )
                await page.mouse.down()
                await page.wait_for_timeout(500)
                await page.mouse.up()
                fired = await page.evaluate(SNAPSHOT)
                result["snapshots"]["fired"] = fired
                before_ammo, after_ammo = aimed["ammo"], fired["ammo"]
                result["checks"]["shoot_consumes_ammo"] = (
                    isinstance(before_ammo, int | float)
                    and isinstance(after_ammo, int | float)
                    and after_ammo < before_ammo
                )
                await page.keyboard.press("r", delay=100)
                wait = fired.get("reload_time_raw")
                duration_ms = wait if isinstance(wait, int | float) else 3000
                if args.reload_time_unit == "seconds" and isinstance(wait, int | float):
                    duration_ms *= 1000
                await page.wait_for_timeout(int(min(max(duration_ms, 100), 10000) + 500))
                reloaded = await page.evaluate(SNAPSHOT)
                result["snapshots"]["reloaded"] = reloaded
                result["checks"]["reload"] = (
                    isinstance(after_ammo, int | float)
                    and isinstance(reloaded["ammo"], int | float)
                    and reloaded["ammo"] > after_ammo
                )
                selections = []
                for key in ("1", "2", "3", "4", "5"):
                    await page.keyboard.press(key, delay=100)
                    await page.wait_for_timeout(200)
                    selections.append({"key": key, "state": await page.evaluate(SNAPSHOT)})
                result["weapon_selection"] = selections
                result["checks"]["multiple_weapon_selection"] = len({str(s["state"]["weapon"]) for s in selections}) > 1
                result["scene_metrics"] = await page.evaluate("""() => {
                  const s=Game.Engine?.getScene?.()||Game.Core?.scene;
                  const textures=new Set(), geometries=new Set();let meshes=0;
                  s.traverse(o=>{if(o.isMesh){meshes++;if(o.geometry)geometries.add(o.geometry.uuid);
                    for(const m of [].concat(o.material||[]))for(const k of ['map','normalMap','roughnessMap'])
                      if(m[k])textures.add(m[k].uuid)}});
                  const c=document.querySelector('canvas'),gl=c.getContext('webgl2')||c.getContext('webgl');
                  const ext=gl?.getExtension('WEBGL_debug_renderer_info');
                  const camera=Game.Engine?.getCamera?.()||Game.Core?.camera;
                  let ancestor=camera,sceneContainsCamera=false,cameraMeshes=0;
                  while(ancestor){if(ancestor===s)sceneContainsCamera=true;ancestor=ancestor.parent}
                  camera.traverse(o=>{if(o.isMesh)cameraMeshes++});
                  return {meshes,geometries:geometries.size,textures:textures.size,
                    scene_contains_camera:sceneContainsCamera,meshes_under_camera:cameraMeshes,
                    graphics_renderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null};
                }""")
                await page.screenshot(path=str(output / "gameplay.png"))
                save()
                result["frame_timing"] = await page.evaluate("""() => new Promise(resolve=>{
                  const times=[];const begin=performance.now();let done=false;
                  function end(){if(!done){done=true;resolve({frames:times.length,elapsed_ms:performance.now()-begin,
                    average_raf_fps:times.length>1?(times.length-1)*1000/(times.at(-1)-times[0]):null})}}
                  function next(t){if(done)return;times.push(t);
                    if(times.length>=120)end();else requestAnimationFrame(next)}
                  requestAnimationFrame(next);setTimeout(end,8000);
                })""")
                await page.wait_for_timeout(20000)
                result["snapshots"]["after_natural_wait"] = await page.evaluate(SNAPSHOT)
                result["dom_text_after_wait"] = (await page.locator("body").inner_text())[:8000]
                await page.screenshot(path=str(output / "after-wait.png"))
                await page.keyboard.press("Escape", delay=100)
                await page.reload(wait_until="domcontentloaded")
                if await page.locator("#btn-sound").count():
                    before = await page.locator("#btn-sound").inner_text()
                    await page.locator("#btn-sound").click()
                    after = await page.locator("#btn-sound").inner_text()
                    await page.locator("#btn-sound").click()
                    result["checks"]["sound_toggle_text"] = before != after
                result["checks"]["no_runtime_errors"] = not result["page_errors"]
                result["unverified"].append(
                    "Independent win, full match and restart need actual reachable combat interaction; "
                    "natural wait alone is not a win test"
                )
            finally:
                await browser.close()
    except Exception as exc:
        result["evaluation_error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        result["source_unchanged"] = digest_files(source) == original_hashes
        required = {
            "boot_3d",
            "pointer_lock",
            "started_alive",
            "keyboard_movement",
            "mouse_aim",
            "shoot_consumes_ammo",
            "reload",
            "multiple_weapon_selection",
            "no_runtime_errors",
        }
        result["controls_smoke_passed"] = (
            required <= result["checks"].keys()
            and all(result["checks"].values())
            and not result["page_errors"]
            and "evaluation_error" not in result
        )
        save()
        print("INDEPENDENT_CONTROLS", result["controls_smoke_passed"], result.get("evaluation_error", ""), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reload-time-unit", choices=["milliseconds", "seconds"], default="milliseconds")
    asyncio.run(main(parser.parse_args()))
