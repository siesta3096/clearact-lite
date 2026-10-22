"""Exercise the actual Windows launcher; retain all logs and stop only the owned process tree."""

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil


def main():
    root = Path(__file__).resolve().parents[2]
    records = root / "development/records/evals" / datetime.now().strftime("launcher-%Y%m%d-%H%M%S")
    records.mkdir(parents=True)
    stub = records / "python-startup-hook"
    stub.mkdir()
    signal_path = records / "browser-opened.txt"
    (stub / "sitecustomize.py").write_text(
        "import os, pathlib, webbrowser\n"
        "def capture_open(url, *args, **kwargs):\n"
        "    pathlib.Path(os.environ['CLEARACT_TEST_BROWSER_SIGNAL']).write_text(url, encoding='utf-8')\n"
        "    return True\n"
        "webbrowser.open = capture_open\n",
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        PYTHONPATH=str(stub),
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
        CLEARACT_TEST_BROWSER_SIGNAL=str(signal_path),
    )
    outside = records / "different-working-directory"
    outside.mkdir()
    launcher = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(root / "clearact.cmd")]
    results = {"scope": "actual_windows_launcher", "checks": [], "passed": False}
    process = None
    try:
        for name, args, expected in [("help", ["--help"], 0), ("invalid-port", ["web", "--port", "0"], 1)]:
            check = subprocess.run(
                launcher + args,
                cwd=outside,
                env=env,
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (records / f"{name}.log").write_text(check.stdout + check.stderr, encoding="utf-8")
            assert check.returncode == expected, (name, check.returncode)
            if name == "invalid-port":
                assert "See the error above" in check.stdout
            results["checks"].append(name)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 8875))
        with (records / "startup.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                launcher, cwd=outside, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW
            )
            results["owned_root_pid"] = process.pid
            deadline = time.monotonic() + 20
            while not signal_path.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Default launcher did not reach browser-open readiness")
                time.sleep(0.05)
            url = signal_path.read_text(encoding="utf-8")
            assert url == "http://127.0.0.1:8875"
            with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
                page = client.get("/")
                assert page.status_code == 200
                assert "ClearAct Lite" in page.text
                token = re.search(r'name="clearact-token" content="([^"]+)"', page.text)[1]
                state = client.get("/api/state").json()
                assert "api_key" not in state["settings"]
                assert state["active_branch"] is None
                models = client.post("/api/model/probe", headers={"X-ClearAct-Token": token}, json={}).json()
                results["local_model_probe"] = models
                assert models["ok"], models["message"]
            results["checks"].extend(
                [
                    "no_arguments_opens_ui",
                    "works_from_other_cwd",
                    "browser_open_after_http_ready",
                    "localhost_model_probe",
                ]
            )
        results["passed"] = True
    except BaseException as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                parent = psutil.Process(process.pid)
                family = parent.children(recursive=True) + [parent]
                for child in family:
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                _, alive = psutil.wait_procs(family, timeout=5)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.wait(timeout=5)
                results["owned_process_tree_stopped"] = True
            except (psutil.Error, subprocess.TimeoutExpired) as exc:
                results["cleanup_error"] = type(exc).__name__
                results["passed"] = False
        (records / "verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"records": str(records), **results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
