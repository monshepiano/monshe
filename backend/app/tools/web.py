"""Интернет: поиск и чтение страниц. Работает из РФ без VPN (несколько зеркал)."""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any, Dict, List

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

SEARCH_BACKENDS = [
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={q}"),
    ("duckduckgo-lite", "https://lite.duckduckgo.com/lite/?q={q}"),
    ("mojeek", "https://www.mojeek.com/search?q={q}"),
    ("searx", "https://searx.be/search?q={q}&format=json"),
]


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\xa0]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


async def search(query: str, limit: int = 6) -> Dict[str, Any]:
    """Поиск в интернете. Возвращает список результатов с заголовком, ссылкой и описанием."""
    q = urllib.parse.quote_plus(query)
    errors = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "ru,en;q=0.8"}) as cl:
        for name, tpl in SEARCH_BACKENDS:
            url = tpl.format(q=q)
            try:
                r = await cl.get(url)
                if r.status_code != 200:
                    errors.append(f"{name}:{r.status_code}")
                    continue
                if name == "searx":
                    data = r.json()
                    items = [{"title": it.get("title", ""), "url": it.get("url", ""),
                              "snippet": it.get("content", "")}
                             for it in data.get("results", [])][:limit]
                else:
                    items = _parse_results(r.text, name)[:limit]
                if items:
                    return {"ok": True, "engine": name, "query": query, "results": items}
                errors.append(f"{name}:empty")
            except Exception as e:
                errors.append(f"{name}:{type(e).__name__}")
                continue
    return {"ok": False, "query": query, "results": [],
            "error": "Поисковики недоступны (" + ", ".join(errors) + ")"}


def _parse_results(body: str, engine: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if engine.startswith("duckduckgo"):
        for m in re.finditer(
                r'<a[^>]+class="[^"]*result(?:-link|__a)[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                body, re.S):
            link, title = m.group(1), _strip_html(m.group(2))
            link = _unwrap_ddg(link)
            if link.startswith("http"):
                out.append({"title": title, "url": link, "snippet": ""})
        if not out:  # lite-версия — простая таблица ссылок
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', body, re.S):
                link, title = _unwrap_ddg(m.group(1)), _strip_html(m.group(2))
                if "duckduckgo.com" in link or len(title) < 3:
                    continue
                out.append({"title": title, "url": link, "snippet": ""})
        snippets = [_strip_html(s) for s in re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)]
        for i, s in enumerate(snippets[:len(out)]):
            out[i]["snippet"] = s
    elif engine == "mojeek":
        for m in re.finditer(r'<a class="ob"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
            out.append({"title": _strip_html(m.group(2)), "url": m.group(1), "snippet": ""})
    # дедуп
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


def _unwrap_ddg(link: str) -> str:
    if link.startswith("//"):
        link = "https:" + link
    if "duckduckgo.com/l/?" in link or link.startswith("/l/?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if "uddg" in qs:
            return urllib.parse.unquote(qs["uddg"][0])
    return link


async def fetch(url: str, max_chars: int = 12000) -> Dict[str, Any]:
    """Открыть страницу и вернуть её текст."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cl:
            r = await cl.get(url)
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            text = json.dumps(r.json(), ensure_ascii=False, indent=2)[:max_chars]
        elif "text/" in ctype or "html" in ctype or not ctype:
            text = _strip_html(r.text)[:max_chars]
        else:
            return {"ok": False, "url": url, "error": f"Неподдерживаемый тип: {ctype}"}
        title = ""
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text or "")
        if m:
            title = _strip_html(m.group(1))
        return {"ok": True, "url": str(r.url), "status": r.status_code,
                "title": title, "text": text}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}
