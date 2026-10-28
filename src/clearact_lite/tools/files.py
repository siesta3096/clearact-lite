import json

from clearact_lite.core.contracts import TextRange, ToolResult, ToolSpec
from clearact_lite.core.ports import ToolContext
from clearact_lite.storage.snapshots import digest, workspace_path


def object_schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class ReadFile:
    spec = ToolSpec(
        "file_read",
        "Read UTF-8 text. offset/limit count characters, not lines; offset starts at zero. "
        "Use returned next_offset for the next non-overlapping range; eof=true means end of text.",
        object_schema(
            {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
            },
            ["path"],
        ),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        path = workspace_path(context.workspace, arguments["path"])
        content = path.read_text(encoding="utf-8")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 6000)
        return ToolResult(TextRange.read(content, offset, limit).to_tool_text())


class WriteFile:
    spec = ToolSpec(
        "file_write",
        "Write UTF-8 text; save the previous bytes and a change preview first.",
        object_schema({"path": {"type": "string"}, "text": {"type": "string"}}, ["path", "text"]),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        path = workspace_path(context.workspace, arguments["path"])
        written = context.snapshots.write_text(path, arguments["text"], context.emit)
        data = arguments["text"].encode("utf-8")
        return ToolResult(f"{written}; UTF-8 bytes={len(data)}; sha256={digest(data)}")


class DeliverFile:
    spec = ToolSpec(
        "artifact_register",
        "Register an existing file for delivery; this is not task acceptance.",
        object_schema({"path": {"type": "string"}}, ["path"]),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        path = workspace_path(context.workspace, arguments["path"])
        content = path.read_bytes()
        event = context.emit(
            "artifact.registered",
            {
                "path": str(path.relative_to(context.workspace)),
                "bytes": len(content),
                "sha256": digest(content),
            },
        )
        return ToolResult(f"File registered at event {event.id}; content has not been independently accepted")


class ListFiles:
    spec = ToolSpec(
        "file_list",
        "List one workspace directory. Use returned directory paths to explore further.",
        object_schema(
            {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            [],
        ),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        value = arguments.get("path", ".")
        candidate = (context.workspace / value).resolve()
        path = (
            context.workspace if candidate == context.workspace.resolve() else workspace_path(context.workspace, value)
        )
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        entries = [p for p in entries if p.name != ".clearact"]
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
        return ToolResult(
            json.dumps(
                {
                    "total": len(entries),
                    "offset": offset,
                    "entries": [
                        {
                            "path": p.relative_to(context.workspace).as_posix(),
                            "directory": p.is_dir(),
                            "bytes": p.stat().st_size if p.is_file() else None,
                        }
                        for p in entries[offset : offset + limit]
                    ],
                },
                ensure_ascii=False,
            )
        )


class EditFile:
    spec = ToolSpec(
        "file_edit",
        "Append a manageable chunk or replace one exact text occurrence without rewriting the whole file. "
        "Use mode append with expected_bytes (UTF-8 bytes from file_list or this tool). "
        "Append adds bytes at EOF, not inside a closed HTML/script tag; use a separate source file or replace "
        "at the intended insertion point when needed. "
        "Use replace with old_text (must occur exactly once). Each complete call saves a snapshot. "
        "Return bytes/hash for the next chunk; truncated calls are never applied.",
        object_schema(
            {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["append", "replace"]},
                "text": {"type": "string"},
                "expected_bytes": {"type": "integer", "minimum": 0},
                "expected_sha256": {"type": "string"},
                "old_text": {"type": "string", "minLength": 1},
            },
            ["path", "mode", "text"],
        ),
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        import json

        path = workspace_path(context.workspace, arguments["path"])
        before = path.read_bytes() if path.exists() else b""
        if "expected_sha256" in arguments and digest(before) != arguments["expected_sha256"]:
            raise ValueError("File hash changed; read current file before editing")
        if arguments["mode"] == "append":
            if "expected_bytes" not in arguments:
                raise ValueError("Append requires expected_bytes to prevent duplicate chunks")
            if len(before) != arguments["expected_bytes"]:
                raise ValueError(f"Append offset changed: actual UTF-8 bytes={len(before)}; inspect before retrying")
            text = before.decode("utf-8") + arguments["text"]
        else:
            old = arguments.get("old_text", "")
            source = before.decode("utf-8")
            if not old or source.count(old) != 1:
                raise ValueError("Replace requires old_text occurring exactly once in current file")
            text = source.replace(old, arguments["text"], 1)
        context.snapshots.write_text(path, text, context.emit)
        content = text.encode("utf-8")
        return ToolResult(json.dumps({"path": arguments["path"], "bytes": len(content), "sha256": digest(content)}))
