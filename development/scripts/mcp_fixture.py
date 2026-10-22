"""Real local MCP fixture; writes stay in the explicitly supplied test directory."""

import argparse
import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse


def make_server(root: Path, port: int = 8000) -> FastMCP:
    server = FastMCP("ClearAct isolated fixture", host="127.0.0.1", port=port, log_level="ERROR")

    @server.custom_route("/", methods=["GET"])
    async def health(request):
        return JSONResponse({"fixture": True})

    @server.tool()
    def add(a: float, b: float) -> dict:
        """Add two numbers and return their sum."""
        return {"sum": a + b}

    @server.tool()
    def read_reference() -> dict:
        """Read the fixture's reference code and amount for validation."""
        return {"reference": "MCP-LOCAL-37", "amount": 37}

    @server.tool()
    def write_note(filename: str, text: str) -> dict:
        """Write a note in the fixture directory. This is a real external write."""
        target = (root / filename).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("Fixture path escaped test directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {"written": filename, "characters": len(text)}

    @server.tool()
    async def slow(seconds: float = 30) -> dict:
        """Record that execution started, then wait to exercise cancellation."""
        root.mkdir(parents=True, exist_ok=True)
        (root / "slow-started.txt").write_text("started", encoding="utf-8")
        await asyncio.sleep(seconds)
        return {"waited": seconds}

    @server.tool()
    def fail() -> str:
        """Return a deliberate MCP tool failure for protocol testing."""
        raise ValueError("intentional MCP fixture error")

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    args = parser.parse_args()
    make_server(args.root, args.port).run(transport=args.transport)
