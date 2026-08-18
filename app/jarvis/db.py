"""SQLite-хранилище JARVIS: чаты, сообщения, задачи AUTO, память, подтверждения."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import DATA_DIR

_LOCK = threading.RLock()
_DB_PATH = DATA_DIR / "jarvis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL,
    updated_at REAL,
    pinned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT,
    role TEXT,
    content TEXT,
    meta TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    prompt TEXT,
    status TEXT,
    mode TEXT,
    progress REAL DEFAULT 0,
    result TEXT,
    events TEXT,
    schedule TEXT,
    next_run REAL,
    chat_id TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    kind TEXT,
    key TEXT,
    value TEXT,
    weight REAL DEFAULT 1,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    chat_id TEXT,
    task_id TEXT,
    tool TEXT,
    args TEXT,
    risk TEXT,
    reason TEXT,
    status TEXT,
    created_at REAL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    level TEXT,
    title TEXT,
    body TEXT,
    read INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,
    provider TEXT,
    model TEXT,
    tier TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_rub REAL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, next_run);
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_CONN = _connect()
with _LOCK:
    _CONN.executescript(SCHEMA)
    _CONN.commit()


def now() -> float:
    return time.time()


def uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def execute(sql: str, params: tuple = ()) -> None:
    with _LOCK:
        _CONN.execute(sql, params)
        _CONN.commit()


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _LOCK:
        cur = _CONN.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- chats
def create_chat(title: str = "Новый диалог") -> Dict[str, Any]:
    chat_id = uid("c_")
    ts = now()
    execute(
        "INSERT INTO chats(id,title,created_at,updated_at) VALUES(?,?,?,?)",
        (chat_id, title, ts, ts),
    )
    return {"id": chat_id, "title": title, "created_at": ts, "updated_at": ts}


def list_chats(limit: int = 60) -> List[Dict[str, Any]]:
    return query("SELECT * FROM chats ORDER BY updated_at DESC LIMIT ?", (limit,))


def rename_chat(chat_id: str, title: str) -> None:
    execute("UPDATE chats SET title=?, updated_at=? WHERE id=?", (title, now(), chat_id))


def delete_chat(chat_id: str) -> None:
    execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    execute("DELETE FROM chats WHERE id=?", (chat_id,))


def add_message(chat_id: str, role: str, content: str, meta: Optional[Dict] = None) -> Dict[str, Any]:
    msg_id = uid("m_")
    ts = now()
    execute(
        "INSERT INTO messages(id,chat_id,role,content,meta,created_at) VALUES(?,?,?,?,?,?)",
        (msg_id, chat_id, role, content, json.dumps(meta or {}, ensure_ascii=False), ts),
    )
    execute("UPDATE chats SET updated_at=? WHERE id=?", (ts, chat_id))
    return {"id": msg_id, "chat_id": chat_id, "role": role, "content": content, "meta": meta or {}, "created_at": ts}


def get_messages(chat_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    rows = query(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY created_at ASC LIMIT ?",
        (chat_id, limit),
    )
    for row in rows:
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except Exception:
            row["meta"] = {}
    return rows


# ---------------------------------------------------------------- tasks
def get_message(msg_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM messages WHERE id=?", (msg_id,))
    if row:
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except Exception:
            row["meta"] = {}
    return row


def messages_after(chat_id: str, msg_id: str) -> List[Dict[str, Any]]:
    """Всё, что идёт в переписке после указанного сообщения."""
    msg = get_message(msg_id)
    if not msg:
        return []
    rows = query(
        "SELECT * FROM messages WHERE chat_id=? AND (created_at>? OR (created_at=? AND id>?)) "
        "ORDER BY created_at, id",
        (chat_id, msg["created_at"], msg["created_at"], msg_id),
    )
    for row in rows:
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except Exception:
            row["meta"] = {}
    return rows


def edit_message(msg_id: str, new_content: str, chat_id: str = "") -> Optional[Dict[str, Any]]:
    """Правка сообщения = НОВАЯ ВЕРСИЯ старого, а не новое сообщение.

    Варианты текста лежат в meta.versions, meta.version — номер активного.
    Вместе с каждой версией храним и ответы, которые за ней последовали
    (meta.branches), чтобы переключение возвращало всю ветку целиком —
    именно так это работает в GPT и DeepSeek.
    """
    msg = get_message(msg_id)
    if not msg:
        return None
    meta = msg.get("meta") or {}
    versions = list(meta.get("versions") or [msg.get("content", "")])
    branches = list(meta.get("branches") or [])
    while len(branches) < len(versions):
        branches.append([])

    # запоминаем ответы текущей версии, прежде чем их убрать
    cur = int(meta.get("version", len(versions) - 1))
    cur = max(0, min(cur, len(versions) - 1))
    tail = messages_after(chat_id or msg.get("chat_id", ""), msg_id)
    branches[cur] = [{"role": m["role"], "content": m["content"], "meta": m.get("meta") or {}}
                     for m in tail]

    versions.append(new_content)
    branches.append([])
    meta["versions"] = versions
    meta["branches"] = branches
    meta["version"] = len(versions) - 1
    execute(
        "UPDATE messages SET content=?, meta=? WHERE id=?",
        (new_content, json.dumps(meta, ensure_ascii=False), msg_id),
    )
    return get_message(msg_id)


def switch_message_version(msg_id: str, index: int) -> Optional[Dict[str, Any]]:
    """Показать другую версию сообщения вместе с её ответами (‹ 2/3 ›)."""
    msg = get_message(msg_id)
    if not msg:
        return None
    meta = msg.get("meta") or {}
    versions = list(meta.get("versions") or [msg.get("content", "")])
    if not versions:
        return msg
    branches = list(meta.get("branches") or [])
    while len(branches) < len(versions):
        branches.append([])

    chat_id = msg.get("chat_id", "")
    cur = int(meta.get("version", 0))
    cur = max(0, min(cur, len(versions) - 1))
    index = max(0, min(int(index), len(versions) - 1))
    if index == cur:
        return msg

    # сохраняем ветку текущей версии и подставляем ветку выбранной
    tail = messages_after(chat_id, msg_id)
    branches[cur] = [{"role": m["role"], "content": m["content"], "meta": m.get("meta") or {}}
                     for m in tail]
    delete_messages_after(chat_id, msg_id)

    meta["versions"] = versions
    meta["branches"] = branches
    meta["version"] = index
    execute(
        "UPDATE messages SET content=?, meta=? WHERE id=?",
        (versions[index], json.dumps(meta, ensure_ascii=False), msg_id),
    )
    for item in branches[index]:
        add_message(chat_id, item.get("role", "assistant"), item.get("content", ""),
                    item.get("meta") or {})
    return get_message(msg_id)


def delete_messages_after(chat_id: str, msg_id: str) -> int:
    """Убрать всё, что шло после отредактированного сообщения: ответы устарели."""
    msg = get_message(msg_id)
    if not msg:
        return 0
    rows = query(
        "SELECT id FROM messages WHERE chat_id=? AND (created_at>? OR (created_at=? AND id>?))",
        (chat_id, msg["created_at"], msg["created_at"], msg_id),
    )
    for row in rows:
        execute("DELETE FROM messages WHERE id=?", (row["id"],))
    return len(rows)


def create_task(title: str, prompt: str, mode: str = "auto", schedule: str = "", chat_id: str = "") -> Dict[str, Any]:
    task_id = uid("t_")
    ts = now()
    execute(
        """INSERT INTO tasks(id,title,prompt,status,mode,progress,result,events,schedule,next_run,chat_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, title, prompt, "queued", mode, 0, "", "[]", schedule, ts, chat_id, ts, ts),
    )
    return get_task(task_id)  # type: ignore[return-value]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if row:
        try:
            row["events"] = json.loads(row.get("events") or "[]")
        except Exception:
            row["events"] = []
    return row


def list_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    rows = query("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
    for row in rows:
        try:
            row["events"] = json.loads(row.get("events") or "[]")
        except Exception:
            row["events"] = []
    return rows


def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "events" in fields and not isinstance(fields["events"], str):
        fields["events"] = json.dumps(fields["events"], ensure_ascii=False)
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {sets} WHERE id=?", tuple(fields.values()) + (task_id,))


def append_task_event(task_id: str, event: Dict[str, Any]) -> None:
    task = get_task(task_id)
    if not task:
        return
    events = task.get("events") or []
    events.append({**event, "ts": now()})
    update_task(task_id, events=events[-400:])


def delete_task(task_id: str) -> None:
    execute("DELETE FROM tasks WHERE id=?", (task_id,))


# --------------------------------------------------------------- memory
def remember(kind: str, key: str, value: str, weight: float = 1.0) -> Dict[str, Any]:
    existing = query_one("SELECT * FROM memory WHERE kind=? AND key=?", (kind, key))
    ts = now()
    if existing:
        execute(
            "UPDATE memory SET value=?, weight=?, updated_at=? WHERE id=?",
            (value, weight, ts, existing["id"]),
        )
        return {**existing, "value": value, "weight": weight}
    mem_id = uid("mem_")
    execute(
        "INSERT INTO memory(id,kind,key,value,weight,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (mem_id, kind, key, value, weight, ts, ts),
    )
    return {"id": mem_id, "kind": kind, "key": key, "value": value, "weight": weight}


def recall(kind: str = "", limit: int = 80) -> List[Dict[str, Any]]:
    if kind:
        return query(
            "SELECT * FROM memory WHERE kind=? ORDER BY weight DESC, updated_at DESC LIMIT ?",
            (kind, limit),
        )
    return query("SELECT * FROM memory ORDER BY weight DESC, updated_at DESC LIMIT ?", (limit,))


def forget(mem_id: str) -> None:
    execute("DELETE FROM memory WHERE id=?", (mem_id,))


# ------------------------------------------------------------- approvals
def create_approval(tool: str, args: Dict, risk: str, reason: str, chat_id: str = "", task_id: str = "") -> Dict[str, Any]:
    app_id = uid("a_")
    execute(
        """INSERT INTO approvals(id,chat_id,task_id,tool,args,risk,reason,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (app_id, chat_id, task_id, tool, json.dumps(args, ensure_ascii=False), risk, reason, "pending", now()),
    )
    return get_approval(app_id)  # type: ignore[return-value]


def get_approval(app_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM approvals WHERE id=?", (app_id,))
    if row:
        try:
            row["args"] = json.loads(row.get("args") or "{}")
        except Exception:
            row["args"] = {}
    return row


def list_approvals(status: str = "pending") -> List[Dict[str, Any]]:
    rows = query("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT 50", (status,))
    for row in rows:
        try:
            row["args"] = json.loads(row.get("args") or "{}")
        except Exception:
            row["args"] = {}
    return rows


def decide_approval(app_id: str, status: str) -> None:
    execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?", (status, now(), app_id))


# ---------------------------------------------------------- notifications
def notify(title: str, body: str = "", level: str = "info") -> Dict[str, Any]:
    note_id = uid("n_")
    execute(
        "INSERT INTO notifications(id,level,title,body,read,created_at) VALUES(?,?,?,?,0,?)",
        (note_id, level, title, body, now()),
    )
    return {"id": note_id, "level": level, "title": title, "body": body, "read": 0, "created_at": now()}


def list_notifications(limit: int = 40) -> List[Dict[str, Any]]:
    return query("SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,))


def mark_notifications_read() -> None:
    execute("UPDATE notifications SET read=1 WHERE read=0")


# ----------------------------------------------------------------- usage
def log_usage(provider: str, model: str, tier: str, prompt_tokens: int, completion_tokens: int, cost_rub: float) -> None:
    execute(
        """INSERT INTO usage(id,provider,model,tier,prompt_tokens,completion_tokens,cost_rub,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (uid("u_"), provider, model, tier, prompt_tokens, completion_tokens, cost_rub, now()),
    )


def usage_summary(hours: int = 24) -> Dict[str, Any]:
    since = now() - hours * 3600
    row = query_one(
        """SELECT COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct,
                  COALESCE(SUM(cost_rub),0) AS cost, COUNT(*) AS calls
           FROM usage WHERE created_at > ?""",
        (since,),
    )
    by_model = query(
        """SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_rub),0) AS cost
           FROM usage WHERE created_at > ? GROUP BY model ORDER BY calls DESC LIMIT 8""",
        (since,),
    )
    return {"total": row or {}, "by_model": by_model}
