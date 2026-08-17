"""HTTP-сервер JARVIS: статика + JSON API + SSE-стрим агента. Только stdlib."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socket
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import agent, auto, db, llm, orchestrator, tools
from .config import CONFIG, WORKSPACE, HOME
from .tools import media

WEB_DIR = Path(__file__).parent / "web"
VERSION = "1.0.0"


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JARVIS/" + VERSION

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        if os.environ.get("JARVIS_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(data))

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        # поток не имеет Content-Length: завершение обозначаем закрытием
        # соединения, иначе браузер ждёт продолжения и не считает поток
        # оконченным (кнопка «Стоп» висит до таймаута)
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

    def _sse_close(self) -> None:
        """Корректно закрыть поток, чтобы клиент увидел конец."""
        self.close_connection = True
        try:
            self.wfile.flush()
        except Exception:
            pass
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except Exception:
            pass

    def _sse(self, event: Dict[str, Any]) -> bool:
        try:
            chunk = "data: %s\n\n" % json.dumps(event, ensure_ascii=False)
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ------------------------------------------------------------------ GET
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])

        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/config":
            return self._json(CONFIG.public())
        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION, "providers": llm.health(),
                               "usage": db.usage_summary()})
        if path == "/api/models":
            provider = (params.get("provider") or ["cloudru"])[0]
            return self._json({"ok": True, "models": llm.list_models(provider, force=True),
                               "tiers": {t: llm.pick_model(t, provider)
                                         for t in ("nano", "base", "smart", "coder", "vision")}})
        if path == "/api/chats":
            return self._json({"ok": True, "chats": db.list_chats()})
        if path == "/api/messages":
            chat_id = (params.get("chat_id") or [""])[0]
            return self._json({"ok": True, "messages": db.get_messages(chat_id)})
        if path == "/api/tasks":
            return self._json({"ok": True, "tasks": db.list_tasks()})
        if path == "/api/approvals":
            return self._json({"ok": True, "approvals": db.list_approvals()})
        if path == "/api/notifications":
            return self._json({"ok": True, "notifications": db.list_notifications()})
        if path == "/api/memory":
            return self._json({"ok": True, "memory": db.recall()})
        if path == "/api/files":
            return self._json(tools.call("list_files", {}))
        if path == "/api/files/download":
            return self._download((params.get("name") or [""])[0])
        if path == "/api/usage":
            return self._json({"ok": True, **db.usage_summary()})

        self._json({"ok": False, "error": "not found"}, 404)

    # ----------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        if path == "/api/chat/stream":
            return self._chat_stream(body)
        if path == "/api/chats/new":
            return self._json({"ok": True, "chat": db.create_chat(body.get("title") or "Новый диалог")})
        if path == "/api/chats/rename":
            db.rename_chat(body.get("chat_id", ""), body.get("title", ""))
            return self._json({"ok": True})
        if path == "/api/chats/delete":
            db.delete_chat(body.get("chat_id", ""))
            return self._json({"ok": True})
        if path == "/api/tasks/new":
            task = db.create_task(body.get("title") or "Задача",
                                  body.get("prompt") or "",
                                  schedule=body.get("schedule") or "")
            return self._json({"ok": True, "task": task})
        if path == "/api/tasks/run":
            threading.Thread(target=auto.execute_task, args=(body.get("task_id", ""),), daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/tasks/delete":
            db.delete_task(body.get("task_id", ""))
            return self._json({"ok": True})
        if path == "/api/tasks/cancel":
            db.update_task(body.get("task_id", ""), status="cancelled")
            return self._json({"ok": True})
        if path == "/api/approvals/decide":
            db.decide_approval(body.get("id", ""), body.get("decision", "rejected"))
            return self._json({"ok": True})
        if path == "/api/notifications/read":
            db.mark_notifications_read()
            return self._json({"ok": True})
        if path == "/api/config/update":
            CONFIG.update(body.get("patch") or {})
            return self._json({"ok": True, "config": CONFIG.public()})
        if path == "/api/memory/add":
            return self._json({"ok": True, "item": db.remember(body.get("kind", "fact"),
                                                               body.get("key", ""), body.get("value", ""))})
        if path == "/api/memory/delete":
            db.forget(body.get("id", ""))
            return self._json({"ok": True})
        if path == "/api/upload":
            return self._json(self._upload(body))
        if path == "/api/vision":
            return self._json(self._vision(body))
        if path == "/api/transcribe":
            return self._json(media.transcribe_audio(body.get("audio", ""), body.get("language", "ru")))
        if path == "/api/tool":
            return self._json(tools.call(body.get("name", ""), body.get("args") or {}))
        if path == "/api/computer/screenshot":
            return self._json(tools.call("screenshot", {}))

        self._json({"ok": False, "error": "not found"}, 404)

    # -------------------------------------------------------------- статика
    def _serve_static(self, rel: str) -> None:
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.exists():
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _download(self, name: str) -> None:
        if not name:
            return self._json({"ok": False, "error": "нет имени файла"}, 400)
        target = (WORKSPACE / name).resolve()
        if not str(target).startswith(str(WORKSPACE.resolve())) or not target.exists():
            return self._json({"ok": False, "error": "файл не найден"}, 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        disposition = "inline" if ctype.startswith("image/") else "attachment"
        self._send(200, target.read_bytes(), ctype,
                   {"Content-Disposition": '%s; filename="%s"' % (disposition, target.name)})

    # ------------------------------------------------------------- загрузки
    def _upload(self, body: Dict[str, Any]) -> Dict[str, Any]:
        name = re.sub(r"[^\w.\-() ]+", "_", body.get("name") or "upload.bin")[:120]
        data_url = body.get("data") or ""
        if "," in data_url:
            data_url = data_url.split(",", 1)[1]
        try:
            raw = base64.b64decode(data_url)
        except Exception:
            return {"ok": False, "error": "не удалось прочитать файл"}
        dest = WORKSPACE / name
        dest.write_bytes(raw)
        kind = "image" if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) else "file"
        preview = ""
        if kind == "file" and name.lower().endswith((".txt", ".md", ".csv", ".json", ".py", ".js", ".html")):
            try:
                preview = dest.read_text("utf-8")[:6000]
            except Exception:
                preview = ""
        return {"ok": True, "name": name, "size": len(raw), "kind": kind, "preview": preview,
                "download_url": "/api/files/download?name=" + urllib.parse.quote(name)}

    def _vision(self, body: Dict[str, Any]) -> Dict[str, Any]:
        image = body.get("image") or ""
        question = body.get("question") or "Что ты видишь? Опиши кратко и по делу."
        if not image:
            return {"ok": False, "error": "нет изображения"}
        return media.analyze_image(image, question)

    # --------------------------------------------------------------- состояние
    def _state(self) -> Dict[str, Any]:
        tasks = db.list_tasks(limit=50)
        active = [t for t in tasks if t.get("status") in ("running", "queued", "scheduled")]
        notes = db.list_notifications(20)
        return {
            "ok": True,
            "version": VERSION,
            "tasks": tasks,
            "active_tasks": len(active),
            "approvals": db.list_approvals(),
            "notifications": notes,
            "unread": len([n for n in notes if not n.get("read")]),
            "usage": db.usage_summary(),
            "config": CONFIG.public(),
            "providers_ready": bool(llm.active_providers()),
            "home": str(HOME),
        }

    # ------------------------------------------------------------ SSE-чат
    def _chat_stream(self, body: Dict[str, Any]) -> None:
        chat_id = body.get("chat_id") or ""
        text = (body.get("text") or "").strip()
        agent_mode = bool(body.get("agent_mode"))
        computer_use = bool(body.get("computer_use"))
        attachments: List[Dict[str, Any]] = body.get("attachments") or []

        if not chat_id:
            chat_id = db.create_chat(text[:40] or "Новый диалог")["id"]

        self._sse_open()
        self._sse({"type": "chat", "chat_id": chat_id})

        if not llm.active_providers():
            self._sse({"type": "error", "error": "Не заданы API-ключи. Открой Настройки и вставь ключ Cloud.ru."})
            self._sse({"type": "end"})
            self._sse_close()
            return

        # пользовательское сообщение
        user_meta = {"attachments": [{"name": a.get("name"), "kind": a.get("kind"),
                                      "url": a.get("download_url")} for a in attachments],
                     "agent_mode": agent_mode, "computer_use": computer_use}
        db.add_message(chat_id, "user", text, user_meta)

        # авто-название чата
        history_all = db.get_messages(chat_id)
        if len([m for m in history_all if m["role"] == "user"]) == 1:
            db.rename_chat(chat_id, (text[:38] or "Новый диалог"))
            self._sse({"type": "chat_title", "title": text[:38] or "Новый диалог"})

        # фон?
        decision = auto.should_background(text)
        if decision["background"] and not computer_use:
            task = db.create_task(title=text[:48] or "Фоновая задача", prompt=text,
                                  schedule=decision["schedule"], chat_id=chat_id)
            note = ("Задача ушла в фон — вкладка **AUTO**. Пришлю результат, как только будет готово."
                    + (" Расписание: `%s`." % decision["schedule"] if decision["schedule"] else ""))
            self._sse({"type": "background", "task_id": task["id"], "title": task["title"],
                       "schedule": decision["schedule"]})
            self._sse({"type": "delta", "text": note})
            db.add_message(chat_id, "assistant", note, {"task_id": task["id"]})
            self._sse({"type": "done", "content": note, "files": [], "tools": ["schedule_task"]})
            self._sse({"type": "end"})
            self._sse_close()
            return

        # сборка контекста
        history = orchestrator.summarize_history([
            {"role": m["role"], "content": m["content"]}
            for m in history_all if m["role"] in ("user", "assistant") and m["content"]
        ][:-1])

        content_parts: List[Any] = []
        has_image = False
        text_for_model = text
        for att in attachments:
            if att.get("kind") == "image" and att.get("data"):
                has_image = True
                content_parts.append({"type": "image_url", "image_url": {"url": att["data"]}})
            elif att.get("preview"):
                text_for_model += "\n\n[Файл %s]\n%s" % (att.get("name"), att["preview"][:6000])
            elif att.get("name"):
                text_for_model += "\n\n[Прикреплён файл в песочнице: %s]" % att["name"]

        if has_image:
            content_parts.insert(0, {"type": "text", "text": text_for_model or "Опиши, что на изображении."})
            user_message: Dict[str, Any] = {"role": "user", "content": content_parts}
        else:
            user_message = {"role": "user", "content": text_for_model}

        messages = [{"role": "system", "content": agent.build_system_prompt(agent_mode, computer_use)}]
        messages.extend(history)
        messages.append(user_message)

        runner = agent.Agent(chat_id=chat_id, agent_mode=agent_mode, computer_use=computer_use)
        final_text = ""
        files: List[Dict[str, Any]] = []
        used_tools: List[str] = []
        alive = True
        try:
            for event in runner.run(messages, user_text=text, has_image=has_image):
                if event.get("type") == "done":
                    final_text = event.get("content", "")
                    files = event.get("files", [])
                    used_tools = event.get("tools", [])
                alive = self._sse(event)
                if not alive:
                    break
        except Exception as exc:
            self._sse({"type": "error", "error": str(exc)})
        finally:
            if final_text:
                db.add_message(chat_id, "assistant", final_text,
                               {"files": files, "tools": used_tools, "model": runner.model_used})
            if alive:
                self._sse({"type": "end"})
            self._sse_close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_port(preferred: int, host: str) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return preferred


def run() -> None:
    host = CONFIG.get("server.host", "127.0.0.1")
    port = find_port(int(CONFIG.get("server.port", 8765)), host)
    auto.start()
    httpd = Server((host, port), Handler)
    url = "http://%s:%d/" % ("localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    banner = """
   ╦╔═╗╦═╗╦  ╦╦╔═╗
   ║╠═╣╠╦╝╚╗╔╝║╚═╗   персональный ИИ-агент
  ╚╝╩ ╩╩╚═ ╚╝ ╩╚═╝   v%s

  Интерфейс:  %s
  Данные:     %s
  Остановить: Ctrl+C
""" % (VERSION, url, HOME)
    print(banner, flush=True)
    if CONFIG.get("server.open_browser", True) and not os.environ.get("JARVIS_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nJARVIS остановлен.")
    finally:
        auto.stop()
        httpd.server_close()


if __name__ == "__main__":
    run()
