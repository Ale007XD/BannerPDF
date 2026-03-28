# BannerPrint — сайт-конструктор печатных баннеров

Веб-сервис для самостоятельного создания и заказа печатных баннеров.
Сайт: [bannerbot.ru](https://bannerbot.ru)

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
- [Флоу оплаты (текущий)](#флоу-оплаты-текущий)
- [Реферальная программа](#реферальная-программа)
- [Корп. API тарифы](#корп-api-тарифы)
- [Тесты](#тесты)
- [Nginx и rate limits](#nginx-и-rate-limits)
- [152-ФЗ](#152-фз)
- [Pending tasks](#pending-tasks)

---

## Статус проекта

| | |
|---|---|
| Готовность | **99%** — сайт запущен, принимает боевые платежи |
| URL | `https://bannerbot.ru` |
| Что готово | Весь бэкенд (P0 закрыты), фронтенд (превью, кастомный размер, шрифты в кнопках, ЮКасса-виджет), FSM заказов, E2E флоу оплаты (ЮКасса → webhook → PDF), TG-уведомления (`@BannerBotInfo_bot`), корп. API с тарифом Trial, adminка, боевые ключи ЮКасса |
| Что осталось | Реферальные команды бота · тесты корп. API |

---

## Стек

| Компонент | Технология |
|---|---|
| Backend | FastAPI 0.110+ + Uvicorn (строго 1 worker) |
| Рендер превью | Pillow 10 (JPEG, синхронно через ThreadPoolExecutor) |
| Рендер PDF | ReportLab → Ghostscript 10 (PDF/X-1a, CMYK, ICC) |
| Executor | `ProcessPoolExecutor(max_workers=2)` — только для GS в batch_worker |
| Frontend | Vanilla HTML/CSS/JS, без фреймворков и сборки |
| База данных | SQLite WAL — `banner_web.db` (отдельная от `banner_bot.db`) |
| Прокси | Nginx 1.25 в Docker — реверс-прокси + статика + rate limits, порты 80/443/88 |
| Деплой | GitHub Actions → SCP → Docker Compose |
| Оплата (активная) | ЮКасса — embedded-виджет, верификация через GET `/v3/payments/{id}` |
| Оплата (запасная) | Сам.Эквайринг (selfwork.ru) — SHA256 в теле, `*_selfwork.py` |
| Бот | python-telegram-bot 21 (async), Python 3.11+ |

---

## Архитектура

```
                        ┌──────────────────────────────────────┐
  Браузер               │  Nginx (Docker, порты 80/443/88)     │
  ──────────────────►   │  • Статика /app/frontend              │
                        │  • Статика /app/frontend/admin/       │
                        │  • Rate limits (preview/order/dl)     │
                        │  • TLS (Let's Encrypt, :443)          │
                        │  • Редирект 80 → https://$host        │
                        │  • :88 → TG webhook                   │
                        └──────────────┬───────────────────────┘
                                       │ proxy_pass :8000
                        ┌──────────────▼───────────────────────┐
                        │  FastAPI (Uvicorn, 1 worker)          │
                        │                                       │
                        │  Роутеры:                             │
                        │  • /api/preview      (Pillow)         │
                        │  • /api/order        (FSM + ЮКасса)  │
                        │  • /api/payment/*    (ЮКасса)        │
                        │  • /api/download/*   (GS)             │
                        │  • /api/admin/*      (force_token)    │
                        │  • /api/referral/*                    │
                        │  • /api/v1/*         (Corp API)       │
                        │  • /api/tg/callback  (TG webhook)     │
                        │                                       │
                        │  ProcessPoolExecutor(2)               │
                        │  └─ render_pdf_sync()  ◄── GS        │
                        └──────────────┬───────────────────────┘
                                       │
                        ┌──────────────▼───────────────────────┐
                        │  SQLite WAL — banner_web.db           │
                        │  (volume: bannerprint_data)           │
                        └──────────────────────────────────────┘

  ЮКасса webhook ──► POST /api/payment/callback
                         └─ verify через GET /v3/payments/{id}
                         └─ FSM: pending→paid→token_issued
                         └─ editMessageText в TG (@BannerBotInfo_bot, статус «💳 Оплачено»)
                         └─ Клиент получает PDF автоматически (поллинг)

  @BannerPrintBot ────► POST /api/referral/internal/create
                            (X-Bot-Secret)
```

**Ключевые архитектурные решения:**

Uvicorn строго 1 worker — `token_store` и `order_store` в SQLite без внешней синхронизации. 2 воркера = 2 независимых стора = потерянные токены. Пересматривается при переходе на Redis.

Ghostscript CPU-bound. Прямой вызов из asyncio event loop заблокировал бы весь сервер. Решение: `ProcessPoolExecutor(max_workers=2)`, вызов только через executor в `batch_worker.py`.

`token_store` и `order_store` перенесены из in-memory в SQLite — переживают рестарты.

Рендерер — локальная копия `services/banner_generator.py`, не зависит от бота. Web-контейнер полностью автономен.

Пропорциональный layout — `scale = min(1, safe_h * 0.85 / Σh)`, равные отступы `padding = (safe_h - Σh) / (n+1)`. Работает для 1–6 строк и любого соотношения сторон.

`do_force_token()` вынесена из HTTP-эндпоинта — позволяет вызывать без обхода `Depends(require_admin)`.

---

## Структура репозитория

```
BannerPDF/
├── .github/
│   └── workflows/
│       ├── deploy-web.yml        # Деплой сайта при push в main
│       └── ci-web.yml            # CI: ruff + pytest
│
├── web/                          # Сайт
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   ├── ruff.toml                 # known-first-party=['web']
│   ├── pytest.ini                # testpaths=web/tests, PYTHONPATH=.
│   ├── templates.json            # Размеры, шрифты, цвета — источник для GET /api/templates
│   ├── migrate_add_yookassa.py   # Миграция: yookassa_payment_id + tg_message_id
│   │
│   ├── api/
│   │   ├── main.py               # FastAPI app + lifespan
│   │   ├── db/
│   │   │   ├── __init__.py       # get_db(), init_db()
│   │   │   └── schema.sql        # Схема всех таблиц (включая api_plans с Trial)
│   │   ├── routers/
│   │   │   ├── preview.py        # POST /api/preview
│   │   │   ├── order.py          # FSM + POST /api/order (создаёт платёж ЮКасса)
│   │   │   ├── payment.py        # POST /api/payment/callback (ЮКасса, активный)
│   │   │   ├── payment_selfwork.py  # POST /api/payment/callback (Selfwork, запасной)
│   │   │   ├── download.py       # GET /api/download/{token}
│   │   │   ├── admin.py          # stats|orders|funnel + force_token
│   │   │   ├── referral.py       # Реферальная программа
│   │   │   ├── corp_api.py       # Корп. API (Trial + платные тарифы)
│   │   │   ├── batch.py          # Batch-рендер
│   │   │   └── tg_webhook.py     # POST /api/tg/callback (TG inline-кнопки)
│   │   └── services/
│   │       ├── banner_generator.py  # create_preview_jpeg(), create_final_pdf()
│   │       │                        # Пропорциональный layout (uniform scale)
│   │       ├── renderer.py          # Адаптер: build_render_data()
│   │       ├── sanitizer.py         # sanitize_text_lines(), validate_banner_config()
│   │       ├── config.py            # FONTS, COLORS, BANNER_SIZES
│   │       ├── payment.py           # ЮКасса: create_payment(), verify_yookassa_payment()
│   │       ├── payment_selfwork.py  # Selfwork: compute_init_signature(), verify_selfwork_callback()
│   │       ├── token_store.py       # create_token(), consume_token() — SQLite
│   │       ├── order_store.py       # save_pending(), get_pending() — SQLite
│   │       ├── referral_store.py    # accrue_commission() 15%
│   │       ├── api_key_store.py     # generate_key(), verify_key()
│   │       ├── batch_worker.py      # asyncio.Queue + ProcessPoolExecutor
│   │       └── tg_notify.py         # notify_new_order() → message_id, notify_order_paid()
│   │
│   ├── frontend/
│   │   ├── index.html            # Конструктор + модалка #modal-pay (ЮКасса embedded-виджет)
│   │   ├── style.css             # v7: mobile-first, sticky превью, двухколонка ≥820px
│   │   ├── app.js                # v12: openYooKassaWidget(), поллинг, FONT_CSS_MAP
│   │   └── admin/
│   │       └── index.html        # Adminка: заказы, корп. ключи (Trial бейдж)
│   │
│   ├── nginx/
│   │   └── default.conf          # Server-блоки: 80→443 редирект, 443 HTTPS, 88 TG webhook
│   │                             # ⚠️ nginx.conf и proxy_params НЕ монтировать
│   │
│   └── tests/
│       ├── conftest.py
│       ├── test_fsm.py
│       ├── test_hmac.py
│       ├── test_token_store.py
│       ├── test_order_store.py
│       ├── test_sanitizer.py
│       ├── test_order_api.py
│       ├── test_payment_webhook.py
│       ├── test_preview_api.py
│       └── test_renderer_preview.py
│
└── bot/                          # Telegram-бот (отдельный контейнер)
    └── ...
```

---

## Быстрый старт (локально)

### Требования

- Docker + Docker Compose
- Шрифты TTF: `GolosText-Regular.ttf`, `TenorSans-Regular.ttf`, `FiraSansCondensed-ExtraBold.ttf`, `PTSansNarrow-Bold.ttf`
- ICC-профиль `ISOcoated_v2_300_eci.icc`

> ❌ `FiraSans-Regular.ttf` и `IgraSans-Regular.ttf` на сервере отсутствуют — не использовать.

### 1. Клонирование

```bash
git clone https://github.com/Ale007XD/BannerPDF.git
cd BannerPDF/web
```

### 2. Переменные окружения

```bash
cp .env.example .env
# Заполнить: ADMIN_TOKEN, BOT_INTERNAL_SECRET, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
# TG_NOTIFY_TOKEN, TG_ADMIN_CHAT_ID, TG_WEBHOOK_SECRET — для TG-уведомлений
```

### 3. Подготовка шрифтов и профиля

```bash
mkdir -p fonts profiles
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
```

Сайт: `http://localhost`
Adminка: `http://localhost/admin/`
Swagger: `http://localhost/api/docs`

---

## Деплой на VPS

**VPS:** Hetzner, `bannerweb@host1884433-2`
**Путь на сервере:** `/home/bannerweb/banner_web/`
**URL:** `https://bannerbot.ru`

> ✅ Новый сервер — нет конфликта с amnezia-xray. Nginx слушает стандартные порты 80 и 443.

### Порты docker-compose

| Порт | Сервис |
|---|---|
| **80** | Nginx — редирект на 443 |
| **443** | Nginx — основной сайт (HTTPS) |
| **88** | Nginx → FastAPI — TG webhook |

### Автоматический деплой (GitHub Actions)

При `push` в `main` с изменениями в `web/**` автоматически запускается `.github/workflows/deploy-web.yml`:

1. Копирует файлы на VPS через SCP (`web/` → `/home/bannerweb/banner_web/`)
2. `docker compose build --no-cache api && docker compose up -d --force-recreate --remove-orphans`
3. `docker image prune -f`
4. Health check через `urllib.request` внутри контейнера

**Необходимые секреты в GitHub:**

```
VPS_HOST      — IP сервера
VPS_USER      — bannerweb
VPS_SSH_KEY   — приватный SSH-ключ
```

### Файлы вне репо (монтируются как volume)

```
/home/bannerweb/banner_web/fonts/
  GolosText-Regular.ttf
  TenorSans-Regular.ttf
  FiraSansCondensed-ExtraBold.ttf
  PTSansNarrow-Bold.ttf

/home/bannerweb/banner_web/profiles/
  ISOcoated_v2_300_eci.icc

/home/bannerweb/banner_web/.env   # секреты
```

### SSL-сертификат

```
Домен:    bannerbot.ru
Cert:     /etc/letsencrypt/live/bannerbot.ru/fullchain.pem
Key:      /etc/letsencrypt/live/bannerbot.ru/privkey.pem
Истекает: 2026-06-19
```

### Перезагрузка Nginx без даунтайма

```bash
docker exec bannerprint_nginx nginx -t
docker exec bannerprint_nginx nginx -s reload
```

---

## Переменные окружения

| Переменная | Описание | Примечание |
|---|---|---|
| `YOOKASSA_SHOP_ID` | ID магазина ЮКасса (числовой) | тест: числовой ID из ЛК |
| `YOOKASSA_SECRET_KEY` | Секретный ключ ЮКасса | тест: `test_...` |
| `SITE_BASE_URL` | Базовый URL сайта | `https://bannerbot.ru` |
| `SITE_PDF_PRICE` | Цена PDF в рублях (читать из env, не хардкодить) | `299` |
| `ADMIN_TOKEN` | Bearer-токен для `/api/admin/*` и `/api/v1/admin/*` | 32 байта |
| `BOT_INTERNAL_SECRET` | Секрет для внутреннего API бота | |
| `TG_NOTIFY_TOKEN` | Токен `@BannerBotInfo_bot` (сигнальный бот) | |
| `TG_ADMIN_CHAT_ID` | chat_id администратора | `195351142` |
| `TG_WEBHOOK_SECRET` | Секрет TG webhook (только A-Z a-z 0-9 - _) | `token_hex(32)` |
| `ALLOWED_ORIGINS` | CORS origins | `https://bannerbot.ru` |
| `WEB_DB_PATH` | Путь к SQLite БД | `/app/data/banner_web.db` |
| `FONTS_DIR` | Директория с TTF-шрифтами | `/app/fonts` |
| `BATCH_DIR` | Временная директория для batch ZIP | `/tmp/bannerprint_batches` |
| `UVICORN_WORKERS` | Количество воркеров (строго 1) | `1` |

> Selfwork-переменные (`SELFWORK_SHOP_ID`, `SELFWORK_API_KEY`) нужны только при переключении на запасной провайдер.

---

## API

### Публичные эндпоинты

```
GET  /api/health                   — Healthcheck
GET  /api/templates                — Размеры, шрифты, цвета
POST /api/preview                  — JPEG-превью баннера (30 RPM/IP)
POST /api/order                    — Создание заказа → {order_id, confirmation_token, amount_rub, payment_id}
GET  /api/payment/status/{id}      — Статус заказа (поллинг)
POST /api/payment/callback         — Webhook от ЮКасса (verify_yookassa_payment() ПЕРВЫМ)
GET  /api/download/{token}         — Скачать PDF (10 RPM/IP, одноразовый токен TTL 15 мин)
GET  /api/referral/stats/{code}    — Статистика реферального кода
```

### TG Webhook

```
POST /api/tg/callback              — TG inline-кнопки (X-Telegram-Bot-Api-Secret-Token)
```

### Корп. API

```
POST /api/v1/render                — Рендер PDF; X-PDF-Used, X-PDF-Limit в headers
GET  /api/v1/usage                 — Лимиты, счётчики, is_trial
POST /api/v1/batch/submit          — Batch-рендер из CSV
GET  /api/v1/batch/{id}            — Статус задачи
GET  /api/v1/batch/{id}/download   — Скачать ZIP
```

### Внутренние (только бот)

```
POST /api/referral/internal/create  — Создание реф. кода (X-Bot-Secret)
```

### Административные

```
GET        /api/admin/stats                  — Общая статистика
GET        /api/admin/orders                 — Список заказов с пагинацией
GET        /api/admin/funnel                 — Воронка конверсии
POST       /api/admin/force_token/{order_id} — Ручная выдача PDF-токена
POST/GET/DELETE /api/v1/admin/keys           — Управление корп. ключами
GET        /api/referral/admin/list          — Список рефереров
POST       /api/referral/admin/payout/{code} — Вывод баланса
```

Все admin-эндпоинты: `Authorization: Bearer <ADMIN_TOKEN>`.
Интерактивная документация: `https://bannerbot.ru/api/docs`

### Пример: создание заказа

```bash
curl -X POST https://bannerbot.ru/api/order \
  -H "Content-Type: application/json" \
  -d '{
    "size_key": "3x2",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [
      {"text": "ЛЕТНЯЯ РАСПРОДАЖА", "scale": 100},
      {"text": "Скидки до 70%", "scale": 70}
    ]
  }'
# {"order_id": "uuid4...", "confirmation_token": "...", "amount_rub": 299, "payment_id": "..."}
```

> `text_lines[].scale` — целое число от 50 до 100 (100 = полный размер, 50 = половина).

---

## База данных

SQLite WAL-режим, файл `banner_web.db`. Инициализируется автоматически через `schema.sql`. Хранится в named volume `bannerprint_data`.

```sql
web_orders(
  id TEXT PRIMARY KEY,              -- UUID4
  amount_rub INT, size_key TEXT, ref_code TEXT,
  config_json TEXT,                 -- конфиг баннера, постоянное хранение
  status TEXT,                      -- pending|paid|token_issued|expired
  created_at TEXT, paid_at TEXT,
  yookassa_payment_id TEXT,         -- ID платежа в ЮКасса
  tg_message_id INTEGER             -- message_id в TG для editMessageText
)

download_tokens(token, order_id, expires_at, used)  -- TTL 15 мин
pending_orders(order_id, config_json, expires_at)   -- TTL 30 мин
api_plans(id, name, pdf_limit, rpm_limit, price_rub)
api_keys(id, key_hash, key_prefix, plan_id, label, email, active, pdf_used, period_start)
batch_jobs(id, api_key_id, status, total, done, errors_json, expires_at)  -- TTL 1 ч
referrers(ref_code, balance_rub, created_at)
referrals(id, referrer_id, order_id, order_amount, commission, ...)
```

### Миграции

```bash
# Колонки yookassa_payment_id и tg_message_id (если БД создана до миграции):
docker compose exec api python -c "
from api.db import get_db
with get_db() as conn:
    for col in ['yookassa_payment_id TEXT', 'tg_message_id INTEGER']:
        try:
            conn.execute(f'ALTER TABLE web_orders ADD COLUMN {col}')
            print(f'OK: {col}')
        except Exception as e:
            print(f'Skip: {e}')
"

# Тариф Trial (если БД создана до добавления плана):
docker compose exec -T api python << 'EOF'
import sys; sys.path.insert(0, '/app')
from api.db import get_db
with get_db() as conn:
    conn.execute("INSERT OR IGNORE INTO api_plans (id, name, pdf_limit, rpm_limit, price_rub) VALUES ('trial', 'Trial', 3, 5, 0)")
    row = conn.execute("SELECT * FROM api_plans WHERE id = 'trial'").fetchone()
    print('OK:', dict(row))
EOF
```

---

## FSM заказов

```
create_order  ──►  PENDING
                      │
         webhook_paid / force_token
                      │
                      ▼
                    PAID
                      │
                issue_token
                      │
                      ▼
               TOKEN_ISSUED

PENDING ──[ttl_expired]──► EXPIRED
PAID    ──[ttl_expired]──► EXPIRED
```

Все переходы — только через `transition(order_id, event)`. Прямой `UPDATE status` в обход FSM запрещён. `force_token` делает оба перехода последовательно: `PENDING→PAID→TOKEN_ISSUED`.

---

## Авторизация

| Уровень | Заголовок | Эндпоинты |
|---|---|---|
| Admin | `Authorization: Bearer <ADMIN_TOKEN>` | `/api/admin/*`, `/api/v1/admin/*` |
| Bot internal | `X-Bot-Secret: <BOT_INTERNAL_SECRET>` | `/api/referral/internal/*` |
| Corp API | `Authorization: Bearer bp_live_<32b base64url>` | `/api/v1/*` |
| ЮКасса webhook | Верификация через GET `/v3/payments/{id}` | `/api/payment/callback` |
| TG webhook | `X-Telegram-Bot-Api-Secret-Token` | `/api/tg/callback` |

**Форматы:**

```
order_id:   UUID4  (36 символов)
token:      32 bytes hex  (64 символа)
ref_code:   8 символов A-Z0-9
api_key:    bp_live_<32 chars base64url>
```

---

## Флоу оплаты (текущий)

ЮКасса embedded-виджет — полностью автоматический флоу, боевые ключи установлены.

```
1. Клиент настраивает баннер → нажимает «Получить PDF»
2. POST /api/order → бэкенд создаёт платёж в ЮКасса → {confirmation_token, order_id}
3. Фронтенд открывает модалку #modal-pay → YooMoneyCheckoutWidget(confirmation_token)
4. Клиент оплачивает внутри виджета
5. ЮКасса → POST /api/payment/callback
6. Бэкенд: verify_yookassa_payment() (GET /v3/payments/{id}) → FSM: PENDING→PAID→TOKEN_ISSUED
7. notify_order_paid() → editMessageText в TG (@BannerBotInfo_bot)
8. Поллинг клиента ловит token_issued → PDF скачивается автоматически
```

**Тестовые карты ЮКасса:**

| Карта | Результат |
|---|---|
| `5555 5555 5555 4477` | Успешная оплата |
| `5555 5555 5555 4592` | Отказ |

**Переключение на Selfwork (запасной провайдер):**

```python
# routers/order.py:
from ..services.payment_selfwork import create_payment
# main.py:
from .routers import payment_selfwork as payment
# .env:
SELFWORK_SHOP_ID=...
SELFWORK_API_KEY=...
```

---

## Реферальная программа

**Механика:** 15% от суммы заказа начисляется на баланс реферера. Покупатель платит полную цену.

```
Сайт хранит:   ref_code (8 A-Z0-9) + balance_rub — не ПД
Бот хранит:    tg_id → ref_code в banner_bot.db
Сайт не знает tg_id владельца кода — намеренно (152-ФЗ)
```

Создание реф. кода — только через `POST /api/referral/internal/create` (X-Bot-Secret).

**Команды бота (P1):** `/referral`, `/balance`, `/payout`

---

## Корп. API тарифы

| План | PDF | RPM | Цена | Особенность |
|---|---|---|---|---|
| **trial** | 3 | 5 | 0 ₽ | Пожизненный лимит, не сбрасывается. HTTP 402 при исчерпании |
| starter | 100/мес | 10 | 1 900 ₽ | — |
| business | 1 000/мес | 60 | 9 900 ₽ | — |
| enterprise | ∞ | 300 | договор | `pdf_limit = -1` |

**Trial-специфика:**
- `pdf_used` не сбрасывается — `api_key_store.py` не реализует сброс (намеренно)
- При исчерпании → HTTP **402** (не 429) — «Перейдите на платный тариф»
- `GET /api/v1/usage` → `is_trial: true`, `period_start/end: null`
- Выдача — только через adminку `POST /api/v1/admin/keys`
- Бейдж в adminке — зелёный (`.key-plan--trial { background: #e8f5e8; color: #1a6e1a; }`)

**Создать trial-ключ:**

```bash
curl -X POST https://bannerbot.ru/api/v1/admin/keys \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan_id":"trial","label":"Имя","email":"email@example.com"}'
```

---

## Тесты

```bash
PYTHONPATH=. pytest web/tests/ -v

# Только юнит-тесты (без HTTP)
PYTHONPATH=. pytest web/tests/test_fsm.py web/tests/test_hmac.py -v
```

| Файл | Что проверяет |
|---|---|
| `test_fsm.py` | Все переходы FSM, guard, идемпотентность |
| `test_hmac.py` | SHA256 верификация webhook |
| `test_token_store.py` | create/consume/cleanup токенов |
| `test_order_store.py` | save/get/delete/cleanup pending_orders |
| `test_sanitizer.py` | sanitize_line, validate_banner_config |
| `test_order_api.py` | HTTP: POST /api/order, GET /api/payment/status |
| `test_payment_webhook.py` | HTTP: webhook, FSM через HTTP |
| `test_preview_api.py` | HTTP: POST /api/preview, size_key, кастомный размер |
| `test_renderer_preview.py` | build_render_data, executor lifecycle |

GS замокан в `conftest.py` — тесты работают без Ghostscript и Docker.

---

## Nginx и rate limits

| Зона | Лимит | Burst | Эндпоинт |
|---|---|---|---|
| `preview_limit` | 30 req/min/IP | 5 | `POST /api/preview` |
| `order_limit` | 5 req/min/IP | 2 | `POST /api/order` |
| `download_limit` | 10 req/min/IP | 3 | `GET /api/download/*` |

`/admin/` обслуживается отдельным `location /admin/` с `alias /app/frontend/admin/`. Rate limit не применяется — защита на уровне `ADMIN_TOKEN`.

> ❌ `proxy_params` не использовать — вызывает конфликт директив.
> ❌ `listen 443 ssl http2` → использовать `listen 443 ssl;` (http2 добавляется отдельно позже).
> ❌ nginx монтирует только `nginx/default.conf` → `/etc/nginx/conf.d/default.conf`.

---

## 152-ФЗ

Сервис не собирает и не обрабатывает персональные данные пользователей.

| | |
|---|---|
| **Не хранится** | email, имя, tg_id, IP-адреса |
| **Хранится** | `order_id` (UUID), `size_key`, `ref_code`, `token` (hex), `amount_rub`, `config_json`, временные метки — не являются ПД |
| **Исключение** | `email` в `api_keys` — основание: исполнение договора |
| **VPS** | Hetzner, Германия |
| **Формулировка** | «не собираем и не обрабатываем» (не «152-ФЗ не применяется») |

---

## Pending tasks

### ✅ Выполнено

- [x] FSM `OrderStatus(Enum)` + `transition()` + guard
- [x] `token_store` → SQLite `download_tokens`
- [x] `order_store` → SQLite `pending_orders`
- [x] Lifespan cleanup просроченных токенов и заказов
- [x] Автодеплой через GitHub Actions + health check
- [x] SSL (bannerbot.ru, истекает 2026-06-19)
- [x] Переезд на новый сервер `bannerweb@host1884433-2`, nginx на стандартных портах 80/443
- [x] Фронтенд: динамический рендер из `/api/templates`, шрифты в кнопках
- [x] Фронтенд: FAB + sticky-бар превью на мобиле, двухколонка на десктопе
- [x] Фронтенд: кастомный размер 100–3000 мм, защита от совпадения цветов
- [x] Фронтенд: футер (Соглашение, Конфиденциальность, Связаться)
- [x] `banner_generator.py`: пропорциональный layout (uniform scale), PDF y_pos через ReportLab
- [x] `banner_generator.py`: вотермарка на JPEG-превью
- [x] Переключение на ЮКасса: `services/payment.py` + `routers/payment.py`
- [x] Selfwork сохранён как запасной провайдер (`*_selfwork.py`)
- [x] `app.js` v12: модалка `#modal-pay` с виджетом ЮКасса, очистка контейнера
- [x] E2E флоу: превью → заказ → виджет ЮКасса → webhook → PDF скачан ✅
- [x] TG-уведомления о новых заказах (`@BannerBotInfo_bot` + inline-кнопка «Выдать PDF»)
- [x] TG webhook зарегистрирован на порту 88
- [x] `do_force_token()` вынесена — вызывается из TG и из adminки
- [x] `tg_message_id` в `web_orders`: после webhook редактируется TG-сообщение («💳 Оплачено»)
- [x] Миграция БД: колонки `yookassa_payment_id` и `tg_message_id`
- [x] Тариф **Trial**: `api_plans`, `corp_api.py` (HTTP 402), `admin/index.html` (бейдж + select)
- [x] `scale` int (50–100) в API, float (0–1) в рендерере — конвертация в роутерах
- [x] Боевые ключи ЮКасса установлены — автоматический флоу оплаты полностью работает
- [x] TG-сообщение о заказе меняет статус на «💳 Оплачено» после webhook

### P1 — боевые ключи ЮКасса

- [x] Получить боевые `YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY`
- [x] Обновить `.env` → `docker compose restart api`
- [x] Подтвердить webhook URL в ЛК ЮКасса: `https://bannerbot.ru/api/payment/callback`
- [x] E2E боевой флоу: оплата → webhook → TG «Оплачено» → PDF скачан ✅

### P1 — бот и реферальная программа

- [ ] Бот: таблица `referral_codes(tg_id, ref_code, created_at)` в `banner_bot.db`
- [ ] Бот: команды `/referral`, `/balance`, `/payout`

### P1 — тесты

- [ ] `test_corp_render.py` (verify_key, лимиты, HTTP 429/402, trial исчерпан)
- [ ] `test_batch.py` (submit → статус → download)
- [ ] `stress_test_preview.py` + `.github/workflows/stress_test_preview.yml`

### P2 — улучшения

- [ ] Nginx: `http2 on;`
- [ ] Preview → `image/jpeg` streaming вместо base64 (+33% overhead)
- [ ] Cleanup `/tmp/bannerprint_batches` старше TTL
- [ ] Structured JSON logging
- [ ] Перенос домена на `bannerprintbot.ru` + порт 443
- [ ] Postman-коллекция из `/api/docs`
