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
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    pdf_limit INTEGER NOT NULL,   -- -1 = безлимит
    rpm_limit INTEGER NOT NULL,
    price_rub INTEGER NOT NULL
);

-- Предзаполнение планов (INSERT OR IGNORE — идемпотентно)
INSERT OR IGNORE INTO api_plans (id, name, pdf_limit, rpm_limit, price_rub)
VALUES
    ('starter',    'Starter',    100,   10,    1900),
    ('business',   'Business',   1000,  60,    9900),
    ('enterprise', 'Enterprise', -1,    300,   0);

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
    id          TEXT    PRIMARY KEY,        -- UUID4 = order_id
    amount_rub  INTEGER NOT NULL,
    size_key    TEXT    NOT NULL,
    ref_code    TEXT,                       -- реферальный код, может быть NULL
    config_json TEXT    NOT NULL,           -- JSON конфига баннера (постоянное хранение)
    status      TEXT    NOT NULL DEFAULT 'pending',
                                            -- pending | paid | token_issued | expired
    created_at  TEXT    NOT NULL,
    paid_at               TEXT,                       -- NULL пока не оплачен
    yookassa_payment_id   TEXT,                       -- ID платежа в ЮKassa (для верификации webhook)
    tg_message_id         INTEGER                     -- ID сообщения в TG для обновления статуса
);

CREATE INDEX IF NOT EXISTS idx_web_orders_status ON web_orders(status);
CREATE INDEX IF NOT EXISTS idx_web_orders_created ON web_orders(created_at);

-- ------------------------------------------------------------
-- Download-токены (одноразовые, TTL 15 мин)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS download_tokens (
    token       TEXT    PRIMARY KEY,        -- 32 bytes hex (64 символа)
    order_id    TEXT    NOT NULL REFERENCES web_orders(id),
    expires_at  TEXT    NOT NULL,
    used        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_tokens_order ON download_tokens(order_id);
CREATE INDEX IF NOT EXISTS idx_tokens_expires ON download_tokens(expires_at);

-- ------------------------------------------------------------
-- Pending-заказы (TTL-буфер до webhook, 30 мин)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_orders (
    order_id    TEXT    PRIMARY KEY,
    config_json TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending_orders(expires_at);

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
    ref_code    TEXT    PRIMARY KEY,        -- 8 символов A-Z0-9
    balance_rub INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS referrals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id  TEXT    NOT NULL REFERENCES referrers(ref_code),
    order_id     TEXT    NOT NULL UNIQUE,   -- UNIQUE = идемпотентность начислений
    order_amount INTEGER NOT NULL,
    commission   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    paid_out     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
