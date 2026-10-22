import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from clearact_lite.core.contracts import SCHEMA_VERSION, BranchInfo, Event, Message, validate_messages
from clearact_lite.core.errors import ProtocolError


class SQLiteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS branches(
                id TEXT PRIMARY KEY, parent TEXT, cutoff INTEGER
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, branch TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_branch ON events(branch, id);
        """)
        row = self.connection.execute("SELECT version FROM metadata").fetchone()
        if row is None:
            self.connection.execute("INSERT INTO metadata VALUES (?)", (SCHEMA_VERSION,))
            self.connection.commit()
        elif row[0] != SCHEMA_VERSION:
            self.connection.close()
            raise ProtocolError("Unsupported storage version; explicit migration required")

    def _branch(self, branch_id: str) -> tuple:
        row = self.connection.execute(
            "SELECT parent, cutoff FROM branches WHERE id=?",
            (branch_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown branch")
        return row

    def append(self, branch_id: str, kind: str, payload: dict) -> Event:
        self._branch(branch_id)
        created = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(branch, kind, payload, created) VALUES (?, ?, ?, ?)",
                (branch_id, kind, encoded, created),
            )
        return Event(cursor.lastrowid, branch_id, kind, payload, created)

    def events(self, branch_id: str) -> list[Event]:
        parent, cutoff = self._branch(branch_id)
        inherited = [] if parent is None else [e for e in self.events(parent) if e.id <= cutoff]
        rows = self.connection.execute(
            "SELECT id, branch, kind, payload, created FROM events WHERE branch=? ORDER BY id",
            (branch_id,),
        ).fetchall()
        return inherited + [Event(row[0], row[1], row[2], json.loads(row[3]), row[4]) for row in rows]

    def fork(self, branch_id: str | None = None, at_event: int | None = None) -> str:
        cutoff = None
        if branch_id is not None:
            events = self.events(branch_id)
            cutoff = events[-1].id if events else 0
            if at_event is not None:
                if at_event not in {0, *(e.id for e in events)}:
                    raise ValueError("Fork point is not in this branch")
                cutoff = at_event
            validate_messages([Message.from_dict(e.payload) for e in events if e.id <= cutoff and e.kind == "message"])
        elif at_event is not None:
            raise ValueError("Fork point requires a parent branch")
        new_id = uuid4().hex
        with self.connection:
            self.connection.execute("INSERT INTO branches VALUES (?, ?, ?)", (new_id, branch_id, cutoff))
        return new_id

    def close(self) -> None:
        self.connection.close()

    def branches(self) -> list[BranchInfo]:
        rows = self.connection.execute("SELECT id, parent FROM branches ORDER BY rowid DESC").fetchall()
        result = []
        for branch_id, parent in rows:
            events = self.events(branch_id)
            users = [e for e in events if e.kind == "message" and e.payload["role"] == "user"]
            ends = [e for e in events if e.kind == "run.ended"]
            last_start = max((e.id for e in events if e.kind == "run.started"), default=0)
            last_end = ends[-1].id if ends else 0
            status = ends[-1].payload["status"] if ends else "ready"
            if last_start > last_end:
                status = "interrupted"
            result.append(
                BranchInfo(
                    branch_id,
                    parent,
                    users[0].payload["content"][:64] if users else "新话题",
                    status,
                    events[-1].created_at if events else None,
                )
            )
        return sorted(result, key=lambda b: b.updated_at or "", reverse=True)
