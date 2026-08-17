"""Браузер в песочнице (Playwright). Если он не установлен — деградируем до HTTP-чтения."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .. import config
from . import web

_pw = None
_browser = None
_page = None
_lock = asyncio.Lock()


def available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


async def _ensure_page():
    global _pw, _browser, _page
    from playwright.async_api import async_playwright
    if _page is not None and not _page.is_closed():
        return _page
    if _pw is None:
        _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = await _browser.new_context(
        viewport={"width": 1440, "height": 900}, locale="ru-RU",
        user_agent=web.UA)
    _page = await ctx.new_page()
    return _page


async def act(action: str, url: str = "", selector: str = "", text: str = "",
              wait: float = 1.5) -> Dict[str, Any]:
    """Действие в браузере: open | click | type | read | screenshot | scroll."""
    if not available():
        if action in ("open", "read"):
            res = await web.fetch(url)
            res["note"] = "Playwright не установлен, использовано простое чтение страницы"
            return res
        return {"ok": False, "error": "Playwright не установлен — доступно только чтение страниц. "
                                      "Запустите install.command ещё раз и выберите установку браузера."}
    async with _lock:
        try:
            page = await _ensure_page()
            if action == "open":
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(wait)
                return {"ok": True, "url": page.url, "title": await page.title()}
            if action == "click":
                await page.click(selector, timeout=15000)
                await asyncio.sleep(wait)
                return {"ok": True, "clicked": selector, "url": page.url}
            if action == "type":
                await page.fill(selector, text, timeout=15000)
                return {"ok": True, "typed": text[:80], "selector": selector}
            if action == "press":
                await page.keyboard.press(text or "Enter")
                await asyncio.sleep(wait)
                return {"ok": True, "pressed": text or "Enter"}
            if action == "scroll":
                await page.mouse.wheel(0, 900)
                return {"ok": True}
            if action == "read":
                if url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(wait)
                body = await page.inner_text("body")
                return {"ok": True, "url": page.url, "title": await page.title(),
                        "text": body[:14000]}
            if action == "screenshot":
                from .sandbox import root
                path = root() / "screenshot.png"
                await page.screenshot(path=str(path), full_page=False)
                return {"ok": True, "path": "screenshot.png"}
            return {"ok": False, "error": f"Неизвестное действие: {action}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def close() -> None:
    global _pw, _browser, _page
    try:
        if _browser:
            await _browser.close()
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None
