"""Интернет: поиск, чтение страниц, скачивание. Без внешних зависимостей.

Все источники подобраны так, чтобы работать из России без VPN
(DuckDuckGo html, Яндекс-поиск через lite-выдачу, Mail.ru, Wikipedia).
"""
from __future__ import annotations

import concurrent.futures
import gzip
import html as html_mod
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import zlib
from typing import Any, Callable, Dict, List, Tuple

from .. import telemetry

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
    html = _fetch(url, timeout=7)
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
    html = _fetch(url, timeout=7)
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
    body = json.loads(_fetch(api, timeout=7))
    out = []
    for item in body.get("query", {}).get("search", []):
        out.append({
            "title": item.get("title", ""),
            "url": "https://ru.wikipedia.org/wiki/" + urllib.parse.quote(item.get("title", "").replace(" ", "_")),
            "snippet": html_to_text(item.get("snippet", ""), 300),
        })
    return out


def web_search(query: str, count: int = 6) -> Dict[str, Any]:
    """Search several independent engines concurrently under one hard deadline."""
    span = telemetry.Span("web.search", engine_count=3, requested_count=count)
    engines: List[Tuple[str, Callable[[str, int], List[Dict[str, str]]]]] = [
        ("ddg", _ddg), ("mail_search", _mail_search), ("wikipedia", _wikipedia),
    ]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(engines),
                                                  thread_name_prefix="jarvis-search")
    futures = {
        pool.submit(fn, query, count): (index, name)
        for index, (name, fn) in enumerate(engines)
    }
    completed: Dict[int, Tuple[str, List[Dict[str, str]]]] = {}
    errors: List[str] = []
    deadline = time.monotonic() + 8.0
    first_success_at: float | None = None
    timed_out = False
    try:
        pending = set(futures)
        while pending:
            stop_at = deadline
            # Once one useful engine answers, allow a short enrichment window
            # instead of making the user wait for the slowest mirror.
            if first_success_at is not None:
                stop_at = min(stop_at, first_success_at + 0.35)
            left = stop_at - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            done, pending = concurrent.futures.wait(
                pending, timeout=left,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                timed_out = bool(pending)
                break
            for future in done:
                index, name = futures[future]
                try:
                    results = future.result()
                    completed[index] = (name, results)
                    if results and first_success_at is None:
                        first_success_at = time.monotonic()
                        span.first_token()
                except Exception as exc:
                    errors.append("%s: %s" % (name, exc))
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        # Never wait past the public deadline for a blocked DNS/socket worker.
        pool.shutdown(wait=False, cancel_futures=True)

    merged: List[Dict[str, str]] = []
    seen = set()
    used: List[str] = []
    for index in sorted(completed):
        name, results = completed[index]
        if results:
            used.append(name)
        for item in results:
            key = (item.get("url") or "").rstrip("/").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= count:
                break
        if len(merged) >= count:
            break

    # Один удачный mirror не превращает быстрые ошибки остальных в «полный»
    # поиск. Это важно и для UI, и для latency telemetry: degraded результат
    # полезен, но по нему нельзя делать вид, будто проверены все источники.
    incomplete = timed_out or bool(errors) or len(completed) < len(engines)
    if merged:
        status = "partial" if incomplete else "ok"
        span.finish(status, result_count=len(merged), engines=",".join(used),
                    completed_engines=len(completed), error_count=len(errors))
        answer: Dict[str, Any] = {
            "ok": True, "query": query, "engine": "+".join(used),
            "results": merged, "partial": incomplete,
        }
        if errors:
            answer["warnings"] = errors
        return answer
    span.finish("timeout" if timed_out else "error", result_count=0,
                completed_engines=len(completed), error_count=len(errors))
    if timed_out:
        errors.append("общий лимит поиска 8 секунд")
    return {"ok": False, "query": query, "results": [],
            "error": "; ".join(errors) or "ничего не найдено"}


def _read_page(url: str, limit: int, timeout: int) -> Dict[str, Any]:
    raw = _fetch(url, timeout=timeout)
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = html_to_text(title_m.group(1), 200) if title_m else url
    if raw.lstrip().startswith(("{", "[")):
        return {"ok": True, "url": url, "title": title,
                "text": raw[:limit], "kind": "json"}
    return {"ok": True, "url": url, "title": title,
            "text": html_to_text(raw, limit), "kind": "html"}


def open_url(url: str, limit: int = 9000, _deadline: float = 12.0) -> Dict[str, Any]:
    """Read one page without letting DNS or a stalled socket block the agent."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    deadline = max(1.0, min(float(_deadline), 12.0))
    span = telemetry.Span("web.page_read")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                  thread_name_prefix="jarvis-page")
    future = pool.submit(_read_page, url, limit, max(1, int(deadline - 1)))
    try:
        page = future.result(timeout=deadline)
        span.first_token()
        span.finish("ok", kind=page.get("kind", ""),
                    content_chars=len(page.get("text", "")))
        return page
    except concurrent.futures.TimeoutError:
        future.cancel()
        span.finish("timeout")
        return {"ok": False, "url": url,
                "error": "страница не ответила за %.0f секунд" % deadline}
    except Exception as exc:
        span.finish("error", error_type=type(exc).__name__)
        return {"ok": False, "url": url, "error": str(exc)}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def deep_research(query: str, pages: int = 3) -> Dict[str, Any]:
    """Search, then read source pages concurrently with partial-result semantics."""
    span = telemetry.Span("web.deep_research", requested_pages=pages)
    found = web_search(query, count=max(pages, 4))
    sources = found.get("results", [])[:max(0, pages)]
    if not sources:
        span.finish("error", page_count=0)
        return {"ok": False, "query": query,
                "sources": found.get("results", []), "documents": []}

    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(sources), 4), thread_name_prefix="jarvis-research")
    futures = {
        pool.submit(open_url, item["url"], 5000, 8.0): (index, item)
        for index, item in enumerate(sources)
    }
    done: set[concurrent.futures.Future[Any]] = set()
    pending: set[concurrent.futures.Future[Any]] = set(futures)
    try:
        done, pending = concurrent.futures.wait(pending, timeout=9.0)
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    documents: Dict[int, Dict[str, Any]] = {}
    for future in done:
        index, item = futures[future]
        try:
            page = future.result()
        except Exception:
            continue
        if page.get("ok"):
            documents[index] = {"title": item.get("title"), "url": item["url"],
                                "text": page["text"]}
    docs = [documents[index] for index in sorted(documents)]
    if docs:
        span.first_token()
    # Reading every selected page cannot upgrade a degraded search into a
    # falsely complete research result. Preserve the upstream partial bit so
    # the model can qualify a news summary when one engine timed out or failed.
    partial = bool(found.get("partial")) or len(docs) < len(sources)
    status = "partial" if docs and partial else ("ok" if docs else "error")
    span.finish(status, page_count=len(docs), source_count=len(sources),
                timed_out=bool(pending), search_partial=bool(found.get("partial")))
    return {"ok": bool(docs), "query": query,
            "sources": found.get("results", []), "documents": docs,
            "partial": partial}


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
