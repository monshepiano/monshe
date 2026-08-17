"""
Память Джарвиса: SQLite.

Хранит:
  - сообщения диалога (по сессиям)
  - задачи агента и их шаги
  - долговременные факты о пользователе (персонализация)
  - события/уведомления
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT DEFAULT '{}',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session, id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,          -- planning|running|waiting_approval|done|failed|cancelled
    session TEXT,
    plan TEXT DEFAULT '[]',
    result TEXT DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    background INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- thought|tool|result|error|approval
    title TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, id);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL,
    source TEXT DEFAULT '',
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    read INTEGER DEFAULT 0,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cron TEXT DEFAULT '',
    every_minutes INTEGER DEFAULT 0,
    next_run REAL DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created REAL NOT NULL
);
"""


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# ---------------- сообщения ----------------

def add_message(session: str, role: str, content: str, meta: dict | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO messages(session, role, content, meta, ts) VALUES(?,?,?,?,?)",
            (session, role, content, json.dumps(meta or {}, ensure_ascii=False), time.time()),
        )


def history(session: str, limit: int = 40) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT role, content, meta, ts FROM messages WHERE session=? "
            "ORDER BY id DESC LIMIT ?", (session, limit),
        ).fetchall()
    return [
        {"role": r["role"], "content": r["content"],
         "meta": json.loads(r["meta"] or "{}"), "ts": r["ts"]}
        for r in reversed(rows)
    ]


def sessions() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT session, COUNT(*) n, MAX(ts) last, "
            "(SELECT content FROM messages m2 WHERE m2.session=m.session AND role='user' "
            " ORDER BY id LIMIT 1) first_msg "
            "FROM messages m GROUP BY session ORDER BY last DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


def clear_session(session: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM messages WHERE session=?", (session,))


# ---------------- задачи ----------------

def create_task(title: str, goal: str, session: str = "", background: bool = False) -> str:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,goal,status,session,plan,result,created,updated,background)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, title, goal, "planning", session, "[]", "", now, now, int(background)),
        )
    return tid


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated"] = time.time()
    if "plan" in fields and not isinstance(fields["plan"], str):
        fields["plan"] = json.dumps(fields["plan"], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                     (*fields.values(), task_id))


def get_task(task_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["plan"] = json.loads(d.get("plan") or "[]")
    return d


def list_tasks(limit: int = 40) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["plan"] = json.loads(d.get("plan") or "[]")
        out.append(d)
    return out


def add_step(task_id: str, kind: str, title: str = "", payload: dict | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO steps(task_id,kind,title,payload,ts) VALUES(?,?,?,?,?)",
            (task_id, kind, title,
             json.dumps(payload or {}, ensure_ascii=False), time.time()),
        )


def task_steps(task_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT kind,title,payload,ts FROM steps WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    return [
        {"kind": r["kind"], "title": r["title"],
         "payload": json.loads(r["payload"] or "{}"), "ts": r["ts"]}
        for r in rows
    ]


# ---------------- факты о пользователе ----------------

def remember(key: str, value: str, source: str = "chat") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO facts(key,value,source,ts) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key.strip().lower(), value, source, time.time()),
        )


def recall(limit: int = 60) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT key,value,ts FROM facts ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def forget(key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM facts WHERE key=?", (key.strip().lower(),))


# ---------------- события ----------------

def add_event(kind: str, title: str, body: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO events(kind,title,body,read,ts) VALUES(?,?,?,0,?)",
            (kind, title, body, time.time()),
        )


def events(unread_only: bool = False, limit: int = 50) -> list[dict]:
    q = "SELECT id,kind,title,body,read,ts FROM events"
    if unread_only:
        q += " WHERE read=0"
    q += " ORDER BY id DESC LIMIT ?"
    with db() as conn:
        rows = conn.execute(q, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_events_read() -> None:
    with db() as conn:
        conn.execute("UPDATE events SET read=1 WHERE read=0")


# ---------------- расписания ----------------

def add_schedule(title: str, prompt: str, every_minutes: int = 0,
                 cron: str = "", next_run: float = 0) -> str:
    sid = uuid.uuid4().hex[:10]
    with db() as conn:
        conn.execute(
            "INSERT INTO schedules(id,title,prompt,cron,every_minutes,next_run,enabled,created)"
            " VALUES(?,?,?,?,?,?,1,?)",
            (sid, title, prompt, cron, every_minutes,
             next_run or (time.time() + every_minutes * 60), time.time()),
        )
    return sid


def list_schedules() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY created DESC").fetchall()
    return [dict(r) for r in rows]


def update_schedule(sid: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE schedules SET {sets} WHERE id=?", (*fields.values(), sid))


def delete_schedule(sid: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM schedules WHERE id=?", (sid,))


init()
