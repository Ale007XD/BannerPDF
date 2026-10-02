"""
test_db_migration.py
~~~~~~~~~~~~~~~~~~~~
init_db() донакатывает колонки на уже развёрнутой БД (старая схема).
"""

import re
import sqlite3
from pathlib import Path

from web.api.db import init_db

SCHEMA = Path(__file__).parent.parent / "api" / "db" / "schema.sql"


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return cols


def _make_legacy_db(db_path: str) -> None:
    """Схема в том виде, как она была до колонки downloads."""
    sql = SCHEMA.read_text(encoding="utf-8")
    legacy = sql.replace(
        "used       BOOLEAN NOT NULL DEFAULT FALSE,\n"
        "    downloads  INTEGER NOT NULL DEFAULT 0  -- число успешных выдач PDF\n",
        "used       BOOLEAN NOT NULL DEFAULT FALSE\n",
    )
    legacy_orders = re.sub(
        r"tg_message_id\s+INTEGER,\s+--[^\n]*\n.*?\n\);",
        "tg_message_id       INTEGER               -- ID сообщения в TG для обновления статуса\n);",
        legacy,
        count=1,
        flags=re.S,
    )
    assert legacy_orders != legacy, "шаблон legacy-схемы web_orders устарел"
    legacy = legacy_orders
    assert legacy != sql, "шаблон legacy-схемы устарел"
    conn = sqlite3.connect(db_path)
    conn.executescript(legacy)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) "
        "VALUES ('legacy-1', 299, '1x0.5', '{}', 'token_issued', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO download_tokens (token, order_id, expires_at, used) "
        "VALUES ('legacy-token', 'legacy-1', '2026-01-01T00:15:00+00:00', 0)"
    )
    conn.commit()
    conn.close()


def test_init_db_adds_missing_columns_and_keeps_data(tmp_db_path):
    _make_legacy_db(tmp_db_path)
    assert "downloads" not in _columns(tmp_db_path, "download_tokens")

    init_db()

    assert "downloads" in _columns(tmp_db_path, "download_tokens")
    conn = sqlite3.connect(tmp_db_path)
    row = conn.execute(
        "SELECT used, downloads FROM download_tokens WHERE token = 'legacy-token'"
    ).fetchone()
    conn.close()
    assert row == (0, 0)


def test_init_db_adds_acceptance_columns_to_web_orders(tmp_db_path):
    _make_legacy_db(tmp_db_path)
    before = _columns(tmp_db_path, "web_orders")
    assert not {"offer_version", "accepted_at", "accepted_ip", "accepted_ua",
                "amended_at", "original_config_json"} & before

    init_db()

    assert {
        "offer_version", "accepted_at", "accepted_ip", "accepted_ua",
        "amended_at", "original_config_json",
    } <= _columns(tmp_db_path, "web_orders")
    conn = sqlite3.connect(tmp_db_path)
    row = conn.execute(
        "SELECT offer_version, accepted_at FROM web_orders WHERE id = 'legacy-1'"
    ).fetchone()
    conn.close()
    assert row == (None, None)  # старые заказы остаются без акцепта


def test_init_db_is_idempotent(tmp_db_path):
    _make_legacy_db(tmp_db_path)
    init_db()
    init_db()  # повторный запуск не падает на duplicate column
    assert "downloads" in _columns(tmp_db_path, "download_tokens")


def test_init_db_on_empty_database(tmp_db_path):
    init_db()
    assert "downloads" in _columns(tmp_db_path, "download_tokens")
