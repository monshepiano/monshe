"""Stdlib HTTP server: UI, API, SSE. No pip required."""

from __future__ import annotations

import base64
import json
import mimetypes
import posixpath
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import agent, memory, safety, tools
from .config import DATA, SANDBOX, SCREEN, STATIC, UPLOADS, load_settings, new_id, save_settings
from .events import BUS

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("text/css", ".css")

MISSIONS: list[dict] = []
NOTIFS: list[dict] = []


def _json(handler: BaseHTTPRequestHandler, code: int, obj) -> None:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if not n:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _file(handler: BaseHTTPRequestHandler, path: Path, download_name: str | None = None) -> None:
    if not path.exists() or not path.is_file():
        handler.send_error(404)
        return
    data = path.read_bytes()
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    if download_name:
        handler.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS/1.0"

    def log_message(self, fmt, *args):
        # quieter
        if "/api/events" in str(args[0] if args else ""):
            return
        try:
            print(f"[jarvis] {self.address_string()} {args[0]}")
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pin")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = posixpath.normpath(urllib.parse.unquote(parsed.path))
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/JARVIS.zip", "/download", "/download/JARVIS.zip"):
            z = STATIC / "assets" / "JARVIS.zip"
            if not z.exists():
                z = Path("/home/user/monshe/dist/JARVIS.zip")
            return _file(self, z, download_name="JARVIS.zip")
        if path in ("/", "/index.html"):
            return _file(self, STATIC / "index.html")
        if path == "/sw.js":
            return _file(self, STATIC / "sw.js")
        if path == "/manifest.json":
            return _file(self, STATIC / "manifest.json")

        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            return _file(self, STATIC / rel)
        # also allow css/js/assets at root of static
        if path.startswith("/css/") or path.startswith("/js/") or path.startswith("/assets/"):
            return _file(self, STATIC / path.lstrip("/"))

        if path == "/api/state":
            s = load_settings()
            safe = dict(s)
            for k in ("fm_key", "ds_key", "telegram_token"):
                if safe.get(k):
                    safe[k] = safe[k][:6] + "…" + safe[k][-4:]
            files = []
            for p in SANDBOX.rglob("*"):
                if p.is_file():
                    files.append(
                        {
                            "path": str(p.relative_to(SANDBOX)),
                            "size": p.stat().st_size,
                            "mtime": p.stat().st_mtime,
                        }
                    )
            return _json(
                self,
                200,
                {
                    "ok": True,
                    "settings": safe,
                    "raw_settings": s,  # local app, user owns the keys
                    "memory": memory.load_memory(),
                    "history": memory.load_history()[-40:],
                    "files": files[:300],
                    "notifs": NOTIFS[-40:],
                    "missions": MISSIONS[-40:],
                    "pending": [v for v in safety.PENDING.values() if v.get("status") == "pending"],
                    "time": time.time(),
                },
            )

        if path == "/api/events":
            return self._sse()

        if path.startswith("/api/sandbox/"):
            rel = path[len("/api/sandbox/") :]
            try:
                p = tools._safe_sandbox(rel)
            except Exception:
                self.send_error(400)
                return
            return _file(self, p, download_name=p.name)

        if path.startswith("/api/screen/"):
            name = path.split("/")[-1]
            return _file(self, SCREEN / name)

        if path == "/api/export":
            # zip sandbox
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in SANDBOX.rglob("*"):
                    if p.is_file():
                        z.write(p, arcname=str(p.relative_to(SANDBOX)))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="jarvis-sandbox.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/chat":
            body = _read_json(self)
            text = (body.get("text") or "").strip()
            if not text and not body.get("attachments"):
                return _json(self, 400, {"error": "empty"})
            agent_mode = bool(body.get("agent"))
            background = bool(body.get("background"))
            atts = body.get("attachments") or []

            def job():
                try:
                    BUS.emit("status", state="thinking", agent=agent_mode)
                    res = agent.run_dialog(
                        text,
                        agent=agent_mode,
                        attachments=atts,
                        history=memory.load_history(),
                        background=background,
                    )
                    if res.get("mission_id"):
                        MISSIONS.append(
                            {
                                "id": res["mission_id"],
                                "title": text[:80],
                                "status": "done",
                                "steps": res.get("steps") or [],
                                "background": background,
                                "ts": time.time(),
                            }
                        )
                    BUS.emit("assistant", **res)
                    BUS.emit("status", state="idle")
                except Exception as e:
                    BUS.emit("assistant", text=f"Сбой контура: {e}", fallback=True)
                    BUS.emit("status", state="idle")

            if background:
                threading.Thread(target=job, daemon=True).start()
                BUS.emit(
                    "notify",
                    level="info",
                    title="Фоновая миссия",
                    body=text[:140],
                    auto_open=True,
                )
                return _json(self, 200, {"ok": True, "background": True})
            # foreground: still run in this request so HTTP returns the answer
            BUS.emit("status", state="thinking", agent=agent_mode)
            res = agent.run_dialog(
                text,
                agent=agent_mode,
                attachments=atts,
                history=memory.load_history(),
                background=False,
            )
            if res.get("mission_id"):
                MISSIONS.append(
                    {
                        "id": res["mission_id"],
                        "title": text[:80],
                        "status": "done",
                        "steps": res.get("steps") or [],
                        "ts": time.time(),
                    }
                )
            BUS.emit("assistant", **res)
            BUS.emit("status", state="idle")
            return _json(self, 200, res)

        if path == "/api/confirm":
            body = _read_json(self)
            rec = safety.resolve(body.get("id") or "", bool(body.get("allow")))
            return _json(self, 200, {"ok": bool(rec), "rec": rec})

        if path == "/api/settings":
            body = _read_json(self)
            # don't wipe secrets if UI sent masked values
            cur = load_settings()
            for secret in ("fm_key", "ds_key", "telegram_token"):
                v = body.get(secret)
                if v is None:
                    continue
                if "…" in str(v) or str(v).endswith("…"):
                    body[secret] = cur.get(secret)
            s = save_settings(body)
            BUS.emit("notify", level="ok", title="Настройки", body="Сохранено", auto_open=False)
            return _json(self, 200, {"ok": True, "settings": s})

        if path == "/api/upload":
            return self._upload()

        if path == "/api/notify":
            body = _read_json(self)
            rec = {
                "id": new_id("nt"),
                "level": body.get("level") or "info",
                "title": body.get("title") or "Джарвис",
                "body": body.get("body") or "",
                "ts": time.time(),
            }
            NOTIFS.append(rec)
            BUS.emit("notify", **rec, auto_open=True)
            return _json(self, 200, rec)

        if path == "/api/remember":
            body = _read_json(self)
            memory.remember(body.get("text") or "")
            return _json(self, 200, {"ok": True, "memory": memory.load_memory()})

        if path == "/api/reset_history":
            memory.save_history([])
            return _json(self, 200, {"ok": True})

        if path == "/api/proactive_tick":
            # called by UI timer / server loop
            return _json(self, 200, {"ok": True})

        self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/sandbox/"):
            rel = parsed.path[len("/api/sandbox/") :]
            try:
                msg = tools.delete_file(rel)
            except Exception as e:
                return _json(self, 400, {"error": str(e)})
            return _json(self, 200, {"ok": True, "msg": msg})
        self.send_error(404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = BUS.subscribe()
        try:
            # replay a bit
            for ev in BUS.history()[-12:]:
                chunk = f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
            # hello
            hello = {"kind": "hello", "ts": time.time()}
            self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            while True:
                ev = BUS.wait(q, timeout=20)
                if ev is None:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            BUS.unsubscribe(q)

    def _upload(self):
        ctype = self.headers.get("Content-Type", "")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        # very small multipart parser
        if "multipart/form-data" not in ctype:
            return _json(self, 400, {"error": "need multipart"})
        m = re_boundary(ctype)
        if not m:
            return _json(self, 400, {"error": "boundary"})
        parts = raw.split(b"--" + m.encode())
        saved = []
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header, _, body = part.partition(b"\r\n\r\n")
            body = body.rstrip(b"\r\n")
            if body.endswith(b"--"):
                body = body[:-2]
            name_m = re_search(rb'filename="([^"]+)"', header)
            if not name_m:
                continue
            fname = name_m.group(1).decode("utf-8", "replace")
            safe = "".join(ch for ch in fname if ch.isalnum() or ch in "._- ").strip() or new_id("up")
            dest = UPLOADS / f"{new_id('up')}_{safe}"
            dest.write_bytes(body)
            # copy into sandbox too
            sdest = SANDBOX / "inbox" / dest.name
            sdest.parent.mkdir(parents=True, exist_ok=True)
            sdest.write_bytes(body)
            preview = ""
            kind = "file"
            low = fname.lower()
            data_url = None
            if low.endswith((".txt", ".md", ".json", ".csv", ".py", ".js", ".html", ".css", ".log")):
                kind = "text"
                preview = body.decode("utf-8", "replace")[:8000]
            elif low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                kind = "image"
                mime = mimetypes.guess_type(fname)[0] or "image/png"
                data_url = f"data:{mime};base64," + base64.b64encode(body).decode("ascii")
            elif low.endswith((".mp3", ".wav", ".m4a", ".ogg", ".webm")):
                kind = "audio"
                try:
                    from .llm import transcribe_wav

                    preview = transcribe_wav(str(dest))
                except Exception as e:
                    preview = f"(голос не распознан: {e})"
            elif low.endswith((".mp4", ".mov", ".mkv", ".webm")):
                kind = "video"
                preview = _video_preview(dest)
            saved.append(
                {
                    "name": dest.name,
                    "path": str(sdest.relative_to(SANDBOX)),
                    "kind": kind,
                    "size": len(body),
                    "text_preview": preview,
                    "data_url": data_url,
                    "url": f"/api/sandbox/inbox/{dest.name}",
                }
            )
            BUS.emit("sandbox", action="upload", path=f"inbox/{dest.name}", kind=kind)
        return _json(self, 200, {"ok": True, "files": saved})


def re_boundary(ctype: str) -> str | None:
    import re

    m = re.search(r"boundary=([^;]+)", ctype)
    return m.group(1).strip().strip('"') if m else None


def re_search(pat, data):
    import re

    return re.search(pat, data)


def _video_preview(path: Path) -> str:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return f"Видео сохранено ({path.name}). ffmpeg нет — кадры не извлечены."
    outdir = SANDBOX / "frames" / path.stem
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                "fps=1/3",
                "-frames:v",
                "5",
                str(outdir / "f%02d.jpg"),
            ],
            capture_output=True,
            timeout=40,
        )
        frames = sorted(outdir.glob("*.jpg"))
        return "Кадры: " + ", ".join(str(f.relative_to(SANDBOX)) for f in frames)
    except Exception as e:
        return f"Видео сохранено, разбор кадров: {e}"


def _proactive_loop():
    last = 0
    while True:
        time.sleep(40)
        s = load_settings()
        if not s.get("proactive"):
            continue
        # once per ~2 hours of uptime greet / remind
        if time.time() - last < 7200:
            continue
        last = time.time()
        mem = memory.load_memory()
        facts = mem.get("facts") or []
        if not facts:
            continue
        BUS.emit(
            "notify",
            level="info",
            title="Джарвис на связи",
            body="Могу закрыть фоновые дела, пока вы заняты. Напишите миссию.",
            auto_open=False,
        )


def serve(host: str | None = None, port: int | None = None):
    s = load_settings()
    host = host or s.get("host") or "0.0.0.0"
    port = int(port or s.get("port") or 8787)
    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_proactive_loop, daemon=True).start()
    BUS.emit("notify", level="ok", title="Джарвис онлайн", body=f"http://127.0.0.1:{port}", auto_open=False)
    print(f"\n  JARVIS  →  http://127.0.0.1:{port}  (bind {host})\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nJARVIS offline")
