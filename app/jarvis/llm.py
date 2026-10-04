"""OpenAI-совместимый клиент без внешних зависимостей (urllib).

Поддерживает: Cloud.ru Foundation Models (основной) и DeepSeek (резерв).
Стриминг SSE, function calling, vision (image_url), список моделей.
"""
from __future__ import annotations

import contextvars
import http.client
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from pathlib import Path

from .config import CONFIG, LOG_DIR
from . import db, telemetry

# Счётчик расходов прогона: agent ставит сюда приёмник, и КАЖДЫЙ вызов
# (стрим, обычный chat, vision, планировщик, сводки, подсказки) капает в
# лимит ₽. Раньше лимит видел только основной стрим — остальные запросы
# были для него невидимы, и бюджет «не срабатывал».
_USAGE_SINK = contextvars.ContextVar("jarvis_usage_sink", default=None)


def _report_usage(model: str, pt: int, ct: int) -> None:
    sink = _USAGE_SINK.get()
    if sink is not None:
        try:
            sink(model, pt, ct)
        except Exception:
            pass


_SSL_CTX = ssl.create_default_context()
# Единственный кэш каталога моделей (полные метаданные провайдера).
# Прежде здесь жил второй, производный кэш одних имён (_MODELS_CACHE) —
# две сущности одной истины, которые приходилось писать синхронно.
_META_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_LOCK = threading.RLock()
# Параметры OpenAI-compatible API на практике различаются даже у моделей
# одного gateway. Запоминаем доказанное HTTP-ошибкой отсутствие по паре
# provider/model, чтобы каждый новый ответ не тратил первый запрос на 400.
_UNSUPPORTED: Dict[Tuple[str, str], set[str]] = {}
_CAPABILITY_PATH = LOG_DIR / "llm-capabilities.json"
_CAPABILITY_LOADED = False

def _reasoning_increment(seen_tail: str, piece: str) -> str:
    """AR: причина мусора «ости октябряости октября России» — провайдер
    гоняет reasoning кусками, начинающимися с ПОВТОРА уже сказанного
    (хвост прошлого куска прилетает снова). Сверяем начало нового куска
    с хвостом накопленного: большое перекрытие — это повтор, отдаём
    только прирост. Короткие повторы (слова, «так так») не трогаем —
    это живая речь, а не сбой потока."""
    k = min(len(seen_tail), len(piece), 64)
    while k >= 10 and seen_tail[-k:] != piece[:k]:
        k -= 1
    return piece[k:] if k >= 10 else piece

_CAPABILITY_FIELDS = {"reasoning_effort", "stream_options", "tools", "tool_choice"}

# Ориентировочные цены (₽ за 1 млн токенов) — для счётчика расходов в UI.
# BM7: Ориентировочные цены (₽ за 1 млн токенов) — РАЗДЕЛЕНЫ ПО
# ПРОВАЙДЕРАМ: одна и та же модель у разных провайдеров стоит по-разному
# (gpt-oss-120b: Cloud.ru 15.9₽, Яндекс ~300₽ вход). У провайдеров нет
# машиночитаемого прайса — таблица курируется и обновляется с релизами.
PRICES_RUB: Dict[str, Dict[str, Tuple[float, float]]] = {
    "cloudru": {
        "ai-sage/GigaChat3-10B-A1.8B": (12.2, 12.2),
        "openai/gpt-oss-120b": (15.86, 61.0),
        "openai/gpt-oss-20b": (5.888, 27.5),
        "Qwen/Qwen3-30B-A3B": (13.9, 55.6),
        "Qwen/Qwen3.6-35B-A3B": (219.6, 329.4),
        "zai-org/GLM-4.7": (549.0, 793.0),
        "MiniMaxAI/MiniMax-M2.5": (353.8, 475.8),
        "Qwen/Qwen3-Coder-Next": (122.0, 244.0),
        "Qwen/Qwen3-VL-8B-Instruct": (30.7, 119.6),
        "GigaChat3.5-432B": (96.22, 288.6),
        "GigaChat-3-Pro": (73.03, 176.39),
        "GLM-5.2": (173.15, 606.05),
        "GPT 4o Mini": (29.46, 117.85),
    },
    "yandex": {   # синхронный режим, ₽/1М; асинхронный вдвое дешевле
        "gpt-oss-120b": (300.0, 450.0),
        "gpt-oss-20b": (100.0, 150.0),
        "yandexgpt": (800.0, 1200.0),       # Pro 5.1
        "yandexgpt-lite": (200.0, 300.0),
        "qwen3": (500.0, 750.0),
        "aliceai": (500.0, 1200.0),         # Alice AI LLM
    },
    "deepseek": {
        "deepseek-chat": (25.0, 100.0),
    },
    "gigachat": {},      # бесплатный грант 1M токенов/год
    "aitunnel": {},      # наценка агрегатора ×2-2.3 от базовых цен
}
# Цена по умолчанию для модели без своей строки — у каждого провайдера своя
DEFAULT_PRICE: Dict[str, Tuple[float, float]] = {
    "cloudru": (30.0, 90.0),
    "yandex": (300.0, 600.0),
    "deepseek": (25.0, 100.0),
    "gigachat": (0.0, 0.0),
    "aitunnel": (60.0, 180.0),
}
_PRICE_ANY = {m: v for tab in PRICES_RUB.values() for m, v in tab.items()}
_PRICE_FALLBACK = (30.0, 90.0)
TARIFFS_UPDATED = "2026-10-04"


class LLMError(Exception):
    pass


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "JARVIS/1.0",
    }


# ------------------------------------------------ схемы авторизации провайдеров
# BM6: ЛЮБОЙ OpenAI-совместимый провайдер подключается ключом из конфига.
# Схемы: bearer (по умолчанию: Cloud.ru, DeepSeek, AITunnel, GPTunnel...),
# api-key (Yandex AI Studio: Authorization: Api-Key + папка), gigachat
# (Сбер: Authorization Key меняется на короткоживущий OAuth-токен).
_GIGA_SSL_CTX: Optional[ssl.SSLContext] = None


def _gigachat_ssl_ctx() -> ssl.SSLContext:
    """Сбер использует собственный корневой сертификат."""
    global _GIGA_SSL_CTX
    if _GIGA_SSL_CTX is None:
        ctx = ssl.create_default_context()
        ca = Path(__file__).resolve().parent / "certs" / "russian_trusted_root_ca.pem"
        try:
            ctx.load_verify_locations(castr := str(ca))
        except Exception:
            pass
        _GIGA_SSL_CTX = ctx
    return _GIGA_SSL_CTX


_TOKEN_CACHE: Dict[str, Tuple[float, str]] = {}
_TOKEN_LOCK = threading.Lock()


def _ssl_ctx_for(conf: Dict[str, Any]) -> ssl.SSLContext:
    return _gigachat_ssl_ctx() if str(conf.get("auth") or "") == "gigachat" else _SSL_CTX


def _gigachat_token(conf: Dict[str, Any]) -> str:
    """Authorization Key Сбера -> короткоживущий OAuth-токен (кэш 25 мин)."""
    key = str(conf.get("api_key") or "")
    now = time.time()
    with _TOKEN_LOCK:
        hit = _TOKEN_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    req = urllib.request.Request(
        "https://ngw.devices.sber.ru:9443/api/v2/oauth",
        data=b"scope=GIGACHAT_API_PERS",
        headers={
            "Authorization": "Basic " + key,
            "Content-Type": "application/x-www-form-urlencoded",
            "RqUID": str(uuid.uuid4()),
            "User-Agent": "JARVIS/1.0",
        },
        method="POST")
    with urllib.request.urlopen(req, timeout=12,
                                context=_gigachat_ssl_ctx()) as resp:
        token = str(json.load(resp).get("access_token") or "")
    if not token:
        raise LLMError("GigaChat не выдал токен")
    with _TOKEN_LOCK:
        _TOKEN_CACHE[key] = (now + 25 * 60, token)
    return token


def provider_headers(conf: Dict[str, Any]) -> Dict[str, str]:
    """Заголовки авторизации под схему конкретного провайдера."""
    scheme = str(conf.get("auth") or "bearer").lower()
    key = str(conf.get("api_key") or "")
    out = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "JARVIS/1.0",
    }
    if scheme == "api-key":
        # Yandex AI Studio: ключ сервиса + папка облака
        out["Authorization"] = "Api-Key " + key
        folder = str(conf.get("folder_id") or "")
        if folder:
            out["x-folder-id"] = folder
            out["OpenAI-Project"] = folder
    elif scheme == "gigachat":
        out["Authorization"] = "Bearer " + _gigachat_token(conf)
    else:
        out["Authorization"] = "Bearer " + key
    return out


def _request(url: str, api_key: str, payload: Optional[Dict] = None, method: str = "POST",
             timeout: int = 180, headers: Optional[Dict[str, str]] = None,
             ssl_ctx: Optional[ssl.SSLContext] = None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers or _headers(api_key), method=method)
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx or _SSL_CTX)


# ------------------------------------------------- приоритет главного ответа
# BM4: ПРИЧИНА «МЕДЛЕННО ДУМАЕТ И ПЕЧАТАЕТ» — ПОСТОРОННИЕ ВЫЗОВЫ В ПУТИ
# ОТВЕТА. Каждый рецидив этого бага имел один и тот же механизм: очередной
# «полезный фоновый» LLM-вызов (сводка, планировщик, подсказки, память)
# оказывался в критическом пути или в конкуренции с главным стримом — и
# провайдер с лимитами на ключ заставлял ответ человека ждать. Правило
# теперь железное: главный ответ держит «foreground», а все второстепенные
# вызовы (background=True) стартуют только когда ни один главный стрим
# не активен. Дедлока нет: ожидание ограничено, после таймаута вызов
# всё равно выполняется.
_FG_LOCK = threading.Lock()
_FG_COUNT = 0
_FG_FREE = threading.Event()
_FG_FREE.set()


class _Foreground:
    """Держит «линию свободной для фоновых вызовов» на время главного стрима."""

    def __enter__(self) -> "_Foreground":
        global _FG_COUNT
        with _FG_LOCK:
            _FG_COUNT += 1
            _FG_FREE.clear()
        return self

    def __exit__(self, *exc: Any) -> bool:
        global _FG_COUNT
        with _FG_LOCK:
            _FG_COUNT -= 1
            if _FG_COUNT <= 0:
                _FG_COUNT = 0
                _FG_FREE.set()
        return False


def wait_foreground_free(timeout: float = 20.0) -> bool:
    """Подождать, пока закончатся главные ответы. False — время вышло."""
    return _FG_FREE.wait(timeout)


# --------------------------------------------------- здоровье провайдеров
# BM5: ЭПИЗОДИЧЕСКАЯ ДЕГРАДАЦИЯ ПРОВАЙДЕРА — «думает долго, печатает по
# слову в секунду» при том же коде, что обычно отвечает мгновенно. Единственное,
# что меняется само по себе, — состояние API провайдера: периоды, когда первый
# токен приходит через 10-30 секунд, а сами токены капают по одному. Приложение
# было беззащитно: честно ждало зависшего провайдера и не переключалось.
# Теперь (1) СТОРОЖ ПЕРВОГО ТОКЕНА: если провайдер молчит дольше 12 секунд
# и есть резервный — попытка обрывается, запрос уходит к резерву; долгая
# генерация не страдает (любой токен сбрасывает сторож); (2) ПОРЯДОК
# ПРОВАЙДЕРОВ ПО ЗДОРОВЬЮ: свежие неудачи/медлительность понижают провайдера
# в очереди — следующие запросы сразу идут к тому, кто отвечает быстро.
TTFT_WATCHDOG_S = 12.0          # потолок молчания ДО первого токена (есть резерв)
_HEALTH_LOCK = threading.Lock()
_PROVIDER_HEALTH: Dict[str, "deque"] = {}
_HEALTH_TTL = 600.0             # здоровье живёт 10 минут — эпизоды проходят сами
_HEALTH_MIN_SAMPLES = 2         # меньше двух опытов — мнение не сложилось
_HEALTH_DEGRADED_TTFT = 4.0     # медиана TTFT выше — провайдер медленный
                                # (обычный отклик 0.5-2с; 4с — уже деградация)
_HEALTH_DEAD_CPS = 25.0         # меньше 25 зн/с печати — провайдер еле жив
                                # (обычная печать 100-200 зн/с)


def _record_provider_health(prov: str, ok: bool, ttft_s: float = 0.0,
                            cps: float = 0.0) -> None:
    """Записать исход попытки у провайдера (для адаптивного порядка)."""
    try:
        with _HEALTH_LOCK:
            q = _PROVIDER_HEALTH.setdefault(prov, deque(maxlen=24))
            q.append((time.monotonic(), bool(ok), float(ttft_s or 0.0),
                      float(cps or 0.0)))
    except Exception:
        pass


def _provider_penalty(prov: str) -> int:
    """Насколько провайдер плох ПО СВЕЖИМ фактам. 0 — претензий нет."""
    with _HEALTH_LOCK:
        now = time.monotonic()
        rows = [(ok, ttft, cps) for (ts, ok, ttft, cps)
                in _PROVIDER_HEALTH.get(prov, ())
                if now - ts < _HEALTH_TTL]
    penalty = 0
    if _probe_dead(prov):
        # BM6: зонд дважды упал — обходим сразу, даже если истории
        # разговоров ещё нет (раньше ранний выход по малой выборке
        # прятал это, и мёртвый провайдер упрямо оставался первым)
        penalty += 10
    if len(rows) < _HEALTH_MIN_SAMPLES:
        return penalty
    ok_rate = sum(1 for r in rows if r[0]) / float(len(rows))
    ttfts = sorted(r[1] for r in rows if r[0] and r[1] > 0)
    cps_all = sorted(r[2] for r in rows if r[0] and r[2] > 0)
    med_ttft = ttfts[len(ttfts) // 2] if ttfts else 0.0
    med_cps = cps_all[len(cps_all) // 2] if cps_all else 0.0
    if ok_rate < 0.6:
        penalty += 2
    if med_ttft > _HEALTH_DEGRADED_TTFT:
        penalty += 1 + (1 if med_ttft > 2 * _HEALTH_DEGRADED_TTFT else 0)
    if 0 < med_cps < _HEALTH_DEAD_CPS:
        penalty += 1
    return penalty


def _ttft_watchdog_fire(resp: Any, fired: List[bool]) -> None:
    """Сторож первого токена: закрыть сокет молчащего провайдера."""
    fired[0] = True
    try:
        resp.close()
    except Exception:
        pass


# --------------------------------------------------------- зонды провайдеров
# BM6: ТОЧНОЕ РАСПОЗНАВАНИЕ СБОЯ ПРОВАЙДЕРА. Сторож первого токена (BM5)
# ловит зависание ВО ВРЕМЯ ответа; зонд ловит ситуацию «сайт провайдера
# лежит целиком» ДО того, как человек нажмёт Enter: бесплатный GET /models
# каждые 45 секунд (при сбое — каждые 10, до восстановления). Два зонда
# подряд упали — провайдер помечен мёртвым и обходится СРАЗУ, без 12 секунд
# ожидания. Ответил — вернулся в строй сам.
_PROBE_STATE: Dict[str, Dict[str, Any]] = {}
_PROBE_LOCK = threading.Lock()
_PROBER_STOP = threading.Event()
_PROBER: Optional[threading.Thread] = None


def probe_provider(name: str, timeout: float = 4.0) -> bool:
    """Бесплатный зонд доступности API: GET /models с коротким таймаутом.

    401/403 — сервис ЖИВ (сеть и API работают, вопрос только в ключе);
    таймаут/обрыв/5xx — провайдер недоступен. Возвращает True, если жив."""
    conf = provider_conf(name)
    base = str(conf.get("base_url") or "").rstrip("/")
    if not base or not str(conf.get("api_key") or "").strip():
        return False
    # GigaChat: первый зонд обменивает OAuth-токен (отдельный round-trip
    # на порт 9443) — 4 секунды на всё не хватает, отдаём больше
    if str(conf.get("auth") or "") == "gigachat":
        timeout = max(timeout, 10.0)
    ok = False
    ms = 0.0
    try:
        t0 = time.monotonic()
        req = urllib.request.Request(base + "/models", method="GET",
                                     headers=provider_headers(conf))
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx_for(conf)) as r:
            ok = r.status < 500
        ms = (time.monotonic() - t0) * 1000.0
    except urllib.error.HTTPError as exc:
        ok = exc.code < 500          # 401/403/404: API отвечает — провайдер жив
        ms = 0.0
    except Exception:
        ok = False
    with _PROBE_LOCK:
        st = _PROBE_STATE.setdefault(
            name, {"fails": 0, "ok": None, "ms": 0.0, "at": 0.0, "flap": 0})
        was_dead = st["fails"] >= 2
        st["at"] = time.monotonic()
        st["ms"] = ms
        if ok:
            st["fails"] = 0
            st["ok"] = True
        else:
            st["fails"] += 1
            st["ok"] = False
        now_dead = st["fails"] >= 2
    if was_dead != now_dead:
        telemetry.emit("provider_probe", status=("down" if now_dead else "up"),
                       provider=name, probe_ms=round(ms, 1))
    return ok


_GEN_PROBE_STATE: Dict[str, Dict[str, float]] = {}
_GEN_PROBE_GAP_S = 300.0        # здоровый провайдер — микро-запрос раз в 5 минут
_GEN_PROBE_GAP_HOT_S = 120.0    # под подозрением (штраф) — раз в 2 минуты


def generation_probe(prov: str, timeout: float = 12.0) -> Optional[float]:
    """BM7: микро-запрос генерации — измеряет НАСТОЯЩУЮ скорость модели.

    Первопричина «зонд говорит жив, а отвечать не может»: GET /models
    обслуживает шлюз, генерацию — inference-кластер, и деградирует именно
    он. Один токен на самой дешевой модели: тайминги честные, цена ≈ 0.
    Результат пишется в здоровье провайдера: медленный, но «живой»
    провайдер понижается в очереди ещё до того, как человек успеет
    пожаловаться на медленный ответ."""
    conf = provider_conf(prov)
    base = str(conf.get("base_url") or "").rstrip("/")
    if not base or not str(conf.get("api_key") or "").strip():
        return None
    try:
        # BM8: меряем РАБОЧУЮ модель (base) — на nano всё быстро даже у
        # деградировавшего провайдера, и зонд врал «ген. 0.16с»
        model = pick_model("base", prov)
        payload = {"model": model,
                   "messages": [{"role": "user", "content": "Скажи: ок"}],
                   "max_tokens": 16, "stream": False}
        t0 = time.monotonic()
        resp = _request(base + "/chat/completions", conf["api_key"], payload,
                        "POST", timeout=int(timeout),
                        headers=provider_headers(conf),
                        ssl_ctx=_ssl_ctx_for(conf))
        with resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _record_provider_health(prov, False)   # перегружен — это деградация
        # 401/403/404/400: API отвечает, вопрос в ключе или имени модели —
        # это НЕ деградация провайдера, здоровье не портим
        return None
    except Exception:
        _record_provider_health(prov, False)
        return None
    ttft = time.monotonic() - t0
    _record_provider_health(prov, True, ttft_s=ttft)
    _GEN_PROBE_STATE[prov] = {"ttft": ttft, "at": time.monotonic()}
    return ttft


def _gen_probe_due(prov: str, names: List[str]) -> bool:
    """Генерационный зонд нужен основному (и тому, кто под штрафом).

    Темп бережливый: здоровому основному — раз в 5 минут (≈0.5₽ в день),
    провайдеру под подозрением — раз в 2 минуты, пока не оправдается."""
    hot = _provider_penalty(prov) > 0
    gap = _GEN_PROBE_GAP_HOT_S if hot else _GEN_PROBE_GAP_S
    last = _GEN_PROBE_STATE.get(prov)
    if last and time.monotonic() - float(last.get("at") or 0.0) < gap:
        return False
    order = provider_order(names)
    return prov == order[0] if order else False


_DEEP_PROBE_LOCK = threading.Lock()


def deep_probe_all(timeout_s: float = 15.0) -> None:
    """Генерационный зонд ВСЕХ активных провайдеров — по кнопке человека.

    Даёт полную картину «кто как реально отвечает» за один проход;
    параллельно, с общим потолком времени. Частые запуски не плодят
    запросы: пока один идёт, второй просто ждёт его конца."""
    names = active_providers()
    if not names:
        return
    with _DEEP_PROBE_LOCK:
        threads = []
        for name in names:
            def run(n: str = name) -> None:
                try:
                    generation_probe(n)
                except Exception:
                    pass
            t = threading.Thread(target=run, daemon=True,
                                 name="jarvis-genprobe")
            t.start()
            threads.append(t)
        deadline = time.monotonic() + max(3.0, timeout_s)
        for t in threads:
            t.join(max(0.1, deadline - time.monotonic()))


def provider_probe_status(name: str) -> Dict[str, Any]:
    """Снимок состояния зонда провайдера (для /api/providers)."""
    with _PROBE_LOCK:
        st = dict(_PROBE_STATE.get(name) or {})
    fresh = st and (time.monotonic() - float(st.get("at") or 0.0)) < 300
    return {
        "probed": bool(fresh),
        "ok": bool(fresh and st.get("ok")),
        "dead": bool(fresh and int(st.get("fails") or 0) >= 2),
        "probe_ms": round(float(st.get("ms") or 0.0), 1),
        "fails": int(st.get("fails") or 0) if fresh else 0,
    }


def _probe_dead(prov: str) -> bool:
    with _PROBE_LOCK:
        st = _PROBE_STATE.get(prov)
    if not st:
        return False
    if time.monotonic() - float(st.get("at") or 0.0) > 300:
        return False            # данные старше 5 минут — не осуждаем
    return int(st.get("fails") or 0) >= 2


def start_prober() -> None:
    """Фоновый дозор: опрашивает провайдеров, пока жив сервер.

    Обычный темп — раз в 45 секунд; если кто-то помечен мёртвым,
    темп поднимается до 10 секунд: восстановление замечается быстро."""
    global _PROBER
    if _PROBER and _PROBER.is_alive():
        return

    def loop() -> None:
        while not _PROBER_STOP.wait(45.0):
            names = active_providers()
            for name in names:
                if _PROBER_STOP.is_set():
                    break
                try:
                    probe_provider(name)
                except Exception:
                    pass
                # BM7: сайт жив — проверяем и ГЕНЕРАЦИЮ (микро-запрос).
                # Основной и оштрафованный провайдеры: медленный, но живой
                # сайт больше не обманывает очередь
                if not _probe_dead(name):
                    try:
                        if _gen_probe_due(name, names) or _provider_penalty(name) > 0:
                            generation_probe(name)
                    except Exception:
                        pass
            # кто-то мёртв — опрашиваем чаще, чтобы поймать восстановление
            if any(_probe_dead(n) for n in names):
                deadline = time.monotonic() + 35.0
                while (time.monotonic() < deadline
                       and not _PROBER_STOP.wait(10.0)):
                    for name in names:
                        if _PROBER_STOP.is_set():
                            break
                        try:
                            probe_provider(name)
                        except Exception:
                            pass
                    if not any(_probe_dead(n) for n in names):
                        break

    _PROBER = threading.Thread(target=loop, daemon=True, name="jarvis-prober")
    _PROBER.start()


def provider_order(candidates: List[str]) -> List[str]:
    """Кандидаты, отсортированные по свежему здоровью (стабильно).

    Провайдеры без претензий держат исходный порядок; провайдер в эпизоде
    деградации (молчит/медленный/льётся по слову в секунду) опускается ниже.
    Когда эпизод проходит (окно 10 минут), порядок возвращается сам."""
    base = list(candidates)
    if len(base) < 2:
        return base
    return sorted(base, key=lambda p: (_provider_penalty(p), base.index(p)))


# ------------------------------------------------------------- keep-alive
# urllib открывает новое TCP+TLS-соединение на каждый запрос. На нестримовых
# вызовах (chat, каталог моделей) соединение можно переиспользовать: это
# убирает рукопожатие (~50–150 мс) с каждого запроса к провайдеру. Пул
# поток-локальный: соединение никогда не делится между потоками. Стриминг
# остаётся на urllib — там соединение живёт весь ответ и до EOF не дочитывается.
_POOL_TLS = threading.local()
_POOL_PER_KEY = 4


def _pool_take(key: tuple, timeout: float,
               ssl_ctx: Optional[ssl.SSLContext] = None) -> http.client.HTTPConnection:
    pool = getattr(_POOL_TLS, "pool", None)
    if pool is None:
        pool = _POOL_TLS.pool = {}
    conns = pool.get(key)
    if conns:
        return conns.pop()
    scheme, host, port = key
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ssl_ctx or _SSL_CTX)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _pool_return(key: tuple, conn: http.client.HTTPConnection) -> None:
    pool = getattr(_POOL_TLS, "pool", None)
    if pool is None:
        pool = _POOL_TLS.pool = {}
    conns = pool.setdefault(key, [])
    if len(conns) < _POOL_PER_KEY:
        conns.append(conn)
        return
    try:
        conn.close()
    except Exception:
        pass


class _PooledResponse:
    """Ответ поверх http.client: после ПОЛНОГО чтения соединение возвращается в пул."""

    def __init__(self, key: tuple, conn: http.client.HTTPConnection,
                 resp: http.client.HTTPResponse) -> None:
        self._key = key
        self._conn = conn
        self._resp = resp
        self.headers = resp.headers
        self.status = resp.status

    def read(self, n: int = -1) -> bytes:
        # read(-1) у http.client значит «до EOF» и вечно висит на keep-alive
        # сокете; «всё тело» — это read(None) (ровно Content-Length байт).
        return self._resp.read(None if n is None or n < 0 else n)

    def close(self) -> None:
        try:
            # переиспользовать можно только полностью прочитанное соединение,
            # на котором сервер не объявил Connection: close
            if self._resp.isclosed() and not self._resp.will_close:
                _pool_return(self._key, self._conn)
                return
        except Exception:
            pass
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "_PooledResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


def _request_pooled(url: str, api_key: str, payload: Optional[Dict] = None,
                    method: str = "POST", timeout: int = 180,
                    headers: Optional[Dict[str, str]] = None,
                    ssl_ctx: Optional[ssl.SSLContext] = None):
    """Keep-alive запрос; при любой проблеме молча возвращаем None -> urllib-путь."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    key = (parsed.scheme, parsed.hostname,
           parsed.port or (443 if parsed.scheme == "https" else 80))
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    hdrs = dict(headers or _headers(api_key))
    try:
        conn = _pool_take(key, timeout, ssl_ctx)
        try:
            conn.request(method, target, body=data, headers=hdrs)
            resp = conn.getresponse()
        except Exception:
            # соединение из пула могло протухнуть: закрываем и уходим в urllib
            try:
                conn.close()
            except Exception:
                pass
            return None
        return _PooledResponse(key, conn, resp)
    except Exception:
        return None


def provider_conf(name: str) -> Dict[str, Any]:
    return CONFIG.get("providers." + name, {}) or {}


def active_providers() -> List[str]:
    """Все включённые провайдеры с ключом, по приоритету (меньше = раньше).

    BM6: список больше не зашит — любой OpenAI-совместимый провайдер
    (Yandex AI Studio, GigaChat, AITunnel, GPTunnel...) добавляется в
    конфиг и подхватывается сам. "priority" задаёт порядок основного
    выбора: 0 — основной, больше — запасные."""
    confs = CONFIG.get("providers", {}) or {}
    rows = []
    for idx, (name, conf) in enumerate(confs.items()):
        if not isinstance(conf, dict):
            continue
        if conf.get("enabled") and str(conf.get("api_key") or "").strip():
            try:
                prio = float(conf.get("priority", 100))
            except (TypeError, ValueError):
                prio = 100
            rows.append((prio, idx, name))
    rows.sort()
    return [name for _, _, name in rows]


def list_models_meta(provider: str, force: bool = False) -> List[Dict[str, Any]]:
    """Каталог моделей ЦЕЛИКОМ, вместе с метаданными провайдера.

    Раньше мы сохраняли только идентификаторы и потом угадывали умения модели
    по её названию. Но провайдер сам сообщает тип модели в metadata.type
    (text-to-text, audio-to-text, image-text-to-text...). Это факт, а не догадка,
    поэтому храним каталог как есть.
    """
    with _CACHE_LOCK:
        cached = _META_CACHE.get(provider)
        if cached and not force:
            age = time.time() - cached[0]
            if age < 600:
                return cached[1]
            # СТАРЫЙ КАТАЛОГ ЛУЧШЕ, ЧЕМ ОЖИДАНИЕ. Каталог моделей меняется раз
            # в недели, а протухал раз в 10 минут — и тогда первый же вопрос
            # пользователя вставал в очередь за походом в облако за списком
            # моделей (до 25 с таймаута). Именно поэтому Джарвис «иногда»
            # отвечал медленно: скорость зависела от того, попал ли вопрос в
            # окно обновления кэша. Отдаём что есть, обновляем в фоне.
            if cached[1] and not _META_BUSY.get(provider):
                _META_BUSY[provider] = True
                threading.Thread(target=_refresh_meta, args=(provider,),
                                 name="jarvis-models", daemon=True).start()
            return cached[1]
    conf = provider_conf(provider)
    if not conf.get("api_key"):
        return []
    try:
        # таймаут короткий: каталог — вспомогательные данные, а не ответ
        # пользователю. Не дождались — уйдём на предпочтения из настроек.
        models_url = conf["base_url"].rstrip("/") + "/models"
        mresp = _request_pooled(models_url, conf["api_key"], None, "GET", timeout=6,
                                headers=provider_headers(conf),
                                ssl_ctx=_ssl_ctx_for(conf))
        if mresp is None:
            mresp = _request(models_url, conf["api_key"], None, "GET", timeout=6,
                             headers=provider_headers(conf),
                             ssl_ctx=_ssl_ctx_for(conf))
        with mresp as resp:
            body = json.loads(resp.read().decode("utf-8"))
        items = [m for m in body.get("data", []) if m.get("id")]
    except Exception:
        items = []
    with _CACHE_LOCK:
        _META_CACHE[provider] = (time.time(), items)
    return items


_META_BUSY: Dict[str, bool] = {}


def _refresh_meta(provider: str) -> None:
    """Обновить каталог моделей в фоне, никого не задерживая."""
    try:
        list_models_meta(provider, force=True)
    except Exception:
        pass
    finally:
        _META_BUSY.pop(provider, None)


def model_type(item: Dict[str, Any]) -> str:
    """Тип модели так, как его называет сам провайдер (пустая строка — не сказал)."""
    meta = item.get("metadata") or {}
    return str(meta.get("type") or item.get("type") or "").lower()


def models_of_type(provider: str, *needles: str) -> List[str]:
    """Модели, ТИП которых (по данным провайдера) содержит одну из подстрок."""
    out = []
    for item in list_models_meta(provider):
        kind = model_type(item)
        if kind and any(n in kind for n in needles):
            out.append(item["id"])
    return out


def list_models(provider: str, force: bool = False) -> List[str]:
    """Имена моделей провайдера; TTL и фоновое обновление — в list_models_meta."""
    return [m["id"] for m in list_models_meta(provider, force=force)]


def vision_models(provider: str) -> List[str]:
    """Модели, которые ПО СЛОВАМ ПРОВАЙДЕРА принимают изображение.

    Если каталог не пришёл (нет сети), падаем на грубую догадку по имени —
    иначе оффлайн-сбой каталога выглядел бы как «зрения не существует».
    """
    seeing = models_of_type(provider, "image-text-to-text", "image-to-text", "multimodal")
    if seeing:
        return seeing
    return [m for m in list_models(provider) if "-vl" in m.lower() or "vision" in m.lower()]


def model_can_see(provider: str, model: str) -> bool:
    return bool(model) and model in vision_models(provider)


def has_image(messages: List[Dict]) -> bool:
    """Есть ли в диалоге хоть одна картинка (мультимодальный content)."""
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def flatten_images(messages: List[Dict]) -> List[Dict]:
    """Сплющить мультимодальный content в обычный текст.

    ПРИЧИНА существования этой функции: модель, не умеющая смотреть, на
    список частей отвечает HTTP 400 «unknown variant 'image_url'» и весь
    ответ превращается в «Модели недоступны». Такое случалось на второй
    попытке (эскалация vision → smart) и при фолбэке на резервного
    провайдера. Теперь картинка уходит ТОЛЬКО тому, кто умеет её принять,
    а остальным достаётся честная пометка вместо неперевариваемых данных.
    """
    out: List[Dict] = []
    for m in messages or []:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                parts.append("[изображение приложено, но эта модель не умеет смотреть]")
        flat = dict(m)
        flat["content"] = "\n".join(p for p in parts if p)
        out.append(flat)
    return out


def pick_model(tier: str, provider: str = "cloudru") -> str:
    """Выбирает конкретное имя модели под «уровень» из доступных у провайдера."""
    prefs = CONFIG.get("model_tiers." + tier, []) or []

    # БЫСТРЫЙ ПУТЬ. Каталог нужен только чтобы проверить, существует ли
    # модель. Но пока каталог не пришёл, ждать его нельзя: это ожидание
    # стоит перед первым словом ответа. Если каталог уже лежит в кэше —
    # сверяемся с ним; если нет — берём предпочтение из настроек и идём
    # спрашивать модель, а каталог подтянется в фоне к следующему разу.
    # Ошибиться тут почти невозможно: имена в model_tiers мы задаём сами,
    # а если модель вдруг исчезла, провайдер ответит ошибкой и сработает
    # обычный запасной путь.
    with _CACHE_LOCK:
        have_cache = bool(_META_CACHE.get(provider))
    if not have_cache and prefs and tier != "vision":
        if not _META_BUSY.get(provider):
            _META_BUSY[provider] = True
            threading.Thread(target=_refresh_meta, args=(provider,),
                             name="jarvis-models", daemon=True).start()
        return prefs[0]

    available = list_models(provider)
    if tier == "vision" and available:
        # Для зрения выбираем ТОЛЬКО среди зрячих моделей. Иначе предпочтение
        # вроде «VL» могло подстрокой поймать текстовую модель, и картинка
        # уходила тому, кто её не переваривает.
        seeing = vision_models(provider)
        if seeing:
            available = seeing
    if not available:
        # провайдер не ответил — берём первое предпочтение как есть
        return prefs[0] if prefs else ("deepseek-chat" if provider == "deepseek" else "openai/gpt-oss-120b")
    lowered = {m.lower(): m for m in available}
    for pref in prefs:
        if pref in available:
            return pref
        p = pref.lower()
        if p in lowered:
            return lowered[p]
        for low, orig in lowered.items():
            if p in low or low in p:
                return orig
    # Ничего не совпало. Дальше решает НЕ название, а тип модели из каталога
    # провайдера: «image-text-to-text» — это зрение, и это факт, а не догадка.
    if tier == "vision":
        seeing = vision_models(provider)
        if seeing:
            return seeing[0]
    return available[0]


def _price_for(model: str, provider: str) -> Tuple[float, float]:
    """Цена модели у КОНКРЕТНОГО провайдера: точное имя, потом подстрока
    в обе стороны (у Cloud.ru каталог с префиксами openai/..., у Яндекса
    без), потом дефолт провайдера. Чужая таблица не используется: одна и
    та же модель у разных провайдеров стоит по-разному."""
    prov = str(provider or "").lower()
    table = PRICES_RUB.get(prov) or {}
    if model in table:
        return table[model]
    low = model.lower()
    for key, price in table.items():
        k = key.lower()
        if k in low or low in k:
            return price
    if not prov and model in _PRICE_ANY:   # старый вызов без провайдера
        return _PRICE_ANY[model]
    return DEFAULT_PRICE.get(prov, _PRICE_FALLBACK)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  provider: str = "") -> float:
    price_in, price_out = _price_for(str(model or ""), provider)
    return prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out


# Глубина размышления по уровню. Ключ — тот же tier, что выбрал оркестратор:
# один источник истины, никаких вторых правил «когда думать дольше».
_REASONING_EFFORT = {
    "nano": "low",      # болтовня — думать не о чем
    "base": "low",      # обычные вопросы: ответ важнее внутреннего монолога
    "coder": "low",     # специализированная coder уже умеет код; AGENT важнее начать быстро
    "vision": "low",    # описать картинку — не задача на рассуждение
    "smart": "high",    # сюда попадают только те, кому рассуждение и нужно
}


def _load_capability_cache() -> None:
    """Лениво загружает только безопасный allowlist полей; corrupt файл игнорируется."""
    global _CAPABILITY_LOADED
    with _CACHE_LOCK:
        if _CAPABILITY_LOADED:
            return
        _CAPABILITY_LOADED = True
        try:
            raw = json.loads(_CAPABILITY_PATH.read_text(encoding="utf-8"))
            for item in raw.get("unsupported", []):
                provider = str(item.get("provider") or "")
                model = str(item.get("model") or "")
                names = set(item.get("fields") or []) & _CAPABILITY_FIELDS
                if provider and model and names:
                    _UNSUPPORTED[(provider, model)] = names
        except Exception:
            pass


def _persist_capability_cache() -> None:
    """Атомарный tiny state: не заставляет каждый запуск повторять известный HTTP 400."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        body = {
            "version": 1,
            "unsupported": [
                {"provider": provider, "model": model, "fields": sorted(names)}
                for (provider, model), names in sorted(_UNSUPPORTED.items()) if names
            ],
        }
        tmp = _CAPABILITY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(_CAPABILITY_PATH)
    except Exception:
        pass


def _remember_unsupported(provider: str, model: str, *names: str) -> None:
    names_set = set(names) & _CAPABILITY_FIELDS
    if not provider or not model or not names_set:
        return
    _load_capability_cache()
    with _CACHE_LOCK:
        known = _UNSUPPORTED.setdefault((provider, model), set())
        before = len(known)
        known.update(names_set)
        if len(known) != before:
            _persist_capability_cache()


def _apply_capability_cache(payload: Dict[str, Any], provider: str, model: str) -> None:
    _load_capability_cache()
    with _CACHE_LOCK:
        missing = set(_UNSUPPORTED.get((provider, model), set()))
    for name in missing:
        payload.pop(name, None)
    if "tools" not in payload:
        payload.pop("tool_choice", None)


def _drop_unsupported(payload: Dict[str, Any], detail: str,
                      provider: str = "", model: str = "") -> bool:
    """Убрать и закэшировать ровно доказанно неподдерживаемый параметр."""
    low = (detail or "").lower()
    candidates: List[str] = []
    if "stream_options" in payload and (
            "stream_options" in low or "include_usage" in low):
        candidates = ["stream_options"]
    elif "reasoning_effort" in payload and "reasoning_effort" in low:
        candidates = ["reasoning_effort"]
    elif "tool_choice" in payload and "tool_choice" in low:
        candidates = ["tool_choice"]
    elif "tools" in payload and ("tools" in low or "function calling" in low):
        candidates = ["tools", "tool_choice"]
    if not candidates:
        return False
    for name in candidates:
        payload.pop(name, None)
    _remember_unsupported(provider, model, *candidates)
    return True


def _build_payload(model: str, messages: List[Dict], tools: Optional[List[Dict]], stream: bool,
                   temperature: Optional[float], max_tokens: Optional[int],
                   provider: str = "cloudru", tier: str = "base") -> Dict[str, Any]:
    # ЕДИНСТВЕННОЕ место, где рождается запрос к модели, — здесь же и
    # единственная проверка «а этот собеседник вообще умеет смотреть».
    # Раньше картинку клали в сообщение выше по коду и надеялись, что
    # маршрутизация не подведёт; любая эскалация или смена провайдера
    # ломала эту надежду и приносила HTTP 400.
    if has_image(messages) and not model_can_see(provider, model):
        messages = flatten_images(messages)
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": CONFIG.get("orchestrator.temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens or CONFIG.get("orchestrator.max_output_tokens", 2400),
    }
    # ГЛУБИНА РАЗМЫШЛЕНИЯ. Вот настоящая причина «иногда думает бесконечно»:
    # и gpt-oss-120b (base), и GLM-4.7 (smart) — рассуждающие модели, и по
    # умолчанию они работают на medium. Размышление идёт ДО первого слова
    # ответа, пользователь всё это время смотрит в пустоту, а токены капают.
    # Замеры сообщества: low ≈ 880 токенов рассуждения, high ≈ 8000 — почти
    # десятикратная разница во времени ожидания на ровном месте.
    # Мы не угадываем сложность по тексту (эти списки слов уже выкинуты) —
    # глубина следует за УРОВНЕМ, который выбрал оркестратор: болтовня и
    # обычные вопросы отвечаются быстро, а smart зовётся только там, где
    # рассуждение действительно нужно.
    effort = _REASONING_EFFORT.get(tier or "base")
    if effort:
        payload["reasoning_effort"] = effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream_options"] = {"include_usage": True}
    _apply_capability_cache(payload, provider, model)
    return payload


def chat(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None,
         provider: Optional[str] = None, timeout: int = 180,
         operation: str = "llm", background: bool = False) -> Dict[str, Any]:
    """Не-стриминговый вызов с автоматическим фолбэком на резервного провайдера.

    ``timeout`` — общий wall-clock budget всего вызова, включая повтор и
    резервного провайдера. Раньше он ошибочно применялся к КАЖДОЙ попытке:
    planner с timeout=25 мог задержать AGENT более чем на 100 секунд.
    """
    # BM4: второстепенный вызов не конкурирует с главным ответом: пока идёт
    # чей-то foreground-стрим, ждём свободного окна (с потолком — без дедлоков)
    if background:
        wait_foreground_free(min(20.0, max(1.0, float(timeout))))
    providers = [provider] if provider else provider_order(
        active_providers() or ["cloudru"])
    span = telemetry.Span(operation, tier=tier)
    deadline = time.monotonic() + max(0.25, float(timeout))
    last_error: Optional[Exception] = None
    last_provider = ""
    last_model = ""
    for prov in providers:
        if time.monotonic() >= deadline:
            break
        conf = provider_conf(prov)
        if not conf.get("api_key"):
            continue
        model = pick_model(tier, prov)
        last_provider, last_model = prov, model
        try:
            pheaders = provider_headers(conf)
        except Exception as exc:
            # GigaChat-токен не обменялся — идём к следующему провайдеру
            last_error = exc
            span.retried()
            continue
        pctx = _ssl_ctx_for(conf)
        payload = _build_payload(model, messages, tools, False, temperature, max_tokens, prov, tier)
        # BM13: НАЗВАНИЕ МОДЕЛИ МОЖЕТ НЕ СУЩЕСТВОВАТЬ у этого провайдера:
        # в nano-списке имена чужих площадок (ai-sage/…), и без каталога
        # выбиралось первое предпочтение — 404 на КАЖДОМ вызове. Пока
        # каталог не пришёл, tier != base честно пробует БАЗОВУЮ модель
        # (она отвечает — главные ответы работают), а не умирает
        with _CACHE_LOCK:
            _have_models = bool(_META_CACHE.get(prov))
        if tier != "base" and not _have_models:
            model = pick_model("base", prov)
            payload = _build_payload(model, messages, tools, False,
                                     temperature, max_tokens, prov, "base")
        for attempt in range(2):
            t_attempt = time.monotonic()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                url = conf["base_url"].rstrip("/") + "/chat/completions"
                resp = _request_pooled(url, conf["api_key"], payload, "POST",
                                       timeout=max(0.1, remaining),
                                       headers=pheaders, ssl_ctx=pctx)
                if resp is None:
                    resp = _request(url, conf["api_key"], payload,
                                    timeout=max(0.1, remaining),
                                    headers=pheaders, ssl_ctx=pctx)
                with resp:
                    raw_body = resp.read().decode("utf-8")
                try:
                    body = json.loads(raw_body)
                except ValueError:
                    # AG: 200 с ПУСТЫМ телом — раньше наружу летел сырой
                    # «Expecting value: line 1 column 1 (char 0)». Это отказ
                    # МОДЕЛИ, а не поломка: понятные слова + повтор/следующая
                    last_error = LLMError("пустой ответ от модели %s" % model)
                    span.retried()
                    continue
                span.first_token()
                usage = body.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or 0)
                ct = int(usage.get("completion_tokens") or 0)
                db.log_usage(prov, model, tier, pt, ct,
                             estimate_cost(model, pt, ct, prov))
                _report_usage(model, pt, ct)
                choice = (body.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                _dur = max(0.001, time.monotonic() - t_attempt)
                _content = message.get("content") or ""
                _record_provider_health(prov, True, _dur,
                                        len(_content) / _dur)
                span.finish("ok", provider=prov, model=model)
                return {
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls") or [],
                    "reasoning": message.get("reasoning_content") or "",
                    "model": model,
                    "provider": prov,
                    "usage": usage,
                }
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:400]
                except Exception:
                    pass
                last_error = LLMError("HTTP %s %s: %s" % (exc.code, model, detail))
                span.retried()
                _record_provider_health(prov, False)
                if exc.code in (400, 404, 422) and _drop_unsupported(
                        payload, detail, prov, model):
                    # модель не поняла какой-то параметр (tools или
                    # reasoning_effort) — выбрасываем именно его и повторяем,
                    # а не заваливаем весь запрос
                    continue
                break
            except Exception as exc:  # сеть/таймаут
                last_error = exc
                span.retried()
                _record_provider_health(prov, False)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(1.2, remaining))
    expired = time.monotonic() >= deadline
    span.finish("timeout" if expired else "error", provider=last_provider, model=last_model,
                error_type=type(last_error).__name__ if last_error else "no_provider")
    raise LLMError("Не удалось получить ответ от моделей%s: %s" %
                   (" за отведённое время" if expired else "", last_error))


def _chat_stream_impl(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
                      temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                      provider: Optional[str] = None, operation: str = "llm_stream",
                      _span: Optional[telemetry.Span] = None,
                      should_stop: Optional[Callable[[], bool]] = None) -> Generator[Dict[str, Any], None, None]:
    """Внутренняя реализация; публичная обёртка гарантирует закрытие span."""
    providers = [provider] if provider else provider_order(
        active_providers() or ["cloudru"])
    span = _span or telemetry.Span(operation, tier=tier)
    last_error: Optional[Exception] = None
    last_provider = ""
    last_model = ""
    for prov_idx, prov in enumerate(providers):
        conf = provider_conf(prov)
        if not conf.get("api_key"):
            continue
        model = pick_model(tier, prov)
        last_provider, last_model = prov, model
        span.fields.update({"provider": prov, "model": model})
        try:
            pheaders = provider_headers(conf)
        except Exception as exc:
            # GigaChat-токен не обменялся — идём к следующему провайдеру
            last_error = exc
            span.retried()
            continue
        pctx = _ssl_ctx_for(conf)
        # BM5: сторож armed только при наличии резерва — если резервного
        # провайдера нет, ждать можно долго: поздний ответ лучше никакого
        has_fallback = prov_idx < len(providers) - 1
        payload = _build_payload(model, messages, tools, True, temperature, max_tokens, prov, tier)
        # попытка 1 — с инструментами; попытка 2 — без них (если модель их не умеет)
        for attempt in range(2):
            started_output = False
            saw_done = False
            fired: List[bool] = [False]
            wd: Dict[str, Any] = {"timer": None}
            t_attempt = time.monotonic()
            first_at: Optional[float] = None
            try:
                # BM5: ПОТОЛОК МОЛЧАНИЯ 180 С — долгая генерация не ошибка,
                # сокет сбрасывается каждым чанком, так что это таймаут на
                # МОЛЧАНИЕ провайдера (пожелание человека: ждать можно долго).
                # Но зависший ДО ПЕРВОГО ТОКЕНА провайдер выручается раньше:
                # сторож закрывает сокет через TTFT_WATCHDOG_S секунд, и
                # запрос уходит к резервному провайдеру. Живая печать сторож
                # не трогает — первый же токен его отменяет.
                with _request(conf["base_url"].rstrip("/") + "/chat/completions",
                              conf["api_key"], payload, timeout=180,
                              headers=pheaders, ssl_ctx=pctx) as resp:
                    if has_fallback:
                        wd["timer"] = threading.Timer(
                            TTFT_WATCHDOG_S, _ttft_watchdog_fire,
                            args=(resp, fired))
                        wd["timer"].daemon = True
                        wd["timer"].start()
                    acc_content: List[str] = []
                    acc_reasoning: List[str] = []
                    tool_acc: Dict[int, Dict[str, Any]] = {}
                    usage: Dict[str, Any] = {}
                    # Открытый HTTP response и служебное событие `model` ещё не
                    # являются выводом модели. Если сокет оборвался до первого
                    # content/reasoning/tool delta, следующий provider может
                    # безопасно продолжить — пользователю нечего дублировать.
                    yield {"type": "model", "model": model, "provider": prov, "tier": tier}
                    for raw in resp:
                        if should_stop is not None:
                            try:
                                if should_stop():
                                    break
                            except Exception:
                                pass
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            saw_done = True
                            break
                        try:
                            obj = json.loads(chunk)
                        except Exception:
                            continue
                        if obj.get("usage"):
                            usage = obj["usage"]
                        for choice in obj.get("choices") or []:
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                started_output = True
                                span.first_token()
                                acc_content.append(piece)
                                yield {"type": "delta", "text": piece}
                            think = delta.get("reasoning_content") or delta.get("reasoning")
                            if think:
                                started_output = True
                                span.first_token()
                                # AR: повтор хвоста = сбой потока, а не мысль —
                                # отдаём только настоящий прирост
                                think = _reasoning_increment(
                                    "".join(acc_reasoning[-4:])[-80:], think)
                                # AW: НЕ continue — в этой же дельте могут
                                # ехать content и tool_calls; чистый дубль
                                # просто не эмитится, остальное живёт
                                if think:
                                    acc_reasoning.append(think)
                                    yield {"type": "reasoning", "text": think}
                            for tc in delta.get("tool_calls") or []:
                                started_output = True
                                span.first_token()
                                idx = tc.get("index", 0)
                                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                                    yield {"type": "tool_partial", "name": slot["name"], "args": slot["arguments"]}
                        if started_output and wd["timer"] is not None:
                            # BM5: модель подала первый знак жизни — сторож
                            # первого токена отменяется, дальше живёт обычный
                            # потолок молчания
                            wd["timer"].cancel()
                            wd["timer"] = None
                            if first_at is None:
                                first_at = time.monotonic() - t_attempt
                # A clean socket EOF is not a completion signal. Before the
                # first real delta it is safe to try the next provider; after a
                # delta, fallback would duplicate already-visible output and is
                # therefore forbidden by the same started_output boundary.
                if not saw_done:
                    raise LLMError("поток завершился без маркера [DONE]")
                pt = int((usage or {}).get("prompt_tokens") or 0)
                ct = int((usage or {}).get("completion_tokens") or 0)
                if pt or ct:
                    db.log_usage(prov, model, tier, pt, ct,
                             estimate_cost(model, pt, ct, prov))
                    _report_usage(model, pt, ct)
                calls = []
                for idx in sorted(tool_acc):
                    slot = tool_acc[idx]
                    if slot.get("name"):
                        calls.append({
                            "id": slot.get("id") or ("call_%d" % idx),
                            "type": "function",
                            "function": {"name": slot["name"], "arguments": slot.get("arguments") or "{}"},
                        })
                _dur = max(0.001, time.monotonic() - t_attempt)
                _record_provider_health(
                    prov, True,
                    ttft_s=(first_at if first_at is not None else _dur),
                    cps=len("".join(acc_content)) / _dur)
                span.finish("ok", provider=prov, model=model)
                yield {
                    "type": "done",
                    "content": "".join(acc_content),
                    "reasoning": "".join(acc_reasoning),
                    "tool_calls": calls,
                    "model": model,
                    "provider": prov,
                    "usage": usage,
                }
                return
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:
                    pass
                last_error = LLMError("HTTP %s: %s" % (exc.code, detail))
                span.retried()
                _record_provider_health(prov, False)
                if (exc.code in (400, 404, 422) and not started_output
                        and _drop_unsupported(payload, detail, prov, model)):
                    # сервер не понял какой-то параметр (reasoning_effort или
                    # tools) — выбрасываем именно его и пробуем ещё раз
                    continue
                if started_output:
                    span.finish("error", provider=prov, model=model,
                                error_type=type(last_error).__name__)
                    yield {"type": "error", "error": "Поток модели прерван: %s" % last_error}
                    return
                break
            except Exception as exc:
                last_error = exc
                span.retried()
                _record_provider_health(prov, False)
                if fired[0] and not started_output and has_fallback:
                    # BM5: сторож закрыл молчащего провайдера — человек видит
                    # честную строку вместо мёртвого «думаю», запрос уходит
                    # к резервному провайдеру
                    yield {"type": "provider_switch", "from": prov}
                if started_output:
                    span.finish("error", provider=prov, model=model,
                                error_type=type(exc).__name__)
                    yield {"type": "error", "error": "Поток модели прерван: %s" % exc}
                    return
                break
            finally:
                t = wd["timer"]
                if t is not None:
                    t.cancel()
                    wd["timer"] = None
    span.finish("error", provider=last_provider, model=last_model,
                error_type=type(last_error).__name__ if last_error else "no_provider")
    yield {"type": "error", "error": "Модели недоступны: %s" % last_error}


def chat_stream(messages: List[Dict], tier: str = "base", tools: Optional[List[Dict]] = None,
                temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                provider: Optional[str] = None,
                operation: str = "llm_stream",
                should_stop: Optional[Callable[[], bool]] = None) -> Generator[Dict[str, Any], None, None]:
    """Стриминг с telemetry success/error/cancel даже при досрочном close().

    ``should_stop`` проверяется на КАЖДОЙ строке провайдера: Stop пользователя
    обязан рвать чтение немедленно, не дожидаясь конца потока или таймаута —
    иначе «остановленный» агент продолжал платить за генерацию."""
    span = telemetry.Span(operation, tier=tier)
    try:
        # BM4: главный стрим держит foreground — фоновые вызовы (подсказки,
        # планировщик, память, идеи) ждут свободного окна, а не рвут печать
        with _Foreground():
            yield from _chat_stream_impl(messages, tier=tier, tools=tools,
                                         temperature=temperature, max_tokens=max_tokens,
                                         provider=provider, operation=operation, _span=span,
                                         should_stop=should_stop)
    except GeneratorExit:
        span.finish("cancelled")
        raise
    except BaseException as exc:
        span.finish("error", error_type=type(exc).__name__)
        raise
    finally:
        # Идемпотентно: normal/error уже закрыты implementation, а close()
        # до первого/между yield получает корректный cancelled.
        span.finish("cancelled")


def vision(prompt: str, image_data_url: str, tier: str = "vision") -> str:
    """Анализ изображения (кадр камеры, скриншот, фото).

    Смотреть зовём ТОЛЬКО того провайдера, у которого есть зрячая модель.
    Раньше сюда приходил общий список провайдеров, и при любой заминке
    Cloud.ru картинка уезжала в DeepSeek — тот отвечал HTTP 400, а
    пользователь читал «Модели недоступны», хотя недоступно было зрение.
    """
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }]
    seeing = [p for p in (active_providers() or ["cloudru"]) if vision_models(p)]
    if not seeing:
        raise LLMError("ни у одного подключённого провайдера нет модели со зрением")
    last: Optional[Exception] = None
    for prov in seeing:
        try:
            return chat(messages, tier=tier, max_tokens=1200,
                        provider=prov).get("content", "")
        except Exception as exc:
            last = exc
    raise LLMError("зрение не ответило: %s" % last)


def provider_display(name: str) -> str:
    """Человеческое имя провайдера для строк состояния."""
    label = str(provider_conf(name).get("label") or name)
    return label.split("(")[0].strip() or name


def providers_status() -> Dict[str, Any]:
    """Полный снимок состояния всех сконфигурированных провайдеров.

    Сходится всё, что нужно для точного ответа «кто сейчас жив»:
    зонд (сеть/API), здоровье (TTFT и скорость печати) и штраф очереди."""
    out: Dict[str, Any] = {}
    for name, conf in (CONFIG.get("providers") or {}).items():
        if not isinstance(conf, dict):
            continue
        probe = provider_probe_status(name)
        act = active_providers()
        gen = _GEN_PROBE_STATE.get(name)
        sample = {"label": provider_display(name),
                  "enabled": bool(conf.get("enabled")),
                  "has_key": bool(str(conf.get("api_key") or "").strip()),
                  "order": act.index(name) if name in act else -1,
                  "probe": probe,
                  "gen_ttft_s": (round(float(gen.get("ttft") or 0.0), 2)
                                 if gen and time.monotonic() - float(gen.get("at") or 0.0) < 600 else None),
                  "penalty": _provider_penalty(name)}
        now = time.monotonic()
        with _HEALTH_LOCK:
            rows = [(ok, ttft, cps) for (ts, ok, ttft, cps)
                    in _PROVIDER_HEALTH.get(name, ()) if now - ts < _HEALTH_TTL]
        if rows:
            ttfts = sorted(t for (ok, t, _) in rows if ok and t > 0)
            cps = [c for (ok, _, c) in rows if ok and c > 0]
            sample["ok_rate"] = round(sum(1 for r in rows if r[0]) / float(len(rows)), 2)
            sample["samples"] = len(rows)
            if ttfts:
                sample["med_ttft_s"] = round(ttfts[len(ttfts) // 2], 2)
            if cps:
                sample["med_cps"] = round(sorted(cps)[len(cps) // 2], 1)
        out[name] = sample
    return out


def health() -> Dict[str, Any]:
    out = {}
    for name in active_providers() or ["cloudru"]:
        conf = provider_conf(name)
        if not conf.get("api_key"):
            out[name] = {"ok": False, "reason": "нет ключа", "models": 0}
            continue
        models = list_models(name, force=True)
        out[name] = {"ok": bool(models), "models": len(models),
                     "reason": "" if models else "нет ответа от API",
                     "label": provider_display(name)}
    return out
