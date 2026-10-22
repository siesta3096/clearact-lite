import json
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from clearact_lite.storage.snapshots import atomic_write, digest, workspace_path


class UploadStore:
    """Keep the original upload and a separately editable workspace copy."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".clearact" / "uploads"

    def add(self, name: str, data: bytes) -> dict:
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", PureWindowsPath(name).name).strip(" .")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if not filename or filename.upper().split(".")[0] in reserved:
            filename = "upload_" + (filename or "file")
        upload_id = uuid4().hex
        relative = Path("uploads") / upload_id / filename
        path = workspace_path(self.workspace, str(relative))
        checksum = digest(data)
        blob = self.root / "blobs" / checksum
        if not blob.exists():
            atomic_write(blob, data)
        atomic_write(path, data)
        info = {
            "id": upload_id,
            "name": filename,
            "path": relative.as_posix(),
            "bytes": len(data),
            "sha256": checksum,
            "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "created_at": datetime.now(UTC).isoformat(),
        }
        atomic_write(self.root / f"{upload_id}.json", json.dumps(info, ensure_ascii=False).encode("utf-8"))
        return info

    def get(self, upload_id: str) -> dict:
        if re.fullmatch(r"[a-f0-9]{32}", upload_id) is None:
            raise ValueError("Invalid upload id")
        path = self.root / f"{upload_id}.json"
        if not path.exists():
            raise ValueError("附件不属于当前工作区，请重新上传")
        info = json.loads(path.read_text(encoding="utf-8"))
        workfile = workspace_path(self.workspace, info["path"])
        if digest(workfile.read_bytes()) != info["sha256"]:
            raise ValueError("附件工作副本已变化，请重新选择要发送的文件；原始上传仍保留")
        return info
