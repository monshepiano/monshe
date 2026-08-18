"""Оркестратор: незаметно выбирает самую дешёвую подходящую модель.

Логика: быстрая эвристика по тексту запроса → уровень (nano/base/smart/coder/vision).
Если эвристика не уверена — спрашиваем самую дешёвую модель-классификатор.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .config import CONFIG

TIER_ORDER = ["nano", "base", "smart", "coder", "vision"]

# ЗАКРЫТЫЙ список реплик, которым инструменты не нужны в принципе: это
# чистая вежливость. Он безопасен именно потому, что закрытый и полный —
# в отличие от попытки перечислить все темы, требующие интернета (новости,
# погода, курсы, спорт, цены...). Тот список нельзя закончить, этот — можно.
_SOCIAL_ONLY = re.compile(
    r"^\s*(привет|здравствуй(те)?|хай|йо|добрый (день|вечер|утро)|доброе утро|"
    r"как дела|как ты|спасибо|благодарю|спс|пока|до свидания|ок(ей)?|хорошо|"
    r"да|нет|ага|угу|понял|понятно|ясно|круто|отлично|супер|давай)"
    r"[\s!.,)?]*$", re.I)


def is_social_only(text: str) -> bool:
    """Реплика, для которой инструменты заведомо бесполезны."""
    return bool(_SOCIAL_ONLY.match((text or "").strip()))


def tier_can(tier: str, cap: str) -> bool:
    """Умеет ли уровень то, что от него требуется (инструменты, зрение)."""
    caps = CONFIG.get("model_caps." + tier, None)
    if not isinstance(caps, dict):
        return True          # про модель ничего не знаем — не мешаем
    return bool(caps.get(cap, True))


def cheapest_tier_with(cap: str, fallback: str = "base") -> str:
    """Самый дешёвый уровень, который умеет нужное. Порядок = цена."""
    for tier in TIER_ORDER:
        if tier_can(tier, cap):
            return tier
    return fallback


def _score_complexity(text: str) -> float:
    """Насколько запрос объёмный. Только измеримое — без угадывания темы.

    ПОЧЕМУ так. Раньше здесь лежали четыре списка слов («проанализируй»,
    «почему», «функци», «найди»...), и каждое совпадение толкало запрос
    вверх по уровням. Слово «почему» есть в любом бытовом вопросе, «план» —
    в «какие планы на вечер». Так простая реплика уезжала в smart-модель,
    которая дороже базовой в 35 раз (549₽ против 15.86₽ за млн токенов) и
    заметно медленнее — отсюда «думал долго и дорого».

    Списки слов невозможно закончить: язык больше любого перечисления.
    Поэтому тему мы больше не угадываем. Считаем то, что действительно
    измеримо, — размер задачи. Что именно нужно сделать, решит сама модель,
    у неё для этого есть инструменты.
    """
    t = (text or "").strip()
    words = len(t.split())
    # 40 слов ≈ 0.16, 120 слов ≈ 0.48, 250+ ≈ насыщение
    score = min(words / 250.0, 1.0)
    if t.count("\n") > 3:
        score += 0.1
    return max(0.0, min(score, 1.5))


def _choose_tier_raw(text: str, has_image: bool = False, agent_mode: bool = False,
                     has_tools: bool = False, computer_use: bool = False) -> Dict[str, Any]:
    """Предпочтительный уровень по «сложности» текста (без учёта способностей)."""
    forced = CONFIG.get("orchestrator.force_tier") or ""
    if forced:
        return {"tier": forced, "reason": "принудительно в настройках", "score": 1.0}
    if not CONFIG.get("orchestrator.auto_route", True):
        return {"tier": "base", "reason": "авто-маршрутизация выключена", "score": 0.5}
    if computer_use:
        # НЕ переключаемся на vision-модель: она не умеет вызывать инструменты,
        # и агент превращается в болтуна. Экран ей покажет отдельный вызов
        # (см. agent._describe_screen), а рулит процессом tool-capable модель.
        return {"tier": "smart", "reason": "управление компьютером — нужен точный вызов действий",
                "score": 1.0}
    if has_image:
        return {"tier": "vision", "reason": "во вложении изображение — нужна vision-модель", "score": 1.0}

    score = _score_complexity(text)

    # Вежливость («привет», «спасибо») — единственный случай, который можно
    # перечислить полностью, и он уходит в самую дешёвую модель.
    if is_social_only(text) and not agent_mode:
        return {"tier": "nano", "reason": "короткая реплика — экономим", "score": score}

    # Всё остальное — рабочая модель base. Она умеет вызывать инструменты,
    # то есть сама сходит в интернет, в песочницу и к файлам, если надо.
    # Дорогая smart остаётся резервом: её берёт escalate(), когда base
    # реально не справилась. Платить за неё авансом «на всякий случай»
    # незачем — именно это делало ответы долгими и дорогими.
    # ЗАЧЕМ УБРАНА ВЕТКА «score > 1.0 -> smart».
    # Длина запроса — это не сложность. Вставленный кусок текста, лог ошибки
    # или письмо на 300 слов набирали score выше единицы и уезжали в GLM-4.7 с
    # reasoning_effort=high: она думает заметно дольше базовой и стоит в 35 раз
    # дороже. Со стороны это выглядело так, что Джарвис без всякой причины
    # «залипает» именно на длинных сообщениях, хотя от него часто ждали
    # одну строчку в ответ. Скорость ответа не должна зависеть от того,
    # сколько текста человек вставил.
    # Умную модель по-прежнему можно получить — принудительно в настройках
    # или через escalate(), когда базовая реально не справилась.
    return {"tier": "base", "reason": "рабочая модель", "score": score}


def choose_tier(text: str, has_image: bool = False, agent_mode: bool = False,
                has_tools: bool = False, computer_use: bool = False) -> Dict[str, Any]:
    """Возвращает {tier, reason, score} с ГАРАНТИЕЙ, что модель потянет задачу.

    Раньше «сложность» текста была единственным критерием, и короткий вопрос
    вроде «кто выиграл вчера матч» уезжал в самую дешёвую модель, которая не
    умеет вызывать инструменты. Она отвечала «у меня нет доступа к интернету».
    Теперь способности модели — жёсткое ограничение, а не пожелание: экономия
    возможна только среди тех уровней, которые реально умеют требуемое.
    """
    route = _choose_tier_raw(text, has_image=has_image, agent_mode=agent_mode,
                             has_tools=has_tools, computer_use=computer_use)
    tier = route["tier"]

    # ЗРЕНИЕ ВАЖНЕЕ ИНСТРУМЕНТОВ. Раньше проверка «умеет ли модель вызывать
    # инструменты» стояла первой, и запрос с картинкой уходил на vision-уровень,
    # а следом сбивался обратно на текстовый (vision-модель не умеет tools).
    # Картинка при этом оставалась в сообщении, но смотреть её было некому —
    # модель отвечала «пришлите изображение». Если во вложении картинка,
    # уровень определяет именно зрение, а инструменты просто не предлагаем.
    if has_image:
        if not tier_can(tier, "vision"):
            tier = cheapest_tier_with("vision", "vision")
        return {"tier": tier, "score": route.get("score", 1.0),
                "offer_tools": tier_can(tier, "tools"),
                "reason": route.get("reason") or "во вложении изображение — нужна vision-модель"}

    # Инструменты подключены — значит модель обязана уметь их вызывать.
    # Мы не знаем заранее, понадобится ли поиск: это решает сама модель уже
    # в процессе. Поэтому «умеет вызывать» требуется всегда, когда есть tools.
    if has_tools and not tier_can(tier, "tools"):
        if is_social_only(text):
            # вежливость: дешёвая модель справится, инструменты ей не даём —
            # тогда и «не умею вызывать» никак не проявится
            route["offer_tools"] = False
            return route
        better = cheapest_tier_with("tools")
        return {"tier": better, "score": route.get("score", 0.0), "offer_tools": True,
                "reason": "нужна модель, умеющая искать и вызывать инструменты"}
    route.setdefault("offer_tools", True)
    return route


def escalate(tier: str) -> Optional[str]:
    """Повышение уровня при неудаче/плохом ответе."""
    ladder = {"nano": "base", "base": "smart", "coder": "smart", "vision": "smart", "smart": None}
    return ladder.get(tier)


# Готовые выжимки истории: ключ — граница сжатия, значение — текст выжимки.
# Живёт в памяти процесса; потерять её не страшно, в худшем случае следующая
# длинная реплика посчитает выжимку заново — уже в фоне.
_SUM_CACHE: Dict[str, str] = {}
_SUM_BUSY: Dict[str, bool] = {}


def _sum_key(head: List[Dict[str, Any]]) -> str:
    """Отпечаток сжимаемой части. Пока она не изменилась, выжимка годна."""
    import hashlib
    raw = "\n".join("%s:%s" % (m.get("role"), str(m.get("content"))[:200]) for m in head)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _make_summary(head: List[Dict[str, Any]], key: str) -> str:
    from . import llm  # локальный импорт, чтобы избежать циклов
    text = "\n".join("%s: %s" % (m.get("role"), str(m.get("content"))[:600]) for m in head)
    try:
        summary = llm.chat(
            [
                {"role": "system", "content": "Сожми диалог в 10 фактов-тезисов на русском. Только факты, кратко."},
                {"role": "user", "content": text[:12000]},
            ],
            tier="nano", max_tokens=500, temperature=0.2,
        ).get("content", "")
    except Exception:
        summary = ""
    if summary:
        _SUM_CACHE[key] = summary
    _SUM_BUSY.pop(key, None)
    return summary


def summarize_history(messages: List[Dict[str, Any]], keep_last: int = 12) -> List[Dict[str, Any]]:
    """Экономия токенов без платы временем ответа.

    ПОЧЕМУ ЗДЕСЬ НЕТ ОЖИДАНИЯ МОДЕЛИ. Раньше сжатие было обычным вызовом
    llm.chat прямо в этой функции, а зовут её ПЕРЕД первым словом ответа.
    Пока диалог короткий (до 16 сообщений), вызова нет и Джарвис отвечает
    сразу. Как только диалог перевалил порог, к каждому ответу молча
    добавлялся целый лишний поход в облако — и ответ, ничем не отличавшийся
    от предыдущего, вдруг начинал ждать. Это и есть «то очень быстро, то
    очень медленно»: скорость зависела не от вопроса, а от длины переписки,
    причём в момент, когда пользователь уже смотрит на пустой экран.

    Ждать ради экономии токенов нельзя: ответ важнее. Поэтому выжимку мы
    БЕРЁМ готовую, если она есть, а если её нет — отдаём хвост немедленно и
    считаем выжимку в фоне, чтобы она была готова к следующей реплике.
    """
    if len(messages) <= keep_last + 4:
        return messages

    head = messages[:-keep_last]
    tail = messages[-keep_last:]
    key = _sum_key(head)

    summary = _SUM_CACHE.get(key)
    if summary:
        return [{"role": "system", "content": "Краткая память о предыдущей части диалога:\n" + summary}] + tail

    # Выжимки ещё нет. Не задерживаем ответ ни на секунду: считаем её в фоне.
    if not _SUM_BUSY.get(key):
        _SUM_BUSY[key] = True
        import threading
        threading.Thread(target=_make_summary, args=(head, key),
                         name="jarvis-summary", daemon=True).start()
    return tail


# ------------------------------------------------------------- имя диалога
_TITLE_STOP = re.compile(r"^[\s\"'«»`*#>\-–—.:]+|[\s\"'«»`*#>\-–—.:]+$")

# такие «названия» бессмысленны — их не принимаем ни от модели, ни как фолбэк
_BAD_TITLES = {
    "новый диалог", "новый чат", "диалог", "чат", "беседа", "разговор", "без названия",
    "название", "заголовок", "тема", "новая тема", "запрос", "вопрос", "сообщение",
    "new chat", "new dialog", "untitled", "conversation", "chat", "title",
    "привет", "здравствуйте", "ответ", "текст", "задача", "разное", "общение",
}
_STOPWORDS = {
    "а", "бы", "в", "во", "все", "вот", "да", "для", "до", "его", "ее", "её", "если",
    "есть", "ещё", "еще", "же", "за", "и", "из", "или", "как", "мне", "мной", "мы",
    "на", "над", "не", "нет", "но", "о", "об", "она", "они", "от", "по", "под",
    "пожалуйста", "при", "про", "с", "со", "так", "также", "те", "то", "ты", "у",
    "уже", "что", "чтобы", "это", "я", "мой", "моя", "меня", "нам", "вы", "ваш",
    "такое", "такой", "такая", "мне", "нужно", "надо", "хочу", "давай", "можешь",
}
_VERB_HINTS = ("напиши", "сделай", "создай", "найди", "посчитай", "собери", "объясни",
               "переведи", "проверь", "покажи", "расскажи", "составь", "подбери",
               "сравни", "проанализируй", "помоги", "придумай", "оформи", "скачай")


def _is_bad_title(title: str) -> bool:
    t = re.sub(r"[^\wа-яё ]+", "", (title or "").lower()).strip()
    if not t or len(t) < 3:
        return True
    if t in _BAD_TITLES:
        return True
    # «Новый диалог 2», «Чат №3»
    if re.fullmatch(r"(новый диалог|новый чат|чат|диалог|беседа)\s*[№#]?\s*\d*", t):
        return True
    return False


def _keyword_title(text: str) -> str:
    """Осмысленный заголовок из ключевых слов, если модель не помогла."""
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return ""
    greet = ("джарвис", "jarvis", "привет", "здравствуй", "здравствуйте", "слушай",
             "эй", "пожалуйста", "окей", "ок", "плиз", "будь", "добр", "доброе", "утро",
             "добрый", "день", "вечер")
    # первая фраза; если она — только приветствие, берём следующую
    sentences = [s.strip() for s in re.split(r"[.!?\n]", clean) if s.strip()]
    first = clean
    for sent in sentences:
        rest = [w for w in re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", sent)
                if w.lower().strip(",.!") not in greet]
        if rest:
            first = sent
            break
    words = re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", first)
    while words and words[0].lower().strip(",.!") in greet:
        words.pop(0)
    if not words:
        words = re.findall(r"[\wА-Яа-яЁё\-\+#\.]+", first)
    keep = [w for w in words if w.lower() not in _STOPWORDS]
    if not keep:
        keep = words
    picked = keep[:6]
    title = " ".join(picked)
    if len(title) > 38:
        out: List[str] = []
        for w in picked:
            if len(" ".join(out + [w])) > 38:
                break
            out.append(w)
        title = " ".join(out) or title[:38]
    title = title.strip(" ,;:—-")
    if not title:
        return ""
    return title[0].upper() + title[1:]


def _fallback_title(text: str) -> str:
    """Если модель недоступна — собираем заголовок из ключевых слов."""
    title = _keyword_title(text)
    if title and not _is_bad_title(title):
        return title[:40]
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if not clean:
        return "Диалог " + __import__("time").strftime("%d.%m %H:%M")
    return clean[:38].rstrip(" ,;:-") or "Диалог"


def _clean_model_title(raw: str) -> str:
    title = (raw or "").split("\n")[0]
    title = re.sub(r"^\s*(название|заголовок|title)\s*[:\-—]\s*", "", title, flags=re.I)
    title = _TITLE_STOP.sub("", title).strip()
    title = re.sub(r"\s+", " ", title)
    return title


def _make_title(text: str, kind: str = "chat") -> str:
    """Название придумывает сама модель — коротко и по смыслу."""
    from . import llm  # локальный импорт, чтобы избежать циклов

    snippet = re.sub(r"\s+", " ", (text or "").strip())[:900]
    if not snippet:
        return _fallback_title(text)

    what = ("названия диалогов" if kind == "chat" else "названия фоновых задач")
    system = (
        "Ты придумываешь %s. По сообщению пользователя дай короткое название на русском.\n"
        "ПРАВИЛА:\n"
        "1. 2-5 слов, до 34 символов, по сути темы — чтобы через неделю было понятно, о чём речь.\n"
        "2. Без кавычек, без точки в конце, без эмодзи, без пояснений.\n"
        "3. ЗАПРЕЩЕНО отвечать общими словами: «Новый диалог», «Чат», «Диалог», «Беседа», "
        "«Вопрос», «Запрос», «Без названия», «Разное», «Общение», «Тема».\n"
        "4. Если тема непонятна — назови её по ключевым словам сообщения.\n"
        "5. Ответь ТОЛЬКО названием, одной строкой." % what
    )
    for attempt in range(2):
        try:
            raw = llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": snippet},
                ],
                tier="nano", max_tokens=24, temperature=0.2 if attempt == 0 else 0.7,
            ).get("content", "")
        except Exception:
            break
        title = _clean_model_title(raw)
        if title and len(title) <= 48 and not _is_bad_title(title):
            return title[:40]
    return _fallback_title(text)


def make_chat_title(text: str) -> str:
    return _make_title(text, "chat")


def make_task_title(text: str) -> str:
    title = _make_title(text, "task")
    return title or (text[:48] or "Фоновая задача")
