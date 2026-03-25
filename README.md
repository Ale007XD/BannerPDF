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
- [Тесты](#тесты)
- [Nginx и rate limits](#nginx-и-rate-limits)
- [152-ФЗ](#152-фз)
- [Pending tasks](#pending-tasks)

---

## Статус проекта

| | |
|---|---|
| Готовность | **99%** — сайт запущен, принимает заказы |
| URL | `https://bannerbot.ru:8444` |
| Что готово | Весь бэкенд (P0 закрыты), фронтенд (превью, кастомный размер, sticky-бар превью на мобиле, шрифты в кнопках), FSM заказов, ручной флоу оплаты (СБП + adminка), `POST /api/admin/force_token`, мобильная adminка `/admin/`, футер с модалками (Соглашение, Конфиденциальность, Связаться), строка согласия в модалке оплаты |
| Что осталось | E2E тест · реферальные команды бота · интеграция платёжного провайдера (P1) |

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
| Прокси | Nginx 1.25 в Docker — реверс-прокси + статика + rate limits, порт 8444 |
| Деплой | GitHub Actions → SCP → Docker Compose |
| Оплата (текущая) | Ручная: СБП на +7 914 002-27-77 (МТС Банк) + `force_token` через adminку |
| Оплата (планируемая) | Сам.Эквайринг (selfwork.ru) — для самозанятых, автовыдача чека ФНС |
| Бот | python-telegram-bot 21 (async), Python 3.11+ |

---

## Архитектура

```
                        ┌──────────────────────────────────┐
  Браузер               │  Nginx (Docker, порты 8444/80)   │
  ──────────────────►   │  • Статика /app/frontend          │
                        │  • Статика /app/frontend/admin/   │
                        │  • Rate limits (preview/order/dl) │
                        │  • TLS (Let's Encrypt, :8444)     │
                        │  • Редирект 80 → https://$host:8444│
                        └──────────────┬───────────────────┘
                                       │ proxy_pass :8000
                        ┌──────────────▼───────────────────┐
                        │  FastAPI (Uvicorn, 1 worker)      │
                        │                                   │
                        │  Роутеры:                         │
                        │  • /api/preview    (Pillow)       │
                        │  • /api/order      (FSM)          │
                        │  • /api/payment/*                 │
                        │  • /api/download/* (GS)           │
                        │  • /api/admin/*    (force_token)  │
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

  Ручная оплата ───► Adminка /admin/ → force_token
                         └─ FSM: pending→paid→token_issued
                         └─ Клиент получает PDF автоматически (поллинг)

  @BannerPrintBot ────► POST /api/referral/internal/create
                            (X-Bot-Secret)
```

**Ключевые архитектурные решения:**

Uvicorn строго 1 worker — `token_store` и `order_store` в SQLite без внешней синхронизации. 2 воркера = 2 независимых in-memory стора = потерянные токены при переключении. Пересматривается при переходе на Redis.

Ghostscript CPU-bound. Вызов из asyncio event loop заблокировал бы весь сервер. Решение: `ProcessPoolExecutor(max_workers=2)`, вызов только через executor.

`token_store` и `order_store` перенесены из in-memory в SQLite — переживают рестарты.

Рендерер — локальная копия `banner_generator.py`, не зависит от бота. Web-контейнер полностью автономен.

Слотовое вертикальное распределение текста — `safe_h / n` равных слотов, каждая строка центрируется в своём. Работает для 1–6 строк и любого соотношения сторон баннера.

Порт 443 занят amnezia-xray (VLESS+Reality). Сайт работает на 8444.

---

## Структура репозитория

```
BannerPDF/
├── .github/
│   └── workflows/
│       ├── deploy-web.yml        # Деплой сайта при push в main
│       └── ci-web.yml            # CI: линтер + pytest
│
├── web/                          # Сайт
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   ├── templates.json            # Размеры, шрифты, цвета — источник для GET /api/templates
│   │
│   ├── api/
│   │   ├── main.py               # FastAPI app + lifespan
│   │   ├── db/
│   │   │   ├── __init__.py       # get_db(), init_db()
│   │   │   └── schema.sql        # Схема всех таблиц
│   │   ├── routers/
│   │   │   ├── preview.py        # POST /api/preview (Optional size_key | width_mm+height_mm)
│   │   │   ├── order.py          # FSM + POST /api/order + GET /api/payment/status
│   │   │   ├── payment.py        # POST /api/payment/callback
│   │   │   ├── download.py       # GET /api/download/{token}
│   │   │   ├── admin.py          # stats|orders|funnel + POST force_token/{order_id}
│   │   │   ├── referral.py       # Реферальная программа
│   │   │   ├── corp_api.py       # Корп. API (заглушки 501)
│   │   │   └── batch.py          # Batch (заглушки 501)
│   │   └── services/
│   │       ├── banner_generator.py  # create_preview_jpeg(), create_final_pdf()
│   │       │                        # Слотовое распределение текста по высоте
│   │       ├── renderer.py          # Адаптер: build_render_data(), set/get_executor()
│   │       ├── sanitizer.py         # sanitize_text_lines(), validate_banner_config()
│   │       ├── config.py            # FONTS, COLORS, BANNER_SIZES (MIN=100мм)
│   │       ├── payment.py           # create_payment() — подготовка данных виджета
│   │       ├── token_store.py       # create_token(), consume_token() — SQLite
│   │       ├── order_store.py       # save_pending(), get_pending() — SQLite
│   │       ├── referral_store.py    # accrue_commission() 15%
│   │       ├── api_key_store.py     # generate_key(), verify_key()
│   │       └── batch_worker.py      # asyncio.Queue + ProcessPoolExecutor
│   │
│   ├── frontend/
│   │   ├── index.html            # Конструктор + модалка оплаты (СБП + TG + «Я оплатил»)
│   │   ├── style.css             # Mobile-first, sticky-бар превью внизу на мобиле, двухколонка ≥820px
│   │   ├── app.js                # Поллинг с момента открытия модалки, FONT_CSS_MAP, buildConfig()
│   │   └── admin/
│   │       └── index.html        # Мобильная adminка: список заказов, кнопка «Выдать PDF»
│   │
│   └── nginx/
│       ├── nginx.conf            # Rate limit зоны (http-блок)
│       └── default.conf          # Server блоки, /admin/ location, proxy_pass, TLS (:8444)
│
├── web/tests/                    # Тесты (pytest)
│   ├── conftest.py
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
- Шрифты TTF: `GolosText-Regular.ttf`, `TenorSans-Regular.ttf`, `FiraSansCondensed-ExtraBold.ttf`, `PTSansNarrow-Bold.ttf`
- ICC-профиль `ISOcoated_v2_300_eci.icc`

### 1. Клонирование

```bash
git clone https://github.com/Ale007XD/BannerPDF.git
cd BannerPDF/web
```

### 2. Переменные окружения

```bash
cp .env.example .env
# Заполнить: ADMIN_TOKEN, BOT_INTERNAL_SECRET
# SELFWORK_* — можно оставить placeholder для локального запуска
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

Сайт: `http://localhost:8444`
Adminка: `http://localhost:8444/admin/`

---

## Деплой на VPS

**VPS:** Hetzner CX41, `bannerpdf@r1018353` (4 vCPU, 16 GB RAM, Ubuntu)
**Путь на сервере:** `/home/bannerpdf/banner_web/`
**URL:** `https://bannerbot.ru:8444`

### Занятые порты

| Порт | Сервис |
|---|---|
| 443 | amnezia-xray (VLESS+Reality — не трогать) |
| 8443 | telemt |
| **8444** | **bannerprint_nginx (наш сайт)** |
| 80 | bannerprint_nginx (редирект → 8444) |

### Автоматический деплой (GitHub Actions)

При `push` в `main` с изменениями в `web/**` автоматически запускается `.github/workflows/deploy-web.yml`:

1. Копирует файлы на VPS через SCP (`web/` → `/home/bannerpdf/banner_web/`)
2. Пересобирает `api` без кэша только если изменился `Dockerfile` или `requirements.txt`
3. `docker compose up -d --force-recreate --remove-orphans`
4. `docker image prune -f`
5. Health check через `urllib.request` внутри контейнера

**Необходимые секреты в GitHub:**

```
VPS_HOST      — 103.115.18.224
VPS_USER      — bannerpdf
VPS_SSH_KEY   — приватный SSH-ключ
```

### Файлы вне репо (монтируются как volume)

```
/home/bannerpdf/banner_web/fonts/
  GolosText-Regular.ttf
  TenorSans-Regular.ttf
  FiraSansCondensed-ExtraBold.ttf
  PTSansNarrow-Bold.ttf

/home/bannerpdf/banner_web/profiles/
  ISOcoated_v2_300_eci.icc

/home/bannerpdf/banner_web/.env   # секреты
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

| Переменная | Описание | Значение на проде |
|---|---|---|
| `SELFWORK_SHOP_ID` | ID магазина в Сам.Эквайринг | `PLACEHOLDER` → заменить при подключении |
| `SELFWORK_API_KEY` | API-ключ Сам.Эквайринг | `PLACEHOLDER` → заменить при подключении |
| `SITE_BASE_URL` | Базовый URL сайта | `https://bannerbot.ru` |
| `SITE_PDF_PRICE` | Цена PDF в рублях (не хардкодить) | `299` |
| `ADMIN_TOKEN` | Bearer-токен для `/api/admin/*` (32 байта) | задан |
| `BOT_INTERNAL_SECRET` | Секрет для внутреннего API бота | задан |
| `ALLOWED_ORIGINS` | CORS origins | `https://bannerbot.ru` |
| `WEB_DB_PATH` | Путь к SQLite БД | `/app/data/banner_web.db` |
| `FONTS_DIR` | Директория с TTF-шрифтами | `/app/fonts` |
| `ICC_PROFILE_PATH` | Путь к ICC-профилю | `/profiles/ISOcoated_v2_300_eci.icc` |
| `TEMPLATES_PATH` | Путь к templates.json | `/app/templates.json` |
| `BATCH_DIR` | Временная директория для batch ZIP | `/tmp/bannerprint_batches` |
| `UVICORN_WORKERS` | Количество воркеров (строго 1) | `1` |

---

## API

### Публичные эндпоинты

```
GET  /api/health                   — Healthcheck
GET  /api/templates                — Размеры, шрифты, цвета (из templates.json)
POST /api/preview                  — JPEG-превью баннера (30 RPM/IP)
POST /api/order                    — Создание заказа (5 RPM/IP)
GET  /api/payment/status/{id}      — Статус заказа (поллинг)
POST /api/payment/callback         — Webhook от Сам.Эквайринг (SHA256 первым)
GET  /api/download/{token}         — Скачать PDF (10 RPM/IP, одноразовый токен TTL 15 мин)
GET  /api/referral/stats/{code}    — Статистика реферального кода
```

### Пример: создание заказа (типовой размер)

```bash
curl -X POST https://bannerbot.ru:8444/api/order \
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
# {"order_id": "uuid4..."}
```

### Пример: кастомный размер

```bash
curl -X POST https://bannerbot.ru:8444/api/order \
  -H "Content-Type: application/json" \
  -d '{
    "width_mm": 1200,
    "height_mm": 800,
    "bg_color": "Красный",
    "text_color": "Белый",
    "font": "Fira Sans Cond",
    "text_lines": [{"text": "АКЦИЯ", "scale": 1.0}]
  }'
```

### Внутренние (только бот)

```
POST /api/referral/internal/create  — Создание реф. кода (X-Bot-Secret)
```

### Административные

```
GET  /api/admin/stats                      — Общая статистика
GET  /api/admin/orders                     — Список заказов с пагинацией
GET  /api/admin/funnel                     — Воронка конверсии по статусам
POST /api/admin/force_token/{order_id}     — Ручная выдача PDF-токена после оплаты
```

Все admin-эндпоинты: `Authorization: Bearer <ADMIN_TOKEN>`.

Интерактивная документация: `https://bannerbot.ru:8444/api/docs`

---

## База данных

SQLite WAL-режим, файл `banner_web.db`. Инициализируется автоматически через `schema.sql`. Хранится в named volume `bannerprint_data` — переживает деплои.

```
web_orders          — заказы (статус управляется FSM, хранит config_json)
download_tokens     — одноразовые токены скачивания (TTL 15 мин)
pending_orders      — TTL-буфер config до подтверждения оплаты (TTL 30 мин)
api_plans           — тарифы корп. API (предзаполнена)
api_keys            — ключи корп. API
batch_jobs          — задачи batch-рендера
referrers           — рефереры и балансы
referrals           — начисленные комиссии
```

---

## FSM заказов

```
create_order ──► PENDING
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
PAID    ──[ttl_paid]──────► EXPIRED
```

Все переходы — только через `transition(order_id, event)` в `routers/order.py`. Прямой `UPDATE status` в обход FSM запрещён. `force_token` делает оба перехода последовательно: `PENDING→PAID→TOKEN_ISSUED`.

---

## Авторизация

| Уровень | Заголовок | Эндпоинты |
|---|---|---|
| Admin | `Authorization: Bearer <ADMIN_TOKEN>` | `/api/admin/*` |
| Bot internal | `X-Bot-Secret: <BOT_INTERNAL_SECRET>` | `/api/referral/internal/*` |
| Corp API | `Authorization: Bearer bp_live_<32b>` | `/api/v1/*` |
| Selfwork webhook | SHA256 в теле JSON | `/api/payment/callback` |

**Форматы:**

```
order_id:   UUID4  (36 символов)
token:      32 bytes hex  (64 символа)
ref_code:   8 символов A-Z0-9
api_key:    bp_live_<32 chars base64url>
```

---

## Флоу оплаты (текущий)

Ручной флоу без платёжного провайдера — работает с момента запуска.

```
1. Клиент настраивает баннер → нажимает «Получить PDF»
2. POST /api/order → создаётся заказ (PENDING), стартует поллинг
3. Модалка: реквизиты СБП (+7 914 002-27-77, МТС Банк, сумма 299 ₽, номер заказа)
4. Клиент переводит деньги → нажимает «Я оплатил — жду PDF» (или ждёт без действий)
5. Владелец видит перевод → открывает /admin/ → нажимает «Выдать PDF»
6. POST /api/admin/force_token/{order_id} → FSM: PENDING→PAID→TOKEN_ISSUED → токен создан
7. Поллинг клиента ловит token_issued → PDF скачивается автоматически
```

**Выдача токена вручную через curl:**

```bash
curl -X POST https://bannerbot.ru:8444/api/admin/force_token/<ORDER_ID> \
  -H "Authorization: Bearer <ADMIN_TOKEN>"
# {"order_id": "...", "token": "...", "download_url": "/api/download/...", "already_issued": false}
```

**Планируемый автоматический флоу (P1):**
Сам.Эквайринг → `POST /api/payment/callback` → SHA256 верификация → `transition(webhook_paid)` → `transition(issue_token)` → токен без участия владельца.

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

## Тесты

```bash
pip install -r web/requirements.txt
pip install -r requirements-test.txt

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
| `test_sanitizer.py` | sanitize_line, validate_banner_config (size_key и width_mm/height_mm) |
| `test_order_api.py` | HTTP: POST /api/order, GET /api/payment/status |
| `test_payment_webhook.py` | HTTP: webhook, SHA256 первым, FSM через HTTP |
| `test_preview_api.py` | HTTP: POST /api/preview, все size_key, кастомный размер |
| `test_renderer_preview.py` | build_render_data, executor lifecycle |

GS замокан в `conftest.py` — тесты работают без Ghostscript и Docker.

---

## Nginx и rate limits

| Зона | Лимит | Burst | Эндпоинт |
|---|---|---|---|
| `preview_limit` | 30 req/min/IP | 5 | `POST /api/preview` |
| `order_limit` | 5 req/min/IP | 2 | `POST /api/order` |
| `download_limit` | 10 req/min/IP | 3 | `GET /api/download/*` |

`/admin/` обслуживается отдельным `location` с `alias /app/frontend/admin/`. Rate limit не применяется — защита на уровне `ADMIN_TOKEN`.

---

## 152-ФЗ

Сервис не собирает и не обрабатывает персональные данные пользователей. Все действия выполняются без регистрации и без передачи личной информации. Для работы используются только технические данные, не позволяющие идентифицировать пользователя.

| | |
|---|---|
| **Не хранится на сайте** | email, имя, tg_id, IP-адреса |
| **Хранится на сайте** | `order_id` (UUID), `size_key`, `ref_code`, `token` (hex), `amount_rub`, `config_json`, временные метки — не являются ПД |
| **Исключение** | `email` в `api_keys` — основание: исполнение договора (оферта) |
| **VPS** | Hetzner, Германия — безопасно при отсутствии ПД граждан РФ |

---

## Pending tasks

### Выполнено

- [x] SHA256 верификация подписи в `payment.py`
- [x] `token_store` → SQLite `download_tokens`
- [x] `order_store` → SQLite `pending_orders`
- [x] FSM `OrderStatus(Enum)` + `transition()` + guard
- [x] Download rate limit 10/min/IP в Nginx
- [x] `Dockerfile`: `uvicorn --workers 1`
- [x] `order.py`: `SITE_PDF_PRICE` из env
- [x] `batch_worker.py`: `ProcessPoolExecutor(max_workers=2)`
- [x] `main.py` lifespan: cleanup токенов и pending_orders
- [x] Автодеплой через GitHub Actions + health check
- [x] SSL (bannerbot.ru, истекает 2026-06-19)
- [x] Фронтенд: динамический рендер из `/api/templates`
- [x] Фронтенд: FAB + bottom sheet превью на мобиле, двухколонка на десктопе
- [x] Фронтенд: кастомный размер 100–3000 мм
- [x] Фронтенд: защита от совпадения цветов
- [x] Фронтенд: шрифты баннера в кнопках выбора шрифта (FONT_CSS_MAP)
- [x] Бэкенд: поддержка `width_mm`/`height_mm` в preview, order, renderer, sanitizer
- [x] `banner_generator.py`: слотовое вертикальное распределение текста (Pillow + ReportLab)
- [x] `POST /api/admin/force_token/{order_id}` — ручная выдача PDF после оплаты
- [x] Мобильная adminка `/admin/` — список заказов, автообновление 10 сек, кнопка «Выдать PDF»
- [x] Nginx: `location /admin/` с `alias /app/frontend/admin/`
- [x] Флоу оплаты: СБП реквизиты в модалке, поллинг с момента открытия, «Я оплатил», Telegram
- [x] Фронтенд: FAB заменён на sticky-бар «Посмотреть превью» (fixed bottom 0, всегда виден на мобиле)
- [x] Фронтенд: футер (© 2026 BannerPrint, Соглашение, Конфиденциальность, Связаться)
- [x] Фронтенд: модалки Пользовательского соглашения и Конфиденциальности
- [x] Фронтенд: строка согласия с условиями в модалке оплаты (закрытие оферты)
- [x] `banner_generator.py`: вотермарка на JPEG-превью (адаптивный цвет, правый нижний угол)
- [x] `banner_generator.py`: PDF y_pos через `pdfmetrics` face ascent/descent (точное центрирование)

### P1 — платёжная интеграция

- [ ] Регистрация в Сам.Эквайринг → `SELFWORK_SHOP_ID`, `SELFWORK_API_KEY`
- [ ] Callback URL в панели Selfwork: `https://bannerbot.ru:8444/api/payment/callback`
- [ ] E2E тест: превью → заказ → webhook → FSM → download → PDF
- [ ] Бот: таблица `referral_codes(tg_id, ref_code, created_at)` в `banner_bot.db`
- [ ] Бот: команды `/referral`, `/balance`, `/payout`
- [ ] `test_sanitizer.py`: тесты для кастомного размера
- [ ] `test_preview_api.py`: тест POST /api/preview с width_mm/height_mm

### P2 — масштаб

- [ ] Перенос на чистый сервер (80/443, убрать `:8444` из URL)
- [ ] `localStorage` в adminке (токен не сбрасывается при закрытии вкладки)
- [ ] Preview → `image/jpeg` streaming вместо base64
- [ ] Structured JSON logging
- [ ] Нагрузочный тест batch (100-строчный CSV)
- [ ] Cleanup `/tmp/bannerprint_batches` старше TTL
- [ ] Раскрыть корп. API и batch (убрать заглушки 501)
- [ ] Postman-коллекция из `/api/docs`
