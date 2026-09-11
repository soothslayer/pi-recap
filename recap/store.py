"""SQLite storage for episodes, daily recaps, and small key-value state.

Only transcripts, summaries, and derived notes are stored here — never audio.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_db_path: Path | None = None


def init_db(data_dir: str | Path) -> Path:
    """Create data dir + schema if needed. Returns the db path."""
    global _db_path
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _db_path = data_dir / "recap.db"
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_key   TEXT UNIQUE,
                started_at    TEXT NOT NULL,
                ended_at      TEXT NOT NULL,
                title         TEXT DEFAULT '',
                summary       TEXT DEFAULT '',
                key_points    TEXT DEFAULT '[]',
                action_items  TEXT DEFAULT '[]',
                transcript    TEXT DEFAULT '',
                audio_deleted INTEGER DEFAULT 1,
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_started ON episodes(started_at);

            CREATE TABLE IF NOT EXISTS daily_recaps (
                date       TEXT PRIMARY KEY,
                markdown   TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Migration for DBs created before the pending_summary column existed.
        try:
            conn.execute("ALTER TABLE episodes ADD COLUMN pending_summary INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already present
    return _db_path


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("store.init_db() must be called first")
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------- episodes

def save_episode(ep: dict) -> int:
    """Persist a summarized episode. Returns the row id."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO episodes
                (episode_key, started_at, ended_at, title, summary,
                 key_points, action_items, transcript, audio_deleted,
                 pending_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ep.get("episode_key"),
                ep.get("started_at", ""),
                ep.get("ended_at", ""),
                ep.get("title", ""),
                ep.get("summary", ""),
                json.dumps(ep.get("key_points", []) or []),
                json.dumps(ep.get("action_items", []) or []),
                ep.get("transcript", ""),
                1 if ep.get("audio_deleted", True) else 0,
                1 if ep.get("pending_summary", False) else 0,
                _now_iso(),
            ),
        )
        return cur.lastrowid


def list_episodes(limit: int = 50, search: str | None = None) -> list[dict]:
    with _lock, _connect() as conn:
        if search:
            like = f"%{search}%"
            rows = conn.execute(
                """
                SELECT * FROM episodes
                WHERE title LIKE ? OR summary LIKE ? OR transcript LIKE ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_episode(r) for r in rows]


def list_episodes_for_date(date_str: str) -> list[dict]:
    """All episodes whose local start time falls on YYYY-MM-DD, oldest first."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes WHERE started_at LIKE ? ORDER BY started_at ASC",
            (f"{date_str}%",),
        ).fetchall()
        return [_row_to_episode(r) for r in rows]


def get_episode(episode_id: int) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return _row_to_episode(row) if row else None


def delete_episode(episode_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        return cur.rowcount > 0


def count_episodes() -> int:
    with _lock, _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def _row_to_episode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("key_points", "action_items"):
        try:
            d[key] = json.loads(d[key] or "[]")
        except (json.JSONDecodeError, TypeError):
            d[key] = []
    return d


# -------------------------------------------------------------------- dailies

def save_daily(date_str: str, markdown: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_recaps (date, markdown, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET markdown = excluded.markdown,
                                            created_at = excluded.created_at
            """,
            (date_str, markdown, _now_iso()),
        )


def get_daily(date_str: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM daily_recaps WHERE date = ?", (date_str,)
        ).fetchone()
        return dict(row) if row else None


def list_dailies(limit: int = 30) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT date, created_at FROM daily_recaps ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------------ kv

def kv_get(key: str, default: str | None = None) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def kv_delete(key: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
