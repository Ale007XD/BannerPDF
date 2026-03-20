# BannerPrint — сайт-конструктор печатных баннеров

Веб-сервис для самостоятельного создания и заказа печатных баннеров.
Работает в связке с Telegram-ботом [@BannerPrintBot](https://t.me/BannerPrintBot).
Сайт: [bannerprintbot.ru](https://bannerprintbot.ru)

---

## Содержание

- [Статус проекта](#статус-проекта)
- [Стек](#стек)
- [Архитектура](#архитектура)
- [Структура репозитория](#структура-репозитория)
- [Быстрый старт (локально)](#быстрый-старт-локально)
- [Деплой на VPS](#деплой-на-vps)
- [Переменные окружения](#переменные-окружения)
- [API](#api)
- [База данных](#база-данных)
- [FSM заказов](#fsm-заказов)
- [Авторизация](#авторизация)
- [Реферальная программа](#реферальная-программа)
- [Тесты](#тесты)
- [Nginx и rate limits](#nginx-и-rate-limits)
- [152-ФЗ](#152-фз)
- [Pending tasks](#pending-tasks)

---

## Статус проекта

| | |
|---|---|
| Готовность | **85%** — код написан и проверен, готов к деплою |
| Что готово | Весь бэкенд (P0 закрыты), фронтенд, схема БД, Nginx, Dockerfile, docker-compose, GitHub Actions |
| Что осталось | Деплой на VPS · загрузка шрифтов и ICC-профиля · smoke-тест · реферальные команды бота |

---

## Стек

| Компонент | Технология |
|---|---|
| Backend | FastAPI 0.110+ + Uvicorn (строго 1 worker) |
| Рендер превью | Pillow 10 (JPEG, синхронно через ThreadPoolExecutor) |
| Рендер PDF | ReportLab → Ghostscript 10 (PDF/X-1a, CMYK, ICC) |
| Executor | `ProcessPoolExecutor(max_workers=2)` — только для GS |
| Frontend | Vanilla HTML/CSS/JS, без фреймворков и сборки |
| База данных | SQLite WAL — `banner_web.db` (отдельная от `banner_bot.db`) |
| Прокси | Nginx 1.25 в Docker — реверс-прокси + статика + rate limits |
| Деплой | GitHub Actions → SCP → Docker Compose |
| Бот | python-telegram-bot 21 (async), Python 3.11+ |

---

## Архитектура

```
                        ┌──────────────────────────────────┐
  Браузер (мобайл)      │  Nginx (Docker, порты 80/443)    │
  ──────────────────►   │  • Статика /app/frontend          │
                        │  • Rate limits (preview/order/dl) │
                        │  • TLS (Let's Encrypt)            │
                        └──────────────┬───────────────────┘
                                       │ proxy_pass :8000
                        ┌──────────────▼───────────────────┐
                        │  FastAPI (Uvicorn, 1 worker)      │
                        │                                   │
                        │  Роутеры:                         │
                        │  • /api/preview    (Pillow)       │
                        │  • /api/order      (FSM)          │
                        │  • /api/payment/*  (Tona)         │
                        │  • /api/download/* (GS)           │
                        │  • /api/admin/*                   │
                        │  • /api/referral/*                │
                        │                                   │
                        │  ProcessPoolExecutor(2)           │
                        │  └─ render_pdf_sync()  ◄── GS     │
                        └──────────────┬───────────────────┘
                                       │
                        ┌──────────────▼───────────────────┐
                        │  SQLite WAL — banner_web.db       │
                        │  (volume: bannerprint_data)        │
                        └──────────────────────────────────┘

  Tona Webhook ──────────► POST /api/payment/callback
                            └─ HMAC-SHA256 ПЕРВЫМ
                            └─ FSM: pending→paid→token_issued

  @BannerPrintBot ────────► POST /api/referral/internal/create
                            (X-Bot-Secret)
```

**Ключевые архитектурные решения:**

Uvicorn строго 1 worker — `token_store` и `order_store` в SQLite без внешней синхронизации. 2 воркера = 2 независимых in-memory стора = потерянные токены при переключении. Решение пересматривается при переходе на Redis.

Ghostscript CPU-bound. Вызов из asyncio event loop заблокировал бы весь сервер. Решение: `ProcessPoolExecutor(max_workers=2)` в `batch_worker.py`, вызов только через executor.

`token_store` и `order_store` перенесены из in-memory в SQLite — переживают рестарты и корректно работают при единственном воркере.

Рендерер — локальная копия `banner_generator.py`, не зависит от бота. `BOT_SRC_PATH` не используется. Web-контейнер полностью автономен.

---

## Структура репозитория

```
Banner_Bot/
├── .github/
│   └── workflows/
│       ├── deploy-web.yml        # Деплой сайта при push в main
│       └── ci-web.yml            # CI: линтер + pytest
│
├── web/                          # Сайт (этот проект)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   ├── templates.json            # Размеры, шрифты, цвета для GET /api/templates
│   │
│   ├── api/
│   │   ├── main.py               # FastAPI app + lifespan
│   │   ├── db/
│   │   │   ├── __init__.py       # get_db(), init_db()
│   │   │   └── schema.sql        # Схема всех таблиц
│   │   ├── routers/
│   │   │   ├── preview.py        # POST /api/preview
│   │   │   ├── order.py          # FSM + POST /api/order + GET /api/payment/status
│   │   │   ├── payment.py        # POST /api/payment/callback (HMAC первым)
│   │   │   ├── download.py       # GET /api/download/{token}
│   │   │   ├── admin.py          # GET /api/admin/stats|orders|funnel
│   │   │   ├── referral.py       # Реферальная программа
│   │   │   ├── corp_api.py       # Корп. API (заглушки 501)
│   │   │   └── batch.py          # Batch (заглушки 501)
│   │   └── services/
│   │       ├── banner_generator.py  # create_preview_jpeg(), create_final_pdf()
│   │       ├── renderer.py          # Адаптер: build_render_data(), set/get_executor()
│   │       ├── sanitizer.py         # sanitize_text_lines(), validate_banner_config()
│   │       ├── config.py            # FONTS, COLORS, BANNER_SIZES, SAFE_ZONE_MM
│   │       ├── payment.py           # verify_tona_signature(), create_payment()
│   │       ├── token_store.py       # create_token(), consume_token() — SQLite
│   │       ├── order_store.py       # save_pending(), get_pending() — SQLite
│   │       ├── referral_store.py    # accrue_commission() 15%
│   │       ├── api_key_store.py     # generate_key(), verify_key()
│   │       └── batch_worker.py      # asyncio.Queue + ProcessPoolExecutor
│   │
│   ├── frontend/
│   │   ├── index.html            # Конструктор баннера + модалки оплаты
│   │   ├── style.css             # Mobile-first, CSS vars, Unbounded + Onest
│   │   └── app.js                # Дебаунс превью 500мс, FSM оплаты, поллинг 2.5с
│   │
│   └── nginx/
│       ├── nginx.conf            # Rate limit зоны (http-блок)
│       └── default.conf          # Server блоки, proxy_pass, TLS
│
├── web/tests/                    # Тесты (pytest)
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_fsm.py
│   ├── test_hmac.py
│   ├── test_token_store.py
│   ├── test_order_store.py
│   ├── test_sanitizer.py
│   ├── test_order_api.py
│   ├── test_payment_webhook.py
│   ├── test_preview_api.py
│   └── test_renderer_preview.py
│
├── pytest.ini
├── requirements-test.txt
│
└── bot/                          # Telegram-бот (отдельный контейнер, P1)
    └── ...
```

---

## Быстрый старт (локально)

### Требования

- Docker + Docker Compose
- Шрифты TTF (GolosText, TenorSans, FiraSans, IgraSans)
- ICC-профиль `ISOcoated_v2_300_eci.icc`

### 1. Клонирование

```bash
git clone https://github.com/Ale007XD/Banner_Bot.git
cd Banner_Bot/web
```

### 2. Переменные окружения

```bash
cp .env.example .env
# Заполнить: TONA_API_KEY, TONA_SHOP_ID, TONA_WEBHOOK_SECRET, ADMIN_TOKEN, BOT_INTERNAL_SECRET
```

### 3. Подготовка шрифтов и профиля

```bash
mkdir -p /home/deploy/banner_web/fonts /home/deploy/banner_web/profiles

# Скопировать TTF-шрифты в fonts/
# Скопировать ISOcoated_v2_300_eci.icc в profiles/
```

### 4. Запуск

```bash
docker compose up -d --build
```

### 5. Проверка

```bash
curl http://localhost:8000/api/health
# {"status":"ok","service":"bannerprint"}

curl http://localhost:8000/api/templates
```

Сайт доступен на `http://localhost` (Nginx проксирует с 80 на FastAPI :8000).

---

## Деплой на VPS

**VPS:** Hetzner CX41, `deploy@r1018353` (4 vCPU, 16 GB RAM)

### Автоматический деплой (GitHub Actions)

При `push` в `main` с изменениями в `web/**` автоматически запускается `.github/workflows/deploy-web.yml`:

1. Копирует файлы на VPS через SCP (`web/` → `/home/deploy/banner_web/`)
2. Пересобирает контейнер `api`
3. Поднимает `docker compose up -d`
4. Проверяет `GET /api/health`

**Необходимые секреты в GitHub:**

```
VPS_HOST      — IP или hostname VPS
VPS_USER      — deploy
VPS_SSH_KEY   — приватный SSH-ключ
```

### Первый ручной деплой

```bash
# На VPS
mkdir -p /home/deploy/banner_web/fonts
mkdir -p /home/deploy/banner_web/profiles

# Загрузить шрифты и ICC-профиль
scp fonts/*.ttf deploy@r1018353:/home/deploy/banner_web/fonts/
scp ISOcoated_v2_300_eci.icc deploy@r1018353:/home/deploy/banner_web/profiles/

# Создать .env
nano /home/deploy/banner_web/.env

# Запустить
cd /home/deploy/banner_web
docker compose up -d --build
```

### SSL-сертификат

Certbot устанавливается на хосте (не в контейнере). Сертификаты монтируются в Nginx через volume `/etc/letsencrypt`.

```bash
apt install certbot
certbot certonly --standalone -d bannerprintbot.ru -d www.bannerprintbot.ru
```

---

## Переменные окружения

| Переменная | Описание | Пример |
|---|---|---|
| `TONA_API_KEY` | API-ключ платёжного сервиса Tona | — |
| `TONA_SHOP_ID` | ID магазина в Tona | — |
| `TONA_WEBHOOK_SECRET` | Секрет для HMAC-SHA256 верификации webhook | — |
| `SITE_BASE_URL` | Базовый URL сайта | `https://bannerprintbot.ru` |
| `SITE_PDF_PRICE` | Цена PDF в рублях (не хардкодить!) | `299` |
| `ADMIN_TOKEN` | Bearer-токен для `/api/admin/*` (32 байта) | — |
| `BOT_INTERNAL_SECRET` | Секрет для внутреннего API бота | — |
| `ALLOWED_ORIGINS` | CORS origins через запятую | `https://bannerprintbot.ru` |
| `WEB_DB_PATH` | Путь к SQLite БД | `/app/data/banner_web.db` |
| `FONTS_DIR` | Директория с TTF-шрифтами | `/app/fonts` |
| `ICC_PROFILE_PATH` | Путь к ICC-профилю для печати | `/profiles/ISOcoated_v2_300_eci.icc` |
| `BATCH_DIR` | Временная директория для batch ZIP | `/tmp/bannerprint_batches` |
| `UVICORN_WORKERS` | Количество воркеров (строго 1) | `1` |

Секреты (`TONA_*`, `ADMIN_TOKEN`, `BOT_INTERNAL_SECRET`) передаются только через `.env` файл, никогда не хардкодятся в коде или Dockerfile.

---

## API

### Публичные эндпоинты

```
GET  /api/health                   — Healthcheck
GET  /api/templates                — Список размеров, шрифтов, цветов
POST /api/preview                  — JPEG-превью баннера (rate limit: 30 RPM/IP)
POST /api/order                    — Создание заказа + ссылка на оплату (5 RPM/IP)
GET  /api/payment/status/{id}      — Статус заказа (поллинг с фронтенда)
POST /api/payment/callback         — Webhook от Tona (HMAC-SHA256 верификация)
GET  /api/download/{token}         — Скачать PDF (10 RPM/IP, одноразовый токен)
GET  /api/referral/stats/{code}    — Статистика реферального кода
```

### Пример: создание заказа

```bash
curl -X POST https://bannerprintbot.ru/api/order \
  -H "Content-Type: application/json" \
  -d '{
    "size_key": "3x2",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [
      {"text": "ЛЕТНЯЯ РАСПРОДАЖА", "scale": 1.0},
      {"text": "Скидки до 70%", "scale": 0.7}
    ]
  }'

# Ответ:
# {"order_id": "uuid4...", "pay_url": "https://pay.tona.ru/..."}
```

### Пример: генерация превью

```bash
curl -X POST https://bannerprintbot.ru/api/preview \
  -H "Content-Type: application/json" \
  -d '{
    "size_key": "3x2",
    "bg_color": "Красный",
    "text_color": "Белый",
    "font": "Fira Sans",
    "text_lines": [{"text": "АКЦИЯ", "scale": 1.0}]
  }'

# Ответ:
# {"preview_base64": "...", "width_mm": 3000, "height_mm": 2000}
```

### Внутренние (только бот)

```
POST /api/referral/internal/create  — Создание реф. кода (X-Bot-Secret)
```

### Административные

```
GET  /api/admin/stats               — Общая статистика
GET  /api/admin/orders              — Список заказов
GET  /api/admin/funnel              — Воронка конверсии
```

Все admin-эндпоинты требуют `Authorization: Bearer <ADMIN_TOKEN>`.

### Корп. API и Batch (заглушки, P2)

```
POST /api/v1/render
GET  /api/v1/usage
POST /api/v1/batch/submit
GET  /api/v1/batch/{id}
GET  /api/v1/batch/{id}/download
```

Интерактивная документация: `https://bannerprintbot.ru/api/docs`

---

## База данных

SQLite WAL-режим, файл `banner_web.db`. Отдельная от `banner_bot.db`. Инициализируется автоматически при старте через `schema.sql`.

```
web_orders          — заказы (статус управляется FSM)
download_tokens     — одноразовые токены скачивания (TTL 15 мин)
pending_orders      — TTL-буфер config до webhook (TTL 30 мин)
api_plans           — тарифы корп. API (предзаполнена)
api_keys            — ключи корп. API
batch_jobs          — задачи batch-рендера
referrers           — рефереры и балансы
referrals           — начисленные комиссии
```

### Важно: что хранится в `web_orders`

`config_json` хранится постоянно в `web_orders` и продублирован как TTL-буфер в `pending_orders`. `download.py` читает `config_json` из `web_orders` напрямую — PDF можно сгенерировать даже после истечения `pending_orders`.

---

## FSM заказов

```
create_order ──► PENDING
                    │
             webhook_paid
                    │
                    ▼
                  PAID
                    │
              issue_token
                    │
                    ▼
             TOKEN_ISSUED


PENDING ──[ttl_expired]──► EXPIRED
PAID    ──[ttl_paid]──────► EXPIRED
```

Все переходы — только через `transition(order_id, event)` в `routers/order.py`. Прямой `UPDATE status` в обход FSM запрещён. Функция идемпотентна: повторный переход в уже текущий статус возвращает его без ошибки.

---

## Авторизация

| Уровень | Заголовок | Эндпоинты |
|---|---|---|
| Admin | `Authorization: Bearer <ADMIN_TOKEN>` | `/api/admin/*` |
| Bot internal | `X-Bot-Secret: <BOT_INTERNAL_SECRET>` | `/api/referral/internal/*` |
| Corp API | `Authorization: Bearer bp_live_<32b>` | `/api/v1/*` |
| Tona webhook | `X-Tona-Signature` (HMAC-SHA256) | `/api/payment/callback` |

**Форматы идентификаторов:**

```
order_id:    UUID4  (36 символов)
token:       32 bytes hex  (64 символа)
ref_code:    8 символов A-Z0-9
api_key:     bp_live_<32 chars base64url>
```

---

## Реферальная программа

**Механика:** 15% от суммы заказа начисляется на баланс реферера. Покупатель платит полную цену — скидок нет.

**Разделение данных (152-ФЗ):**

```
Сайт хранит:   ref_code (8 A-Z0-9) + balance_rub — не ПД
Бот хранит:    tg_id → ref_code в banner_bot.db
Сайт не знает tg_id владельца кода — намеренно
```

**Создание реф. кода:** только через `POST /api/referral/internal/create` с заголовком `X-Bot-Secret`. Прямое создание кодов через сайт невозможно.

**Команды бота (P1):** `/referral`, `/balance`, `/payout`

---

## Тесты

### Запуск

```bash
# Установка зависимостей
pip install -r web/requirements.txt
pip install -r requirements-test.txt

# Запуск всех тестов
PYTHONPATH=. pytest web/tests/ -v

# Только юнит-тесты (без HTTP)
PYTHONPATH=. pytest web/tests/test_fsm.py web/tests/test_hmac.py -v
```

### Покрытие (~90 тестов)

| Файл | Что проверяет |
|---|---|
| `test_fsm.py` | Все переходы FSM, guard, идемпотентность |
| `test_hmac.py` | HMAC-SHA256 верификация, edge cases |
| `test_token_store.py` | create/consume/cleanup токенов |
| `test_order_store.py` | save/get/delete/cleanup pending_orders |
| `test_sanitizer.py` | sanitize_line, validate_banner_config |
| `test_order_api.py` | HTTP: POST /api/order, GET /api/payment/status |
| `test_payment_webhook.py` | HTTP: webhook, HMAC первым, FSM через HTTP |
| `test_preview_api.py` | HTTP: POST /api/preview, все size_key и цвета |
| `test_renderer_preview.py` | build_render_data, executor lifecycle |

**GS не нужен** — `render_preview_base64` и `ProcessPoolExecutor` замоканы в `conftest.py`. Tona API не дёргается — `create_payment` замокан.

### CI (GitHub Actions)

Workflow `.github/workflows/ci-web.yml` запускается на push в `main`/`dev` при изменениях в `web/**`:

1. **Ruff** — проверка стиля и импортов (E, F, I правила)
2. **pytest** — все тесты без Docker и GS

---

## Nginx и rate limits

| Зона | Лимит | Burst | Эндпоинт |
|---|---|---|---|
| `preview_limit` | 30 req/min/IP | 5 | `POST /api/preview` |
| `order_limit` | 5 req/min/IP | 2 | `POST /api/order` |
| `download_limit` | 10 req/min/IP | 3 | `GET /api/download/*` |

При превышении лимита возвращается `429 Too Many Requests`.

Webhook `/api/payment/callback` не ограничен rate limit — Tona не спамит, а защита осуществляется через HMAC-SHA256 верификацию.

---

## 152-ФЗ

| | |
|---|---|
| **Не хранится на сайте** | email, имя, tg_id, IP-адреса |
| **Хранится на сайте** | `order_id` (UUID), `size_key`, `ref_code`, `token` (hex), `amount_rub`, `config_json`, временные метки — не являются ПД |
| **Исключение** | `email` в `api_keys` — основание: исполнение договора (оферта) |
| **VPS** | Hetzner, Германия — безопасно при отсутствии ПД граждан РФ |

---

## Pending tasks

### P0 — выполнено

- [x] HMAC-SHA256 верификация `X-Tona-Signature` в `payment.py`
- [x] `token_store` → SQLite `download_tokens`
- [x] `order_store` → SQLite `pending_orders`
- [x] FSM `OrderStatus(Enum)` + `transition()` + guard
- [x] Download rate limit 10/min/IP в Nginx
- [x] `Dockerfile`: `uvicorn --workers 1`
- [x] `order.py`: `SITE_PDF_PRICE` из env, не хардкод
- [x] `batch_worker.py`: `ProcessPoolExecutor(max_workers=2)`
- [x] `main.py` lifespan: cleanup `download_tokens` + `pending_orders`

### P1 — сразу после деплоя

- [ ] Первый деплой на VPS + загрузка шрифтов и ICC-профиля
- [ ] Smoke-тест: health → templates → preview → order → webhook → download
- [ ] SSL-сертификат (certbot на хосте)
- [ ] E2E тест полного флоу с реальным Tona webhook
- [ ] Бот: таблица `referral_codes(tg_id, ref_code, created_at)` в `banner_bot.db`
- [ ] Бот: команды `/referral`, `/balance`, `/payout`
- [ ] `bot/` перенести в репо + `deploy-bot.yml`

### P2 — масштаб

- [ ] Preview → `image/jpeg` streaming вместо base64 (+33% overhead)
- [ ] Structured JSON logging (structlog или python-json-logger)
- [ ] Нагрузочный тест batch (100-строчный CSV)
- [ ] Cleanup `/tmp/bannerprint_batches` старше TTL
- [ ] Раскрыть корп. API и batch эндпоинты (убрать заглушки 501)
- [ ] Postman-коллекция из `/api/docs`
