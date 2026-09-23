"""HTTP-сервер JARVIS: статика + JSON API + SSE-стрим агента. Только stdlib."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (__version__, agent, auto, billing, db, ideas, llm, orchestrator,
               sandbox, telemetry, tools)
from .config import CONFIG, WORKSPACE, HOME
from .tools import media
from .tools import system as system_tools

WEB_DIR = Path(__file__).parent / "web"
VERSION = __version__

# Верхний предел POST-тела: вложение до 25 МБ в base64 (~33 МБ) + JSON-обвязка.
MAX_BODY_BYTES = 48 * 1024 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Активные foreground-прогоны. Раньше Stop рвал только SSE-соединение, а сам
# агент продолжал жить до конца: спрашивал санкции, двигал мышью, доводил
# «молчальную» генерацию. Теперь каждый прогон регистрирует здесь свой
# stop-флаг, и /api/chat/stop гасит его — агент видит отмену в своих
# контрольных точках (цикл шагов, ожидание санкции/ответа, стрим).
_RUN_STOPS: Dict[str, threading.Event] = {}
_ACTIVE_RUNS: Dict[str, "agent.Agent"] = {}
_RUN_LOCK = threading.Lock()


def _stop_run(token: str) -> bool:
    """Поставить прогону флаг отмены. True — прогон был найден и жив."""
    with _RUN_LOCK:
        event = _RUN_STOPS.get(token)
    if event is None:
        return False
    event.set()
    return True



def _origin_allowed(origin: str, host: str) -> bool:
    """Пропускать только same-origin и localhost.

    Раньше все ответы несли ``Access-Control-Allow-Origin: *``: любая страница
    в браузере пользователя могла прочитать локальный API (историю, память,
    конфиг) и отправить сообщение от его имени. Теперь CORS выдаётся только
    браузерному же источнику этого приложения (Origin совпадает с Host, что
    покрывает и работу за обратным прокси с proxy_set_header Host $host) и
    localhost; запросы без Origin (curl, тесты) не ограничиваются.

    Origin «null» (песочница iframe на чужой странице) отвергается: отражать
    его в ACAO значило бы дать чужой странице читать ответы.
    """
    if not origin:
        return True
    if origin.lower() == "null":
        return False
    try:
        parsed = urllib.parse.urlparse(origin)
        origin_loc = parsed.netloc.lower()
        if not origin_loc:
            return True
        if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        return origin_loc == (host or "").lower()
    except ValueError:
        return False


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _canonical_response_content(chunks: List[str], done_content: Any) -> str:
    """Один владелец текста ответа: ровно тот поток, который увидел браузер.

    Многошаговый Agent отдаёт delta каждого шага, но его done.content исторически
    содержал только последний шаг. Фронтенд уже показывал весь поток, затем видел
    несовпадающий финал, стирал DOM и печатал последний кусок заново. Если delta
    были, именно их точная склейка является каноническим ответом и для done, и
    для БД. done.content остаётся запасным путём для непроточных ответов.
    """
    streamed = "".join(str(chunk or "") for chunk in chunks)
    if streamed.strip():
        return streamed
    return str(done_content or "")


def _finish_local_post(chat_id: str, text: str, *, title: bool) -> None:
    """Do cheap local bookkeeping only after the foreground SSE is closed.

    The former daemon could start a 25-second semantic LLM request just before
    the user's next message. Memory is now written by the verbatim local parser
    before generation; this boundary deliberately contains no network work.
    """
    if not title:
        return
    try:
        db.rename_chat(chat_id, orchestrator.make_chat_title(text))
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "JARVIS/" + VERSION

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        if os.environ.get("JARVIS_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- helpers
    def _origin_ok(self) -> bool:
        return _origin_allowed(self.headers.get("Origin") or "",
                               self.headers.get("Host") or "")

    def _reject_origin(self) -> bool:
        """Чужой Origin — отказать ДО выполнения логики запроса.

        Проверка в момент отправки ответа была бы бесполезна: POST уже успел
        бы удалить диалог или отправить сообщение. Сторонняя страница в
        браузере пользователя не должна ни читать локальный API, ни действовать
        через него.
        """
        if self._origin_ok():
            return False
        try:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass
        self.close_connection = True
        return True

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._origin_ok():
            # CORS-заголовок нужен только браузерному же источнику приложения
            # (same-origin POST и SSE); без Origin заголовок не обязателен.
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(data))

    def _body(self) -> Optional[Dict[str, Any]]:
        """Прочитать JSON-тело; None означает «уже отвечено, запрос прервать»."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        if length > MAX_BODY_BYTES:
            # тело не читаем: нарушивший лимит запрос не должен попадать в
            # память целиком; соединение закрываем, чтобы не рассинхронизировать
            # поток с непрочитанными байтами
            self.close_connection = True
            self._json({"ok": False, "error": "тело запроса слишком большое"}, 413)
            return None
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
        if self._origin_ok():
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
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
        if self._reject_origin():
            return
        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_origin():
            return
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
        if path == "/api/computer/status":
            # Самопроверка режима «Компьютер» до его включения: фронт показывает
            # конкретную причину (не macOS / нет прав / слепая модель), а не
            # молчаливое бездействие после. Ошибка проверки — тоже ответ, не 500.
            try:
                status = system_tools.computer_status()
            except Exception as exc:  # pragma: no cover - защита полосы
                status = {"ok": False, "error": "Самопроверка не удалась: %s" % exc}
            return self._json({"ok": True, "computer": status})
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
            return self._json({"ok": True, "tasks": db.list_tasks(),
                               "paused": auto.is_paused()})
        if path == "/api/approvals":
            return self._json({"ok": True, "approvals": db.list_approvals()})
        if path == "/api/notifications":
            return self._json({"ok": True, "notifications": db.list_notifications()})
        if path == "/api/memory":
            agent.repair_legacy_automatic_memories()
            return self._json({"ok": True, "memory": db.recall()})
        if path == "/api/scenarios":
            return self._json({"ok": True, "scenarios": db.list_scenarios()})
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
        if self._reject_origin():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if body is None:
            return

        if path == "/api/chat/stream":
            return self._chat_stream(body)
        if path == "/api/scenarios/new":
            steps = [str(x).strip() for x in (body.get("steps") or [])
                     if str(x).strip()]
            if len(steps) < 1:
                return self._json({"ok": False, "error": "нужен хотя бы один шаг"})
            return self._json({"ok": True,
                               "scenario": db.create_scenario(body.get("title") or "",
                                                              steps,
                                                              body.get("emoji") or "")})
        if path == "/api/scenarios/delete":
            return self._json({"ok": True, "deleted": db.delete_scenario(body.get("id", ""))})
        if path == "/api/scenarios/update":
            steps = [str(x).strip() for x in (body.get("steps") or [])
                     if str(x).strip()]
            if len(steps) < 1:
                return self._json({"ok": False, "error": "нужен хотя бы один шаг"})
            updated = db.update_scenario(body.get("id", ""),
                                         body.get("title") or "",
                                         steps, body.get("emoji") or "")
            if not updated:
                return self._json({"ok": False, "error": "сценарий не найден"})
            return self._json({"ok": True, "scenario": updated})
        if path == "/api/computer/permissions":
            # Кнопка «Открыть настройки прав» нажимается самим пользователем —
            # это и есть согласие открыть Системные настройки.
            try:
                opened = system_tools.open_permissions(str(body.get("pane") or "accessibility"))
            except Exception as exc:  # pragma: no cover - защита полосы
                opened = {"ok": False, "error": str(exc)}
            return self._json(opened)
        if path == "/api/budget":
            # Лимит ₽ на лету: работает и до отправки (просто состояние),
            # и во время ответа — активный прогон подхватывает новый лимит
            # с учётом уже потраченного.
            try:
                limit = float(body.get("budget_rub") or 0)
            except (TypeError, ValueError):
                limit = 0.0
            with _RUN_LOCK:
                runner = _ACTIVE_RUNS.get(str(body.get("chat_id") or ""))
                if runner is not None:
                    runner.budget_rub = (limit if limit > 0 else None)
            return self._json({"ok": True, "budget_rub": limit if limit > 0 else None,
                               "applied_to_run": runner is not None})
        if path == "/api/chat/stop":
            # Stop = стоп ВСЕЙ работы прогона, а не только SSE-картинки:
            # инструментам, санкциям и computer-use приходит отмена.
            token = str(body.get("run_token") or "")
            return self._json({"ok": True, "stopped": _stop_run(token)})
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
            started = auto.launch_task(body.get("task_id", ""), manual=True)
            return self._json({"ok": started,
                               "error": "AUTO на паузе или уже занят" if not started else ""})
        if path == "/api/tasks/delete":
            task_id = body.get("task_id", "")
            auto.cancel_task(task_id)
            db.delete_task(task_id)
            return self._json({"ok": True})
        if path == "/api/tasks/cancel":
            return self._json({"ok": auto.cancel_task(body.get("task_id", ""))})
        if path == "/api/tasks/clear-completed":
            return self._json({"ok": True, "deleted": db.delete_completed_tasks()})
        if path == "/api/tasks/pause-all":
            return self._json({"ok": True, "paused": True, "changed": auto.pause_all()})
        if path == "/api/tasks/resume-all":
            return self._json({"ok": True, "paused": False, "changed": auto.resume_all()})
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
        # иерархическая проверка: строковый префикс пропускал соседние каталоги
        if not target.is_relative_to(WEB_DIR.resolve()) or not target.exists():
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
        # клиентский лимит 25 МБ легко обходится прямым POST — проверяем факт
        if len(raw) > MAX_UPLOAD_BYTES:
            return {"ok": False, "error": "файл больше 25 МБ"}
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
        # Убираем legacy-дубли памяти до первого показа карточек. Функция
        # process-local idempotent и после первого state-запроса ничего не делает.
        agent.repair_legacy_automatic_memories()
        tasks = db.list_tasks(limit=50)
        active = [t for t in tasks if t.get("status") in ("running", "queued", "scheduled", "paused")]
        # UI glow означает именно выполняемую сейчас работу. Очередь и расписание
        # остаются в badge, но не имеют права выдавать ожидание за активность.
        running = [t for t in tasks if t.get("status") == "running"]
        notes = db.list_notifications(20)
        return {
            "ok": True,
            "version": VERSION,
            "tasks": tasks,
            "active_tasks": len(active),
            "running_tasks": len(running),
            "auto_paused": auto.is_paused(),
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
        foreground_span = telemetry.Span(
            "foreground", agent_mode=agent_mode, computer_use=computer_use,
            attachment_count=len(attachments))

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
            foreground_span.finish("error", error_type="no_provider")
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

        # Очевидные факты первого лица сохраняются на входной границе. Черновой
        # фильтр локален (обычная реплика без фактов не платит за вызов), а
        # структуру решает nano-модель: «я люблю кошек» должно стать
        # «любимое животное: кошки», а не «я люблю: кошек».
        saved_facts = agent.remember_smart_facts(text)
        memory_facts = [{"kind": item.get("kind", "fact"),
                         "key": item.get("key", ""),
                         "value": item.get("value", "")} for item in saved_facts]
        if memory_facts:
            self._sse({"type": "memory_saved", "count": len(memory_facts),
                       "facts": memory_facts})

        # Окончательный title локален и применяется после закрытия SSE. Память
        # уже записана verbatim parser выше — скрытого auxiliary LLM больше нет.
        history_all = db.get_messages(chat_id)
        post_title = len([m for m in history_all if m["role"] == "user"]) == 1

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
            foreground_span.first_token()
            self._sse({"type": "done", "content": "", "files": [], "tools": ["schedule_task"]})
            self._sse({"type": "end"})
            self._sse_close()
            foreground_span.finish("ok", background=True)
            _finish_local_post(chat_id, text, title=post_title)
            return

        # сборка контекста
        history = orchestrator.summarize_history([
            {"role": m["role"], "content": m["content"]}
            for m in history_all
            if m["role"] in ("user", "assistant") and m["content"]
            # прерванные ответы — не ответы: обрывок в контексте путал модель
            and not (m.get("meta") or {}).get("interrupted")
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

        runner = agent.Agent(chat_id=chat_id, agent_mode=agent_mode, computer_use=computer_use,
                             cancel_check=lambda: False)
        messages = [{"role": "system", "content": agent.build_system_prompt(
            agent_mode, computer_use, vision_direct=runner._vision_direct)}]
        messages.extend(history)
        # Один короткий nearby-контракт ставится перед КАЖДЫМ актуальным user
        # turn. Раньше напоминание было только рядом с изображением, поэтому
        # следующий текст «давай уточним» снова терял controls. Vision-добавка
        # объединяется здесь же: один источник протокола и всё тот же LLM-call.
        # Экономия префилла: контракт нужен там, где реально возможен выбор —
        # агентский ход, входной кадр или вежливость не в счёт. Обычная реплика
        # без инструментов уже несёт полный протокол в системном промпте
        # (правило 10), и ~340 токенам рядом с ней делать нечего.
        if (agent_mode or has_image) and not orchestrator.is_social_only(text):
            messages.append({"role": "system", "content": agent.turn_ui_contract(has_image)})
        if agent_mode and body.get("silent"):
            # Ответ панели — продолжение того же AGENT preflight, а не повод
            # открыть новую анкету. Детерминированная turn-граница сильнее
            # вероятностного «максимум один вопрос»: всё некритичное после неё
            # агент решает сам и переходит к инструментам.
            messages.append({"role": "system", "content":
                "AGENT PREFLIGHT ЗАВЕРШЁН: текущая реплика содержит единый ответ "
                "пользователя на предыдущую interactive-панель. Запрещено задавать "
                "ещё один уточняющий вопрос или показывать новый ui-блок. Считай "
                "критические параметры собранными, некритичные выбери разумно и "
                "сразу продолжай автономное выполнение инструментами."})
        messages.append(user_message)

        # Регистрация прогона для Stop: флаг отмены + контрольная функция.
        # Обрыв соединения отменяет только computer-use (кликать по экрану без
        # зрителя нельзя); обычный чат по-прежнему доигрывается молча и
        # сохраняется в переписку — это осознанное поведение, а не утечка.
        run_token = str(body.get("run_token") or "")
        stop_event = threading.Event()
        if run_token:
            with _RUN_LOCK:
                _RUN_STOPS[run_token] = stop_event
        # Живой прогон этого диалога: монетка ₽ меняет лимит на лету
        with _RUN_LOCK:
            _ACTIVE_RUNS[chat_id] = runner
        alive_box = [True]

        def _run_cancelled() -> bool:
            return stop_event.is_set() or (computer_use and not alive_box[0])

        runner.cancel_check = _run_cancelled
        runner.agent_mode = agent_mode
        runner.computer_use = computer_use

        try:
            budget_rub = float(body.get("budget_rub") or 0)
        except (TypeError, ValueError):
            budget_rub = 0.0

        # ПРОАКТИВНЫЙ РЕЖИМ ДО МОДЕЛИ: реплика явно требует выключенной кнопки
        # («нажми…», «напиши игру…», «как я выгляжу…»). Слабая модель молчит —
        # локальный триггер предлагает режим мгновенно, пользователь решает,
        # и прогон сразу стартует в правильном режиме.
        mode_hint = agent.suggest_mode(text, agent_mode=agent_mode,
                                       computer_use=computer_use)
        if mode_hint:
            labels = {"agent": "AGENT", "computer": "Компьютер",
                      "camera": "Камера", "budget": "Лимит ₽"}
            label = labels.get(mode_hint["mode"], mode_hint["mode"])
            question = ("Включить режим «%s»? Причина: %s"
                        % (label, mode_hint["reason"]))
            record = db.create_question(chat_id, question, ["Включить", "Не нужно"])
            self._sse({"type": "mode_request", "id": record["id"],
                       "mode": mode_hint["mode"], "label": label,
                       "reason": mode_hint["reason"], "question": question})
            # ждём именно ЭТОТ вопрос: _wait_answer создал бы свой id,
            # и ответ пользователя уходил бы мимо
            decided = record
            deadline = time.time() + 300
            while time.time() < deadline:
                if _run_cancelled():
                    db.answer_question(record["id"], "cancelled")
                    break
                fresh = db.get_question(record["id"])
                if fresh and fresh.get("status") == "answered":
                    decided = fresh
                    break
                time.sleep(0.4)
            if (decided.get("status") == "answered"
                    and str(decided.get("answer") or "").startswith("Включить")):
                self._sse({"type": "mode_changed", "mode": mode_hint["mode"], "on": True})
                if mode_hint["mode"] == "agent":
                    agent_mode = True
                elif mode_hint["mode"] == "computer":
                    agent_mode = True
                    computer_use = True
                # промпт уже собран с прошлыми режимами — пересобираем честно
                messages[0] = {"role": "system", "content": agent.build_system_prompt(
                    agent_mode, computer_use, vision_direct=runner._vision_direct)}
                runner = agent.Agent(chat_id=chat_id, agent_mode=agent_mode,
                                     computer_use=computer_use,
                                     cancel_check=_run_cancelled)
                runner.budget_rub = budget_rub
            else:
                self._sse({"type": "mode_declined", "mode": mode_hint["mode"]})
        if budget_rub > 0:
            runner.budget_rub = budget_rub
        # Prompt просит дождаться выбора, а этот флаг делает ожидание границей
        # исполнения: generate_image не будет dispatch-нут для неопределённой
        # творческой обработки кадра, даже если конкретная модель проигнорирует
        # инструкцию. Конкретный «в стиле X / про Y» проходит без остановки.
        require_ui_choice = agent.needs_creative_image_choice(text, has_image)
        final_text = ""
        files: List[Dict[str, Any]] = []
        used_tools: List[str] = []
        selected_tier = ""
        alive = True
        run_error = ""
        partial: List[str] = []
        thinking: List[str] = []
        trace: List[Dict[str, Any]] = (
            [{"kind": "memory", "facts": memory_facts}] if memory_facts else [])
        try:
            for event in runner.run(
                    messages, user_text=text, has_image=has_image,
                    require_ui_choice=require_ui_choice,
                    preflight_resolved=bool(agent_mode and body.get("silent"))):
                etype = event.get("type")
                if etype == "route":
                    selected_tier = str(event.get("tier") or "")
                elif etype == "delta":
                    foreground_span.first_token()
                    partial.append(event.get("text", ""))
                elif etype == "error":
                    run_error = str(event.get("error") or "agent_error")
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
                    # `done` не имеет права подменить уже показанный поток своей
                    # альтернативной версией. Канонизируем ДО отправки события,
                    # затем ту же строку сохраняем — UI и история тождественны.
                    final_text = _canonical_response_content(partial, event.get("content", ""))
                    event = dict(event)
                    event["content"] = final_text
                    files = event.get("files", [])
                    used_tools = event.get("tools", [])
                    selected_tier = str(event.get("tier") or selected_tier)
                if alive:
                    alive = self._sse(event)
                    alive_box[0] = alive
                # Если пользователь ушёл из диалога, соединение рвётся. Раньше мы
                # прекращали работу и ответ пропадал. Теперь генерация доводится
                # до конца молча, а результат сохраняется в переписку.
                # (computer-use — исключение: обрыв там означает полную отмену)
        except Exception as exc:
            run_error = type(exc).__name__
            if alive:
                self._sse({"type": "error", "error": str(exc)})
        finally:
            if run_token:
                with _RUN_LOCK:
                    _RUN_STOPS.pop(run_token, None)
            with _RUN_LOCK:
                if _ACTIVE_RUNS.get(chat_id) is runner:
                    _ACTIVE_RUNS.pop(chat_id, None)
            if not final_text:
                final_text = _canonical_response_content(partial, "")
            if final_text:
                # Ход мыслей и список действий сохраняем вместе с ответом: раньше
                # они жили только в браузере и пропадали, стоило выйти из диалога.
                # Прерванный пользователем ответ помечается: обрывок кода не должен
                # прикидываться полноценным ответом в контексте следующего запроса
                # (модель продолжала «дописывать» несуществующий ответ).
                db.add_message(chat_id, "assistant", final_text,
                               {"files": files, "tools": used_tools, "model": runner.model_used,
                                "tier": selected_tier,
                                "interrupted": bool(stop_event.is_set()),
                                "thinking": "".join(thinking)[:20000], "trace": trace[:60]})
            if alive:
                self._sse({"type": "end"})
            self._sse_close()
            foreground_span.finish(
                "error" if run_error else "ok", model=runner.model_used,
                tier=selected_tier, tool_count=len(used_tools),
                error_type=run_error or None)
            _finish_local_post(chat_id, text, title=post_title)


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
