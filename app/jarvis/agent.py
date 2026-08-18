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

# Лимиты шагов лежат в конфиге (agent.max_steps_chat / max_steps_agent).
# Раньше они были константами здесь, а в конфиге болтался неиспользуемый
# computer_use.max_steps — правка настройки не меняла ничего.
def _max_steps(agent_mode: bool) -> int:
    key = "agent.max_steps_agent" if agent_mode else "agent.max_steps_chat"
    try:
        return max(1, int(CONFIG.get(key, 18 if agent_mode else 6)))
    except Exception:
        return 18 if agent_mode else 6


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
Сегодня {_now_str()}. Кратко, по делу, с лёгкой ноткой уверенного дворецкого-инженера.
ЯЗЫК — РУССКИЙ ВЕЗДЕ И ВСЕГДА: ответ, ход мыслей (reasoning), планы, названия шагов,
пояснения к действиям, заголовки и тексты уведомлений. Даже размышляя «про себя»,
думай по-русски. Английский допустим только внутри кода, команд, путей и имён файлов.
Обращайся к пользователю на «вы» только если он сам так пишет; по умолчанию — дружелюбно на «ты».

{' '.join(who)}

ТЫ УМЕЕШЬ ДЕЙСТВОВАТЬ, а не только говорить. У тебя есть инструменты:
• интернет: web_search, open_url, deep_research, download_file, http_request;
• песочница на сервере: write_file, read_file, list_files, run_python, run_shell, make_archive (файлы можно прислать пользователю);
• управление песочницей: sandbox_info (что внутри), delete_file (убрать лишнее), sandbox_clear (стереть всё), sandbox_rename (дать имя);
• медиа: generate_image, analyze_image, analyze_video, transcribe_audio;
• память: remember (сохраняй важные факты о пользователе САМ, без напоминаний), recall, forget;
• диалог: ask_user — задать короткий уточняющий вопрос с кнопками-вариантами;
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
6. Замечаешь личные факты (предпочтения, планы, имена) — вызывай remember. Память можно и ПРАВИТЬ:
   факт устарел (переехал, сменил работу) — вызови remember с тем же key и новым value, старое заменится;
   просят забыть — вызови forget с этим key. Не плоди дубли вроде «Город» и «Город 2».
7. Не выдумывай результаты инструментов: если инструмент вернул ошибку — честно скажи и предложи обход.
8. Развилка, где ты обязан ОСТАНОВИТЬСЯ и без ответа не можешь работать дальше
   (куда сохранить файл, продолжать ли рискованный путь) — вызови ask_user
   с 2-4 вариантами через |. Он ставит работу на паузу, поэтому используй его
   только когда пауза действительно нужна.
   Если же ты просто ПРЕДЛАГАЕШЬ выбор, а ответить можешь и дальше — не зови
   ask_user, а вставь блок ui из правила 10 прямо в текст ответа.
   Когда ответ очевиден из просьбы — не спрашивай, а делай. Максимум один вопрос подряд.
9. Песочница у каждого диалога своя. Просят «удали файл», «почисти песочницу», «сотри всё» — делай это инструментами delete_file / sandbox_clear, а не отговорками. Просят «назови песочницу» — sandbox_rename.
10. ЖИВЫЕ ЭЛЕМЕНТЫ УПРАВЛЕНИЯ. Предлагаешь варианты на выбор или величину,
   которую надо подобрать, — не описывай их словами и не нумеруй списком,
   а дай пользователю настоящие органы управления прямо в ответе.
   Для этого вставь блок кода с языком ui. Точно так, дословно:

   ```ui
   tiles Формат: PDF | Word | Markdown
   slider Громкость 0..100 = 40
   toggle Уведомления = on
   button Поехали
   ```

   Строки (выбирай тот тип, который ТОЧНО отвечает на вопрос):
   tiles Подпись: A | B | C — выбор ОДНОГО варианта;
   multi Подпись: A | B | C — выбор НЕСКОЛЬКИХ сразу;
   rank Подпись: A | B | C — расставить по важности (ответ — порядок);
   slider Подпись мин..макс [step шаг] [unit ед] = начальное — плавная величина;
   number Подпись = начальное — точное число (мин..макс можно не указывать);
   rate Подпись 1..5 = 0 — оценка звёздами «насколько»;
   toggle Подпись = on|off — да/нет;
   text Подпись = подсказка — короткий ввод в одну строку;
   area Подпись = подсказка — длинный ответ в несколько строк;
   date Подпись = 2026-08-18 — дата; color Подпись = #00c8f0 — цвет;
   button Текст — кнопка действия.
   Пользователь покрутит и пришлёт итог одним сообщением, ты продолжишь.
   ЖЕЛЕЗНОЕ правило: если ты в тексте предлагаешь выбрать (скорость, уровень,
   формат, вариант) — блок ui обязан быть в ЭТОМ ЖЕ сообщении. Написать
   «выбери скорость» и не дать органов управления нельзя: выбирать будет нечем.
   Правила: слово ui после кавычек обязательно, по одному элементу на строку,
   максимум 4 элемента. Бери РАЗНЫЕ типы, а не четыре плитки подряд:
   один и тот же вопрос, заданный четырьмя одинаковыми плитками, читается
   как анкета, а разные органы управления — как живой пульт.

   КОГДА ЭТО ОБЯЗАТЕЛЬНО. Проверь себя перед ответом: собираешься ли ты
   сейчас выбрать за пользователя что-то, что он мог бы выбрать сам?
   Любая просьба «сделай/напиши/собери X» почти всегда имеет несколько
   равноправных решений — размер, сложность, стиль, оформление, набор
   возможностей. Не выбирай молча и не спрашивай словами: покажи блок ui
   с этими параметрами и сразу делай по выставленным значениям.
   Пример: просят игру — дай выбрать размер поля, скорость, оформление.
   Просят текст — объём, тон, формат.
   Без блока отвечай, только когда решение действительно одно: короткий
   фактический вопрос, продолжение уже начатой работы или случай, когда
   пользователь сам задал все параметры.

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


# Действия, реально трогающие компьютер. Служебные «глаза» (silent) сюда не
# входят по определению — источник истины один: реестр инструментов.
COMPUTER_TOOLS = {n for n in tools.group_names("computer") if not tools.is_silent(n)}

# «сейчас нажму», «кликнул», «открыл окно» — заявка на действие
_ACTION_CLAIM = re.compile(
    r"(перемещ|навед|нажал|нажим|кликн|щёлкн|щелкн|открыл|открыва|печата|ввёл|ввел|"
    r"переключ|прокрут|скролл|курсор)", re.I)


def _claims_action(text: str) -> bool:
    return bool(_ACTION_CLAIM.search(text or ""))


def suggest_replies(user_text: str, answer: str) -> List[str]:
    """Три коротких варианта продолжения разговора — кнопками под ответом.

    Делает самая дешёвая модель (nano) и с жёстким лимитом токенов: подсказки
    не должны ни задерживать ответ, ни стоить заметных денег. Любая ошибка
    означает «подсказок нет» — ответ пользователя от этого не страдает.
    """
    if not (answer or "").strip():
        return []
    try:
        out = llm.chat([
            {"role": "system", "content":
             "Ты помогаешь пользователю продолжить разговор с ассистентом. "
             "По последнему ответу ассистента предложи РОВНО 3 коротких варианта "
             "следующей реплики ОТ ЛИЦА ПОЛЬЗОВАТЕЛЯ. Каждый — до 6 слов, по-русски, "
             "без нумерации и кавычек, разные по смыслу: уточнить, углубить, "
             "попросить действие. Ответь ТОЛЬКО JSON-массивом из 3 строк."},
            {"role": "user", "content": ("Мой запрос: %s\n\nОтвет ассистента: %s"
                                         % (user_text[:600], answer[:1200]))},
        ], tier="nano", max_tokens=160, temperature=0.8, timeout=20).get("content", "")
    except Exception:
        return []
    return _parse_replies(out)


def _parse_replies(out: str) -> List[str]:
    """Достать три реплики из ответа модели — как бы она их ни оформила.

    ПОЧЕМУ не просто json.loads. Просили «ТОЛЬКО JSON-массив», и разбор был
    на это завязан. Но nano — самая слабая модель: она регулярно отвечает
    списком с дефисами, нумерацией или добавляет «Вот варианты:». Тогда
    regex не находил массив, функция возвращала пустоту, и полоса подсказок,
    помигав заглушками, просто исчезала. Формат ответа модели — не то, на
    что можно опираться; опираемся на строки, а JSON разбираем как удачу.
    """
    out = (out or "").strip()
    if not out:
        return []
    items: List[str] = []
    match = re.search(r"\[.*\]", out, re.S)
    if match:
        try:
            items = [str(x) for x in json.loads(match.group(0))]
        except Exception:
            items = []
    if not items:
        # Запасной разбор: обычные строки с любой маркировкой в начале.
        # Строку, кончающуюся двоеточием, отбрасываем — это заголовок вроде
        # «Вот варианты:», а не реплика. Признак структурный, не список фраз.
        for line in out.splitlines():
            line = re.sub(r'^\s*(?:[-*•—]|\d+[.)])\s*', '', line).strip()
            line = line.strip('",[]«»').strip()
            if line and not line.endswith(":"):
                items.append(line)
    clean = []
    for it in items:
        it = str(it).strip().strip('"«»').strip()
        if 2 <= len(it) <= 70 and it not in clean:
            clean.append(it)
    return clean[:3]


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

    # ------------------------------------------------------- вопрос к юзеру
    def _wait_answer(self, question: str, options: List[str],
                     timeout: int = 300) -> Dict[str, Any]:
        """Задать вопрос и дождаться нажатия кнопки в интерфейсе.

        Механика та же, что у подтверждений: запись в БД + опрос её статуса.
        Так ответ переживает обрыв SSE и работает из любой вкладки.
        """
        record = db.create_question(self.chat_id, question, options)
        deadline = time.time() + timeout
        while time.time() < deadline:
            fresh = db.get_question(record["id"])
            if fresh and fresh.get("status") == "answered":
                return fresh
            time.sleep(0.5)
        return {**record, "status": "expired", "answer": ""}

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

        # Управление компьютером без разрешения системы невозможно: macOS
        # молча гасит клики, и агент бесконечно «нажимает» впустую. Проверяем
        # ДО работы и честно говорим, что включить, — одним сообщением.
        if self.computer_use:
            from .tools import system as _sys
            if _sys.IS_MAC and not _sys.accessibility_ok():
                yield {"type": "delta", "text": _sys._NO_ACCESS_HINT}
                yield {"type": "done", "content": _sys._NO_ACCESS_HINT,
                       "files": [], "tools": []}
                return

        available = tools.schemas(_tool_groups(self.computer_use))
        if not route.get("offer_tools", True):
            # оркестратор отдал реплику дешёвой модели именно потому, что
            # инструменты тут не нужны — не суём их ей в руки
            available = []
        if self.task_id:
            # мы УЖЕ внутри фоновой задачи: планировать ещё одну запрещено,
            # иначе AUTO наполняется клонами одной и той же просьбы
            available = [t for t in available
                         if (t.get("function") or {}).get("name") != "schedule_task"]
        max_steps = _max_steps(self.agent_mode)

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
        retried_claim = False          # ловушку вранья взводим один раз за прогон
        seen_calls: Dict[str, int] = {}   # защита от зацикливания на одном вызове

        for step in range(max_steps):
            # phase="think" — это то самое ожидание перед первым словом ответа.
            # Фронт по нему показывает мигающий курсор вместо крутилки; угадывать
            # состояние по тексту статуса он не должен.
            if step == 0:
                yield {"type": "status", "text": "Думаю", "phase": "think"}
            else:
                yield {"type": "status", "text": "Работаю над шагом %d" % (step + 1)}
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
                    yield {"type": "tool_hint", "name": event.get("name", ""),
                           "group": tools.group_of(event.get("name", ""))}
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

            # ЛОВУШКА ВРАНЬЯ. В режиме управления компьютером модель любит
            # написать «сейчас нажму» / «переключил диалог», не вызвав ни одного
            # инструмента. Раньше мы это замечали ТОЛЬКО в самом конце и просто
            # дописывали извинение — то есть фиксировали провал вместо того,
            # чтобы его исправить. Теперь возвращаем модель к работе прямо в
            # цикле: заявка на действие без вызова — это не ответ.
            # Условие намеренно НЕ опирается на список глаголов: любая попытка
            # перечислить формы («нажал», «нажму», «щёлкну»...) неизбежно
            # дырявая. Правило закрытое: в режиме управления компьютером ответ
            # без единого действия — подозрителен, и мы даём модели ровно один
            # шанс исправиться. Если действие и правда не требовалось, она
            # просто повторит ответ.
            if (self.computer_use and not tool_calls and text_piece.strip()
                    and not any(t in COMPUTER_TOOLS for t in self.used_tools)
                    and not retried_claim):
                retried_claim = True
                if gate_open:
                    yield {"type": "reset"}
                    gate_open = False
                yield {"type": "status", "text": "Проверяю, что действие выполнено"}
                convo.append({"role": "assistant", "content": text_piece})
                convo.append({"role": "user", "content":
                              "Ты ответил текстом, но не вызвал ни одного инструмента, "
                              "поэтому на компьютере НИЧЕГО не произошло. "
                              "Не описывай действия словами. Сейчас же вызови нужный "
                              "инструмент (screenshot, чтобы увидеть экран, затем "
                              "mouse_click / type_text / press_key). Если действие "
                              "на компьютере не требовалось — просто повтори свой "
                              "ответ без изменений."})
                continue

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

                # Модель может залипнуть, повторяя один и тот же вызов с теми же
                # аргументами. Раньше это молча съедало все шаги, и пользователь
                # видел «думаю» до самого конца. Считаем повторы и вмешиваемся.
                sig = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)[:300]
                seen_calls[sig] = seen_calls.get(sig, 0) + 1
                if seen_calls[sig] > 2:
                    self._append_tool_result(convo, call, name, {
                        "ok": False,
                        "error": "Этот вызов с теми же аргументами уже повторялся. "
                                 "Результат не изменится. Смени подход или дай ответ.",
                    }, from_text)
                    continue

                # Уточняющий вопрос исполняет сам агент: инструменту нужно
                # остановиться и дождаться нажатия кнопки, а не вернуть значение.
                if name == "ask_user":
                    options = [o.strip() for o in
                               str(args.get("options") or "").split("|") if o.strip()]
                    question = str(args.get("question") or "").strip()
                    if not question or len(options) < 2:
                        self._append_tool_result(convo, call, name, {
                            "ok": False,
                            "error": "нужен непустой question и минимум два варианта "
                                     "в options через |",
                        }, from_text)
                        continue
                    self.used_tools.append(name)
                    record = self._wait_answer(question, options[:5])
                    yield {"type": "question", "id": record["id"],
                           "question": question, "options": options[:5],
                           "answer": record.get("answer", ""),
                           "status": record.get("status")}
                    answered = record.get("status") == "answered"
                    self._append_tool_result(convo, call, name, {
                        "ok": answered,
                        "answer": record.get("answer", ""),
                    } if answered else {
                        "ok": False,
                        "error": "Пользователь не ответил. Действуй по самому "
                                 "разумному варианту и скажи, какой выбрал.",
                    }, from_text)
                    continue

                self.used_tools.append(name)
                yield {"type": "tool_start", "id": call.get("id"), "name": name,
                       "label": tools.label_of(name), "args": args,
                       "group": tools.group_of(name),
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
        # Предупреждаем, если в режиме управления компьютером не выполнено ни
        # одного действия И модель уже проигнорировала прямое требование их
        # выполнить (retried_claim). Опираться на список глаголов нельзя —
        # «нажму» / «нажал» / «щёлкну» не перечислить полностью.
        if self.computer_use and not any(t in COMPUTER_TOOLS for t in self.used_tools):
            if retried_claim or _claims_action(final_text):
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

        # Варианты продолжения СЮДА НЕ ВХОДЯТ. Раньше они считались прямо здесь,
        # и пользователь ждал ещё один запрос к модели уже после готового ответа:
        # ответ дописан, а поток не закрыт и кнопка «стоп» продолжает гореть.
        # Теперь ответ завершается немедленно, а подсказки браузер запрашивает
        # отдельно (/api/replies) — они не могут задержать или сорвать ответ.
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
