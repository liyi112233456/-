from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH

_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress REAL NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT,
                options_json TEXT NOT NULL,
                summary_json TEXT
            )
            """
        )
        conn.commit()


def create_task(task_id: str, filename: str, options: dict[str, Any]) -> None:
    now = utcnow()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename, "queued", "queued", 0.0, "等待后台计算", now, now, None,
             json.dumps(options, ensure_ascii=False), None),
        )
        conn.commit()


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    if "summary" in fields:
        fields["summary_json"] = json.dumps(fields.pop("summary"), ensure_ascii=False)
    columns = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [task_id]
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE tasks SET {columns} WHERE id=?", values)
        conn.commit()


def get_task(task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["options"] = json.loads(d.pop("options_json") or "{}")
    d["summary"] = json.loads(d.pop("summary_json") or "null")
    return d
