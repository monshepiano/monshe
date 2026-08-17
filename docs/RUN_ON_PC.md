# JARVIS на своём ПК: настоящие «руки»

Облачная песочница показывает интерфейс, голос, камеру и поток санкций. А вот
управлять **вашим** компьютером (открыть VPN, написать в Telegram, купить на
Ozon) можно только локально — Джарвис должен крутиться на той же машине.

## 1. Установка

```bash
# Python 3.10+
git clone <ваш-репозиторий> && cd monshe
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# «руки» для браузера и мыши (по желанию, что нужно):
pip install playwright pyautogui pillow
playwright install chromium
```

## 2. Ключи — файл `.env`

Скопируйте `.env.example` в `.env` и впишите ключи:

```env
DEEPSEEK_API_KEY=sk-...          # текст, дёшево, без VPN из РФ
GOOGLE_API_KEY=AIza...           # зрение (камера) — или OpenAI/Anthropic
JARVIS_ENABLE_PC=1               # включить управление ПК
```

Запуск: `python run.py` → http://localhost:8000

## 3. Сценарии «как у друга»

### 🖼 Увидеть картину и купить такую же на Ozon
1. Включите камеру, наведите на картину, нажмите «СКАНИРОВАТЬ».
2. Скажите: «**купи такую же**».
3. Роутер выберет vision-модель → Джарвис опишет картину, найдёт похожее на
   Ozon (инструмент `buy_assist`) и покажет план в «САНКЦИЯХ».
4. Нажмите «ОДОБРИТЬ» → на локальном ПК стартует Playwright: откроется Ozon,
   товар добавится в корзину. **Оплату делаете вы.**

Минимальная цепочка Playwright (сниппет для `tools.py`, функция `_run_approved`):

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)      # окно видно
    page = browser.new_context(storage_state="ozon.json").new_page()  # сессия входа
    page.goto("https://www.ozon.ru/search/?text=" + query)
    page.locator("a", has_text=query).first.click()
    page.get_by_text("Добавить в корзину").click()
```

Вход в Ozon: один раз войдите вручную и сохраните сессию
`context.storage_state(path="ozon.json")` — дальше Джарвис переиспользует её.

### 🛡 Включить VPN, написать в Telegram, выключить VPN
Три инструмента `pc_control` с действиями `toggle_vpn` и `send_telegram`.
Примеры (вставьте в `_pc_exec` в `server/tools.py`):

```python
# VPN (замените на свою команду клиента, напр. WireGuard/Outline/приложение)
subprocess.Popen(["wireguard", "up", "your-tunnel"])   # up
subprocess.Popen(["wireguard", "down", "your-tunnel"]) # down

# Telegram (через Telethon — официальный client API)
from telethon import TelegramClient
client = TelegramClient("session", api_id, api_hash)
await client.send_message("username", "Привет, я через Джарвиса!")
```

Санкции: `send_telegram` и `toggle_vpn` тоже идут через подтверждение — Джарвис
не напишет и не включит VPN без вашего «да».

## 4. Как менять движок на лету

В `.env` (или в «Настройках» интерфейса):

```env
JARVIS_FAST_PROVIDER=deepseek
JARVIS_FAST_MODEL=deepseek-chat
JARVIS_SMART_PROVIDER=deepseek
JARVIS_SMART_MODEL=deepseek-reasoner
JARVIS_VISION_PROVIDER=gemini
JARVIS_VISION_MODEL=gemini-2.0-flash
```

Хотите локально и бесплатно — Ollama:

```bash
ollama pull llama3.2-vision   # или qwen2.5 / llama3.1
# .env: JARVIS_FAST_PROVIDER=ollama, JARVIS_FAST_MODEL=llama3.2
```

## 5. Важно (честно)

- **Ozon защищён капчей** — сложные сценарии могут требовать ручного шага.
  Поэтому финальную оплату делает человек. Это осознанный дизайн.
- **Управление мышью/клавиатурой** (`pyautogui`) включайте осознанно — агент
  получает реальные права на систему.
- **Не светите ключи**: `.env` в `.gitignore`, в интерфейсе ключи живут только
  в памяти сессии.
