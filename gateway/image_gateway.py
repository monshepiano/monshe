#!/usr/bin/env python3
"""Small, dependency-free image gateway for a Russian VPS.

The distributable JARVIS client knows only a revocable release token. The
provider Authorization Key is read from this process' environment and never
returned to, or stored by, a client.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Dict, Iterable, Tuple

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"
MAX_REQUEST_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")


class GatewayError(RuntimeError):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(os.environ.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


def _tokens() -> Tuple[str, ...]:
    raw = os.environ.get("JARVIS_GATEWAY_CLIENT_TOKENS", "")
    return tuple(part.strip() for part in re.split(r"[,;\n]", raw) if part.strip())


UPSTREAM = os.environ.get("JARVIS_IMAGE_UPSTREAM", "timeweb").strip().lower()
AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY", "").strip()
SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_B2B").strip()
MODEL = os.environ.get("GIGACHAT_MODEL", "GigaChat").strip()
TIMEWEB_KEY = os.environ.get("TIMEWEB_AI_GATEWAY_KEY", "").strip()
TIMEWEB_MODEL = os.environ.get(
    "TIMEWEB_IMAGE_MODEL", "gemini-3.1-flash-image-preview").strip()
TIMEWEB_API = os.environ.get(
    "TIMEWEB_AI_GATEWAY_URL", "https://api.timeweb.ai/v1").strip().rstrip("/")
TIMEWEB_IMAGE_HOSTS = tuple(
    value.strip().lower().lstrip(".")
    for value in os.environ.get(
        "TIMEWEB_IMAGE_HOSTS", "timeweb.ai,timeweb.cloud").split(",")
    if value.strip()
)
CLIENT_TOKENS = _tokens()
TRUST_PROXY = os.environ.get("JARVIS_GATEWAY_TRUST_PROXY", "0") == "1"
PER_MINUTE = _positive_int("JARVIS_GATEWAY_PER_MINUTE", 4, 120)
PER_DAY = _positive_int("JARVIS_GATEWAY_PER_DAY", 40, 10000)
GLOBAL_PER_DAY = _positive_int("JARVIS_GATEWAY_GLOBAL_PER_DAY", 200, 100000)
CONCURRENCY = _positive_int("JARVIS_GATEWAY_CONCURRENCY", 2, 16)
SLOTS = threading.BoundedSemaphore(CONCURRENCY)
SSL_CONTEXT = ssl.create_default_context()
_HERE = Path(__file__).resolve().parent
for CA_FILE in (
    _HERE / "russian_trusted_root_ca.pem",
    _HERE / "app" / "jarvis" / "certs" / "russian_trusted_root_ca.pem",
    _HERE.parent / "app" / "jarvis" / "certs" / "russian_trusted_root_ca.pem",
):
    if CA_FILE.exists():
        SSL_CONTEXT.load_verify_locations(cafile=str(CA_FILE))
        break

_TOKEN_LOCK = threading.RLock()
_ACCESS_TOKEN = ""
_ACCESS_EXPIRES = 0.0


class RateGate:
    """Process-local hard caps; nginx adds a second request-rate boundary."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.minute: Dict[str, Deque[float]] = {}
        self.daily: Dict[Tuple[str, str], int] = {}

    def allow(self, identity: str) -> bool:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self.lock:
            hits = self.minute.setdefault(identity, collections.deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            used = self.daily.get((day, identity), 0)
            global_used = self.daily.get((day, "*"), 0)
            if len(hits) >= PER_MINUTE or used >= PER_DAY or global_used >= GLOBAL_PER_DAY:
                return False
            hits.append(now)
            self.daily[(day, identity)] = used + 1
            self.daily[(day, "*")] = global_used + 1
            # Keep only today/yesterday-ish cardinality rather than growing forever.
            if len(self.daily) > 10000:
                self.daily = {k: v for k, v in self.daily.items() if k[0] == day}
            return True


RATE_GATE = RateGate()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


def _http(url: str, *, method: str = "GET", headers: Dict[str, str] | None = None,
          data: bytes | None = None, timeout: int = 180,
          max_bytes: int = 4 * 1024 * 1024,
          follow_redirects: bool = True) -> Tuple[bytes, str]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        if follow_redirects:
            response = urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)
        else:
            opener = urllib.request.build_opener(
                _NoRedirect(), urllib.request.HTTPSHandler(context=SSL_CONTEXT))
            response = opener.open(request, timeout=timeout)
        with response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > max_bytes:
                raise GatewayError("upstream response is too large")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise GatewayError("upstream response is too large")
            return body, str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        raise GatewayError("upstream HTTP %s" % int(exc.code or 0), int(exc.code or 502)) from None
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError("upstream connection failed: %s" % type(exc).__name__) from None


def _access_token(force: bool = False) -> str:
    global _ACCESS_TOKEN, _ACCESS_EXPIRES
    now = time.time()
    with _TOKEN_LOCK:
        if _ACCESS_TOKEN and not force and _ACCESS_EXPIRES > now + 45:
            return _ACCESS_TOKEN
        payload = urllib.parse.urlencode({"scope": SCOPE}).encode("ascii")
        body, _ = _http(
            OAUTH_URL,
            method="POST",
            headers={
                "Authorization": "Basic " + AUTH_KEY,
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=payload,
            timeout=45,
        )
        try:
            answer = json.loads(body.decode("utf-8"))
            token = str(answer["access_token"])
            expires = float(answer.get("expires_at") or 0)
        except Exception:
            raise GatewayError("invalid OAuth response") from None
        if expires > 10_000_000_000:
            expires /= 1000
        if expires <= now:
            expires = now + 29 * 60
        _ACCESS_TOKEN, _ACCESS_EXPIRES = token, expires
        return token


def _request_generation(prompt: str, token: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": (
                "Ты арт-директор. Обязательно вызови text2image. Создай одно качественное "
                "изображение без текста, логотипов, рамок и водяных знаков.")},
            {"role": "user", "content": "Нарисуй изображение: " + prompt},
        ],
        "function_call": "auto",
    }
    body, _ = _http(
        API_URL + "/chat/completions",
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise GatewayError("invalid generation response") from None


def _image_id(answer: dict) -> str:
    try:
        message = answer["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise GatewayError("upstream returned no generation") from None
    content = str(message.get("content") or "")
    tag = re.search(r"(?is)<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", content)
    if tag:
        found = UUID_RE.search(tag.group(1))
        if found:
            return found.group(0).lower()
    for attachment in message.get("attachments") or []:
        if isinstance(attachment, dict):
            for key in ("file_id", "id", "src"):
                found = UUID_RE.search(str(attachment.get(key) or ""))
                if found:
                    return found.group(0).lower()
    raise GatewayError("upstream returned no image")


def _download(file_id: str, token: str) -> Tuple[bytes, str]:
    body, content_type = _http(
        "%s/files/%s/content" % (API_URL, urllib.parse.quote(file_id)),
        headers={"Authorization": "Bearer " + token, "Accept": "application/jpg"},
        max_bytes=MAX_IMAGE_BYTES,
    )
    valid = (body.startswith(b"\xff\xd8\xff") or
             body.startswith(b"\x89PNG\r\n\x1a\n") or
             (body.startswith(b"RIFF") and body[8:12] == b"WEBP"))
    if len(body) < 1000 or not valid:
        raise GatewayError("upstream returned invalid image")
    return body, content_type


def _generate_gigachat(prompt: str, _width: int, _height: int) -> Tuple[bytes, str, str]:
    token = _access_token()
    try:
        answer = _request_generation(prompt, token)
    except GatewayError as exc:
        if exc.status != 401:
            raise
        token = _access_token(force=True)
        answer = _request_generation(prompt, token)
    file_id = _image_id(answer)
    try:
        image, content_type = _download(file_id, token)
    except GatewayError as exc:
        if exc.status != 401:
            raise
        token = _access_token(force=True)
        image, content_type = _download(file_id, token)
    return image, content_type, file_id


def _timeweb_size(width: int, height: int) -> str:
    ratio = max(1, width) / max(1, height)
    if ratio > 1.2:
        return "1536x1024"
    if ratio < 0.83:
        return "1024x1536"
    return "1024x1024"


def _trusted_timeweb_asset(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https" and host and not parsed.username and
        not parsed.password and port in (None, 443) and
        any(host == suffix or host.endswith("." + suffix)
            for suffix in TIMEWEB_IMAGE_HOSTS)
    )


def _generate_timeweb(prompt: str, width: int, height: int) -> Tuple[bytes, str, str]:
    """Use Timeweb AI Gateway's OpenAI-compatible Images API.

    Asking for base64 keeps the provider API key and temporary asset URL on the
    trusted server. A URL response is accepted as a compatibility fallback.
    """
    payload = {
        "model": TIMEWEB_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": _timeweb_size(width, height),
        "response_format": "b64_json",
    }
    body, _ = _http(
        TIMEWEB_API + "/images/generations",
        method="POST",
        headers={
            "Authorization": "Bearer " + TIMEWEB_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "JarvisImageGateway/1",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=300,
        max_bytes=36 * 1024 * 1024,
    )
    try:
        answer = json.loads(body.decode("utf-8"))
        item = answer["data"][0]
    except Exception:
        raise GatewayError("invalid Timeweb image response") from None

    encoded = str(item.get("b64_json") or "")
    content_type = "image/png"
    if encoded.startswith("data:"):
        match = re.match(r"data:(image/(?:png|jpeg|webp));base64,(.*)", encoded, re.S)
        if not match:
            raise GatewayError("invalid Timeweb data URL")
        content_type, encoded = match.group(1), match.group(2)
    if encoded:
        try:
            image = base64.b64decode(encoded, validate=True)
        except Exception:
            raise GatewayError("invalid Timeweb image encoding") from None
    else:
        asset_url = str(item.get("url") or "").strip()
        if not _trusted_timeweb_asset(asset_url):
            raise GatewayError("Timeweb returned no trusted image asset")
        image, content_type = _http(
            asset_url,
            headers={"Accept": "image/jpeg,image/png,image/webp",
                     "User-Agent": "JarvisImageGateway/1"},
            timeout=180, max_bytes=MAX_IMAGE_BYTES, follow_redirects=False)
    valid = (image.startswith(b"\xff\xd8\xff") or
             image.startswith(b"\x89PNG\r\n\x1a\n") or
             (image.startswith(b"RIFF") and image[8:12] == b"WEBP"))
    if len(image) < 1000 or len(image) > MAX_IMAGE_BYTES or not valid:
        raise GatewayError("Timeweb returned invalid image")
    image_id = hashlib.sha256(image).hexdigest()[:16]
    return image, content_type, image_id


def generate(prompt: str, width: int, height: int) -> Tuple[bytes, str, str]:
    if UPSTREAM == "timeweb":
        return _generate_timeweb(prompt, width, height)
    if UPSTREAM == "gigachat":
        return _generate_gigachat(prompt, width, height)
    raise GatewayError("unsupported image upstream")


def _provider_configured() -> bool:
    return bool(TIMEWEB_KEY) if UPSTREAM == "timeweb" else bool(AUTH_KEY)


def _constant_match(candidate: str, choices: Iterable[str]) -> str:
    """Return the matching configured token without timing-sensitive equality."""
    matched = ""
    for choice in choices:
        if hmac.compare_digest(candidate.encode("utf-8"), choice.encode("utf-8")):
            matched = choice
    return matched


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisImageGateway/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log Authorization or prompt bodies. The default line is safe.
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args),
              flush=True)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._json(HTTPStatus.OK, {
                "ok": True,
                "service": "jarvis-image-gateway",
                "provider_configured": _provider_configured(),
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/images/generations":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        request_id = secrets.token_hex(8)
        auth = self.headers.get("Authorization", "")
        candidate = auth[7:].strip() if auth.startswith("Bearer ") else ""
        matched = _constant_match(candidate, CLIENT_TOKENS)
        if not matched:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "request_id": request_id})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 2 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                       {"error": "invalid request size", "request_id": request_id})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            prompt = str(data.get("prompt") or "").strip()
            width = max(256, min(int(data.get("width") or 1024), 2048))
            height = max(256, min(int(data.get("height") or 1024), 2048))
        except Exception:
            prompt, width, height = "", 1024, 1024
        if not prompt or len(prompt) > 1800:
            self._json(HTTPStatus.BAD_REQUEST,
                       {"error": "prompt must contain 1-1800 characters", "request_id": request_id})
            return

        remote = self.client_address[0]
        if TRUST_PROXY:
            remote = (self.headers.get("X-Real-IP") or
                      self.headers.get("X-Forwarded-For") or remote).split(",", 1)[0].strip()
        identity = "%s:%s" % (matched[:12], remote[:64])
        if not RATE_GATE.allow(identity) or not SLOTS.acquire(blocking=False):
            self._json(HTTPStatus.TOO_MANY_REQUESTS,
                       {"error": "rate limit exceeded", "request_id": request_id})
            return
        try:
            image, content_type, file_id = generate(prompt, width, height)
            if image.startswith(b"\xff\xd8\xff"):
                content_type = "image/jpeg"
            elif image.startswith(b"\x89PNG"):
                content_type = "image/png"
            elif image.startswith(b"RIFF"):
                content_type = "image/webp"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", request_id)
            self.send_header("X-Image-ID", file_id)
            self.end_headers()
            self.wfile.write(image)
        except GatewayError as exc:
            print("request %s failed: %s (upstream_status=%s)" %
                  (request_id, str(exc), exc.status), flush=True)
            if exc.status in (400, 422):
                status, error = HTTPStatus.BAD_REQUEST, "image prompt was rejected"
            elif exc.status == 429:
                status, error = HTTPStatus.TOO_MANY_REQUESTS, "provider rate limit exceeded"
            else:
                status, error = HTTPStatus.BAD_GATEWAY, "image provider temporarily unavailable"
            self._json(status, {"error": error, "request_id": request_id})
        except Exception as exc:
            print("request %s failed: %s" % (request_id, type(exc).__name__), flush=True)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": "internal gateway error", "request_id": request_id})
        finally:
            SLOTS.release()


def main() -> None:
    if UPSTREAM not in ("timeweb", "gigachat"):
        raise SystemExit("JARVIS_IMAGE_UPSTREAM must be timeweb or gigachat")
    if not _provider_configured():
        required = "TIMEWEB_AI_GATEWAY_KEY" if UPSTREAM == "timeweb" else "GIGACHAT_AUTH_KEY"
        raise SystemExit(required + " is required")
    if not CLIENT_TOKENS:
        raise SystemExit("JARVIS_GATEWAY_CLIENT_TOKENS is required")
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    host = os.environ.get("JARVIS_GATEWAY_HOST", default_host)
    if os.environ.get("PORT") and not os.environ.get("JARVIS_GATEWAY_PORT"):
        os.environ["JARVIS_GATEWAY_PORT"] = os.environ["PORT"]
    port = _positive_int("JARVIS_GATEWAY_PORT", 8780, 65535)
    server = ThreadingHTTPServer((host, port), Handler)
    print("JARVIS image gateway listening on %s:%s" % (host, port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
