# Продолжение работы над image-backend

Дата фиксации: **16 сентября 2026**. Production-развёртывание сознательно
отложено: Джарвис пока является бета-версией. Этот файл — точка входа для
следующего диалога/агента.

## Как начать следующий диалог

Передайте агенту эту фразу:

> Продолжи image-backend Джарвиса по `gateway/CONTINUE-RU.md` из ветки
> `arena/01a0113d-monshe` репозитория `monshepiano/monshe`. Сначала проверь
> фактическое состояние репозитория и не публикуй установщик до live smoke-test.

Если новая рабочая ветка создана от `main` и файлов gateway в ней ещё нет,
нужно получить изменения из ветки `origin/arena/01a0113d-monshe`. Готовая
реализация находится в коммитах `77b54a6`, `e0b20f8` и `eb51aa9`. Нельзя
переписывать или публиковать секреты при переносе.

## Зафиксированное решение

- Пользователь выбрал облачный proxy, а не локальную модель.
- Целевой Mac — Apple Silicon с 8 ГБ RAM; локальный FLUX для него слишком
  тяжёлый.
- Предпочтительная площадка — Timeweb Cloud: AI Gateway как upstream и App
  Platform как HTTPS-host.
- Аккаунт Timeweb создан.
- Отдельный API-ключ AI Gateway создан и остаётся только у пользователя.
  **Не просить прислать его в чат и не встраивать в установщик.**
- В интерфейсе Timeweb пользователь подтвердил точный model ID:
  `black_forest_labs/flux-2-pro`.
- Никакое приложение в App Platform ещё не развёрнуто. Технического домена и
  успешной реальной генерации пока нет.
- Production release token ещё не создан. Ранее показанный в диалоге тестовый
  token не был развёрнут и не должен использоваться — при продолжении создать
  новый случайный token.

## Что уже реализовано

### Сервер

`gateway/image_gateway.py` — dependency-free HTTP gateway:

- принимает `POST /v1/images/generations` с release Bearer token;
- передаёт серверный `TIMEWEB_AI_GATEWAY_KEY` только в
  `POST https://api.timeweb.ai/v1/images/generations`;
- запрашивает ровно одно изображение и предпочитает `b64_json`;
- принимает URL fallback только по HTTPS и только с разрешённых доменных
  суффиксов, без redirect-following;
- нормализует ориентацию в `1536x1024`, `1024x1536` или `1024x1024`;
- ограничивает request/response size, MIME/magic bytes, concurrency и квоты;
- не логирует prompt или Authorization;
- возвращает клиенту непосредственно JPEG, PNG или WebP;
- сохраняет GigaChat только как необязательный personal/B2B fallback.

`Dockerfile` и `.dockerignore` дают минимальный непривилегированный контейнер.
Он слушает `0.0.0.0`, содержит `EXPOSE 8780`, использует переданный платформой
`PORT` и имеет `/health` healthcheck.

VPS/systemd/nginx остаются альтернативой в `gateway/README.md`, но первый
production-вариант должен использовать App Platform.

### Клиент и установщик

- `app/jarvis/tools/media.py` умеет вызывать HTTPS gateway с ограниченным
  release token, валидировать ответ и атомарно сохранять файл с
  детерминированным SHA-256 именем.
- `app/jarvis/config.py` хранит URL/token, маскирует token в публичной
  конфигурации и предпочитает gateway при наличии полной пары.
- `install/build.py` и `install/template.sh` умеют внедрять
  `JARVIS_IMAGE_GATEWAY_URL` + `JARVIS_IMAGE_GATEWAY_TOKEN` и не допускают
  наполовину заданную пару.
- В beta user-facing документации явно написано, что image-cloud пока не
  включён. Не менять это обещание до завершения live-проверок.

## Проверенное состояние

На `eb51aa9` проходили:

```bash
python3 -m unittest tests.test_package28
# Ran 41 tests ... OK

node tests/package28_frontend_runtime.js
# 11 regression groups passed

python3 -m py_compile gateway/image_gateway.py app/jarvis/tools/media.py
node --check app/jarvis/web/js/app.js
bash -n install/template.sh
git diff --check
```

Проверки покрывают Base64 и URL ответы Timeweb, malformed/oversized payload,
provider dispatch, отсутствие upstream key на клиенте, rate/token gates,
детерминированное сохранение и frontend-регрессии.

## Порядок продолжения

### 1. Сначала перепроверить модель и контракт

Model ID, подтверждённый пользователем:

```text
black_forest_labs/flux-2-pro
```

Не заменять его по памяти или маркетинговому названию. После первого live
запроса проверить, что Timeweb действительно принимает для этой модели:

- endpoint `/v1/images/generations`;
- `response_format=b64_json`;
- `size=1024x1024`, затем один landscape/portrait size;
- результат без видимого watermark.

Если модель игнорирует `b64_json` и отдаёт URL, определить фактический hostname
asset-сервера и только затем добавить минимально необходимый suffix в
`TIMEWEB_IMAGE_HOSTS`. Не разрешать произвольные URL и не отключать TLS.

### 2. Создать новый release token

В доверенной release-среде:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Этот token не является upstream credential, но его всё равно нельзя коммитить
до осознанной release-сборки. Добавить server-side значение в
`JARVIS_GATEWAY_CLIENT_TOKENS`; тот же token позднее внедрить в Mac installer.

### 3. Развернуть Docker Backend в Timeweb App Platform

Параметры:

```text
repository:        https://github.com/monshepiano/monshe.git
branch:            arena/01a0113d-monshe (или main после merge)
project directory: repository root
Dockerfile:        Dockerfile
healthcheck:       /health
build/run command: оставить пустыми
```

Минимально необходимые environment variables:

```text
JARVIS_IMAGE_UPSTREAM=timeweb
TIMEWEB_AI_GATEWAY_KEY=<вставляет пользователь только в secret-поле Timeweb>
TIMEWEB_IMAGE_MODEL=black_forest_labs/flux-2-pro
JARVIS_GATEWAY_CLIENT_TOKENS=<новый release token>
JARVIS_GATEWAY_TRUST_PROXY=1
```

Опциональные безопасные лимиты уже имеют defaults; для первого production
можно задать явно:

```text
JARVIS_GATEWAY_PER_MINUTE=4
JARVIS_GATEWAY_PER_DAY=40
JARVIS_GATEWAY_GLOBAL_PER_DAY=200
JARVIS_GATEWAY_CONCURRENCY=2
```

Не задавать `JARVIS_GATEWAY_HOST` и `JARVIS_GATEWAY_PORT` в App Platform:
контейнер уже слушает публичный интерфейс и автоматически использует `PORT`.

### 4. Live smoke-test до любой сборки

После получения технического HTTPS URL:

```bash
curl -fsS https://<technical-domain>/health
# ожидается ok=true и provider_configured=true

curl -sS -o /tmp/jarvis-timeweb-smoke.img \
  -D /tmp/jarvis-timeweb-smoke.headers \
  -H "Authorization: Bearer $JARVIS_IMAGE_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"prompt":"A cinematic blue futuristic city at dawn, no text, no logo, no watermark","width":1024,"height":1024}' \
  https://<technical-domain>/v1/images/generations

file /tmp/jarvis-timeweb-smoke.img
```

Обязательно проверить:

1. запрос без token возвращает 401 и не вызывает платную модель;
2. с корректным token приходит один валидный JPEG/PNG/WebP;
3. заголовки содержат `X-Request-ID` и `X-Image-ID`;
4. изображение открывается, качественное и без видимого watermark;
5. provider/API key нигде не появился в response, логах, Git или ZIP;
6. URL и генерация доступны из РФ без VPN;
7. затем выполнить один end-to-end запрос из Джарвиса на целевом Mac.

При ошибке использовать request ID и server logs, но не печатать prompt/key.
Исправлять первопричину, а не ослаблять TLS, URL allowlist или byte limits.

### 5. Только после smoke-test собрать release

В доверенной среде задать полную пару:

```bash
export JARVIS_IMAGE_GATEWAY_URL='https://<technical-domain>'
export JARVIS_IMAGE_GATEWAY_TOKEN='<тот же новый release token>'
python3 install/build.py
```

Перед публикацией:

1. обновить version в `app/jarvis/__init__.py`;
2. вернуть user-facing обещание image-cloud только если Mac e2e прошёл;
3. повторить backend/frontend/syntax tests;
4. проверить `JARVIS.zip` на upstream key, Timeweb key, временные файлы и
   неожиданные большие артефакты;
5. проверить reproducible build/hash;
6. установить ZIP на Apple Silicon Mac с 8 ГБ RAM поверх предыдущей версии;
7. проверить обычный чат, AGENT/AUTO и настоящую генерацию;
8. commit + push, затем дать immutable raw URL на новый commit.

Текущий `JARVIS.zip` имеет старую версию и не содержит готового production
image-cloud. Его нельзя выдавать как финальный результат этой задачи.

## Неприемлемые обходные пути

- не помещать `TIMEWEB_AI_GATEWAY_KEY`, GigaChat key или иной provider secret в
  публичный ZIP/config/frontend;
- не считать Base64 способом спрятать secret;
- не использовать Puter (российский номер не подходит);
- не возвращаться к Cloudflare Workers для больших image bodies из РФ;
- не отключать TLS verification;
- не запускать тяжёлый локальный FLUX на 8-ГБ Mac;
- не обещать zero-setup image generation, пока нет реального HTTPS deployment
  и smoke-test из РФ;
- не использовать старый release token из предыдущего диалога.
