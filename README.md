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
- [Промокоды](#промокоды)
- [Реферальная программа](#реферальная-программа)
- [Партнёры-типографии](#партнёры-типографии)
- [Корп. API тарифы](#корп-api-тарифы)
- [Тесты](#тесты)
- [Nginx и rate limits](#nginx-и-rate-limits)
- [Web Push и PWA админки](#web-push-и-pwa-админки)
- [Telegram через Cloudflare Worker](#telegram-через-cloudflare-worker)
- [152-ФЗ](#152-фз)
- [Pending tasks](#pending-tasks)
- [Персональные данные, счётчики, журналы](#персональные-данные-счётчики-журналы)

---

## Статус проекта

| | |
|---|---|
| Готовность | **99%** — сайт запущен, принимает боевые платежи |
| URL | `https://bannerbot.ru` |
| Партнёры | Первый партнёр «Реклама-Сити» (`reklamasity`) подключён и работает на проде: брендированная страница, почта через SMTP Яндекса, статистика в `/admin/partners/`. Отправка макетов на почту проверена и из консоли (`partner_mail_check.py`), и в боевых заказах |
| Что готово | Весь бэкенд (P0 закрыты), фронтенд (превью, кастомный размер, шрифты в кнопках, ЮКасса-виджет, промокоды), FSM заказов, E2E флоу оплаты (ЮКасса → webhook → PDF) + бесплатный флоу по промокоду, TG-уведомления (`@BannerBotInfo_bot`), корп. API с тарифом Trial, adminка, боевые ключи ЮКасса, партнёрский конструктор для типографий (брендирование, 100 ₽ за заказ, макет на почту типографии) |
| Что осталось | Реферальные команды бота · тесты `/api/v1/render` и `/api/v1/usage` (тесты batch есть) |

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
                        │  • /api/promo/check   (код без списания)│
                        │  • /api/partner/*     (брендинг типографий)│
                        │  • /api/beat          (счётчик без IP)│
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
                         └─ партнёрский заказ: +100 ₽ типографии; при скачивании копия PDF на её почту (SMTP)

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
│       ├── ci-web.yml            # CI: ruff + pytest (Python 3.11, Ghostscript)
│       └── ruff-fix.yml          # Ручной запуск: автоисправление ruff
│
├── API.md                        # Документация корп. API (/api/v1) для клиентов
├── docs/partners/                # Скриншоты для раздела «Партнёры-типографии»
│
├── web/                          # Сайт
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   ├── requirements-test.txt     # pytest, pytest-asyncio (CI ставит поверх основных)
│   ├── ruff.toml                 # known-first-party=['web']
│   ├── pytest.ini                # testpaths=web/tests, PYTHONPATH=.
│   ├── templates.json            # Размеры, шрифты, цвета — источник для GET /api/templates
│   ├── migrate_add_yookassa.py   # Миграция: yookassa_payment_id + tg_message_id
│   ├── migrate_add_trial_plan.py # Миграция: тариф Trial (идемпотентно)
│   ├── fonts/                    # TTF для PDF (в репо, монтируются в контейнер)
│   ├── profiles/                 # ICC-профиль (не используется: PDF пишется в чистом DeviceCMYK)
│   ├── seo/build_pages.py        # Генератор статических SEO-страниц и /privacy/ (python web/seo/build_pages.py)
│   ├── utils/                    # api_check.py, evidence.py: сквозные проверки корп. API по ключу
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
│   │   │   ├── partner.py        # Партнёры-типографии: GET /api/partner/{code} + админка /api/admin/partners*
│   │   │   ├── stat.py           # POST /api/beat (счётчик) + GET /api/admin/events
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
│   │       ├── stat_store.py        # Счётчик посещений и действий без идентификаторов (stat_counters)
│   │       ├── conversions.py       # Серверные события воронки: оплата и выдача файла
│   │       ├── retention.py         # Стирание текста макета, IP и User-Agent через ORDER_PII_RETENTION_DAYS
│   │       ├── pdf_check.py         # Самопроверка печатного PDF (stdlib)
│   │       ├── push_notify.py       # Web Push (VAPID) админу: push_new_order(), push_order_paid()
│   │       ├── promo_store.py       # промокоды: reserve() атомарно, calc_amount(), release_*, create_promo()
│   │       ├── referral_store.py    # accrue_commission() 15%
│   │       ├── partner_store.py     # партнёры: create_partner(), accrue_for_order() (100 ₽), отчёт, выплаты, учёт отправок
│   │       ├── partner_naming.py    # номер заказа 482-915 и имя файла «Заказ …, скажите его в … по номеру …»
│   │       ├── partner_stats.py     # статистика партнёров за период (сегодня / 7 дней / месяц / даты) и выгрузка в CSV
│   │       ├── partner_delivery.py  # deliver_order_pdf(): письмо с макетом типографии
│   │       ├── mailer.py            # SMTP (стандартная библиотека), SMTP_* из env
│   │       ├── api_key_store.py     # generate_key(), verify_key()
│   │       ├── batch_worker.py      # asyncio.Queue + ProcessPoolExecutor
│   │       └── tg_notify.py         # notify_new_order() → message_id, notify_order_paid()
│   │
│   ├── frontend/
│   │   ├── index.html            # Конструктор + модалка #modal-pay (ЮКасса embedded-виджет)
│   │   ├── style.css             # mobile-first, sticky превью, двухколонка ≥820px; правила партнёра `.logo--partner`
│   │   ├── app.js                # конструктор: виджет ЮKassa, поллинг, шрифты, промокоды, правка текста
│   │   ├── kit.js                # Собственный счётчик событий (POST /api/beat), без cookie
│   │   ├── <slug>/index.html     # SEO-страницы (razmery-bannerov, baner-3-na-2 и др.), генерируются seo/build_pages.py
│   │   ├── privacy/, requisites.html  # Политика и реквизиты
│   │   ├── fonts/                # woff2 для сайта (без внешних шрифтов)
│   │   ├── partner.js            # Брендирование под типографию (?partner=<code>), уведомление о передаче макета
│   │   ├── prefill.js            # Предзаполнение конструктора из адреса (?size=&text1=&bg=&color=&font=)
│   │   ├── partners/             # Логотипы типографий (SVG в UTF-8 / PNG), раздаются как /partners/<файл>
│   │   └── admin/
│   │       ├── index.html        # Adminка: заказы, корп. ключи (Trial бейдж), карточка «Уведомления»; в шапке навигация ЗАКАЗЫ · СТАТИСТИКА · ПАРТНЁРЫ
│   │       ├── stats/index.html  # /admin/stats/: счётчики событий (GET /api/admin/events: воронка, SEO)
│   │       ├── partners/index.html  # /admin/partners/: статистика партнёров за период, выгрузка CSV
│   │       ├── sw.js             # Service worker: только Web Push, без кэша
│   │       ├── manifest.json     # PWA: установка на экран «Домой»
│   │       └── icons/            # 192/512 + maskable 512
│   │
│   ├── nginx/
│   │   ├── nginx.conf            # Глобальные настройки: log_format с обезличенным IP, зоны лимитов
│   │   ├── default.conf          # Server-блоки: 80→443 редирект, 443 HTTPS, http2, точные location с лимитами
│   │   └── partner-redirect.example.conf  # Шаблон: редирект домена типографии на ?partner=<code> (не подключается сам)
│   │                             # Оба файла (nginx.conf, default.conf) монтируются по одному; proxy_params не используется
│   │
│   ├── ops/
│   │   ├── logrotate-bannerbot   # Ротация журналов nginx (14 дней)
│   │   └── partner_mail_check.py # Проверка почты партнёров из контейнера: вход SMTP/IMAP, тестовое письмо
│   │
│   ├── tools/
│   │   └── cf-tg-relay.js        # Cloudflare Worker: релей Telegram ⇄ сайт (не входит в образ)
│   │
│   └── tests/
│       ├── conftest.py
│       ├── test_fsm.py
│       ├── test_token_store.py
│       ├── test_order_store.py
│       ├── test_sanitizer.py
│       ├── test_order_api.py
│       ├── test_payment_webhook.py
│       ├── test_preview_api.py
│       ├── test_renderer_preview.py
│       ├── test_partner.py             # Партнёры: начисления, письма, админка, телефон, имя скачиваемого файла
│       ├── test_partner_naming.py      # Номер заказа 482-915, имя файла, Content-Disposition
│       ├── test_partner_stats.py       # Статистика партнёров: периоды, границы суток, CSV
│       ├── test_admin_pages.py         # Навигация между страницами админки, страница партнёров
│       ├── test_partner_frontend.py    # partner.js, проводка в index.html/app.js/nginx, SVG-логотипы, политика
│       └── …                           # всего 40 файлов, см. раздел «Тесты»
│
└── bot/                          # Telegram-бот (отдельный контейнер)
    └── ...
```

---

## Быстрый старт (локально)

### Требования

- Docker + Docker Compose
- Шрифты TTF лежат в репо (`web/fonts/`: `GolosText-Bold`, `TenorSans-Regular`, `FiraSansCondensed-ExtraBold`, `PTSansNarrow-Bold`, `Caveat-Bold`, лицензии OFL), монтируются в контейнер

> ❌ `FiraSans-Regular.ttf` и `IgraSans-Regular.ttf` на сервере отсутствуют — не использовать.

### 1. Клонирование

```bash
git clone https://github.com/Ale007XD/BannerPDF.git
cd BannerPDF/web
```

### 2. Переменные окружения

Шаблона `.env.example` в репо нет: создайте `web/.env` вручную (полный список переменных: раздел «Переменные окружения»).

```bash
# Минимум: ADMIN_TOKEN, BOT_INTERNAL_SECRET, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, SITE_BASE_URL
# TG_NOTIFY_TOKEN, TG_ADMIN_CHAT_ID, TG_WEBHOOK_SECRET — для TG-уведомлений
# SMTP_* — для писем партнёрам
```

### 3. Шрифты

Ничего делать не нужно: шрифты в `web/fonts/`. ICC-профиль не нужен: PDF пишется чистым DeviceCMYK без встроенного профиля.

### 4. Запуск

```bash
docker compose up -d --build
```

### 5. Проверка

```bash
# порт 8000 наружу не публикуется (expose), проверка изнутри контейнера, как в деплое
docker exec bannerprint_api python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/health').read())"
# {"status":"ok","service":"bannerprint"}
```

`default.conf` ссылается на сертификат Let's Encrypt (`/etc/letsencrypt/live/bannerbot.ru/`): без него контейнер nginx локально не стартует. Для разработки хватает контейнера `api` (`docker compose up -d --build api`) и проверки выше; сайт и админка по `https://bannerbot.ru` и `/admin/` работают только на сервере с сертификатом.

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

При любом `push` в `main` автоматически запускается `.github/workflows/deploy-web.yml` (CI с ruff и pytest, `ci-web.yml`, идёт отдельно и срабатывает при изменениях в `web/**`):

1. Копирует весь репозиторий на VPS через SCP (в каталог `<DEPLOY_PATH>`; `.env` в репо нет и остаётся на сервере нетронутым)
2. `cd web && docker compose build api && docker compose up -d --force-recreate --remove-orphans`: пересоздаются оба контейнера, поэтому изменения `nginx.conf` и `default.conf` (одиночные bind mount) подхватываются
3. `docker image prune -f`
4. Health check изнутри контейнера: до 12 попыток с паузой 5 с (60 с), при неудаче в лог выводятся последние 50 строк `api`

**Необходимые секреты в GitHub:**

```
VPS_HOST      — IP сервера
VPS_USER      — пользователь для SSH
VPS_SSH_KEY   — приватный SSH-ключ
```

### Файлы вне репо (монтируются как volume)

```
<DEPLOY_PATH>/web/.env   # секреты (в репо нет, деплой его не трогает)
```

Шрифты (`web/fonts/`), `templates.json` и `frontend/` приезжают вместе с репозиторием.

### SSL-сертификат

```
Домен:    bannerbot.ru
Cert:     /etc/letsencrypt/live/bannerbot.ru/fullchain.pem
Key:      /etc/letsencrypt/live/bannerbot.ru/privkey.pem
Срок:     проверка: `echo | openssl s_client -connect bannerbot.ru:443 -servername bannerbot.ru 2>/dev/null | openssl x509 -noout -enddate`
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
| `UVICORN_WORKERS` | Справочная переменная из Dockerfile; число воркеров задаёт `CMD` (`--workers 1`), строго 1 | `1` |
| `ORDER_PII_RETENTION_DAYS` | Через сколько дней после оплаты стираются текст макета, IP и User-Agent заказа (`services/retention.py`) | `14` |
| `BATCH_MAX_FILE_BYTES` | Максимальный размер CSV для batch-рендера | `1000000` |
| `FRONTEND_DIR`, `TEMPLATES_PATH` | Каталог статики и путь к `templates.json` внутри контейнера (заданы в Dockerfile) | `/app/frontend`, `/app/templates.json` |
| `VAPID_PRIVATE_KEY` | Приватный ключ Web Push (VAPID). Пусто — push в админку выключен | `genkey`, см. «Web Push» |
| `VAPID_SUBJECT` | Контакт в VAPID-токене (`mailto:` или `https://`) | по умолчанию `SITE_BASE_URL` |
| `SMTP_HOST` | SMTP-сервер для писем типографиям-партнёрам. Пусто — почта выключена, макеты не отправляются | `smtp.example.ru` |
| `SMTP_PORT` | Порт; по умолчанию 465 для `ssl`, 587 для остальных | `465` |
| `SMTP_SECURITY` | `ssl` (по умолчанию, TLS с первого байта), `starttls` или `none` | `ssl` |
| `SMTP_USER`, `SMTP_PASSWORD` | Логин и пароль; без `SMTP_USER` вход не выполняется | |
| `SMTP_FROM` | Адрес отправителя; у его домена должны быть SPF и DKIM, иначе письма уйдут в спам | `BannerBot <noreply@bannerbot.ru>` |
| `PARTNER_MAIL_BCC` | Необязательно: скрытая копия каждого письма партнёру (контроль владельца) | |
| `STATS_UTC_OFFSET_HOURS` | Часовой пояс суток в статистике партнёров (`/admin/partners/`), целое от -12 до 14; по умолчанию `3` (Москва). «Сегодня» и границы месяца считаются по нему, не по UTC | `3` |

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
GET  /api/order/{id}/amend         — Доступна ли единственная бесплатная правка текста (24 ч после оплаты) и текущий текст
POST /api/order/{id}/amend         — Применить правку текста; новый PDF по новому токену
POST /api/beat                     — Собственный счётчик событий (204; IP не передаётся и не логируется)
POST /api/payment/callback         — Webhook от ЮКасса (verify_yookassa_payment() ПЕРВЫМ)
GET  /api/download/{token}         — Скачать PDF (10 RPM/IP, burst 3, токен TTL 15 мин и до 3 успешных выдач; 429 токен не расходует)
GET  /api/referral/stats/{code}    — Статистика реферального кода
GET  /api/partner/{code}           — Брендинг партнёра-типографии: название, логотип, цвет, телефон, delivers (кэш 5 мин; 404 — неизвестный или отключён)
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
GET        /api/admin/events                 — Счётчики событий за ?days=1–365 (страница `/admin/stats/`)
GET        /api/admin/order/{id}/audit       — Аудит заказа: акцепт, текст до и после правки, история выдач
GET        /api/admin/audit/by-payment/{id}  — То же по ID платежа ЮKassa
POST       /api/admin/force_token/{order_id} — Ручная выдача PDF-токена
POST       /api/admin/promo                  — Выдать промокод {discount 1–100, uses 1–1000, code?, expires_in_days?, note?}
GET        /api/admin/promo                  — Список промокодов: остаток, оплачено, статус
GET        /api/admin/promo/report           — Отчёт: итоги, рубли скидок, неиспользованные коды, заказы с кодом ?code=&limit=1–200 (50)
POST       /api/admin/promo/{code}/active    — Включить или отключить промокод {active}
GET        /api/admin/push/config            — Публичный VAPID-ключ для подписки устройства
POST       /api/admin/push/subscribe         — Сохранить подписку устройства (PushSubscription)
POST       /api/admin/push/unsubscribe       — Удалить подписку по endpoint
POST       /api/admin/push/test              — Проверочное уведомление на все устройства
POST/GET   /api/admin/partners               — Создать партнёра-типографию / список с суммами к выплате (подробнее: «Партнёры-типографии»)
PATCH       /api/admin/partners/{code}        — Изменить название, почту, логотип, цвет, ставку, телефон
POST/GET   /api/admin/partners/{code}/active|phone|payout, /report, /deliveries/{order_id}/resend — заморозка и восстановление, телефон, выплата, отчёт за месяц (по UTC), повтор письма
GET        /api/admin/partners/stats         — Статистика партнёров за период: ?period=today|week|month|prev_month или ?from=&to=, ?partner=<code>
GET        /api/admin/partners/stats.csv     — То же в CSV: ?kind=orders|days и те же параметры
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
  discount_rub INTEGER,             -- скидка заказа в рублях: цена сайта на момент заказа − amount_rub; NULL у заказов до отчёта
  partner_code TEXT                 -- партнёр-типография, с чьей страницы пришёл заказ; NULL — обычный заказ
)

download_tokens(token, order_id, expires_at, used, downloads)  -- TTL 15 мин, до 3 успешных выдач (used=1 при исчерпании)
pending_orders(order_id, config_json, expires_at)   -- TTL 30 мин
promo_codes(code, uses_left, expires_at, discount, uses_total, created_at, note, active)
                                                     -- discount: % скидки, 100 → бесплатно; uses_left списывается
                                                     -- при создании заказа (любая скидка), возвращается, если платёж не создан
api_plans(id, name, pdf_limit, rpm_limit, price_rub)
api_keys(id, key_hash, key_prefix, plan_id, label, email, active, pdf_used, period_start)
batch_jobs(id, api_key_id, status, total, done, errors_json, expires_at, charged_at)  -- TTL 1 ч; лимит списывается один раз при первой выдаче ZIP
partners(code PK, name, email, phone, logo_url, accent, reward_rub, active, created_at)  -- типографии-партнёры; email на сайт не отдаётся
partner_accruals(id, partner_code FK, order_id UNIQUE, order_amount, reward_rub, created_at, paid_out_at)  -- одно начисление на оплаченный заказ
partner_deliveries(order_id, config_hash, partner_code, created_at, sent_at, error, attempts)  -- письмо с макетом: одна запись на версию макета
push_subscriptions(id, endpoint UNIQUE, p256dh, auth, user_agent, fail_count, created_at, last_ok_at)  -- устройства админа (Web Push)
stat_counters(day, event, page, source, detail, n)  -- суммарные счётчики за день, без идентификаторов
referrers(ref_code, balance_rub, created_at)
referrals(id, referrer_id, order_id, order_amount, commission, ...)
```

### Миграции

Новые таблицы и колонки применяются сами при старте контейнера: `init_db()` выполняет `schema.sql` и `_COLUMN_MIGRATIONS` (`web/api/db/__init__.py`), идемпотентно. Новая колонка = строка в `schema.sql` плюс запись в `_COLUMN_MIGRATIONS`. Ручные скрипты ниже исторические, нужны только для очень старых баз.

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

Переход `ttl_expired` описан в таблице FSM, но в боевом коде не вызывается: статус `expired` у заказов практически не встречается, просроченные `pending_orders` убирает фоновый cleanup.

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

Заказ со страницы типографии (`partner_code`) идёт тем же флоу; отличия (начисление 100 ₽ на `paid`, копия PDF на почту типографии при скачивании, имя файла с номером заказа) описаны в разделе «Партнёры-типографии».

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

## Партнёры-типографии

Типография ставит редирект со своего домена на `https://bannerbot.ru/?partner=<code>`. Клиент видит конструктор в фирменной шапке типографии, платит полную цену (`SITE_PDF_PRICE`, сейчас 299 ₽) и скачивает PDF; **тот же файл** уходит на почту типографии, а ей начисляется **100 ₽** за каждый оплаченный заказ. Платёж принимает BannerBot, и страница об этом говорит прямо («Оплата и поддержка: BannerBot»).

![Шапка обычной страницы и страницы партнёра](docs/partners/header-compare.png)

Разделы: [как устроено](#как-устроено) · [визуальная часть](#визуальная-часть) · [логотип](#логотип-партнёра) · [ссылка с предзаполнением](#ссылка-с-предзаполнением) · [подключение](#подключение-типографии) · [почта](#почта-партнёрам) · [эксплуатация](#эксплуатация) · [если что-то не так](#если-что-то-не-так) · [где лежит код](#где-лежит-код)

### Как устроено

| Что | Как устроено |
|---|---|
| Партнёр | Таблица `partners`: `code` (slug `a-z0-9-`, 2–32 символа), название, почта, **телефон** (`phone`, публичный рабочий номер типографии), логотип, цвет, ставка (`reward_rub`, по умолчанию 100), `active`. Не путать с `ref_code` реферальной программы бота (8 символов, 15%): в партнёрском заказе `ref_code` не пишется, у одной оплаты один получатель |
| Начисление | `services/partner_store.accrue_for_order` из `transition()` при переходе в `paid`: срабатывает и на вебхук ЮKassa, и на ручную выдачу (`force_token`). Сумма = `min(ставка, сколько заплатил клиент)`; бесплатный заказ (промокод 100%) ничего не начисляет; повтор идемпотентен (`UNIQUE order_id`). Баланс не хранится: это `SUM(reward_rub)` по строкам `partner_accruals` с `paid_out_at IS NULL` |
| Отправка макета | При первом скачивании каждой версии макета (`routers/download.py`, после ответа клиенту, фоновой задачей): в письме тот же PDF, номер заказа (`482-915`, см. следующую строку), размер и текст. После бесплатной правки текста уходит «Исправленный макет». Сбой SMTP клиента не касается: ошибка пишется в `partner_deliveries.error`, следующее скачивание повторяет отправку |
| Связь клиента с типографией | Клиент анонимен, ни имени, ни телефона, ни почты мы не просим (минимизация ПДн). Связь держится на **номере заказа** и **публичном телефоне типографии**. Номер: 6 цифр вида `482-915`, `services/partner_naming.order_number` = первые 8 hex-символов `sha256(order_id)` по модулю 10⁶; в БД не хранится, поэтому в файле, окне и письме он совпадает по построению. Скачиваемый файл партнёрского заказа называется `Заказ 482-915, скажите его в Реклама-Сити по номеру 90-20-55.pdf` (без телефона: `Заказ 482-915, скажите его в Реклама-Сити.pdf`), тот же номер и телефон показывает окно после оплаты, номер стоит в теме письма, теле и имени вложения типографии (`Заказ 482-915, баннер 2×1 м.pdf`). Сервер отдаёт имя в `Content-Disposition` (`filename*=UTF-8''…` по RFC 5987, обычный `filename` ASCII для старых клиентов) и номер в `X-Order-Number`, фронтенд (`Partner.fromDownload`) берёт их оттуда: правило считается в одном месте. Для обычного заказа и для отключённого партнёра имя прежнее (`banner_<размер>_<id>.pdf`). Название и телефон чистятся для имени файла: убираются `\ / : * ? " < > \|`, управляющие и невидимые символы (в том числе RTL-override), пробелы схлопываются, название до 40, телефон до 30 символов. Совпадение номеров у двух заказов одной типографии возможно (при 100 заказах в месяц порядка 0,5%): письмо находится по номеру, а при совпадении различается по тексту макета |
| Привязка заказа | `POST /api/order` принимает `partner_code`; неизвестный или отключённый код молча отбрасывается (заказ без партнёра). Фронтенд передаёт код только после того, как брендинг загружен (при настроенной почте клиент к этому моменту уже видел на экране проверки уведомление о передаче макета) |
| Данные | Типография получает текст макета: это указано в политике `/privacy/` (получатель «Типография-партнёр») и в окне «Конфиденциальность». IP, User-Agent и платёжные данные ей не передаются. Телефон типографии публичный (его отдаёт `GET /api/partner/{code}`), почта и ставка нет. Через `ORDER_PII_RETENTION_DAYS` текст у нас стирается, повторная отправка (`resend`) после этого невозможна (410) |
| Без почты | Если `SMTP_HOST`/`SMTP_FROM` не заданы, партнёр всё равно получает начисления, но письма не уходят, а уведомления для клиента на странице нет (`delivers: false`). Флаг `mail_configured` виден в `GET /api/admin/partners` |

### Визуальная часть

<p>
  <img src="docs/partners/partner-desktop.png" width="640" alt="Страница партнёра на десктопе">
  <img src="docs/partners/partner-mobile.png" width="200" alt="Страница партнёра на телефоне">
</p>

Что меняется на странице (всё делает `partner.js`, данные берутся из публичного `GET /api/partner/{code}`: название, логотип, цвет, телефон, `delivers`; почты и ставки там нет):

| Элемент | Что видит клиент |
|---|---|
| Логотип | Вместо «BannerBot» логотип партнёра на белой плашке (скругление 8 px, отступы 4×12 px). Высота всегда 28 px, ширина по пропорциям, не шире 62% экрана. Нет логотипа или он не загрузился: название партнёра текстом на той же плашке |
| Телефон в шапке | Рабочий телефон типографии (`phone`) ссылкой `tel:` справа от логотипа: на телефоне звонок в один тап. На экране уже 480 px плашка «PDF 299 ₽» скрывается, чтобы шапка помещалась; длинное название обрезается многоточием. Нет `phone`: ничего не показывается. Ссылка строится только из значения, прошедшего ту же проверку, что на сервере (`safePhone`), и состоит из цифр и ведущего «+». Чтобы звонок срабатывал с мобильного из другого города, задавайте номер с кодом: `+7 (843) 290-20-55` |
| Строка под шапкой | «Конструктор макетов для «Название». Оплата и поддержка: BannerBot, реквизиты» (ссылка на `/requisites.html`) |
| Цвет | `accent` партнёра подменяет **вторичный** акцент интерфейса `--accent2` (по умолчанию синий): кнопка «+ строка», кнопки масштаба строки, обводка активных полей ввода, рамка уведомления, ссылки в SEO-блоке. Основной оранжевый BannerBot (кнопка оплаты, цена) не меняется намеренно: платит клиент BannerBot |
| Экран проверки | Перед галочкой соглашения: «Готовый макет будет также отправлен в «Название» для печати. Другие данные мы типографии не передаём.» (если `delivers`) |
| Окно после оплаты | «PDF-файл скачивается. Копия макета отправляется в «Название». Номер заказа: 482-915 (он же в названии файла). Назовите его в типографии по телефону 90-20-55.» (если `delivers`; без телефона: «Назовите его в типографии.») |

<img src="docs/partners/partner-confirm.png" width="640" alt="Экран проверки макета с уведомлением о передаче типографии">

Не меняется: цена, шаблоны, футер, документы, ЮKassa. Поведение при загрузке:

- Пока идёт запрос брендинга (не дольше 4 с), логотип BannerBot скрыт (`html.partner-pending`), чтобы он не мигал перед логотипом типографии.
- Партнёра нет, он отключён, сеть недоступна или ответ не прошёл проверку: обычная страница BannerBot, заказ создаётся без партнёра, сохранённый код стирается.
- `?partner=` убирается из адресной строки сразу после чтения (`history.replaceState`), остальные параметры (предзаполнение) остаются. Код хранится в `sessionStorage` (`bb_partner`) на время вкладки, поэтому после перезагрузки бренд остаётся; cookie нет. Чтобы увидеть обычный сайт после партнёрского, откройте новую вкладку.
- Ответ `GET /api/partner/{code}` кэшируется браузером на 5 минут (`Cache-Control: public, max-age=300`): новые название, логотип и цвет видны не позже чем через 5 минут. У ответа 404 заголовка кэша нет.
- Весь текст с сервера вставляется через `textContent` и атрибуты, `innerHTML` в `partner.js` нет (тест `TestSafety`).

### Логотип партнёра

- Файл лежит в `web/frontend/partners/<имя>`, в партнёре указывается `logo_url` = `/partners/<имя>` (формат `^/partners/[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$`). Внешние адреса запрещены: браузер посетителя не должен обращаться к чужому хосту (`/privacy/`).
- На сервер файл попадает с обычным деплоем (push в `main`, см. «Деплой на VPS»; при ручном обновлении `git pull`): каталог `frontend` смонтирован в nginx, пересборка образа не нужна.
- SVG или PNG. В шапке высота всегда 28 px, поэтому размер SVG в файле не важен (CorelDRAW задаёт миллиметры: 13×3.8 мм без фикса высоты рисовались 49×14 px). У PNG берите высоту от 56 px (2×), иначе он размоется. Плашка белая: подходят любые цвета логотипа, в том числе тёмные.
- **SVG только в UTF-8 без BOM.** CorelDRAW по умолчанию сохраняет UTF-16: Chromium такой файл в `<img>` показывает, остальные браузеры не обязаны. Перекодировать:
  ```bash
  iconv -f UTF-16 -t UTF-8 in.svg | sed '1s/UTF-16/UTF-8/' > web/frontend/partners/<имя>.svg
  ```
- **В SVG не должно быть скриптов.** В `<img>` они не исполняются, но тот же файл открывается по прямому адресу, и там скрипт выполнился бы на домене сайта. Тест `test_partner_svg_logos_are_utf8_and_inert` проверяет каждый SVG в каталоге: UTF-8 без BOM, well-formed XML, нет `<script`, `<foreignObject`, `<image`, `href=`, `data:`, `javascript:` и обработчиков `on*=`.

### Ссылка с предзаполнением

К партнёрской ссылке можно добавить параметры предзаполнения (`prefill.js`): конструктор откроется сразу с готовым макетом. Удобно для показа типографии и для рекламных ссылок.

| Параметр | Значение |
|---|---|
| `size` | ключ размера из `templates.json` (`2x1`, `2x2`, `3x2`, `3x3`, `1.5x1`, `1x1`, `1.5x0.5`, `1x0.5`) или свой размер в мм вида `3000x1000` (100–3000) |
| `text1`…`text6` | строки текста, до 120 знаков, управляющие символы удаляются |
| `bg`, `color` | `Черный`, `Белый`, `Красный`, `Желтый`, `Синий`, `Зеленый` |
| `font` | `PT Sans Narrow`, `Fira Sans Cond`, `Golos Text`, `Tenor Sans`, `Caveat` |

Всё, чего нет в списках или вне диапазона, молча отбрасывается. Значения нужно кодировать в URL. Пример: баннер 2×1 м, красный фон, белый текст, строки «Реклама-Сити» и «тел. 90-20-55»:

```
https://bannerbot.ru/?partner=reklamasity&size=2x1&text1=%D0%A0%D0%B5%D0%BA%D0%BB%D0%B0%D0%BC%D0%B0-%D0%A1%D0%B8%D1%82%D0%B8&text2=%D1%82%D0%B5%D0%BB.%2090-20-55&bg=%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D1%8B%D0%B9&color=%D0%91%D0%B5%D0%BB%D1%8B%D0%B9
```

Собрать ссылку из своих значений:

```bash
python3 - <<'PY'
from urllib.parse import quote
params = [("partner", "reklamasity"), ("size", "2x1"), ("text1", "Реклама-Сити"), ("text2", "тел. 90-20-55"),
          ("bg", "Красный"), ("color", "Белый")]
print("https://bannerbot.ru/?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in params))
PY
```

Код партнёра в ссылке должен совпадать с тем, что создан на сервере. Показывайте исходную ссылку: после загрузки `partner` пропадает из адресной строки.

### Подключение типографии

```bash
# 1. Логотип (см. выше) в репо: web/frontend/partners/reklamasity.svg, затем git pull на сервере.
# 2. Партнёр (ссылка вернётся в поле link; 409: код занят; 422: текст ошибки в detail):
curl -sS -w '\nHTTP %{http_code}\n' -X POST https://bannerbot.ru/api/admin/partners \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"code":"reklamasity","name":"Реклама-Сити","email":"print@example.ru",
       "logo_url":"/partners/reklamasity.svg","accent":"#e31e24","reward_rub":100,"phone":"90-20-55"}'
#    Партнёр уже заведён без телефона: задать (пустое значение или null убирает):
#    curl -sS -X POST https://bannerbot.ru/api/admin/partners/reklamasity/phone \
#      -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"phone":"90-20-55"}'
# 3. Проверка: брендинг (в ответе "delivers": true, если почта настроена, и "phone") и логотип (200, image/svg+xml).
curl -s https://bannerbot.ru/api/partner/reklamasity
curl -sI https://bannerbot.ru/partners/reklamasity.svg | grep -i -E '^HTTP|content-type|content-length'
# 4. Тестовое письмо на почту типографии (раздел «Почта»).
# 5. Редирект с домена типографии на https://bannerbot.ru/?partner=reklamasity
#    (шаблон nginx: web/nginx/partner-redirect.example.conf; либо редирект у регистратора/DNS-провайдера).
# 6. Боевая проверка: открыть ссылку, собрать макет по промокоду на 100% и скачать: файл должен называться «Заказ ххх-ххх, скажите его в … по номеру …», письмо с тем же номером в теме придёт типографии.
docker logs bannerprint_api --since 30m 2>&1 | grep "Макет заказа"      # «… отправлен партнёру …» или причина сбоя
```

Поля партнёра: `name` 1–80 символов; `email` проверяется только по синтаксису (на него уходят макеты); `phone` необязательный, 5–30 символов: цифры, пробелы, скобки, дефисы, `+` в начале (`90-20-55`, `+7 (843) 290-20-55`); он попадает в имя файла клиента и в окно после оплаты; `accent` формата `#rrggbb`; `reward_rub` 0–1000. Редирект 302 и абсолютный: путь и параметры исходного адреса отбрасываются, любой адрес на домене типографии ведёт на одну страницу. Целевой адрес в редиректе может содержать и предзаполнение.

### Почта партнёрам

Настройка в `web/.env` (список переменных в разделе «Переменные окружения»). `.env` читается **при создании контейнера**: после правки нужен `cd web && docker compose up -d api`, а `docker restart` значения не обновит. Для большинства провайдеров `SMTP_FROM` должен совпадать с логином ящика.

Пример для Яндекса:

```
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=ящик@yandex.ru
SMTP_PASSWORD=<пароль приложения, 16 символов>
SMTP_FROM=ящик@yandex.ru
PARTNER_MAIL_BCC=<ваша почта: скрытая копия каждого письма>
```

Что нужно в самом ящике Яндекса ([инструкция Яндекса](https://yandex.ru/support/yandex-360/customers/mail/ru/mail-clients/others)):

1. Почта → Настройки → «Почтовые программы»: включить «С сервера imap.yandex.ru по протоколу IMAP» и «Пароли приложений и OAuth-токены».
2. Яндекс ID → пароли приложений: создать пароль для «Почта» (показывается один раз). В `SMTP_PASSWORD` идёт **пароль приложения**, не пароль от аккаунта.

Если доступ почтовых программ выключен, вход отклоняется, хотя пароль верный: `SMTPAuthenticationError: (535, … 5.7.8 … This user does not have access rights to this service)` по SMTP и `[AUTHENTICATIONFAILED] … or IMAP is disabled` по IMAP.

Проверка (скрипт `web/ops/partner_mail_check.py` запускается внутри контейнера, из каталога `web/`):

```bash
# вход на SMTP и IMAP; печатает длину и sha256[:6] пароля, который видит контейнер (сам пароль не печатается)
docker exec -i bannerprint_api python3 - auth < ops/partner_mail_check.py
# тестовое письмо с PDF на почту партнёра КОД тем же кодом, что шлёт боевые письма (копия уйдёт и на PARTNER_MAIL_BCC)
docker exec -i bannerprint_api python3 - send КОД < ops/partner_mail_check.py
```

| Вывод | Что значит |
|---|---|
| `SMTP login: OK`, `ОТПРАВЛЕНО на …` | SMTP-сервер принял письмо; дошло ли оно, видно только в ящике (смотрите и «Спам») |
| `SMTPAuthenticationError` | неверный логин или пароль, не пароль приложения, выключен доступ почтовых программ (см. выше) |
| длина или `sha256[:6]` пароля отличаются от значений из `.env` (команда ниже) | контейнер не пересоздан: `docker compose up -d api` |
| `TimeoutError`, `OSError` | порты 465/587 с VPS закрыты, попробуйте `SMTP_SECURITY=starttls` и порт 587 |
| `SMTPSenderRefused` | `SMTP_FROM` не принадлежит аккаунту |
| `SMTPRecipientsRefused` | провайдер отклонил адрес типографии |
| `почта не настроена` | `SMTP_HOST` или `SMTP_FROM` пусты в контейнере |

Хэш пароля из `.env` для сравнения с выводом `auth` (длина и `sha256[:6]` должны совпасть):

```bash
v=$(grep '^SMTP_PASSWORD=' .env | cut -d= -f2- | tr -d '\r\n'); printf 'в .env: длина=%s sha256[:6]=%s\n' "${#v}" "$(printf %s "$v" | sha256sum | cut -c1-6)"
```

Сбои боевых отправок: причина в логе (`Макет заказа … не отправлен`) и в `partner_deliveries.error`, число таких записей по партнёру видно в `failed_deliveries`; следующее скачивание того же заказа повторяет отправку, администратор может повторить вручную через `resend`. Если письма уходят на свой домен, у домена должны быть SPF и DKIM, иначе они попадут в спам.

### Эксплуатация

В браузере на `/admin/partners/` есть управление партнёрами (добавить, изменить, заморозить и восстановить, скопировать ссылку; ниже) и статистика с выгрузкой CSV. Выплата пока только через API. Запросы с `Authorization: Bearer $ADMIN_TOKEN`.

| Запрос | Что делает |
|---|---|
| `POST /api/admin/partners` | Создать партнёра |
| `GET /api/admin/partners` | Список: заказы, начислено, **к выплате**, число неудачных отправок (`failed_deliveries`), `mail_configured` |
| `POST /api/admin/partners/{code}/active` `{"active":false}` | Заморозить (`true`: восстановить): страница без брендинга, новые заказы без партнёра, макеты не уходят; заказы, оформленные до заморозки, начисление сохраняют, история, статистика и выплата доступны |
| `GET /api/admin/partners/stats?period=month` | Статистика за период для страницы `/admin/partners/`: итоги, строка на партнёра, строка на день. Подробнее ниже |
| `PATCH /api/admin/partners/{code}` `{"reward_rub":150}` | Изменить присланные поля: `name`, `email`, `logo_url`, `accent`, `reward_rub`, `phone` (проверки те же, что при создании; `null` или пусто у `logo_url`, `accent`, `phone` убирает значение, у остальных ошибка 422; при ошибке не записывается ничего). Код не меняется. Новая ставка действует только на будущие начисления |
| `POST /api/admin/partners/{code}/phone` `{"phone":"90-20-55"}` | Задать рабочий телефон типографии (`null` или пусто: убрать). Новое значение попадает в имя файла со следующего скачивания, в окно после оплаты не позже чем через 5 минут (кэш) |
| `GET /api/admin/partners/report?month=2026-10` | Сводка за месяц по UTC: заказы, начислено, не выплачено |
| `POST /api/admin/partners/{code}/payout` `{"month":"2026-10"}` | Отметить выплаченным всё начисленное до конца месяца (без тела: всё невыплаченное). Возвращает `{orders, paid_rub}` |
| `GET /api/admin/partners/deliveries?q=482-915&only=failed&partner=&limit=50` | Состояние письма по оплаченным партнёрским заказам, свежие сверху: `sent`, `failed` (с текстом ошибки), `sending`, `none` (клиент не скачивал), `erased` (текст стёрт). `q` ищет по номеру заказа среди последних 2000 заказов (фильтр `only` при поиске не применяется), `limit` 1–200 |
| `POST /api/admin/partners/deliveries/{order_id}/resend` | Заново отрендерить оплаченный партнёрский заказ и отправить его на почту партнёра |

#### Управление партнёрами в админке

На `/admin/partners/` карточка «Партнёры» (под карточкой «Письма типографиям»): у каждого название, код, почта, телефон, ставка, число заказов, «к выплате», число сбоев писем и ссылка для типографии.

| Действие | Как |
|---|---|
| Добавить | «+ Добавить партнёра»: код (латиница, цифры, дефис; потом не меняется), название, почта, телефон, логотип, цвет, ставка. После сохранения показывается готовая ссылка `https://bannerbot.ru/?partner=<код>` |
| Изменить | «Изменить»: любые поля, кроме кода. Сохраняется через `PATCH`, ошибка проверки показывается над кнопкой, ничего частично не пишется. Изменения видят посетители не позже чем через 5 минут (кэш ответа) |
| Заморозить | «Заморозить» с подтверждением: страница типографии становится обычной, новые заказы идут без партнёра, письма не уходят. Уже начисленное, статистика и выплата остаются |
| Восстановить | «Восстановить» у замороженного возвращает брендирование и отправку писем (кэш: до 5 минут). Заказы за время заморозки партнёру не засчитываются |
| Ссылка | «Копировать ссылку» кладёт в буфер адрес для типографии |

Логотип загрузить через админку нельзя: файл кладётся в репозиторий (`web/frontend/partners/`), в поле указывается путь `/partners/<файл>`. Удалить партнёра можно только прямым SQL (ниже): с заказами и начислениями он остаётся, его можно заморозить.

#### Письма типографиям в админке

На `/admin/partners/` карточка «Письма типографиям» (над списком партнёров): для каждого оплаченного партнёрского заказа видно, дошёл ли макет.

| Что | Как |
|---|---|
| Номер | Тот же `482-915`, что клиент видит в имени файла и называет по телефону: введите его в поле поиска (подойдут `482915`, пробелы и дефис) |
| Фильтр | «Сбои» (по умолчанию) показывает только неудавшиеся отправки с текстом ошибки, «Все заказы» всё последнее. Поиск по номеру идёт по всем заказам, фильтр его не сужает |
| Состояния | ушло / сбой / отправляется / клиент не скачивал (письмо уходит при первом скачивании файла, поэтому у нескачанного заказа его ещё нет) / данные удалены (после `ORDER_PII_RETENTION_DAYS` макет не восстановить, повтор невозможен) |
| Повтор | «Отправить повторно» (у ушедшего «Отправить ещё раз»): сервер заново рендерит PDF и шлёт на текущую почту партнёра, поэтому после исправления адреса достаточно нажать кнопку. Результат и ошибка (почта не настроена, сбой SMTP) показываются под списком, счётчик сбоев в списке партнёров обновляется |

Заказы замороженных партнёров в списке остаются.

#### Статистика партнёров в админке

Страница `https://bannerbot.ru/admin/partners/`, ссылка «ПАРТНЁРЫ» есть в шапке каждой страницы админки (рядом «ЗАКАЗЫ» и «СТАТИСТИКА» со счётчиками событий `/admin/stats/`). Токен тот же, что у `/admin/`: войдите там, на остальных страницах он подхватится из вкладки (или из устройства, если отмечено «Запомнить»).

| Что | Как |
|---|---|
| Периоды | Сегодня, 7 дней (включая сегодня), Месяц (с 1-го числа), Прошлый месяц, Даты (свои «с» и «по» включительно, не длиннее 366 дней). Открывается на «Месяц» |
| Партнёр | Список «Все партнёры» или один; отключённые остаются в списке с пометкой, их история не пропадает |
| Итоги | Заказов, выручка, начислено, **к выплате сейчас** (за всё время, не за период), бесплатных по промокоду, сбоев отправки писем |
| По партнёрам | Заказов (и сколько из них бесплатных), выручка, начислено, к выплате, отправлено писем и сбоев |
| По дням | Строка на (день, партнёр), свежие сверху |
| Что считается | Оплаченные заказы партнёра (`paid` / `token_issued`) по моменту оплаты. Бесплатные по промокоду видны, начисление по ним 0 ₽ |
| Сутки | По поясу `STATS_UTC_OFFSET_HOURS` (по умолчанию UTC+3, Москва), он указан на странице. **Отличие от выплаты:** `GET /report` и `payout` считают месяц по UTC, поэтому заказ в первые три часа 1-го числа по Москве страница отнесёт к новому месяцу, а выплата к прошлому. На сумму к выплате это не влияет, но при сверке границы месяца учитывайте |
| CSV | Две кнопки, выгружается ровно то, что на экране (период и партнёр). «По заказам»: дата и время, партнёр, код, **номер заказа** (как в имени файла клиента), размер, сумма, начислено, дата выплаты, строка «ИТОГО». «По дням»: дата, партнёр, код, заказов, бесплатных, выручка, начислено, «ИТОГО». UTF-8 с BOM и разделитель «;»: русский Excel открывает двойным щелчком. Названия, начинающиеся с `=`, `+`, `-`, `@`, получают ведущий апостроф (защита от формул). Данных клиента (текст макета, IP, User-Agent) в файле нет |

```bash
# то же из консоли
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" "https://bannerbot.ru/api/admin/partners/stats?period=week"
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" -o orders.csv "https://bannerbot.ru/api/admin/partners/stats.csv?kind=orders&from=2026-10-01&to=2026-10-31&partner=reklamasity"
```

Налоги и способ выплаты в код не входят: сумма к выплате берётся из отчёта, перевод и оформление (договор, налог) делаются вне сервиса.

**Удалять партнёра через API и админку нельзя** (только заморозить), а код менять нельзя вообще. Для удаления заведённого по ошибке и для смены кода есть прямой SQL ниже, из каталога `web/` на сервере; формат полей там не проверяется. Обычные правки делайте в админке или через `PATCH`.

```bash
# запасной способ, если админка недоступна: изменить поле name | email | logo_url | accent | reward_rub
docker exec -i bannerprint_api python3 - КОД email new@example.ru <<'PY'
import os, sqlite3, sys
code, field, value = sys.argv[1:4]
if field not in ("name", "email", "logo_url", "accent", "reward_rub"):
    raise SystemExit("поле: name | email | logo_url | accent | reward_rub")
c = sqlite3.connect(os.getenv("WEB_DB_PATH", "/app/data/banner_web.db"))
n = c.execute(f"UPDATE partners SET {field}=? WHERE code=?", (value, code)).rowcount
c.commit()
print("изменено строк:", n)
print(c.execute("SELECT code,name,email,logo_url,accent,reward_rub FROM partners WHERE code=?", (code,)).fetchone())
PY

# удалить заведённого по ошибке (откажется, если есть заказы, начисления или отправки)
docker exec -i bannerprint_api python3 - КОД <<'PY'
import os, sqlite3, sys
code = sys.argv[1]
c = sqlite3.connect(os.getenv("WEB_DB_PATH", "/app/data/banner_web.db"))
c.execute("PRAGMA foreign_keys=ON")
n = [c.execute(f"SELECT COUNT(*) FROM {t} WHERE partner_code=?", (code,)).fetchone()[0]
     for t in ("web_orders", "partner_accruals", "partner_deliveries")]
print("заказов / начислений / отправок:", n)
if any(n):
    raise SystemExit("есть заказы или начисления, не удаляю")
print("удалено строк:", c.execute("DELETE FROM partners WHERE code=?", (code,)).rowcount)
c.commit()
PY
```

### Если что-то не так

| Симптом | Причина и что делать |
|---|---|
| Логотип не виден или очень мелкий | На сервере старый CSS: до `style.css?v=22` логотип в SVG с размером в миллиметрах рисовался 49×14 px. Проверка: `curl -s https://bannerbot.ru/ \| grep -o 'style.css?v=[0-9]*'` (нужно `v=22` и выше), деплой (или `git pull`), затем Ctrl+F5 |
| Вместо логотипа текст названия | Файл не загрузился: 404 (деплой не прошёл или не сделан `git pull`, опечатка в `logo_url`) или SVG в UTF-16/битый. `curl -sI https://bannerbot.ru/partners/<файл>` должен дать 200 и `image/svg+xml`; кодировка: см. «Логотип партнёра» |
| Страница без брендинга | Код в ссылке не совпадает с кодом партнёра, партнёр отключён или `GET /api/partner/<код>` не отдаёт 200 |
| `partner` пропал из адресной строки | Так задумано, код держится в `sessionStorage` вкладки. Делитесь исходной ссылкой, а не скопированной из браузера |
| Бренд остался на обычной ссылке | Та же вкладка помнит код партнёра: откройте новую вкладку или приватное окно |
| Клиент назвал номер заказа, а письма с ним нет | Найдите номер в карточке «Письма типографиям» на `/admin/partners/` и при необходимости отправьте повторно. Либо поиск по номеру в теме письма типографии; на сервере `docker logs bannerprint_api --since 14d 2>&1 \| grep "№ 482-915"` покажет `order_id` отправки. Нет строки: письмо не уходило, смотрите `failed_deliveries` и «Письмо не пришло». Номер не секрет и ничего не открывает: файл выдаётся только по токену скачивания (TTL 15 мин, до 3 выдач) |
| Письмо не пришло | `docker logs bannerprint_api --since 1h 2>&1 \| grep "Макет заказа"`, `failed_deliveries` в `GET /api/admin/partners`, раздел «Почта партнёрам» |
| `409` при создании партнёра | Код занят, в том числе заведённый ранее с ошибкой: исправьте в админке («Изменить») или удалите заведённого по ошибке через базу (раздел «Эксплуатация») |
| Заказ с промокодом 100% ничего не начислил | Так задумано: начисление `min(ставка, оплачено)`, оплачено 0 ₽. Письмо типографии при этом уходит |
| `500 ADMIN_TOKEN не настроен` или `403 Неверный admin-токен` | Токен не задан в `.env` контейнера или в запросе другой |

### Где лежит код

| Файл | Что в нём |
|---|---|
| `web/api/services/partner_store.py` | Партнёры, начисления, выплаты, отчёты, учёт отправок |
| `web/api/services/mailer.py` | SMTP-клиент (стандартная библиотека), читает `SMTP_*` при каждой отправке |
| `web/api/services/partner_delivery.py` | Текст письма и отправка макета партнёру |
| `web/api/services/partner_naming.py` | Номер заказа `482-915`, имя скачиваемого файла, `Content-Disposition` с UTF-8 |
| `web/api/services/partner_stats.py` | Периоды и границы суток, статистика по партнёрам и дням, CSV |
| `web/frontend/admin/partners/index.html`, `admin/stats/`, `admin/index.html` | Страница партнёров: управление и статистика; навигация между страницами админки |
| `web/api/routers/partner.py` | Публичный брендинг и админские запросы |
| `web/api/routers/order.py`, `download.py` | Привязка заказа к партнёру, начисление на `paid`, отправка при скачивании |
| `web/frontend/partner.js`, `partners/`, `style.css` | Брендирование страницы, логотипы, правила `.logo--partner`, `.partner-strip`, `.confirm-partner` |
| `web/frontend/prefill.js` | Предзаполнение из адреса |
| `web/nginx/partner-redirect.example.conf` | Шаблон редиректа с домена типографии |
| `web/ops/partner_mail_check.py` | Проверка входа на почтовый сервер и тестовое письмо |
| `web/tests/test_partner.py`, `test_partner_naming.py`, `test_partner_stats.py`, `test_partner_frontend.py`, `test_admin_pages.py` | Тесты бэкенда, номера и имени файла, статистики, фронтенда партнёрки и страниц админки |

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

# Только FSM и хранилища (без HTTP)
PYTHONPATH=. pytest web/tests/test_fsm.py web/tests/test_token_store.py web/tests/test_order_store.py -v

# Линтер (как в CI, ruff==0.4.4)
ruff check web/api/ web/tests/
```

Последний прогон: 1124 passed, 47 skipped (пропущены тесты печатного PDF без Ghostscript; в CI Ghostscript ставится и они выполняются).

| Группа | Файлы | Что проверяют |
|---|---|---|
| Заказы и оплата | `test_fsm`, `test_order_api`, `test_order_store`, `test_order_amend`, `test_order_consent`, `test_order_latency`, `test_custom_order`, `test_payment_webhook`, `test_token_store`, `test_download_api` | Переходы FSM, создание заказа, акцепт, бесплатная правка текста, webhook, токены скачивания |
| Промокоды | `test_promo`, `test_promo_report` | Резерв, возврат, автовозврат, отчёт |
| Партнёры | `test_partner`, `test_partner_naming`, `test_partner_stats`, `test_partner_frontend`, `test_ops_partner_mail_check`, `test_admin_pages` | Начисления, письма, номер заказа и имя файла, статистика и CSV, `partner.js`, логотипы, навигация админки, скрипт проверки почты |
| Рендер и PDF | `test_preview_api`, `test_renderer_preview`, `test_print_pdf`, `test_diagonal_watermark`, `test_fonts`, `test_sanitizer` | Превью, печатный PDF (чистый CMYK, масштаб 1:1; нужен Ghostscript), шрифты, санитайзер |
| Корп. API | `test_batch_charge`, `test_batch_validation` | Лимиты и валидация batch |
| Сайт и статика | `test_static_site`, `test_site_defaults`, `test_seo_pages`, `test_prefill`, `test_kit_js`, `test_stat`, `test_conversions`, `test_privacy` | Версии ассетов, SEO-страницы, предзаполнение, счётчик, соответствие политики коду |
| Админка и уведомления | `test_admin_audit`, `test_push`, `test_tg_notify`, `test_tg_relay` | Аудит заказа, Web Push, Telegram и Worker |
| Прочее | `test_db_migration`, `test_router_wiring` | Миграции колонок, подключение роутеров |

В `conftest.py` рендер через `ProcessPoolExecutor` замокан: почти все тесты работают без Ghostscript и Docker. Исключение: `test_print_pdf.py` вызывает настоящий Ghostscript и без него пропускается.

---

## Nginx и rate limits

| Зона | Лимит | Burst | Эндпоинт |
|---|---|---|---|
| `preview_limit` | 30 req/min/IP | 10 | `POST /api/preview` (правки макета идут подряд; при 429 сайт сам повторяет запрос) |
| `order_limit` | 5 req/min/IP | 2 | `POST /api/order` |
| `download_limit` | 10 req/min/IP | 3 | `GET /api/download/*` |
| `promo_limit` | 20 req/min/IP | 10 | `POST /api/promo/check` (проверка кода при вводе; защита от перебора) |
| `beat_limit` | 120 req/min/IP | 30 | `POST /api/beat` (счётчик; без передачи IP приложению, `access_log off`) |

> Все пять зон применены в `default.conf` точными `location` до общего `/api/`: `location = /api/preview`, `location = /api/order`, `location = /api/promo/check`, `location = /api/beat` и префиксный `location /api/download/`. Точный или более длинный location не наследует настройки `/api/`, поэтому прокси-заголовки в каждом повторены целиком (тест сверяет их с общим блоком).
>
> Зоны не распространяются на `/api/order/{id}/amend`, `/api/order/{id}/abandon` (он возвращает использование промокода, его нельзя душить), `/api/payment/*`, `/api/v1/*` (корпоративный API и batch считаются по ключам, а не по IP) и админку.
>
> Превышение лимита даёт `429`, ответ HTML от nginx. Сайт показывает понятные сообщения: при заказе «Слишком много попыток оформить заказ…», при скачивании «Слишком много запросов» с кнопкой «Скачать ещё раз», превью повторяется само через 2,5 / 5 / 10 секунд (новая правка макета отменяет ожидающий повтор).
> Лимит считается по `$binary_remote_addr`. Если домен проксируется Cloudflare (оранжевое облако), nginx увидит адреса Cloudflare и лимит станет общим для всех посетителей одного узла: тогда нужен `real_ip_header CF-Connecting-IP` с `set_real_ip_from` для диапазонов Cloudflare. Проверка: `curl -sI https://bannerbot.ru | grep -i cf-ray` не должна ничего выводить.
> `nginx.conf` и `default.conf` смонтированы в контейнер одиночными файлами: после `git pull` нужен перезапуск контейнера nginx, а не только reload.

`/admin/` обслуживается отдельным `location /admin/` с `alias /app/frontend/admin/`. Rate limit не применяется — защита на уровне `ADMIN_TOKEN`. `sw.js`, `manifest.json` и иконки PWA отдаются тем же `location`, менять nginx не нужно.

> ❌ `proxy_params` не использовать — вызывает конфликт директив.
> ❌ `listen 443 ssl http2` (устаревший синтаксис) → `listen 443 ssl;` и отдельная директива `http2 on;` (nginx ≥ 1.25.1, уже включена).
> Монтируются два файла: `nginx/nginx.conf` → `/etc/nginx/nginx.conf` и `nginx/default.conf` → `/etc/nginx/conf.d/default.conf`.

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
| **Хранится при заказе** | `order_id` (UUID), `size_key`, `ref_code`, `partner_code`, `promo_code`, `token` (hex), `amount_rub`, `config_json`, временные метки |
| **Акцепт соглашения** | `offer_version`, `accepted_at`, **`accepted_ip`** (X-Real-IP от nginx), **`accepted_ua`** (User-Agent, ≤300 символов) — нужны, чтобы подтвердить, что условия приняты. IP и User-Agent стираются через `ORDER_PII_RETENTION_DAYS` (14 дней), версия и время акцепта остаются |
| **Текст макета** | `config_json` / `original_config_json` содержат введённый пользователем текст; он может включать телефоны и адреса. Стирается через `ORDER_PII_RETENTION_DAYS` (14 дней после оплаты). Сервис текст не анализирует (ответственность пользователя — п. 2 Соглашения) |
| **Не хранится** | имя, tg_id пользователя, платёжные данные (карта вводится на стороне ЮKassa) |
| **Исключение** | `email` в `api_keys` — основание: исполнение договора. `email` и `phone` партнёра-типографии (`partners`) — рабочие контакты типографии, не клиентов |
| **Администратор** | `push_subscriptions` — устройства админа (не клиентов), `user_agent` ≤200 символов |
| **Третьи стороны** | ЮKassa (платежи, виджет грузится на экране проверки); типография-партнёр (только в заказах с её страницы: PDF и текст макета на её почту; IP, User-Agent и платёжные данные не передаются) |
| **Сервер** | Россия, хостинг hostland.ru (так сказано в `/privacy/`, текст закреплён тестом `test_privacy.py`) |

---

## Pending tasks

### ✅ Выполнено

- [x] FSM `OrderStatus(Enum)` + `transition()` + guard
- [x] `token_store` → SQLite `download_tokens`
- [x] `order_store` → SQLite `pending_orders`
- [x] Lifespan cleanup просроченных токенов и заказов
- [x] Автодеплой через GitHub Actions + health check
- [x] SSL Let's Encrypt для bannerbot.ru
- [x] Переезд на новый сервер, nginx на стандартных портах 80/443
- [x] Фронтенд: динамический рендер из `/api/templates`, шрифты в кнопках
- [x] Фронтенд: FAB + sticky-бар превью на мобиле, двухколонка на десктопе
- [x] Фронтенд: кастомный размер 100–3000 мм, защита от совпадения цветов
- [x] Фронтенд: футер (Соглашение, Конфиденциальность, Связаться)
- [x] `banner_generator.py`: пропорциональный layout (uniform scale), PDF y_pos через ReportLab
- [x] `banner_generator.py`: вотермарка на JPEG-превью
- [x] Переключение на ЮКасса: `services/payment.py` + `routers/payment.py`
- [x] Запасной провайдер Selfwork удалён: роутер не импортировался (функция проверки подписи лежала в другом модуле), фронт умел только виджет ЮКасса, инструкция переключения называла несуществующие модули. Код остаётся в истории git
- [x] `app.js`: модалка `#modal-pay` с виджетом ЮКасса, очистка контейнера
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
- [x] Партнёры-типографии: брендированная страница `?partner=<code>`, 100 ₽ за оплаченный заказ, макет на почту типографии (SMTP), админка, отчёт и выплаты, ссылка с предзаполнением; первый партнёр — «Реклама-Сити» подключён на проде
- [x] Партнёры: боевая почта подтверждена (консольная проверка и письма из реальных заказов)
- [x] Партнёры: телефон типографии в шапке (`tel:`), номер заказа `482-915` и телефон в имени скачиваемого файла, страница `/admin/partners/` (периоды, фильтр, CSV), навигация между страницами админки
- [x] Партнёры: добавление, правка, заморозка и восстановление из админки (`PATCH /api/admin/partners/{code}`)
- [x] Партнёры: письма типографиям в админке (состояние по заказам, поиск по номеру, повтор отправки)
- [x] Бесплатная правка текста после оплаты (`/api/order/{id}/amend`), собственный счётчик без cookie (`kit.js`, `/api/beat`), стирание текста и IP через 14 дней
- [x] Nginx: `http2 on;`, зоны лимитов `preview` / `order` / `download` / `promo` / `beat`
- [x] Очистка `/tmp/bannerprint_batches` по TTL (`batch_worker.cleanup_old_batches`)

### P1 — партнёры

- [ ] Выплата из админки (сейчас через API), удаление партнёра (сейчас прямым SQL)
- [ ] Единая граница месяца: статистика считает сутки по UTC+3, `report` и `payout` по UTC

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

- [ ] Preview → `image/jpeg` streaming вместо base64 (+33% overhead)
- [ ] Structured JSON logging
- [ ] Postman-коллекция из `/api/docs`

---

## Персональные данные, счётчики, журналы

Принцип: не собирать лишнего и хранить как можно меньше. Публичный документ: `/privacy/` (генерируется из `web/seo/build_pages.py`); тесты `test_privacy.py` следят, чтобы он не расходился с кодом.

| Что | Где | Срок |
|---|---|---|
| Текст макета, IP и User-Agent оплаченного заказа | `web_orders` | `ORDER_PII_RETENTION_DAYS` (14 дней после оплаты), стирает фоновый цикл `services/retention.py` |
| Неоплаченный заказ | `pending_orders` | 30 минут |
| Журнал nginx | `/var/log/nginx/access.log`, IP сокращён до сети /24 | 14 дней, ротация `web/ops/logrotate-bannerbot` |
| Статистика посещений | таблица `stat_counters` (только суммарные числа за день) | без ограничения, это не персональные данные |

- **Cookie нет, сторонних счётчиков (Метрика, Google) и внешних шрифтов нет.** Шрифты в `web/frontend/fonts` (латиница и кириллица, лицензии OFL рядом). SDK ЮKassa грузится только при открытии экрана проверки.
- **Собственный счётчик:** `web/frontend/kit.js` шлёт `POST /api/beat` (событие, страница, категория источника, метка). nginx не передаёт приложению IP этого запроса и не пишет его в журнал. Имена `kit.js` и `/api/beat` выбраны нейтральными: `stat.js` и `/api/stat` попадают под правила блокировщиков (EasyPrivacy). Счётчик не считает роботов (по User-Agent и `navigator.webdriver`) и посетителей с DNT / GPC; владелец отключает его у себя, открыв любую страницу с `?notrack=1` (в `localStorage` сохраняется флаг `nt`).
- **Конверсии считает сервер:** вместе с заказом приходит метка входа `entry` (страница сайта или `yandex` / `google` / `direct`, не персональные данные), хранится в `config_json` и используется для событий `order_paid` (оплата, `transition`) и `file_downloaded` (выдача файла, `download.py`) с источником `server`. Они не зависят от блокировщиков, перезагрузки и редиректа оплаты (3DS, СБП). События браузера: `pageview`, `prefill`, `preview_generated`, `start_order`, `payment_started`, `download_success`, `example_click`, `cta_click`, `custom_contact_click`, `custom_link_click`; серверные: `order_paid`, `file_downloaded`. Новые добавляются в `stat_store.EVENTS`.
- **Чтение статистики:** `curl -H "Authorization: Bearer $ADMIN_TOKEN" "https://bannerbot.ru/api/admin/events?days=30"`. Подвоз с поисковиков: события `pageview` с `source` = `yandex` / `google`; конверсии воронки несут в `detail` страницу, с которой человек пришёл.
- **Ротация журналов (один раз на сервере, нужен root):** `sudo cp web/ops/logrotate-bannerbot /etc/logrotate.d/bannerbot && sudo logrotate -d /etc/logrotate.d/bannerbot`.
- Меняете срок хранения: правьте `ORDER_PII_RETENTION_DAYS` (env), `ORDER_RETENTION_DAYS` в генераторе, `rotate` в logrotate и текст в окне «Конфиденциальность» и п. 6 соглашения; тест сверит совпадение.
