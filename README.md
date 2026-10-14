# BannerBot — сайт-конструктор печатных баннеров

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
- [Web Push и PWA админки](#web-push-и-pwa-админки)
- [Telegram через Cloudflare Worker](#telegram-через-cloudflare-worker)
- [152-ФЗ](#152-фз)
- [Pending tasks](#pending-tasks)

---

## Статус проекта

| | |
|---|---|
| Готовность | **99%** — сайт запущен, принимает боевые платежи |
| URL | `https://bannerbot.ru` |
| Что готово | Весь бэкенд (P0 закрыты), фронтенд (превью, кастомный размер, шрифты в кнопках, ЮКасса-виджет, промокоды), FSM заказов, E2E флоу оплаты (ЮКасса → webhook → PDF) + бесплатный флоу по промокоду, TG-уведомления (`@BannerBotInfo_bot`), корп. API с тарифом Trial, adminка, боевые ключи ЮКасса |
| Что осталось | Реферальные команды бота · тесты корп. API |

---

## Стек

| Компонент | Технология |
|---|---|
| Backend | FastAPI 0.110+ + Uvicorn (строго 1 worker) |
| Рендер превью | Pillow 10 (JPEG, синхронно через ThreadPoolExecutor) |
| Рендер PDF | ReportLab → Ghostscript 10 (чистый DeviceCMYK без профиля, шрифты в кривых, масштаб 1:1) |
| Executor | `ProcessPoolExecutor(max_workers=2)` — только для GS в batch_worker |
| Frontend | Vanilla HTML/CSS/JS, без фреймворков и сборки |
| База данных | SQLite WAL — `banner_web.db` (отдельная от `banner_bot.db`) |
| Прокси | Nginx 1.25 в Docker — реверс-прокси + статика + rate limits, порты 80/443 |
| Деплой | GitHub Actions → SCP → Docker Compose |
| Оплата | ЮКасса — embedded-виджет, верификация через GET `/v3/payments/{id}` |
| Бот | python-telegram-bot 21 (async), Python 3.11+ |

---

## Архитектура

```
                        ┌──────────────────────────────────────┐
  Браузер               │  Nginx (Docker, порты 80/443)        │
  ──────────────────►   │  • Статика /app/frontend              │
                        │  • Статика /app/frontend/admin/       │
                        │  • Rate limits (preview/order/dl)     │
                        │  • TLS (Let's Encrypt, :443)          │
                        │  • Редирект 80 → https://$host        │
                        │  • /api/ → FastAPI (в т.ч. TG webhook)│
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
│   │   │   ├── payment.py        # POST /api/payment/callback (ЮКасса)
│   │   │   ├── download.py       # GET /api/download/{token}
│   │   │   ├── admin.py          # stats|orders|funnel + force_token + order audit + промокоды (выдача, список, отчёт)
│   │   │   ├── promo.py          # POST /api/promo/check — скидка и цена по коду без списания
│   │   │   ├── push.py           # /api/admin/push/* — подписка устройств админа (Web Push)
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
│   │       ├── token_store.py       # create_token(), consume_token() — SQLite
│   │       ├── order_store.py       # save_pending(), get_pending() — SQLite
│   │       ├── push_notify.py       # Web Push (VAPID) админу: push_new_order(), push_order_paid()
│   │       ├── promo_store.py       # промокоды: reserve() атомарно, calc_amount(), release_*, create_promo()
│   │       ├── referral_store.py    # accrue_commission() 15%
│   │       ├── api_key_store.py     # generate_key(), verify_key()
│   │       ├── batch_worker.py      # asyncio.Queue + ProcessPoolExecutor
│   │       └── tg_notify.py         # notify_new_order() → message_id, notify_order_paid()
│   │
│   ├── frontend/
│   │   ├── index.html            # Конструктор + модалка #modal-pay (ЮКасса embedded-виджет)
│   │   ├── style.css             # v7: mobile-first, sticky превью, двухколонка ≥820px
│   │   ├── app.js                # v13: openYooKassaWidget(), поллинг, FONT_CSS_MAP, промокоды
│   │   └── admin/
│   │       ├── index.html        # Adminка: заказы, корп. ключи (Trial бейдж), карточка «Уведомления»
│   │       ├── sw.js             # Service worker: только Web Push, без кэша
│   │       ├── manifest.json     # PWA: установка на экран «Домой»
│   │       └── icons/            # 192/512 + maskable 512
│   │
│   ├── nginx/
│   │   └── default.conf          # Server-блоки: 80→443 редирект, 443 HTTPS (включая /api/tg/callback)
│   │                             # ⚠️ nginx.conf и proxy_params НЕ монтировать
│   │
│   ├── tools/
│   │   └── cf-tg-relay.js        # Cloudflare Worker: релей Telegram ⇄ сайт (не входит в образ)
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
- Шрифты TTF: `GolosText-Bold.ttf`, `TenorSans-Regular.ttf`, `FiraSansCondensed-ExtraBold.ttf`, `PTSansNarrow-Bold.ttf`, `Caveat-Bold.ttf` (OFL, лицензия `OFL-Caveat.txt`)

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

### 3. Подготовка шрифтов

```bash
mkdir -p fonts
# Скопировать TTF-шрифты в fonts/
# ICC-профиль не нужен: PDF пишется чистым DeviceCMYK без встроенного профиля
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

**VPS:** Linux-сервер с Docker Compose (адрес и пользователь хранятся в секретах GitHub, см. ниже)
**Путь на сервере:** `<DEPLOY_PATH>` (задан в `.github/workflows/deploy-web.yml`)
**URL:** `https://bannerbot.ru`

> Nginx слушает стандартные порты 80 и 443. Webhook Telegram приходит не на отдельный порт, а через Worker Cloudflare на `/api/tg/callback`
> (см. [Telegram через Cloudflare Worker](#telegram-через-cloudflare-worker)).

### Порты docker-compose

| Порт | Сервис |
|---|---|
| **80** | Nginx — редирект на 443 |
| **443** | Nginx — основной сайт (HTTPS) |

### Автоматический деплой (GitHub Actions)

При `push` в `main` с изменениями в `web/**` автоматически запускается `.github/workflows/deploy-web.yml`:

1. Копирует файлы на VPS через SCP (`web/` → `<DEPLOY_PATH>/`)
2. `docker compose build --no-cache api && docker compose up -d --force-recreate --remove-orphans`
3. `docker image prune -f`
4. Health check через `urllib.request` внутри контейнера

**Необходимые секреты в GitHub:**

```
VPS_HOST      — IP сервера
VPS_USER      — пользователь для SSH
VPS_SSH_KEY   — приватный SSH-ключ
```

### Файлы вне репо (монтируются как volume)

```
<DEPLOY_PATH>/fonts/
  GolosText-Bold.ttf
  TenorSans-Regular.ttf
  FiraSansCondensed-ExtraBold.ttf
  PTSansNarrow-Bold.ttf

<DEPLOY_PATH>/.env   # секреты
```

### SSL-сертификат

```
Домен:    bannerbot.ru
Cert:     /etc/letsencrypt/live/bannerbot.ru/fullchain.pem
Key:      /etc/letsencrypt/live/bannerbot.ru/privkey.pem
Истекает: 2026-12-xx
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
| `TG_ADMIN_CHAT_ID` | chat_id администратора | числовой ID чата, например `123456789` |
| `TG_WEBHOOK_SECRET` | Секрет TG webhook (только A-Z a-z 0-9 - _) | `token_hex(32)` |
| `TG_API_BASE` | Адрес Bot API, если `api.telegram.org` недоступен с сервера (свой релей) | `https://api.telegram.org` |
| `TG_PROXY_URL` | HTTP(S)-прокси для запросов к Telegram (необязательно; для `socks5://` нужен `httpx[socks]`) | `http://user:pass@host:3128` |
| `ALLOWED_ORIGINS` | CORS origins | `https://bannerbot.ru` |
| `WEB_DB_PATH` | Путь к SQLite БД | `/app/data/banner_web.db` |
| `FONTS_DIR` | Директория с TTF-шрифтами | `/app/fonts` |
| `BATCH_DIR` | Временная директория для batch ZIP | `/tmp/bannerprint_batches` |
| `UVICORN_WORKERS` | Количество воркеров (строго 1) | `1` |
| `VAPID_PRIVATE_KEY` | Приватный ключ Web Push (VAPID). Пусто — push в админку выключен | `genkey`, см. «Web Push» |
| `VAPID_SUBJECT` | Контакт в VAPID-токене (`mailto:` или `https://`) | по умолчанию `SITE_BASE_URL` |

---

## API

### Публичные эндпоинты

```
GET  /api/health                   — Healthcheck
GET  /api/templates                — Размеры, шрифты, цвета
POST /api/preview                  — JPEG-превью баннера (30 RPM/IP, burst 10; при 429 сайт повторяет запрос сам)
POST /api/order                    — Создание заказа → {order_id, confirmation_token, amount_rub, payment_id} (5 RPM/IP, burst 2)
POST /api/order/{id}/abandon       — Заказ не оплачен (окно оплаты закрыли): вернуть использование промокода → {released: bool}
POST /api/promo/check              — Проверка промокода → {valid, discount, price, final_price, free} | {valid:false, message}
GET  /api/payment/status/{id}      — Статус заказа (поллинг)
POST /api/payment/callback         — Webhook от ЮКасса (verify_yookassa_payment() ПЕРВЫМ)
GET  /api/download/{token}         — Скачать PDF (10 RPM/IP, burst 3, одноразовый токен TTL 15 мин; 429 токен не расходует)
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
POST       /api/admin/promo                  — Выдать промокод {discount 1–100, uses 1–1000, code?, expires_in_days?, note?}
GET        /api/admin/promo                  — Список промокодов: остаток, оплачено, статус
GET        /api/admin/promo/report           — Отчёт: итоги, рубли скидок, неиспользованные коды, заказы с кодом ?code=&limit=1–200 (50)
POST       /api/admin/promo/{code}/active    — Включить или отключить промокод {active}
GET        /api/admin/push/config            — Публичный VAPID-ключ для подписки устройства
POST       /api/admin/push/subscribe         — Сохранить подписку устройства (PushSubscription)
POST       /api/admin/push/unsubscribe       — Удалить подписку по endpoint
POST       /api/admin/push/test              — Проверочное уведомление на все устройства
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
  promo_code TEXT,                  -- применённый промокод (NULL если не использован)
  config_json TEXT,                 -- конфиг баннера, постоянное хранение
  status TEXT,                      -- pending|paid|token_issued|expired
  created_at TEXT, paid_at TEXT,
  yookassa_payment_id TEXT,         -- ID платежа в ЮКасса
  tg_message_id INTEGER,            -- message_id в TG для editMessageText
  offer_version TEXT, accepted_at TEXT,  -- акцепт соглашения на экране проверки
  accepted_ip TEXT, accepted_ua TEXT,    -- IP (X-Real-IP) и User-Agent (≤300 симв.) в момент акцепта
  amended_at TEXT, original_config_json TEXT, -- единственная бесплатная правка текста
  promo_released_at TEXT,           -- использование промокода возвращено (не оплачен / платёж не создан); NULL — удерживается
  discount_rub INTEGER              -- скидка заказа в рублях: цена сайта на момент заказа − amount_rub; NULL у заказов до отчёта
)

download_tokens(token, order_id, expires_at, used)  -- TTL 15 мин
pending_orders(order_id, config_json, expires_at)   -- TTL 30 мин
promo_codes(code, uses_left, expires_at, discount, uses_total, created_at, note, active)
                                                     -- discount: % скидки, 100 → бесплатно; uses_left списывается
                                                     -- при создании заказа (любая скидка), возвращается, если платёж не создан
api_plans(id, name, pdf_limit, rpm_limit, price_rub)
api_keys(id, key_hash, key_prefix, plan_id, label, email, active, pdf_used, period_start)
batch_jobs(id, api_key_id, status, total, done, errors_json, expires_at, charged_at)  -- TTL 1 ч; лимит списывается один раз при первой выдаче ZIP
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

# Промокоды (добавлены 2026-03-28):
docker compose exec -T api python << 'EOF'
from api.db import get_db
with get_db() as conn:
    for stmt in [
        'ALTER TABLE web_orders ADD COLUMN promo_code TEXT',
        'CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, uses_left INTEGER NOT NULL DEFAULT 0, expires_at TEXT, discount INTEGER NOT NULL DEFAULT 100)',
    ]:
        try:
            conn.execute(stmt)
            print('OK:', stmt[:60])
        except Exception as e:
            print('Skip:', e)
EOF

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
Платный флоу:
1. Клиент настраивает баннер → нажимает «Получить PDF»
2. POST /api/order → бэкенд создаёт платёж в ЮКасса → {confirmation_token, order_id}
3. Фронтенд открывает модалку #modal-pay → YooMoneyCheckoutWidget(confirmation_token)
4. Клиент оплачивает внутри виджета
5. ЮКасса → POST /api/payment/callback
6. Бэкенд: verify_yookassa_payment() (GET /v3/payments/{id}) → FSM: PENDING→PAID→TOKEN_ISSUED
7. notify_order_paid() → editMessageText в TG (@BannerBotInfo_bot)
8. Поллинг клиента ловит token_issued → PDF скачивается автоматически

Бесплатный флоу (промокод discount=100):
1. Клиент вводит промокод → сайт проверяет его (POST /api/promo/check), кнопка становится «Получить PDF бесплатно»
2. POST /api/order → бэкенд атомарно резервирует использование (uses_left--)
3. FSM: PENDING→PAID→TOKEN_ISSUED без ЮКасса
4. Ответ: {free: true, download_token: "..."}
5. Фронтенд показывает модалку успеха → PDF скачивается мгновенно
6. TG: «✅ PDF выдан автоматически по промокоду XXXX»
```

**Тестовые карты ЮКасса:**

| Карта | Результат |
|---|---|
| `5555 5555 5555 4477` | Успешная оплата |
| `5555 5555 5555 4592` | Отказ |

---

## Промокоды

Промокоды дают скидку от 1 до 100% — для маркетинговых кампаний, партнёров и подарков. Код выдаётся из админки (вкладка «Промокоды»).

**Механика:**
- Пока человек вводит код, сайт проверяет его через `POST /api/promo/check` (ничего не списывает) и показывает цену со скидкой: «~~299 ₽~~ 209 ₽». Лимит запросов — `promo_limit` в nginx.
- Фронт передаёт `promo_code` в `POST /api/order`. Бэкенд **атомарно** резервирует использование одним `UPDATE … WHERE uses_left > 0 AND active = 1 AND (срок не истёк)`: параллельные запросы не применят последнее использование дважды (`services/promo_store.py`).
- Скидка меньше 100% → заказ идёт в ЮКасса с уменьшенной суммой (`amount = price * (100 − discount) // 100`, не меньше 1 ₽).
- `discount=100` → `amount_rub=0`, ЮКасса не вызывается, FSM сразу `pending→paid→token_issued`, PDF выдаётся мгновенно.
- Использование возвращается, если заказ не будет оплачен (идемпотентно, метка `web_orders.promo_released_at`, только для заказов в статусе `pending`):
  - платёж в ЮКасса создать не удалось (`create_order`);
  - человек закрыл окно оплаты, платёж отклонён, форма оплаты не открылась: сайт вызывает `POST /api/order/{id}/abandon` и после него заново проверяет код, чтобы цена на экране соответствовала состоянию кода;
  - заказ остался неоплаченным `RESERVE_TTL_MINUTES` (60) минут: возврат делает фоновый cleanup (`promo_store.release_stale`, раз в 5 минут). Если запрос abandon не дошёл (вкладку закрыли, пропала сеть), сработает он.
- Оплата пришла по заказу, у которого использование уже вернули: FSM при переходе в `paid` берёт его обратно (`promo_store.reclaim_for_order`), PDF выдаётся как обычно. Если лимит кода за это время исчерпан, заказ всё равно остаётся оплаченным (деньги получены), в лог пишется предупреждение; так возможен один заказ сверх лимита на каждый открытый и брошенный заказ.
- 60 минут выбраны нами: ЮKassa не называет срок жизни платежа с `confirmation_token`. Ошибка в меньшую сторону безопасна: поздняя оплата берёт использование обратно.
- В админке «ожидают оплаты» считает только заказы, которые ещё удерживают использование.
- TG и Web Push: для бесплатного заказа «✅ PDF выдан автоматически», для оплачиваемого — код и «ожидает оплаты».

**Выдача из админки** (`/admin/`, вкладка «Промокоды»; API: `POST/GET /api/admin/promo`, `POST /api/admin/promo/{code}/active`):
скидка 1–100%, применений 1–1000 (быстрые значения 1 / 3 / 5), свой код или автогенерация (8 символов без `0/O/1/I`), срок (7 / 30 / 90 дней или без срока), заметка «кому выдан». В списке — остаток «N из M», оплачено, ожидают оплаты, статус (действует / исчерпан / истёк / отключён). Код можно отключить и снова включить.

**Отчёт** (вкладка «Промокоды» → «Отчёт»; API: `GET /api/admin/promo/report?code=&limit=`). Отвечает на три вопроса:
- *Сколько рублей ушло на скидки.* Плитки «скидок, ₽» и «выручка по ним, ₽» считаются только по оплаченным и бесплатным заказам (`paid` / `token_issued`). Заказы, ожидающие оплаты, и вернувшиеся (окно оплаты закрыли, прошёл час) в рубли не входят. Скидка заказа записывается в `web_orders.discount_rub` при его создании (цена сайта на тот момент минус сумма к оплате; у бесплатного заказа это вся цена), поэтому смена `SITE_PDF_PRICE` прошлые цифры не меняет. У заказов, оформленных до появления колонки, она `NULL`: для них скидка берётся как текущая цена минус сумма заказа, в отчёте такие суммы помечены «≈» и поясняются (`totals.estimated`).
- *Какие коды не использованы.* Код считается неиспользованным, если по нему нет ни оплаты, ни заказа, который сейчас держит использование. Сверху — те, что ещё можно раздать (нажатие копирует код), серым — истёкшие, отключённые и исчерпанные. Код с заказом, который ждёт оплаты, неиспользованным не считается.
- *Кто и когда применил.* Заказы анонимны (аккаунтов нет), поэтому «кто» — это заказ: короткий номер и полный id, размер, реферальный код, время оформления и оплаты, состояние (оплачен / бесплатно / ждёт оплаты / не оплачен). Кнопка «Заказы с этим кодом» оставляет в списке один код, «Показать ещё» добавляет по 50 (до 200). IP и User-Agent в отчёт не попадают; для спора по конкретному заказу они есть в аудите (`GET /api/admin/order/{id}/audit`).

**Вручную (запасной способ, если админка недоступна):**

**Генерация одноразовых промокодов:**

```bash
# Сгенерировать 10 одноразовых промокодов (uses_left=1, discount=100, бессрочные)
docker compose exec -T api python << 'EOF'
import secrets, string
from api.db import get_db

def gen_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

with get_db() as conn:
    for _ in range(10):
        code = gen_code()
        conn.execute(
            'INSERT INTO promo_codes (code, uses_left, expires_at, discount) VALUES (?, 1, NULL, 100)',
            (code,)
        )
        print(code)
EOF
```

Результат — 10 кодов вида `X7K2BN9Q`. Каждый код работает только **один раз**. Раздавай по одному каждому клиенту.

**Управление промокодами:**

```bash
# Просмотр всех промокодов и остатков
docker compose exec -T api python << 'EOF'
from api.db import get_db
with get_db() as conn:
    rows = conn.execute('SELECT code, uses_left, expires_at, discount FROM promo_codes ORDER BY rowid').fetchall()
    for r in rows:
        print(dict(r))
EOF

# Изменить лимит существующего кода
docker compose exec -T api python << 'EOF'
from api.db import get_db
with get_db() as conn:
    conn.execute("UPDATE promo_codes SET uses_left = 50 WHERE code = 'AVITO26'")
    print('OK')
EOF
```

**Поля таблицы `promo_codes`:**

| Поле | Описание |
|---|---|
| `code` | Код (A-Z0-9, Primary Key) |
| `uses_left` | Остаток использований; при 0 — «Промокод исчерпан» |
| `expires_at` | Срок действия ISO 8601, `NULL` = бессрочно |
| `discount` | Скидка %, `100` = полностью бесплатно |
| `uses_total` | Выданный лимит («осталось N из M»); `NULL` у кодов, добавленных вручную |
| `created_at` | Когда выдан; `NULL` у кодов, добавленных вручную |
| `note` | Кому или зачем выдан (видно только в админке) |
| `active` | `0` = отключён из админки |

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
| `preview_limit` | 30 req/min/IP | 10 | `POST /api/preview` (правки макета идут подряд; при 429 сайт сам повторяет запрос) |
| `order_limit` | 5 req/min/IP | 2 | `POST /api/order` |
| `download_limit` | 10 req/min/IP | 3 | `GET /api/download/*` |
| `promo_limit` | 20 req/min/IP | 10 | `POST /api/promo/check` (проверка кода при вводе; защита от перебора) |

> Все четыре зоны применены в `default.conf` точными `location` до общего `/api/`: `location = /api/preview`, `location = /api/order`, `location = /api/promo/check` и префиксный `location /api/download/`. Точный или более длинный location не наследует настройки `/api/`, поэтому прокси-заголовки в каждом повторены целиком (тест сверяет их с общим блоком).
>
> Зоны не распространяются на `/api/order/{id}/amend`, `/api/order/{id}/abandon` (он возвращает использование промокода, его нельзя душить), `/api/payment/*`, `/api/v1/*` (корпоративный API и batch считаются по ключам, а не по IP) и админку.
>
> Превышение лимита даёт `429`, ответ HTML от nginx. Сайт показывает понятные сообщения: при заказе «Слишком много попыток оформить заказ…», при скачивании «Слишком много запросов» с кнопкой «Скачать ещё раз», превью повторяется само через 2,5 / 5 / 10 секунд (новая правка макета отменяет ожидающий повтор).
> Лимит считается по `$binary_remote_addr`. Если домен проксируется Cloudflare (оранжевое облако), nginx увидит адреса Cloudflare и лимит станет общим для всех посетителей одного узла: тогда нужен `real_ip_header CF-Connecting-IP` с `set_real_ip_from` для диапазонов Cloudflare. Проверка: `curl -sI https://bannerbot.ru | grep -i cf-ray` не должна ничего выводить.
> `nginx.conf` и `default.conf` смонтированы в контейнер одиночными файлами: после `git pull` нужен перезапуск контейнера nginx, а не только reload.

`/admin/` обслуживается отдельным `location /admin/` с `alias /app/frontend/admin/`. Rate limit не применяется — защита на уровне `ADMIN_TOKEN`. `sw.js`, `manifest.json` и иконки PWA отдаются тем же `location`, менять nginx не нужно.

> ❌ `proxy_params` не использовать — вызывает конфликт директив.
> ❌ `listen 443 ssl http2` → использовать `listen 443 ssl;` (http2 добавляется отдельно позже).
> ❌ nginx монтирует только `nginx/default.conf` → `/etc/nginx/conf.d/default.conf`.

---

## Web Push и PWA админки

Уведомления о новых заказах и оплатах прямо на телефон/компьютер админа, **без Telegram**: сайт сам шлёт
сообщение в push-сервис браузера (FCM — Chrome/Android, Mozilla, Apple — Safari/iOS, WNS — Edge), тот будит
service worker `/admin/sw.js`, появляется системное уведомление; нажатие открывает заказ (`/admin/?order=<id>`).
Работает параллельно с Telegram-уведомлениями и не зависит от доступности `api.telegram.org`.

### Включение (один раз)

```bash
# 1. Сгенерировать ключ VAPID (выводится один раз — сразу сохранить)
docker exec bannerprint_api python -m api.services.push_notify genkey

# 2. Прописать в web/.env (рядом с ADMIN_TOKEN):
#    VAPID_PRIVATE_KEY=<строка из шага 1>
#    VAPID_SUBJECT=mailto:you@example.com      # необязательно, по умолчанию SITE_BASE_URL

# 3. Пересоздать контейнер, чтобы он прочитал .env
cd ~/banner_web/web && docker compose up -d --force-recreate api
```

Дальше на каждом устройстве: открыть `https://bannerbot.ru/admin/` → войти токеном → карточка
«Уведомления на этом устройстве» → **«Включить уведомления»** → разрешить в браузере → **«Отправить проверку»**.

- **iPhone/iPad (iOS 16.4+):** push работает только у установленного PWA. Safari → «Поделиться» → «На экран „Домой“»,
  затем открыть админку **с иконки на экране** и включить уведомления там.
- **Android/Chrome, десктоп:** достаточно разрешения в браузере; установка на экран по желанию.
- Токен по умолчанию хранится до закрытия вкладки. Галочка «Запомнить на этом устройстве» сохраняет его в
  `localStorage` — удобно для PWA, но токен остаётся на устройстве до нажатия «Выйти». Ставить её стоит только на своём телефоне.

### Если уведомления не включаются

Карточка «Уведомления» в админке показывает причину прямо в тексте, а раскрывающийся блок **«Диагностика»** — браузер,
режим (вкладка или приложение), разрешение, service worker, подписку и последнюю ошибку. Её и присылать при разборе.

- **«заблокированы» / `denied`:** Chrome → ⋮ → Настройки → Настройки сайтов → Уведомления → разрешить `bannerbot.ru`;
  затем Android → Настройки → Приложения → Chrome (и установленное приложение админки, если есть) → Уведомления включить.
  После возврата на страницу состояние перечитывается само, либо кнопка «Проверить ещё раз».
- **Кнопка отвечает «Chrome отклонил запрос, не показав его»** (в «Диагностике» `разрешение: default`, но последний запрос
  `denied за … мс`): Chrome блокирует повторные запросы примерно на неделю после нескольких закрытых окон; в настройках
  сайта запрета не видно. Chrome → ⋮ → Настройки → Настройки сайтов → Все сайты → `bannerbot.ru` → «Очистить и сбросить»
  (токен слетит), затем один раз «Включить уведомления» → «Разрешить». Либо значок сайта слева от адреса → Разрешения →
  Уведомления → Разрешить: страница подхватит разрешение и включит уведомления сама.
- **Запрос разрешения не появляется** (через 15 с админка пишет «не получил ответа»): окно могло быть скрыто —
  разрешите вручную, как выше.
- **`AbortError` / «не ответил за 20 секунд»:** устройство не достаёт до push-сервиса Google (FCM): проверьте интернет,
  отключите VPN и экономию трафика, обновите «Google Play-сервисы». Без Google-сервисов (часть Huawei) push в Chrome не работает.
- **Тост «Уведомления включены», но статус снова «Выключены»:** старый баг Firefox для Android (страница считала скрытый
  браузером ключ подписки «чужим» и снимала подписку), исправлен; если повторится — пришлите «Диагностику».
- **«Отправлено устройств: 1», но на экране ничего нет:** push дошёл, уведомление гасит система — режим «Не беспокоить»,
  отключённые уведомления Chrome в Android, ограничение батареи для Chrome.

### Как устроено

- Подписки устройств — таблица `push_subscriptions` (до 10 устройств, старые вытесняются).
- Сообщение содержит только номер заказа, размер и сумму (как в Telegram) — **текст баннера не передаётся**.
  Новый заказ и его оплата имеют один `tag`: «Оплачено» заменяет «Новый заказ», а не копится рядом.
- Отправка идёт в фоне (`BackgroundTasks` / `fire_and_forget`): медленный push-сервис не задерживает ни создание
  заказа, ни ответ на webhook ЮКасса. Хранение сообщения у push-сервиса для выключенного устройства — 24 часа.
- Подписка, на которую push-сервис отвечает 404/410, удаляется сразу; после 20 подряд неудач — тоже.
- `endpoint` принимается только от известных push-сервисов (защита от SSRF).
- Смена `VAPID_PRIVATE_KEY` делает старые подписки недействительными: админка при открытии сама сбросит старую
  подписку, останется нажать «Включить уведомления» заново на каждом устройстве. Страница помнит, с каким ключом
  подписано устройство (`localStorage`, несекретно), потому что не все браузеры, например Firefox для Android,
  отдают ключ подписки обратно; в «Диагностике» это строка «ключ подписки».
- Выключить на сервере — убрать `VAPID_PRIVATE_KEY` и пересоздать контейнер: все функции модуля становятся no-op.

---

## Telegram через Cloudflare Worker

Хостинг VPS не пропускает трафик Telegram в обе стороны: с сервера не открывается `api.telegram.org`, а запросы Telegram
к сайту обрываются по таймауту (`getWebhookInfo` → `last_error_message: Connection timed out`, в `access.log` nginx нет
адресов `149.154.*` / `91.108.*`). Поэтому между сайтом и Telegram стоит Worker Cloudflare (`web/tools/cf-tg-relay.js`,
бесплатного тарифа хватает). Код сайта не менялся: адрес Bot API берётся из `TG_API_BASE`.

```
сайт     ──POST──► Worker  /<RELAY_SECRET>/bot<TOKEN>/<method> ──► api.telegram.org
                   (уведомление о заказе, правка сообщения, ответ на кнопку; ответ Telegram возвращается синхронно)
Telegram ──POST──► Worker  /<RELAY_SECRET>/tg-hook ──POST──► https://bannerbot.ru/api/tg/callback
                   (нажатие «✅ Выдать PDF»)
```

### Развёртывание (один раз)

1. Cloudflare → Workers & Pages → Create → Hello World → Edit code → вставить `web/tools/cf-tg-relay.js` → Deploy.
2. Settings → Variables and Secrets → Secret `RELAY_SECRET` = `openssl rand -hex 16`.
3. В `web/.env`: `TG_API_BASE=https://<имя>.<аккаунт>.workers.dev/<RELAY_SECRET>` и `TG_WEBHOOK_SECRET` (`openssl rand -hex 32`);
   затем `docker compose up -d --force-recreate api`.
4. Зарегистрировать webhook на Worker (с VPS; секреты не выводятся):

```bash
cd ~/banner_web/web; set -a; . ./.env; set +a
curl -sS -m 15 -X POST "$TG_API_BASE/bot$TG_NOTIFY_TOKEN/setWebhook" -H 'content-type: application/json' \
  -d "{\"url\":\"$TG_API_BASE/tg-hook\",\"secret_token\":\"$TG_WEBHOOK_SECRET\",\"allowed_updates\":[\"callback_query\"],\"drop_pending_updates\":true}"
# ожидается {"ok":true,"result":true,"description":"Webhook was set"}
```

Проверка (Worker принимает только POST, поэтому `-X POST` обязателен):

```bash
curl -sS -m 15 -X POST "$TG_API_BASE/bot$TG_NOTIFY_TOKEN/getWebhookInfo" -H 'content-type: application/json' -d '{}' \
  | python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];print({k:r.get(k) for k in ("pending_update_count","last_error_message","last_error_date")})'
```

`last_error_date` показывает, свежая ли ошибка: Telegram не стирает старую `last_error_message`. Если `pending_update_count`
не уменьшается, а ошибка свежая, смотреть `docker logs bannerprint_api --since 10m | grep tg/callback`.

### Как устроено

- Без секретного первого сегмента пути Worker отвечает 404. На Telegram пропускаются только методы из `ALLOWED_METHODS`
  (нельзя читать апдейты, удалять webhook, слать файлы), на сайт уходит один фиксированный адрес `SITE_CALLBACK`.
- Секрет webhook проверяет сайт: Worker пересылает `X-Telegram-Bot-Api-Secret-Token` как есть, без верного значения
  `/api/tg/callback` отвечает 403 (Worker возвращает этот статус Telegram, а не маскирует его).
- Если сайт недоступен или завис, Worker отвечает Telegram 502/504, и Telegram повторяет доставку. Повтор безопасен:
  `do_force_token()` идемпотентна.
- После настройки `getWebhookInfo` и `setWebhook` можно убрать из `ALLOWED_METHODS`.
- Смена `RELAY_SECRET` требует нового `TG_API_BASE` в `.env` и повторного `setWebhook`.

---

## 152-ФЗ

Раздел описывает, что **фактически** хранит код (источник правды — `web/api/db/schema.sql`).
Публичные тексты на сайте («Соглашение», «Конфиденциальность») должны ему соответствовать.
Юридическая оценка применимости 152-ФЗ — вне этого README.

Регистрация не нужна, имя, телефон и адрес пользователя не запрашиваются.

| | |
|---|---|
| **Хранится при заказе** | `order_id` (UUID), `size_key`, `ref_code`, `promo_code`, `token` (hex), `amount_rub`, `config_json`, временные метки |
| **Акцепт соглашения** | `offer_version`, `accepted_at`, **`accepted_ip`** (X-Real-IP от nginx), **`accepted_ua`** (User-Agent, ≤300 символов) — нужны, чтобы подтвердить, что условия приняты. Срок хранения в коде не ограничен |
| **Текст макета** | `config_json` / `original_config_json` содержат введённый пользователем текст; он может включать телефоны и адреса. Сервис текст не анализирует (ответственность пользователя — п. 2 Соглашения) |
| **Не хранится** | имя, tg_id пользователя, платёжные данные (карта вводится на стороне ЮKassa) |
| **Исключение** | `email` в `api_keys` — основание: исполнение договора |
| **Администратор** | `push_subscriptions` — устройства админа (не клиентов), `user_agent` ≤200 символов |
| **Третьи стороны** | ЮKassa (платежи); Яндекс.Метрика с Вебвизором (cookie, действия на странице) |
| **Сервер** | Германия |

---

## Pending tasks

### ✅ Выполнено

- [x] FSM `OrderStatus(Enum)` + `transition()` + guard
- [x] `token_store` → SQLite `download_tokens`
- [x] `order_store` → SQLite `pending_orders`
- [x] Lifespan cleanup просроченных токенов и заказов
- [x] Автодеплой через GitHub Actions + health check
- [x] SSL (bannerbot.ru, истекает 2026-06-19)
- [x] Переезд на новый сервер, nginx на стандартных портах 80/443
- [x] Фронтенд: динамический рендер из `/api/templates`, шрифты в кнопках
- [x] Фронтенд: FAB + sticky-бар превью на мобиле, двухколонка на десктопе
- [x] Фронтенд: кастомный размер 100–3000 мм, защита от совпадения цветов
- [x] Фронтенд: футер (Соглашение, Конфиденциальность, Связаться)
- [x] `banner_generator.py`: пропорциональный layout (uniform scale), PDF y_pos через ReportLab
- [x] `banner_generator.py`: вотермарка на JPEG-превью
- [x] Переключение на ЮКасса: `services/payment.py` + `routers/payment.py`
- [x] Запасной провайдер Selfwork удалён: роутер не импортировался (функция проверки подписи лежала в другом модуле), фронт умел только виджет ЮКасса, инструкция переключения называла несуществующие модули. Код остаётся в истории git
- [x] `app.js` v12: модалка `#modal-pay` с виджетом ЮКасса, очистка контейнера
- [x] E2E флоу: превью → заказ → виджет ЮКасса → webhook → PDF скачан ✅
- [x] TG-уведомления о новых заказах (`@BannerBotInfo_bot` + inline-кнопка «Выдать PDF»)
- [x] TG webhook: Telegram → Cloudflare Worker (`<TG_API_BASE>/tg-hook`) → `/api/tg/callback`; порт 88 не используется
- [x] `do_force_token()` вынесена — вызывается из TG и из adminки
- [x] `tg_message_id` в `web_orders`: после webhook редактируется TG-сообщение («💳 Оплачено»)
- [x] Миграция БД: колонки `yookassa_payment_id` и `tg_message_id`
- [x] Промокоды: таблица `promo_codes`, флоу бесплатной выдачи, TG-уведомление с кодом
- [x] Промокоды: автовозврат неоплаченных использований (abandon, TTL 60 мин, поздняя оплата берёт использование обратно), `order_limit` на `POST /api/order`
- [x] Промокоды: отчёт в админке (рубли скидок, неиспользованные коды, кто и когда применил), `web_orders.discount_rub`
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
