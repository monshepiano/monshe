"""Интернет: поиск, чтение страниц, скачивание. Без внешних зависимостей.

Все источники подобраны так, чтобы работать из России без VPN
(DuckDuckGo html, Яндекс-поиск через lite-выдачу, Mail.ru, Wikipedia).
"""
from __future__ import annotations

import gzip
import html as html_mod
import io
import json
import re
import ssl
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, List

_CTX = ssl.create_default_context()
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _fetch(url: str, timeout: int = 25, data: bytes | None = None,
           headers: Dict[str, str] | None = None) -> str:
    head = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate",
    }
    head.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=head)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
        raw = resp.read(4_000_000)
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def html_to_text(html: str, limit: int = 12000) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|form)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    html = re.sub(r"(?is)<li[^>]*>", "• ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:limit]


# ------------------------------------------------------------------ поиск
def _ddg(query: str, count: int) -> List[Dict[str, str]]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    html = _fetch(url, timeout=20)
    out: List[Dict[str, str]] = []
    for m in re.finditer(
        r'(?is)<a[^>]+class="result__a"[^>]+href="(.*?)".*?>(.*?)</a>.*?'
        r'(?:class="result__snippet"[^>]*>(.*?)</a>)?', html):
        link = html_mod.unescape(m.group(1) or "")
        if "uddg=" in link:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            link = (parsed.get("uddg") or [link])[0]
        title = html_to_text(m.group(2) or "", 200)
        snippet = html_to_text(m.group(3) or "", 400)
        if link.startswith("http") and title:
            out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= count:
            break
    return out


def _mail_search(query: str, count: int) -> List[Dict[str, str]]:
    url = "https://go.mail.ru/search?q=" + urllib.parse.quote(query)
    html = _fetch(url, timeout=20)
    out: List[Dict[str, str]] = []
    for m in re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*class="[^"]*link[^"]*"[^>]*>(.*?)</a>', html):
        link, title = html_mod.unescape(m.group(1)), html_to_text(m.group(2), 160)
        if title and "go.mail.ru" not in link:
            out.append({"title": title, "url": link, "snippet": ""})
        if len(out) >= count:
            break
    return out


def _wikipedia(query: str, count: int) -> List[Dict[str, str]]:
    api = ("https://ru.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=%d&srsearch=%s"
           % (count, urllib.parse.quote(query)))
    body = json.loads(_fetch(api, timeout=20))
    out = []
    for item in body.get("query", {}).get("search", []):
        out.append({
            "title": item.get("title", ""),
            "url": "https://ru.wikipedia.org/wiki/" + urllib.parse.quote(item.get("title", "").replace(" ", "_")),
            "snippet": html_to_text(item.get("snippet", ""), 300),
        })
    return out


def web_search(query: str, count: int = 6) -> Dict[str, Any]:
    """Поиск в интернете. Пробует несколько движков подряд."""
    errors = []
    for engine in (_ddg, _mail_search, _wikipedia):
        try:
            results = engine(query, count)
            if results:
                return {"ok": True, "query": query, "engine": engine.__name__.strip("_"), "results": results}
        except Exception as exc:  # пробуем следующий
            errors.append("%s: %s" % (engine.__name__, exc))
    return {"ok": False, "query": query, "results": [], "error": "; ".join(errors) or "ничего не найдено"}


def open_url(url: str, limit: int = 9000) -> Dict[str, Any]:
    """Открыть страницу и вернуть её текст (это «браузер» без твоего компа)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        raw = _fetch(url, timeout=30)
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = html_to_text(title_m.group(1), 200) if title_m else url
    if raw.lstrip().startswith(("{", "[")):
        return {"ok": True, "url": url, "title": title, "text": raw[:limit], "kind": "json"}
    return {"ok": True, "url": url, "title": title, "text": html_to_text(raw, limit), "kind": "html"}


def deep_research(query: str, pages: int = 3) -> Dict[str, Any]:
    """Поиск + чтение первых страниц: сырьё для развёрнутого ответа."""
    found = web_search(query, count=max(pages, 4))
    docs = []
    for item in found.get("results", [])[:pages]:
        page = open_url(item["url"], limit=5000)
        if page.get("ok"):
            docs.append({"title": item.get("title"), "url": item["url"], "text": page["text"]})
    return {"ok": bool(docs), "query": query, "sources": found.get("results", []), "documents": docs}


def download_file(url: str, filename: str = "") -> Dict[str, Any]:
    """Скачать файл в песочницу JARVIS."""
    from .. import sandbox
    if not url.startswith("http"):
        url = "https://" + url
    name = filename or urllib.parse.unquote(url.split("/")[-1].split("?")[0]) or "download.bin"
    name = re.sub(r"[^\w.\-() ]+", "_", name)[:120]
    dest = sandbox.root() / name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=90, context=_CTX) as resp, open(dest, "wb") as fh:
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 80_000_000:
                    break
                fh.write(chunk)
        return {"ok": True, "path": str(dest), "name": name, "size": dest.stat().st_size,
                "download_url": sandbox.dl(name)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def http_request(url: str, method: str = "GET", body: str = "", headers_json: str = "") -> Dict[str, Any]:
    """Произвольный HTTP-запрос (для API сторонних сервисов)."""
    try:
        extra = json.loads(headers_json) if headers_json else {}
    except Exception:
        extra = {}
    try:
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA, **extra}, method=method.upper())
        with urllib.request.urlopen(req, timeout=45, context=_CTX) as resp:
            text = resp.read(1_500_000).decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "body": text[:12000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
