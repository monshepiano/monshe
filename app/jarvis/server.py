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

from . import agent, auto, billing, db, ideas, llm, orchestrator, sandbox, tools
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
        if path == "/api/ideas":
            # только готовое: считать здесь нельзя — экран ждать не должен
            return self._json({"ok": True, "ideas": ideas.current()})
        if path == "/api/files":
            chat_id = (params.get("chat_id") or params.get("chat") or [""])[0]
            return self._json({"ok": True, "files": sandbox.listing(chat_id),
                               "sandbox": sandbox.info(chat_id)})
        if path == "/api/sandbox":
            chat_id = (params.get("chat_id") or params.get("chat") or [""])[0]
            return self._json(sandbox.info(chat_id))
        if path == "/api/files/browse":
            chat_id = (params.get("chat_id") or params.get("chat") or [""])[0]
            subdir = (params.get("dir") or params.get("path") or [""])[0]
            data = sandbox.browse(subdir, chat_id)
            data["sandbox"] = sandbox.info(chat_id)
            return self._json(data)
        if path == "/api/files/view":
            chat_id = (params.get("chat_id") or params.get("chat") or [""])[0]
            return self._json(sandbox.view((params.get("name") or [""])[0], chat_id))
        if path == "/api/files/download":
            return self._download((params.get("name") or [""])[0],
                                  (params.get("chat") or params.get("chat_id") or [""])[0])
        if path == "/api/usage":
            return self._json({"ok": True, **db.usage_summary()})
        if path == "/api/billing":
            force = (params.get("force") or ["0"])[0] in ("1", "true", "yes")
            return self._json({"ok": True, "billing": billing.snapshot(force=force)})

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
            chat_id = body.get("chat_id", "")
            db.delete_chat(chat_id)
            sandbox.drop(chat_id)
            return self._json({"ok": True})
        if path == "/api/tasks/new":
            # через create_background_task, а не напрямую в БД: иначе задача
            # с расписанием («через 10 секунд», «каждый день в 9») стартовала
            # мгновенно, потому что попадала в статус queued
            task = auto.create_background_task(
                title=body.get("title") or "Задача",
                prompt=body.get("prompt") or "",
                schedule=body.get("schedule") or "",
                chat_id=body.get("chat_id") or "")
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
        if path == "/api/replies":
            # Варианты продолжения запрашиваются ОТДЕЛЬНО, уже после того как
            # ответ закрыт: иначе они держали бы поток открытым и кнопка «стоп»
            # горела бы лишние секунды. Считаются по последней паре реплик и
            # сохраняются в сообщение, чтобы при возврате в диалог не считать
            # их заново и не платить второй раз.
            chat_id = body.get("chat_id", "")
            msgs = db.get_messages(chat_id, limit=6) if chat_id else []
            last = msgs[-1] if msgs else None
            if not last or last.get("role") != "assistant":
                return self._json({"ok": True, "items": []})
            meta = last.get("meta") or {}
            if isinstance(meta.get("replies"), list):
                return self._json({"ok": True, "items": meta["replies"]})
            asked = ""
            for m in reversed(msgs[:-1]):
                if m.get("role") == "user":
                    asked = m.get("content", "")
                    break
            items = agent.suggest_replies(asked, last.get("content", ""))
            meta["replies"] = items
            db.update_message_meta(last["id"], meta)
            return self._json({"ok": True, "items": items})
        if path == "/api/questions/answer":
            db.answer_question(body.get("id", ""), str(body.get("answer", ""))[:300])
            return self._json({"ok": True})
        if path == "/api/approvals/decide":
            db.decide_approval(body.get("id", ""), body.get("decision", "rejected"))
            return self._json({"ok": True})
        if path == "/api/notifications/read":
            db.mark_notifications_read()
            return self._json({"ok": True})
        if path == "/api/notifications/delete":
            db.delete_notification(body.get("id", ""))
            return self._json({"ok": True})
        if path == "/api/notifications/clear":
            db.clear_notifications()
            return self._json({"ok": True})
        if path == "/api/config/update":
            CONFIG.update(body.get("patch") or {})
            return self._json({"ok": True, "config": CONFIG.public()})
        if path == "/api/messages/version":
            msg = db.switch_message_version(body.get("id", ""), int(body.get("index", 0)))
            return self._json({"ok": bool(msg), "message": msg})
        if path == "/api/memory/add":
            return self._json({"ok": True, "item": db.remember(body.get("kind", "fact"),
                                                               body.get("key", ""), body.get("value", ""))})
        if path == "/api/memory/update":
            item = db.update_memory(body.get("id", ""), body.get("key", ""), body.get("value", ""))
            return self._json({"ok": bool(item), "item": item})
        if path == "/api/memory/delete":
            db.forget(body.get("id", ""))
            return self._json({"ok": True})
        if path == "/api/sandbox/clear":
            sandbox.set_chat(body.get("chat_id") or "")
            return self._json(sandbox.wipe(body.get("chat_id") or ""))
        if path == "/api/sandbox/rename":
            return self._json(sandbox.rename(body.get("name") or "", body.get("chat_id") or ""))
        if path == "/api/sandbox/delete_file":
            sandbox.set_chat(body.get("chat_id") or "")
            return self._json(sandbox.remove(body.get("name") or "", body.get("chat_id") or ""))
        if path == "/api/sandbox/mkdir":
            return self._json(sandbox.mkdir(body.get("name") or "", body.get("chat_id") or "",
                                            body.get("parent") or ""))
        if path == "/api/sandbox/rename_file":
            return self._json(sandbox.rename_entry(body.get("path") or "", body.get("name") or "",
                                                   body.get("chat_id") or ""))
        if path == "/api/sandbox/move":
            return self._json(sandbox.move(body.get("path") or "", body.get("dest") or "",
                                           body.get("chat_id") or ""))
        if path == "/api/sandbox/delete_many":
            sandbox.set_chat(body.get("chat_id") or "")
            return self._json(sandbox.remove_many(body.get("paths") or [],
                                                  body.get("chat_id") or ""))
        if path == "/api/sandbox/move_many":
            sandbox.set_chat(body.get("chat_id") or "")
            return self._json(sandbox.move_many(body.get("paths") or [], body.get("dest") or "",
                                                body.get("chat_id") or ""))
        if path == "/api/upload":
            return self._json(self._upload(body))
        if path == "/api/vision":
            return self._json(self._vision(body))
        if path == "/api/transcribe":
            return self._json(media.transcribe_audio(body.get("audio", ""), body.get("language", "ru")))
        if path == "/api/tool":
            sandbox.set_chat(body.get("chat_id") or "")
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

    def _download(self, name: str, chat_id: str = "") -> None:
        if not name:
            return self._json({"ok": False, "error": "нет имени файла"}, 400)
        try:
            target = sandbox.safe_path(name, chat_id)
        except ValueError:
            return self._json({"ok": False, "error": "файл не найден"}, 404)
        if not target.exists():
            # старые ссылки могли указывать на общий каталог
            legacy = (WORKSPACE / name).resolve()
            if str(legacy).startswith(str(WORKSPACE.resolve())) and legacy.is_file():
                target = legacy
            else:
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
        chat_id = body.get("chat_id") or ""
        dest = sandbox.root(chat_id) / name
        dest.write_bytes(raw)
        kind = "image" if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) else "file"
        preview = ""
        if kind == "file" and name.lower().endswith((".txt", ".md", ".csv", ".json", ".py", ".js", ".html")):
            try:
                preview = dest.read_text("utf-8")[:6000]
            except Exception:
                preview = ""
        return {"ok": True, "name": name, "size": len(raw), "kind": kind, "preview": preview,
                "download_url": sandbox.dl(name, chat_id)}

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
            "billing": billing.snapshot(),
            "config": CONFIG.public(),
            "silent_tools": tools.silent_names(),   # фронт не хранит свою копию
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
            # Камера ведёт СВОЙ разговор: он нужен ради отдельного контекста,
            # но в списке диалогов ему не место — помечаем видом 'cam'.
            if body.get("kind") == "cam":
                chat_id = db.create_chat("Камера", kind="cam")["id"]
            else:
                chat_id = db.create_chat("Новый диалог")["id"]

        sandbox.set_chat(chat_id)
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
        # выбор из интерактивной панели ```ui: модели он нужен, ленте — нет
        if body.get("silent"):
            user_meta["silent"] = True
        edit_of = body.get("edit_of") or ""
        if edit_of and db.get_message(edit_of):
            # это правка: добавляем ВЕРСИЮ к старому сообщению и убираем
            # устаревший ответ, вместо того чтобы плодить новую пару реплик
            edited = db.edit_message(edit_of, text, chat_id)
            db.delete_messages_after(chat_id, edit_of)
            self._sse({"type": "edited", "id": edit_of,
                       "versions": ((edited or {}).get("meta") or {}).get("versions", []),
                       "version": ((edited or {}).get("meta") or {}).get("version", 0)})
        else:
            saved = db.add_message(chat_id, "user", text, user_meta)
            # без id фронтенд не может превратить правку в новую версию
            self._sse({"type": "user_msg", "id": saved["id"]})

        # Название диалога придумывает модель — но это отдельный запрос к сети.
        # Раньше он выполнялся ДО первого токена ответа, и пользователь ждал
        # молча несколько секунд. Теперь заголовок уезжает в фон.
        history_all = db.get_messages(chat_id)
        if len([m for m in history_all if m["role"] == "user"]) == 1:
            db.rename_chat(chat_id, text[:40].strip() or "Новый диалог")

            def _title(cid: str = chat_id, txt: str = text) -> None:
                try:
                    db.rename_chat(cid, orchestrator.make_chat_title(txt))
                except Exception:
                    pass

            threading.Thread(target=_title, name="jarvis-title", daemon=True).start()

        # Фон? Решение принимает ОДНА сторона — сервер. Раньше сюда же лезла
        # модель через schedule_task, и на одну просьбу появлялись две задачи
        # («Таймер» и «Reminder after 5 seconds» на скриншоте пользователя).
        decision = auto.should_background(text)
        # защита от дублей: такая же задача из этого чата, уже стоящая в очереди
        if decision["background"] and auto.has_similar_pending(text, chat_id):
            decision = {"background": False, "schedule": "", "reason": ""}
        server_scheduled = bool(decision["background"] and not computer_use and not attachments)
        if server_scheduled:
            task_title = orchestrator.make_task_title(text)
            task = auto.create_background_task(title=task_title, prompt=text,
                                               schedule=decision["schedule"], chat_id=chat_id)
            human = auto.describe_schedule(decision["schedule"])
            # Карточка «В фоне» уже сообщает и название, и срок. Дублировать
            # то же самое текстом «Принято…» — лишний шум в диалоге.
            note = ("Задача «%s» — в фоне%s." % (task_title, ", " + human if human else ""))
            self._sse({"type": "background", "task_id": task["id"], "title": task["title"],
                       "schedule": decision["schedule"], "when": human,
                       "reason": decision.get("reason", "")})
            db.add_message(chat_id, "assistant", note,
                           {"task_id": task["id"], "bg_card": True})
            # content пустой: текст уже нарисован карточкой «В фоне»
            self._sse({"type": "done", "content": "", "files": [], "tools": ["schedule_task"]})
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
        partial: List[str] = []
        thinking: List[str] = []
        trace: List[Dict[str, Any]] = []
        try:
            for event in runner.run(messages, user_text=text, has_image=has_image):
                etype = event.get("type")
                if etype == "delta":
                    partial.append(event.get("text", ""))
                elif etype == "reset":
                    partial = []
                elif etype == "thinking":
                    thinking.append(event.get("text", ""))
                elif etype == "tool_start":
                    # служебные «глаза» computer-use в историю не пишем:
                    # иначе при возврате в диалог они снова всплывут строчками
                    if not tools.is_silent(event.get("name", "")):
                        trace.append({"kind": "tool", "name": event.get("name", ""),
                                      "label": event.get("label", ""), "args": event.get("args")})
                elif etype == "plan":
                    trace.append({"kind": "plan", "steps": event.get("steps", [])})
                elif etype == "question":
                    # вопрос с вариантами остаётся в переписке: вернувшись в
                    # диалог, пользователь видит, что спросили и что он выбрал
                    trace.append({"kind": "question", "question": event.get("question", ""),
                                  "options": event.get("options", []),
                                  "answer": event.get("answer", "")})
                elif etype == "done":
                    final_text = event.get("content", "")
                    files = event.get("files", [])
                    used_tools = event.get("tools", [])
                if alive:
                    alive = self._sse(event)
                # Если пользователь ушёл из диалога, соединение рвётся. Раньше мы
                # прекращали работу и ответ пропадал. Теперь генерация доводится
                # до конца молча, а результат сохраняется в переписку.
        except Exception as exc:
            if alive:
                self._sse({"type": "error", "error": str(exc)})
        finally:
            if not final_text:
                final_text = "".join(partial).strip()
            if final_text:
                # Ход мыслей и список действий сохраняем вместе с ответом: раньше
                # они жили только в браузере и пропадали, стоило выйти из диалога.
                db.add_message(chat_id, "assistant", final_text,
                               {"files": files, "tools": used_tools, "model": runner.model_used,
                                "thinking": "".join(thinking)[:20000], "trace": trace[:60]})
            if alive:
                self._sse({"type": "end"})
            self._sse_close()


def _warm_models() -> None:
    """Заранее получить каталоги моделей, чтобы первый ответ не ждал сети."""
    try:
        for prov in (llm.active_providers() or ["cloudru"]):
            llm.list_models(prov)
    except Exception:
        pass


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
    # Каталог моделей греем сразу при старте, в фоне. Пользователь всё равно
    # тратит несколько секунд на то, чтобы открыть окно и набрать вопрос, —
    # пусть это время работает на нас. Иначе первый вопрос за сеанс платил
    # за поход в облако за списком моделей.
    threading.Thread(target=_warm_models, name="jarvis-warm", daemon=True).start()
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
