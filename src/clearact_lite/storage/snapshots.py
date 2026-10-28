import difflib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from clearact_lite.core.errors import WorkspaceConflict
from clearact_lite.core.ports import Emit


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workspace_path(workspace: Path, value: str) -> Path:
    path = (workspace / value).resolve()
    if not path.is_relative_to(workspace.resolve()) or path == workspace.resolve():
        raise ValueError("Path must be a file inside the workspace")
    if path.is_relative_to((workspace / ".clearact").resolve()):
        raise ValueError("Runtime metadata is accessed through history tools")
    return path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".clearact-write-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class FileSnapshots:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".clearact" / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def write_text(self, path: Path, text: str, emit: Emit) -> str:
        return self.write_bytes(path, text.encode("utf-8"), emit, text_preview=True)

    def write_bytes(self, path: Path, data: bytes, emit: Emit, *, text_preview: bool = False) -> str:
        path = workspace_path(self.workspace, str(path))
        before = path.read_bytes() if path.exists() else None
        after = data
        before_hash = digest(before) if before is not None else None
        if before is not None:
            snapshot = self.root / before_hash
            if not snapshot.exists():
                atomic_write(snapshot, before)
        if text_preview:
            diff = "".join(
                difflib.unified_diff(
                    (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                    data.decode("utf-8", errors="replace").splitlines(keepends=True),
                    fromfile=str(path),
                    tofile=str(path),
                )
            )
        else:
            diff = f"Binary file: {len(before or b'')} -> {len(data)} bytes"
        change = {
            "path": str(path.relative_to(self.workspace)),
            "before_hash": before_hash,
            "after_hash": digest(after),
            "diff": diff,
            "binary": not text_preview,
            "bytes": len(data),
            "text_tail": data.decode("utf-8")[-1200:] if text_preview else None,
        }
        prepared = emit("file.write_prepared", change)
        atomic_write(path, after)
        applied = emit("file.written", {**change, "prepared_event": prepared.id})
        return f"Written {change['path']}; change event {applied.id}"

    def restore(self, change: dict[str, Any], emit: Emit) -> None:
        path = workspace_path(self.workspace, change["path"])
        current = digest(path.read_bytes()) if path.exists() else None
        if current != change["after_hash"]:
            raise WorkspaceConflict("Current file differs from recorded output; restore refused")
        before_hash = change["before_hash"]
        before = None if before_hash is None else (self.root / before_hash).read_bytes()
        if before is not None and digest(before) != before_hash:
            raise WorkspaceConflict("Snapshot checksum mismatch")
        emit("file.restore_prepared", {"change": change})
        if before is None:
            path.unlink()
        else:
            atomic_write(path, before)
        emit(
            "file.restored",
            {
                "path": change["path"],
                "restored_hash": before_hash,
                "bytes": len(before) if before is not None else None,
                "text_tail": before.decode("utf-8", errors="replace")[-1200:]
                if before is not None and not change.get("binary")
                else None,
            },
        )
