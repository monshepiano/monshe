"""Хранилище: SQLite. Чаты, задачи, шаги, уведомления, память о пользователе."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, chat_id TEXT, role TEXT, content TEXT,
    meta TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, goal TEXT, status TEXT,
    plan TEXT, result TEXT, chat_id TEXT, created REAL, updated REAL,
    schedule TEXT, next_run REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY, task_id TEXT, idx INTEGER, title TEXT,
    status TEXT, output TEXT, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, level TEXT, title TEXT, body TEXT,
    read INTEGER DEFAULT 0, created REAL, action TEXT
);
CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY, kind TEXT, key TEXT, value TEXT,
    weight REAL DEFAULT 1.0, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, task_id TEXT, tool TEXT, args TEXT,
    reason TEXT, status TEXT, created REAL, decided REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, provider TEXT,
    model TEXT, tokens_in INTEGER, tokens_out INTEGER, tier TEXT, cost REAL
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.ensure_dirs()
        c = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _local.conn = c
    return c


def init() -> None:
    conn()


def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _exec(sql: str, params: tuple = ()) -> None:
    with _write_lock:
        c = conn()
        c.execute(sql, params)
        c.commit()


def now() -> float:
    return time.time()


def nid() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- чаты
def create_chat(title: str = "Новый диалог") -> str:
    cid = nid()
    _exec("INSERT INTO chats(id,title,created,updated) VALUES(?,?,?,?)",
          (cid, title, now(), now()))
    return cid


def list_chats(limit: int = 50) -> List[Dict[str, Any]]:
    return _rows(conn().execute(
        "SELECT * FROM chats ORDER BY updated DESC LIMIT ?", (limit,)))


def rename_chat(chat_id: str, title: str) -> None:
    _exec("UPDATE chats SET title=?, updated=? WHERE id=?", (title, now(), chat_id))


def delete_chat(chat_id: str) -> None:
    _exec("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    _exec("DELETE FROM chats WHERE id=?", (chat_id,))


def add_message(chat_id: str, role: str, content: str,
                meta: Optional[Dict[str, Any]] = None) -> str:
    mid = nid()
    _exec("INSERT INTO messages(id,chat_id,role,content,meta,created) VALUES(?,?,?,?,?,?)",
          (mid, chat_id, role, content, json.dumps(meta or {}, ensure_ascii=False), now()))
    _exec("UPDATE chats SET updated=? WHERE id=?", (now(), chat_id))
    return mid


def get_messages(chat_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    rows = _rows(conn().execute(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY created ASC LIMIT ?", (chat_id, limit)))
    for r in rows:
        try:
            r["meta"] = json.loads(r.get("meta") or "{}")
        except Exception:
            r["meta"] = {}
    return rows


# ---------------------------------------------------------------- задачи
def create_task(title: str, goal: str, chat_id: str = "", source: str = "user",
                schedule: str = "", next_run: Optional[float] = None) -> str:
    tid = nid()
    _exec("""INSERT INTO tasks(id,title,goal,status,plan,result,chat_id,created,updated,schedule,next_run,source)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
          (tid, title, goal, "pending", "[]", "", chat_id, now(), now(), schedule, next_run, source))
    return tid


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    r = conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def list_tasks(limit: int = 60) -> List[Dict[str, Any]]:
    tasks = _rows(conn().execute("SELECT * FROM tasks ORDER BY created DESC LIMIT ?", (limit,)))
    for t in tasks:
        t["steps"] = list_steps(t["id"])
    return tasks


def due_tasks(ts: float) -> List[Dict[str, Any]]:
    return _rows(conn().execute(
        "SELECT * FROM tasks WHERE next_run IS NOT NULL AND next_run<=? AND status IN ('pending','scheduled','done')",
        (ts,)))


def add_step(task_id: str, idx: int, title: str) -> str:
    sid = nid()
    _exec("INSERT INTO steps(id,task_id,idx,title,status,output,created,updated) VALUES(?,?,?,?,?,?,?,?)",
          (sid, task_id, idx, title, "pending", "", now(), now()))
    return sid


def update_step(step_id: str, **fields: Any) -> None:
    fields["updated"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE steps SET {cols} WHERE id=?", (*fields.values(), step_id))


def list_steps(task_id: str) -> List[Dict[str, Any]]:
    return _rows(conn().execute(
        "SELECT * FROM steps WHERE task_id=? ORDER BY idx ASC", (task_id,)))


# ---------------------------------------------------------------- уведомления
def add_notification(title: str, body: str = "", level: str = "info",
                     action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    nidv = nid()
    _exec("INSERT INTO notifications(id,level,title,body,read,created,action) VALUES(?,?,?,?,0,?,?)",
          (nidv, level, title, body, now(), json.dumps(action or {}, ensure_ascii=False)))
    return {"id": nidv, "level": level, "title": title, "body": body,
            "read": 0, "created": now(), "action": action or {}}


def list_notifications(limit: int = 50) -> List[Dict[str, Any]]:
    rows = _rows(conn().execute(
        "SELECT * FROM notifications ORDER BY created DESC LIMIT ?", (limit,)))
    for r in rows:
        try:
            r["action"] = json.loads(r.get("action") or "{}")
        except Exception:
            r["action"] = {}
    return rows


def mark_notifications_read(ids: Optional[List[str]] = None) -> None:
    if ids:
        q = ",".join("?" * len(ids))
        _exec(f"UPDATE notifications SET read=1 WHERE id IN ({q})", tuple(ids))
    else:
        _exec("UPDATE notifications SET read=1", ())


# ---------------------------------------------------------------- память
def remember(kind: str, key: str, value: str, weight: float = 1.0) -> None:
    row = conn().execute("SELECT id FROM memory WHERE kind=? AND key=?", (kind, key)).fetchone()
    if row:
        _exec("UPDATE memory SET value=?, weight=?, updated=? WHERE id=?",
              (value, weight, now(), row["id"]))
    else:
        _exec("INSERT INTO memory(id,kind,key,value,weight,created,updated) VALUES(?,?,?,?,?,?,?)",
              (nid(), kind, key, value, weight, now(), now()))


def recall(kind: str = "", limit: int = 60) -> List[Dict[str, Any]]:
    if kind:
        return _rows(conn().execute(
            "SELECT * FROM memory WHERE kind=? ORDER BY weight DESC, updated DESC LIMIT ?",
            (kind, limit)))
    return _rows(conn().execute(
        "SELECT * FROM memory ORDER BY weight DESC, updated DESC LIMIT ?", (limit,)))


def forget(mem_id: str) -> None:
    _exec("DELETE FROM memory WHERE id=?", (mem_id,))


# ---------------------------------------------------------------- подтверждения
def create_approval(task_id: str, tool: str, args: Dict[str, Any], reason: str) -> str:
    aid = nid()
    _exec("INSERT INTO approvals(id,task_id,tool,args,reason,status,created) VALUES(?,?,?,?,?,?,?)",
          (aid, task_id, tool, json.dumps(args, ensure_ascii=False), reason, "pending", now()))
    return aid


def decide_approval(approval_id: str, status: str) -> None:
    _exec("UPDATE approvals SET status=?, decided=? WHERE id=?", (status, now(), approval_id))


def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    r = conn().execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except Exception:
        d["args"] = {}
    return d


def pending_approvals() -> List[Dict[str, Any]]:
    rows = _rows(conn().execute("SELECT * FROM approvals WHERE status='pending' ORDER BY created ASC"))
    for r in rows:
        try:
            r["args"] = json.loads(r.get("args") or "{}")
        except Exception:
            r["args"] = {}
    return rows


# ---------------------------------------------------------------- расход
def log_usage(provider: str, model: str, tin: int, tout: int, tier: str, cost: float = 0.0) -> None:
    _exec("INSERT INTO usage(ts,provider,model,tokens_in,tokens_out,tier,cost) VALUES(?,?,?,?,?,?,?)",
          (now(), provider, model, tin, tout, tier, cost))


def usage_summary() -> Dict[str, Any]:
    day = now() - 86400
    rows = _rows(conn().execute(
        """SELECT provider, model, COUNT(*) n, SUM(tokens_in) tin, SUM(tokens_out) tout
           FROM usage WHERE ts>? GROUP BY provider, model ORDER BY n DESC""", (day,)))
    total = conn().execute("SELECT COUNT(*) c FROM usage WHERE ts>?", (day,)).fetchone()["c"]
    return {"day_calls": total, "by_model": rows}
