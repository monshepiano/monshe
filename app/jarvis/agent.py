"""Агентское ядро JARVIS: цикл «мысль → инструмент → наблюдение → ответ».

Генерирует поток событий для UI:
  status, thinking, model, delta, tool_start, tool_result, approval, plan, step, file, done, error
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Generator, Iterable, List, Optional

from . import db, llm, orchestrator, sandbox, tools
from .config import CONFIG

MAX_STEPS_CHAT = 6
MAX_STEPS_AGENT = 18


def _now_str() -> str:
    return time.strftime("%d.%m.%Y %H:%M")


def build_system_prompt(agent_mode: bool = False, computer_use: bool = False) -> str:
    user = CONFIG.get("user", {}) or {}
    memories = db.recall(limit=40)
    mem_lines = "\n".join("- [%s] %s: %s" % (m["kind"], m["key"], m["value"]) for m in memories[:30])
    who = []
    if user.get("name"):
        who.append("Имя пользователя: %s." % user["name"])
    if user.get("city"):
        who.append("Город: %s." % user["city"])
    if user.get("about"):
        who.append("О пользователе: %s" % user["about"])

    base = f"""Ты — JARVIS, личный ИИ-агент пользователя (как у Тони Старка).
Сегодня {_now_str()}. Отвечай по-русски, кратко, по делу, с лёгкой ноткой уверенного дворецкого-инженера.
Обращайся к пользователю на «вы» только если он сам так пишет; по умолчанию — дружелюбно на «ты».

{' '.join(who)}

ТЫ УМЕЕШЬ ДЕЙСТВОВАТЬ, а не только говорить. У тебя есть инструменты:
• интернет: web_search, open_url, deep_research, download_file, http_request;
• песочница на сервере: write_file, read_file, list_files, run_python, run_shell, make_archive (файлы можно прислать пользователю);
• управление песочницей: sandbox_info (что внутри), delete_file (убрать лишнее), sandbox_clear (стереть всё), sandbox_rename (дать имя);
• медиа: generate_image, analyze_image, analyze_video, transcribe_audio;
• память: remember (сохраняй важные факты о пользователе САМ, без напоминаний), recall;
• фон: schedule_task — если задача долгая, регулярная или пользователь не должен ждать, отправь её в AUTO;
• компьютер пользователя: screenshot, screen_info, mouse_click, mouse_move, mouse_scroll, mouse_drag, type_text, press_key, open_app.

ПРАВИЛА:
1. Нужны свежие данные, цены, новости, факты после твоего обучения — обязательно вызывай web_search/open_url. Не выдумывай.
   У ТЕБЯ ЕСТЬ ДОСТУП В ИНТЕРНЕТ. Запрещено отвечать «у меня нет доступа к интернету»,
   «я не могу получить новости» или «инструменты не настроены» — это неправда.
   Спросили новости/погоду/курс/цену — молча вызови web_search и дай результат.
   Не переспрашивай «какая тема вас интересует», если просьба понятна: сначала найди, потом уточняй.
2. Не спрашивай разрешения на безопасные шаги — просто делай. Опасные действия система сама поставит на подтверждение.
3. Если пользователь просит файл (отчёт, таблицу, код, презентацию) — создай его в песочнице и укажи, что он готов к скачиванию.
4. Ссылайся на источники ссылками, когда искал в интернете.
5. Форматируй ответ markdown: заголовки, списки, **жирный**, таблицы, ```блоки кода```.
6. Замечаешь личные факты (предпочтения, планы, имена) — вызывай remember.
7. Не выдумывай результаты инструментов: если инструмент вернул ошибку — честно скажи и предложи обход.
8. Песочница у каждого диалога своя. Просят «удали файл», «почисти песочницу», «сотри всё» — делай это инструментами delete_file / sandbox_clear, а не отговорками. Просят «назови песочницу» — sandbox_rename.

КАК ВЫЗЫВАТЬ ИНСТРУМЕНТЫ (это критично):
Инструмент вызывается ТОЛЬКО штатным механизмом function calling твоего API.
НИКОГДА не печатай вызов текстом в ответ пользователю. Запрещены строки вида
«function call open_url("…")», «schedule_task(…)», «tool_call», а также
JSON-описание вызова прямо в тексте. Любые их варианты запрещены.
Если хочешь применить инструмент — примени его, а не описывай.
После того как инструмент вернул результат, напиши пользователю нормальный
человеческий ответ по этому результату. Пустой ответ недопустим: если инструмент
не сработал, скажи об этом словами.

КОГДА ОТПРАВЛЯТЬ ЗАДАЧУ В ФОН:
Просьбы «напомни», «напиши мне через N минут», «проверяй каждый день», «следи за…»,
«пришли утром» — это schedule_task. Вызови его сразу, одним вызовом, с полями
title (коротко о чём), prompt (что именно сделать, когда придёт время)
и schedule в одном из форматов: «in 10s», «in 5m», «in 2h», «every 30m»,
«every 1h», «every 2d», «daily 09:00». Ничего не переспрашивай — просто поставь
задачу и подтверди человеку одной фразой, когда она сработает.
"""
    if computer_use:
        base += """
РЕЖИМ УПРАВЛЕНИЯ КОМПЬЮТЕРОМ — ЖЕЛЕЗНОЕ ПРАВИЛО:
Курсор и клавиатура двигаются ТОЛЬКО вызовом инструментов. Текст ответа ничего не делает.

ЗАПРЕЩЕНО писать «сейчас перемещу курсор», «нажал», «открыл», «кликнул», если ты
не вызвал соответствующий инструмент и не увидел его результат. Это ложь, а не работа.
Никогда не описывай содержимое экрана по памяти или догадке — только по свежему скриншоту.
Ты не видишь картинки сам: единственный источник правды об экране — поле screen
в результате screenshot. Нет screenshot — нет знания об экране.

ПОРЯДОК ДЕЙСТВИЙ:
1. screen_info — узнать размер экрана.
2. screenshot — вернёт поле screen: словесную карту экрана с координатами
   элементов. Это твои глаза, читай её внимательно.
3. Взять из карты координаты нужного элемента. Если сказано, что снимок
   уменьшен, — пересчитать координаты, как там указано.
4. Вызвать mouse_move / mouse_click / type_text / press_key / open_app.
5. Снова screenshot — убедиться, что получилось. Не получилось — поправить и повторить.

Каждый шаг — отдельный вызов инструмента. Между шагами коротко говори, что видишь.
Итог сообщай только после того, как последний скриншот подтвердил результат.
Если инструмент вернул ошибку (нет прав, не macOS) — честно скажи об этом
и объясни, что включить в Системных настройках, вместо выдуманного успеха.
"""
    if agent_mode:
        base += """
АГЕНТСКИЙ РЕЖИМ:
Пользователь не участвует в процессе. Сначала составь план из 3-7 шагов (коротко, списком),
затем выполняй шаги инструментами один за другим, не останавливаясь на уточняющие вопросы.
Если данных не хватает — прими разумное допущение и укажи его в финале.
Заверши развёрнутым итогом: что сделано, что найдено, какие файлы созданы.
"""
    if mem_lines:
        base += "\nЧТО ТЫ ЗНАЕШЬ О ПОЛЬЗОВАТЕЛЕ:\n" + mem_lines + "\n"
    return base


def _tool_groups(computer_use: bool) -> List[str]:
    groups = ["web", "sandbox", "media", "memory", "auto", "base"]
    if computer_use and CONFIG.get("computer_use.enabled", True):
        groups.append("computer")
    return groups


def needs_approval(tool_name: str) -> Optional[str]:
    """Возвращает причину, если нужно подтверждение пользователя."""
    risk = tools.risk_of(tool_name)
    safety = CONFIG.get("safety", {}) or {}
    if risk == "safe":
        return None
    mapping = {
        "delete_file": ("confirm_delete", "удаление данных"),
        "run_shell": ("confirm_shell", "выполнение команды в терминале"),
        "send_telegram": ("confirm_send_message", "отправка сообщения от твоего имени"),
        "telegram_send_file": ("confirm_send_message", "отправка файла в мессенджер"),
        "mouse_click": ("confirm_computer_use", "управление мышью на твоём компьютере"),
        "mouse_move": ("confirm_computer_use", "управление курсором"),
        "mouse_scroll": ("confirm_computer_use", "прокрутка на твоём компьютере"),
        "mouse_drag": ("confirm_computer_use", "перетаскивание мышью"),
        "type_text": ("confirm_computer_use", "ввод текста на твоём компьютере"),
        "press_key": ("confirm_computer_use", "нажатие клавиш на твоём компьютере"),
        "open_app": ("confirm_computer_use", "запуск приложения на твоём компьютере"),
    }
    # screenshot и screen_info — это «глаза» агента, а не действие: они ничего
    # не меняют. Спрашивать санкцию на каждый кадр — значит сделать
    # computer-use неработоспособным, поэтому они проходят молча.
    flag, reason = mapping.get(tool_name, ("", "потенциально опасное действие"))
    if flag and not safety.get(flag, True):
        return None
    if risk == "caution" and safety.get("auto_approve_readonly", True) and tool_name not in mapping:
        return None
    if risk == "danger" or flag:
        return reason
    return None


def detect_payment_intent(args: Dict[str, Any]) -> bool:
    blob = json.dumps(args, ensure_ascii=False).lower()
    return any(word in blob for word in ("оплат", "купить", "payment", "checkout", "оформить заказ", "картой"))


def _describe_screen(data_url: str, shot: Dict[str, Any]) -> str:
    """Показать кадр зрительной модели и получить словесную карту экрана.

    Управляющая модель инструментов «слепая»: сама картинку она не увидит.
    Поэтому глаза и руки разделены — vision-модель описывает, что где лежит,
    в пикселях, а решение о клике принимает основная модель.
    """
    w = shot.get("width") or 0
    h = shot.get("height") or 0
    prompt = (
        "Снимок экрана, %sx%s px. Ответь ТЕЛЕГРАФНО, без вступлений.\n"
        "Строка 1: активное окно.\n"
        "Далее — только строки вида «Подпись — (x, y)» для кликабельных "
        "элементов и пунктов списков. Максимум 25 строк, самое важное.\n"
        "Координаты — центр элемента в пикселях снимка от левого верхнего угла. "
        "Не выдумывай то, чего не видишь."
    ) % (w or "?", h or "?")
    try:
        seen = llm.vision(prompt, data_url)
    except Exception as exc:
        return "не удалось рассмотреть экран: %s" % exc
    if not (seen or "").strip():
        return "экран получен, но зрительная модель ничего не описала"
    scale = shot.get("scale")
    tail = ""
    if scale and scale != 1:
        tail = ("\nВНИМАНИЕ: снимок уменьшен. Координаты выше даны в пикселях снимка; "
                "перед кликом умножь их на %.4g, чтобы получить координаты реального экрана."
                % (1.0 / float(scale)))
    return seen.strip() + tail


COMPUTER_TOOLS = {
    "mouse_click", "mouse_move", "mouse_scroll", "mouse_drag",
    "type_text", "press_key", "open_app", "screenshot",
}

# «сейчас нажму», «кликнул», «открыл окно» — заявка на действие
_ACTION_CLAIM = re.compile(
    r"(перемещ|навед|нажал|нажим|кликн|щёлкн|щелкн|открыл|открыва|печата|ввёл|ввел|"
    r"переключ|прокрут|скролл|курсор)", re.I)


def _claims_action(text: str) -> bool:
    return bool(_ACTION_CLAIM.search(text or ""))


class Agent:
    """Один прогон агента (чат-ответ или фоновая задача)."""

    def __init__(self, chat_id: str = "", task_id: str = "", agent_mode: bool = False,
                 computer_use: bool = False, approvals_auto: bool = False) -> None:
        self.chat_id = chat_id
        self.task_id = task_id
        self.agent_mode = agent_mode
        self.computer_use = computer_use
        self.approvals_auto = approvals_auto
        self.sandbox_id = chat_id or ""
        self.created_files: List[Dict[str, Any]] = []
        self.used_tools: List[str] = []
        self.model_used = ""

    # ------------------------------------------------------------ approvals
    def _wait_approval(self, tool_name: str, args: Dict[str, Any], reason: str,
                       timeout: int = 300) -> Dict[str, Any]:
        risk = "critical" if detect_payment_intent(args) or tool_name == "delete_file" else "high"
        approval = db.create_approval(tool_name, args, risk, reason, self.chat_id, self.task_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            fresh = db.get_approval(approval["id"])
            if fresh and fresh.get("status") in ("approved", "rejected"):
                return fresh
            time.sleep(0.6)
        db.decide_approval(approval["id"], "expired")
        return {**approval, "status": "expired"}

    # ---------------------------------------------------- результат вызова
    @staticmethod
    def _append_tool_result(convo: List[Dict[str, Any]], call: Dict[str, Any], name: str,
                            result: Any, from_text: bool) -> None:
        # Скриншот — картинка, а модель, которая умеет вызывать инструменты,
        # обычно не умеет смотреть. Поэтому кадр сначала «переводит в слова»
        # зрительная модель, и управляющая модель получает готовые координаты.
        if name == "screenshot" and isinstance(result, dict) and result.get("ok"):
            data_url = result.pop("data_url", "")
            if data_url:
                result["screen"] = _describe_screen(data_url, result)

        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) > 14000:
            payload = payload[:14000] + "…(обрезано)"
        if from_text:
            convo.append({
                "role": "user",
                "content": ("[Система] Результат инструмента %s:\n%s\n\n"
                            "Продолжай. Никогда не печатай вызовы инструментов текстом — "
                            "используй только штатный механизм вызова функций."
                            % (name, payload)),
            })
        else:
            convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                          "name": name, "content": payload})


    # ------------------------------------------------------------- planning
    def make_plan(self, task: str) -> List[str]:
        try:
            result = llm.chat([
                {"role": "system", "content":
                 "Ты планировщик. Разбей задачу на 3-6 конкретных исполнимых шагов. "
                 "Ответь ТОЛЬКО JSON-массивом строк на русском, без пояснений."},
                {"role": "user", "content": task},
            ], tier="base", max_tokens=600, temperature=0.3)
            text = result.get("content", "")
            match = re.search(r"\[.*\]", text, re.S)
            if match:
                steps = json.loads(match.group(0))
                return [str(s)[:200] for s in steps][:8]
        except Exception:
            pass
        return []

    # ------------------------------------------------------------------ run
    def run(self, messages: List[Dict[str, Any]], user_text: str = "",
            has_image: bool = False) -> Generator[Dict[str, Any], None, None]:
        # каждый диалог работает в своей песочнице
        sandbox.set_chat(self.sandbox_id)
        route = orchestrator.choose_tier(
            user_text, has_image=has_image, agent_mode=self.agent_mode, has_tools=True,
            computer_use=self.computer_use)
        tier = route["tier"]
        # «Сложность» решает, показывать ли пользователю кухню (ход мыслей,
        # терминал, план). На простой вопрос он ждёт ответ, а не отчёт.
        score = float(route.get("score") or 0)
        verbose = bool(self.agent_mode or self.computer_use or score >= 0.30)
        yield {"type": "route", "tier": tier, "reason": route["reason"],
               "score": score, "verbose": verbose}

        available = tools.schemas(_tool_groups(self.computer_use))
        if self.task_id:
            # мы УЖЕ внутри фоновой задачи: планировать ещё одну запрещено,
            # иначе AUTO наполняется клонами одной и той же просьбы
            available = [t for t in available
                         if (t.get("function") or {}).get("name") != "schedule_task"]
        max_steps = MAX_STEPS_AGENT if self.agent_mode else MAX_STEPS_CHAT

        if self.agent_mode and user_text:
            yield {"type": "status", "text": "Составляю план"}
            plan = self.make_plan(user_text)
            if plan:
                yield {"type": "plan", "steps": plan}
                messages = messages + [{
                    "role": "system",
                    "content": "План выполнения (следуй ему):\n" + "\n".join(
                        "%d. %s" % (i + 1, s) for i, s in enumerate(plan)),
                }]

        convo = list(messages)
        final_text = ""

        for step in range(max_steps):
            yield {"type": "status", "text": "Думаю" if step == 0 else "Работаю над шагом %d" % (step + 1)}
            acc_text: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            stream_failed = None
            # «шлюз»: пока начало ответа похоже на текстовый вызов инструмента,
            # ничего не показываем пользователю — иначе в чат попадёт мусор
            # вида function schedule_task({...}).
            gate_open = False

            for event in llm.chat_stream(convo, tier=tier, tools=available):
                etype = event.get("type")
                if etype == "model":
                    self.model_used = event.get("model", "")
                    yield {"type": "model", "model": event.get("model"), "tier": tier}
                elif etype == "reasoning":
                    yield {"type": "thinking", "text": event["text"]}
                elif etype == "delta":
                    acc_text.append(event["text"])
                    if gate_open:
                        yield {"type": "delta", "text": event["text"]}
                    else:
                        joined = "".join(acc_text)
                        if not tools.looks_like_call_prefix(joined):
                            gate_open = True
                            yield {"type": "delta", "text": joined}
                elif etype == "tool_partial":
                    yield {"type": "tool_hint", "name": event.get("name", "")}
                elif etype == "done":
                    tool_calls = event.get("tool_calls") or []
                    if event.get("reasoning") and not acc_text:
                        pass
                elif etype == "error":
                    stream_failed = event.get("error")

            if stream_failed and not acc_text and not tool_calls:
                higher = orchestrator.escalate(tier)
                if higher:
                    tier = higher
                    yield {"type": "status", "text": "Переключаюсь на резервную модель"}
                    continue
                yield {"type": "error", "error": stream_failed}
                return

            text_piece = "".join(acc_text)
            from_text = False

            # модель напечатала вызов инструмента текстом — распознаём и выполняем
            if text_piece.strip():
                cleaned, text_calls = tools.parse_text_calls(text_piece)
                if text_calls:
                    from_text = True
                    if gate_open:
                        # уже что-то показали — стираем и перерисовываем
                        yield {"type": "reset"}
                        gate_open = False
                    text_piece = cleaned
                    if cleaned:
                        gate_open = True
                        yield {"type": "delta", "text": cleaned}
                    for i, tc in enumerate(text_calls):
                        tool_calls.append({
                            "id": "txt_%d_%d" % (step, i),
                            "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["args"], ensure_ascii=False)},
                        })

            # шлюз так и не открылся, а вызовов нет — показываем придержанный текст
            if not gate_open and text_piece and not tool_calls:
                yield {"type": "delta", "text": text_piece}
                gate_open = True

            if not tool_calls:
                final_text = text_piece
                break

            # модель решила вызвать инструменты
            if from_text:
                # вызов был напечатан текстом: у модели нет полноценного tool-протокола,
                # поэтому результаты вернём обычным системным сообщением
                convo.append({"role": "assistant",
                              "content": text_piece or "Вызываю инструменты."})
            else:
                convo.append({
                    "role": "assistant",
                    "content": text_piece or None,
                    "tool_calls": tool_calls,
                })
            if text_piece.strip():
                final_text = text_piece

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                self.used_tools.append(name)
                yield {"type": "tool_start", "id": call.get("id"), "name": name,
                       "label": tools.label_of(name), "args": args,
                       "risk": tools.risk_of(name)}

                reason = needs_approval(name)
                if reason and not self.approvals_auto:
                    yield {"type": "status", "text": "Жду твоего подтверждения"}
                    yield {"type": "approval_wait", "tool": name, "label": tools.label_of(name),
                           "args": args, "reason": reason}
                    decision = self._wait_approval(name, args, reason)
                    yield {"type": "approval_done", "status": decision.get("status")}
                    if decision.get("status") != "approved":
                        result: Dict[str, Any] = {
                            "ok": False,
                            "error": "Пользователь отклонил действие" if decision.get("status") == "rejected"
                            else "Время ожидания подтверждения истекло",
                        }
                        self._append_tool_result(convo, call, name, result, from_text)
                        yield {"type": "tool_result", "id": call.get("id"), "name": name, "result": result}
                        continue

                started = time.time()
                result = tools.call(name, args)
                elapsed = round(time.time() - started, 2)

                if isinstance(result, dict) and result.get("download_url"):
                    file_info = {"name": result.get("path") or result.get("name"),
                                 "url": result["download_url"],
                                 "size": result.get("size", 0),
                                 "kind": "image" if str(result.get("path", "")).lower().endswith(
                                     (".png", ".jpg", ".jpeg", ".gif", ".webp")) else "file"}
                    self.created_files.append(file_info)
                    yield {"type": "file", **file_info}

                # задача ушла в AUTO — показываем это карточкой, а не сухим результатом
                if name == "schedule_task" and isinstance(result, dict) and result.get("ok"):
                    yield {"type": "background", "task_id": result.get("task_id", ""),
                           "title": result.get("title", ""),
                           "schedule": result.get("schedule", ""),
                           "when": result.get("when", ""),
                           "reason": "я решил выполнить это в фоне"}

                yield {"type": "tool_result", "id": call.get("id"), "name": name,
                       "result": result, "elapsed": elapsed}

                self._append_tool_result(convo, call, name, result, from_text)

        if not final_text:
            yield {"type": "status", "text": "Формулирую ответ"}
            try:
                closing = llm.chat(convo + [{
                    "role": "user",
                    "content": "Подведи итог выполненной работы для пользователя: что сделано и результат. "
                               "Кратко, markdown, по-русски. Не печатай вызовы инструментов.",
                }], tier=tier, max_tokens=1400)
                final_text = closing.get("content", "")
            except Exception as exc:
                yield {"type": "error", "error": str(exc)}
                return
            # итог тоже может прийти с напечатанным вызовом — вычищаем
            if final_text:
                final_text = tools.parse_text_calls(final_text)[0]
            if final_text:
                yield {"type": "delta", "text": final_text}

        # последняя страховка: пустой ответ пользователь видит как поломку
        if not final_text.strip():
            final_text = self._fallback_summary()
            yield {"type": "delta", "text": final_text}

        # Режим управления компьютером: модель могла «отчитаться» о кликах, не
        # тронув мышь. Не выдаём выдумку за правду — честно предупреждаем.
        if self.computer_use and not any(t in COMPUTER_TOOLS for t in self.used_tools):
            if _claims_action(final_text):
                final_text += (
                    "\n\n---\n⚠️ **Я на самом деле ничего не нажал.** Управление "
                    "компьютером не сработало: инструменты мыши и клавиатуры не "
                    "выполнились.\n\nНа macOS это почти всегда права доступа. Открой "
                    "**Системные настройки → Конфиденциальность и безопасность** и "
                    "разреши Терминалу (или приложению, из которого запущен JARVIS) "
                    "два пункта: **Универсальный доступ** и **Запись экрана**. "
                    "После этого перезапусти JARVIS и повтори просьбу."
                )
                yield {"type": "delta", "text": final_text[final_text.index("\n\n---\n"):]}

        yield {"type": "done", "content": final_text, "files": self.created_files,
               "tools": self.used_tools, "model": self.model_used, "tier": tier}

    def _fallback_summary(self) -> str:
        """Что показать, если модель не выдала ни слова."""
        parts = []
        if self.used_tools:
            names = ", ".join(dict.fromkeys(self.used_tools))
            parts.append("Готово. Что я сделал: %s." % names)
        if self.created_files:
            files = ", ".join(f.get("name", "") for f in self.created_files if f.get("name"))
            if files:
                parts.append("Файлы: %s — их можно скачать выше." % files)
        if not parts:
            parts.append("Я обработал запрос, но модель вернула пустой ответ. "
                         "Повтори вопрос — попробую другой моделью.")
        return "\n\n".join(parts)


def run_headless(prompt: str, task_id: str = "", agent_mode: bool = True,
                 chat_id: str = "") -> Dict[str, Any]:
    """Запуск без UI (для фоновых задач AUTO). Возвращает итог и лог событий."""
    agent = Agent(chat_id=chat_id, task_id=task_id, agent_mode=agent_mode, approvals_auto=False)
    # Фоновая задача исполняется «сейчас»: время ожидания уже прошло, поэтому
    # никаких «напомню позже» — нужен готовый текст, который увидит пользователь.
    extra = (
        "\n\nСЕЙЧАС ТЫ ВЫПОЛНЯЕШЬ ОТЛОЖЕННУЮ ЗАДАЧУ.\n"
        "Назначенный момент наступил — выполняй прямо сейчас.\n"
        "Не планируй задачу заново и не пиши, что напомнишь позже.\n"
        "Если просили что-то написать или напомнить — просто напиши это "
        "готовым текстом, обращаясь к пользователю.\n"
        "Ответ попадёт в диалог и в уведомление, поэтому он должен быть "
        "самодостаточным и по делу."
    )
    messages = [
        {"role": "system", "content": build_system_prompt(agent_mode=agent_mode) + extra},
        {"role": "user", "content": prompt},
    ]
    events: List[Dict[str, Any]] = []
    final = ""
    files: List[Dict[str, Any]] = []
    for event in agent.run(messages, user_text=prompt):
        etype = event.get("type")
        if etype in ("plan", "tool_start", "tool_result", "status", "file", "error", "model"):
            slim = {k: v for k, v in event.items() if k != "args"}
            if etype == "tool_result":
                res = slim.get("result")
                slim["result"] = (json.dumps(res, ensure_ascii=False)[:600] if isinstance(res, dict) else str(res)[:600])
            events.append(slim)
            if task_id:
                db.append_task_event(task_id, slim)
        if etype == "done":
            final = event.get("content", "")
            files = event.get("files", [])
    return {"content": final, "events": events, "files": files}
