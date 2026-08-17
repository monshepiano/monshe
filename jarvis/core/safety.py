"""Classify actions that need a human tap before they run."""

from __future__ import annotations

import re
import time
from typing import Any

from .config import load_settings, new_id
from .events import BUS

DANGEROUS_TOOLS = {
    "computer_click",
    "computer_type",
    "computer_hotkey",
    "computer_open",
    "shell",
    "telegram_send",
    "browser_buy",
    "delete_file",
}

DANGEROUS_SHELL = re.compile(
    r"(rm\s|del\s|format|shutdown|reboot|mkfs|dd\s|sudo|passwd|curl.+\|\s*sh|"
    r"invoke-webrequest|reg\s+delete|killall|launchctl)",
    re.I,
)
BUY_RE = re.compile(r"(купи|оплат|закаж|checkout|оформить заказ|купить)", re.I)

PENDING: dict[str, dict] = {}


def needs_confirm(name: str, args: dict) -> tuple[bool, str]:
    s = load_settings()
    if not s.get("confirm_dangerous", True):
        return False, ""
    if name in DANGEROUS_TOOLS:
        return True, f"Инструмент «{name}» меняет внешний мир."
    if name == "shell" and DANGEROUS_SHELL.search(str(args.get("command") or "")):
        return True, "Команда похожа на опасную."
    if name in {"browser_open", "browser_act"} and BUY_RE.search(str(args)):
        return True, "Похоже на покупку."
    text = " ".join(str(v) for v in args.values())
    if BUY_RE.search(text) and name not in {"web_search", "remember"}:
        return True, "Похоже на платёж или заказ."
    return False, ""


def ask(name: str, args: dict, reason: str) -> dict:
    token = new_id("ok")
    rec = {
        "id": token,
        "tool": name,
        "args": args,
        "reason": reason,
        "status": "pending",
        "ts": time.time(),
    }
    PENDING[token] = rec
    BUS.emit(
        "confirm",
        confirm_id=token,
        tool=name,
        args=args,
        reason=reason,
        title="Подтвердите действие",
    )
    BUS.emit(
        "notify",
        level="warn",
        title="Нужно подтверждение",
        body=f"{name}: {reason}",
        confirm_id=token,
        auto_open=True,
    )
    return rec


def resolve(token: str, allow: bool) -> dict | None:
    rec = PENDING.get(token)
    if not rec:
        return None
    rec["status"] = "allow" if allow else "deny"
    rec["resolved"] = time.time()
    BUS.emit("confirm_result", confirm_id=token, allow=allow, tool=rec["tool"])
    return rec


def wait_for(token: str, timeout: float = 180.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        rec = PENDING.get(token)
        if not rec:
            return False
        if rec["status"] == "allow":
            return True
        if rec["status"] == "deny":
            return False
        time.sleep(0.25)
    rec = PENDING.get(token)
    if rec and rec["status"] == "pending":
        rec["status"] = "timeout"
    return False
