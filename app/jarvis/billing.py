"""Расходы и баланс аккаунта Cloud.ru.

У Foundation Models собственного метода «баланс» нет, поэтому:
  1) всегда считаем локальный расход по токенам (db.usage_summary);
  2) если пользователь указал ключ доступа личного кабинета (Настройки → Биллинг),
     дополнительно тянем реальное потребление через API личного кабинета:
     POST https://iam.api.cloud.ru/api/v1/auth/token  → access_token
     GET  https://organization.api.cloud.ru/v1/consumption?agreement_id=…&start_date=…&end_date=…

Всё на stdlib, без внешних зависимостей. Ошибки не роняют UI — просто
возвращается source="local".
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import db
from .config import CONFIG

IAM_URL = "https://iam.api.cloud.ru/api/v1/auth/token"
ORG_URL = "https://organization.api.cloud.ru"

_SSL_CTX = ssl.create_default_context()
_LOCK = threading.RLock()
_TOKEN: Dict[str, Any] = {"value": "", "expires": 0.0}
_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


def _conf() -> Dict[str, Any]:
    return CONFIG.get("billing", {}) or {}


def configured() -> bool:
    c = _conf()
    return bool(c.get("enabled") and c.get("key_id") and c.get("key_secret"))


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "JARVIS/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, token: str, timeout: int = 25) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "JARVIS/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _token() -> str:
    """Авторизационный токен личного кабинета (живёт 1 час)."""
    with _LOCK:
        if _TOKEN["value"] and time.time() < _TOKEN["expires"] - 60:
            return str(_TOKEN["value"])
    c = _conf()
    body = _post_json(IAM_URL, {"keyId": c.get("key_id", ""), "secret": c.get("key_secret", "")})
    token = body.get("access_token") or body.get("accessToken") or ""
    if not token:
        raise RuntimeError("личный кабинет не выдал токен")
    ttl = int(body.get("expires_in") or body.get("expiresIn") or 3600)
    with _LOCK:
        _TOKEN["value"] = token
        _TOKEN["expires"] = time.time() + ttl
    return token


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sum_rows(body: Any) -> float:
    """Суммирует стоимость по строкам потребления (структура ответа может отличаться)."""
    rows = []
    if isinstance(body, dict):
        for key in ("consumption", "items", "data", "result", "records"):
            value = body.get(key)
            if isinstance(value, list):
                rows = value
                break
    elif isinstance(body, list):
        rows = body
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("total", "cost", "amount", "total_cost", "totalCost",
                    "consumption_cost", "value", "sum"):
            val = row.get(key)
            if isinstance(val, (int, float)):
                total += float(val)
                break
            if isinstance(val, str):
                try:
                    total += float(val.replace(",", "."))
                    break
                except ValueError:
                    continue
    return round(total, 2)


def remote_consumption() -> Dict[str, Any]:
    """Потребление по договору за текущий месяц и за сутки."""
    c = _conf()
    token = _token()
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now - timedelta(days=1)

    def fetch(start: datetime) -> float:
        params = {"start_date": _iso(start), "end_date": _iso(now)}
        if c.get("agreement_id"):
            params["agreement_id"] = c["agreement_id"]
        if c.get("customer_id"):
            params["customer_id"] = c["customer_id"]
        url = ORG_URL + "/v1/consumption?" + urllib.parse.urlencode(params)
        return _sum_rows(_get_json(url, token))

    return {
        "month_rub": fetch(month_start),
        "day_rub": fetch(day_start),
    }


def snapshot(force: bool = False) -> Dict[str, Any]:
    """Единая сводка для UI.

    Всегда содержит локальный расход; при настроенном биллинге — данные ЛК.
    """
    day = (db.usage_summary(24) or {}).get("total") or {}
    month = (db.usage_summary(24 * 30) or {}).get("total") or {}
    out: Dict[str, Any] = {
        "source": "local",
        "local_day_rub": round(float(day.get("cost") or 0), 2),
        "local_month_rub": round(float(month.get("cost") or 0), 2),
        "tokens_day": int((day.get("pt") or 0) + (day.get("ct") or 0)),
        "calls_day": int(day.get("calls") or 0),
        "account_rub": None,
        "month_rub": None,
        "cloud_day_rub": None,
        "error": "",
        "configured": configured(),
    }
    if not configured():
        return out

    with _LOCK:
        cached = _CACHE.get("data")
        fresh_for = max(5, int(_conf().get("refresh_minutes", 30))) * 60
        if cached and not force and time.time() - float(_CACHE.get("at") or 0) < fresh_for:
            return {**out, **cached}

    try:
        remote = remote_consumption()
        # «Баланса лицевого счёта» в публичном API Cloud.ru нет —
        # честно отдаём только фактическое потребление.
        data = {
            "source": "cloudru",
            "month_rub": remote.get("month_rub"),
            "cloud_day_rub": remote.get("day_rub"),
            "error": "",
        }
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:
            pass
        data = {"source": "local", "error": "HTTP %s %s" % (exc.code, detail)}
    except Exception as exc:
        data = {"source": "local", "error": str(exc)[:200]}

    with _LOCK:
        _CACHE["at"] = time.time()
        _CACHE["data"] = data
    return {**out, **data}
