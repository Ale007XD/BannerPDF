-- ============================================================
-- banner_web.db — схема базы данных сайта BannerPrint
-- SQLite WAL-режим, отдельная от banner_bot.db
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Корпоративные API-планы (предзаполняются при инициализации)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_plans (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    pdf_limit   INTEGER NOT NULL, -- -1 = безлимит
    rpm_limit   INTEGER NOT NULL,
    price_rub   INTEGER NOT NULL
);

-- Предзаполнение планов (INSERT OR IGNORE — идемпотентно)
INSERT OR IGNORE INTO api_plans (id, name, pdf_limit, rpm_limit, price_rub) VALUES
    ('trial',      'Trial',      3,    5,   0),
    ('starter',    'Starter',    100,  10,  1900),
    ('business',   'Business',   1000, 60,  9900),
    ('enterprise', 'Enterprise', -1,   300, 0);

-- ------------------------------------------------------------
-- Корпоративные API-ключи
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT    NOT NULL UNIQUE,   -- sha256(key), не сам ключ
    key_prefix   TEXT    NOT NULL,          -- первые 12 символов для отображения
    plan_id      TEXT    NOT NULL REFERENCES api_plans(id),
    label        TEXT    NOT NULL,
    email        TEXT    NOT NULL,          -- основание: исполнение договора (152-ФЗ)
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT,                      -- NULL = бессрочно
    pdf_used     INTEGER NOT NULL DEFAULT 0,
    period_start TEXT    NOT NULL
);

-- ------------------------------------------------------------
-- Заказы (статус управляется FSM)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS web_orders (
    id                  TEXT    PRIMARY KEY,  -- UUID4 = order_id
    amount_rub          INTEGER NOT NULL,
    size_key            TEXT    NOT NULL,
    ref_code            TEXT,                 -- реферальный код, может быть NULL
    promo_code          TEXT,                 -- применённый промокод, может быть NULL
    config_json         TEXT    NOT NULL,     -- JSON конфига баннера (постоянное хранение)
    status              TEXT    NOT NULL DEFAULT 'pending',
    -- pending | paid | token_issued | expired
    created_at          TEXT    NOT NULL,
    paid_at             TEXT,                 -- NULL пока не оплачен
    yookassa_payment_id TEXT,                 -- ID платежа в ЮКасса (для верификации webhook)
    tg_message_id       INTEGER,              -- ID сообщения в TG для обновления статуса
    -- Акцепт условий на экране проверки макета (NULL у заказов до введения экрана)
    offer_version       TEXT,                 -- редакция соглашения (OFFER_VERSION в routers/order.py)
    accepted_at         TEXT,                 -- UTC, ISO 8601
    accepted_ip         TEXT,                 -- IP клиента (X-Real-IP от nginx)
    accepted_ua         TEXT,                 -- User-Agent, обрезан до 300 символов
    -- Единственная бесплатная правка текста в течение AMEND_WINDOW_HOURS после оплаты
    amended_at          TEXT,                 -- UTC; NOT NULL = правка использована
    original_config_json TEXT                 -- config_json до правки (для разбора споров)
);

CREATE INDEX IF NOT EXISTS idx_web_orders_status  ON web_orders(status);
CREATE INDEX IF NOT EXISTS idx_web_orders_created ON web_orders(created_at);

-- ------------------------------------------------------------
-- Download-токены (TTL 15 мин, до MAX_DOWNLOADS успешных выдач PDF)
-- used = TRUE, когда лимит выдач исчерпан (см. services/token_store.py)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS download_tokens (
    token      TEXT    PRIMARY KEY,  -- 32 bytes hex (64 символа)
    order_id   TEXT    NOT NULL REFERENCES web_orders(id),
    expires_at TEXT    NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE,
    downloads  INTEGER NOT NULL DEFAULT 0  -- число успешных выдач PDF
);

CREATE INDEX IF NOT EXISTS idx_tokens_order   ON download_tokens(order_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON download_tokens(expires_at);

-- ------------------------------------------------------------
-- Pending-заказы (TTL-буфер до webhook, 30 мин)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id    TEXT NOT NULL PRIMARY KEY,
    config_json TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_orders(expires_at);

-- ------------------------------------------------------------
-- Промокоды
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promo_codes (
    code       TEXT    PRIMARY KEY,         -- напр. "AVITO26"
    uses_left  INTEGER NOT NULL DEFAULT 0,  -- убывает при каждом применении
    expires_at TEXT,                        -- NULL = бессрочно
    discount   INTEGER NOT NULL DEFAULT 100 -- % скидки; 100 = бесплатно, 50 = половина
);

-- ------------------------------------------------------------
-- Batch-задачи
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS batch_jobs (
    id          TEXT    PRIMARY KEY,
    api_key_id  INTEGER NOT NULL REFERENCES api_keys(id),
    status      TEXT    NOT NULL DEFAULT 'queued',
    -- queued | processing | ready | failed
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT    NOT NULL DEFAULT '[]',
    created_at  TEXT    NOT NULL,
    ready_at    TEXT,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_status ON batch_jobs(status);

-- ------------------------------------------------------------
-- Рефераллы (без персональных данных, 152-ФЗ)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS referrers (
    ref_code    TEXT    PRIMARY KEY,  -- 8 символов A-Z0-9
    balance_rub INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id  TEXT    NOT NULL REFERENCES referrers(ref_code),
    order_id     TEXT    NOT NULL UNIQUE,  -- UNIQUE = идемпотентность начислений
    order_amount INTEGER NOT NULL,
    commission   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    paid_out     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);

-- ------------------------------------------------------------
-- Подписки Web Push: устройства админа, на которые уходят уведомления о заказах
-- (PWA админки, services/push_notify.py). Один endpoint = одно устройство/браузер.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT    NOT NULL UNIQUE,   -- адрес в push-сервисе браузера (FCM, Mozilla, Apple, WNS)
    p256dh      TEXT    NOT NULL,          -- публичный ключ устройства, base64url
    auth        TEXT    NOT NULL,          -- секрет аутентификации, base64url
    user_agent  TEXT,                      -- для списка устройств, обрезан до 200 символов
    fail_count  INTEGER NOT NULL DEFAULT 0,-- подряд неудачных отправок; сбрасывается при успехе
    created_at  TEXT    NOT NULL,
    last_ok_at  TEXT
);
