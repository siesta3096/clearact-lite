import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from clearact_lite.bootstrap import build_application
from clearact_lite.core.contracts import Event
from clearact_lite.settings import Settings


def show_event(event: Event) -> None:
    if event.kind == "tool.started":
        print(f"[{event.id}] tool: {event.payload['name']}", flush=True)
    elif event.kind in {"model.started", "file.written", "artifact.registered"}:
        print(f"[{event.id}] {event.kind}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClearAct Lite architecture foundation (full-control mode)")
    parser.add_argument("--workspace", type=Path)
    commands = parser.add_subparsers(dest="command")
    web = commands.add_parser("web", help="Start the local workspace UI")
    web.add_argument("--port", type=int, default=8875)
    web.add_argument("--open", action="store_true", dest="open_browser")
    run = commands.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--branch")
    for name in ("history", "report"):
        commands.add_parser(name).add_argument("branch")
    fork = commands.add_parser("fork")
    fork.add_argument("branch")
    fork.add_argument("--at", type=int)
    restore = commands.add_parser("restore")
    restore.add_argument("branch")
    restore.add_argument("event", type=int)
    args = parser.parse_args()
    if args.command in {None, "web"}:
        try:
            from clearact_lite.gateway.server import serve

            workspace = args.workspace or Path.cwd() / "workspaces" / "default"
            serve(
                Settings.from_env(workspace),
                getattr(args, "port", 8875),
                args.command is None or getattr(args, "open_browser", False),
            )
        except ImportError:
            print("缺少运行依赖，请执行：python -m pip install -e .")
            raise SystemExit(1) from None
        except (OSError, ValueError) as exc:
            print(f"本地工作台启动失败：{exc}")
            raise SystemExit(1) from None
        return
    mcp_path = Path.cwd() / ".clearact" / "mcp-servers.json"
    mcp_servers = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.exists() else {}
    app = build_application(Settings.from_env(args.workspace or Path.cwd()), mcp_servers=mcp_servers)
    try:
        if args.command == "run":

            async def execute():
                try:
                    return await app.run(
                        args.prompt, args.branch, show_event, lambda text: print(text, end="", flush=True)
                    )
                finally:
                    await app.aclose()

            result = asyncio.run(execute())
            print()
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            if result.status != "finished":
                raise SystemExit(1)
        elif args.command == "fork":
            print(app.fork(args.branch, args.at))
        elif args.command == "restore":
            app.restore(args.branch, args.event)
            print("File restored; history retained")
        else:
            value = (
                app.report(args.branch) if args.command == "report" else [asdict(e) for e in app.events(args.branch)]
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
    finally:
        app.close()
