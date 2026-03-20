"""
test_token_store.py
~~~~~~~~~~~~~~~~~~~
Тесты хранилища одноразовых download-токенов (SQLite).

Покрывает:
  - create_token: формат, уникальность
  - consume_token: валидный → возвращает order_id и помечает used
  - consume_token: повторный вызов → None (одноразовость)
  - consume_token: истёкший токен → None
  - consume_token: несуществующий токен → None
  - cleanup_expired: удаляет просроченные и использованные
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from web.api.services.token_store import (
    cleanup_expired,
    consume_token,
    create_token,
)


def _insert_order(db_path: str, order_id: str) -> None:
    """Вставляет минимальный заказ для FK в download_tokens."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) "
        "VALUES (?, 299, '1x0.5', '{}', 'paid', ?)",
        (order_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _insert_expired_token(db_path: str, order_id: str) -> str:
    """Вставляет токен с истёкшим TTL напрямую в БД."""
    import secrets
    token = secrets.token_hex(32)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO download_tokens (token, order_id, expires_at, used) VALUES (?, ?, ?, FALSE)",
        (token, order_id, past),
    )
    conn.commit()
    conn.close()
    return token


class TestCreateToken:

    def test_returns_64_char_hex(self, init_test_db):
        """create_token возвращает hex-строку длиной 64 символа (32 байта)."""
        _insert_order(init_test_db, "order-tk-001")
        token = create_token("order-tk-001")
        assert isinstance(token, str)
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)

    def test_tokens_are_unique(self, init_test_db):
        """Два вызова create_token возвращают разные токены."""
        _insert_order(init_test_db, "order-tk-002")
        t1 = create_token("order-tk-002")
        t2 = create_token("order-tk-002")
        assert t1 != t2

    def test_token_saved_to_db(self, init_test_db):
        """create_token сохраняет запись в download_tokens."""
        _insert_order(init_test_db, "order-tk-003")
        token = create_token("order-tk-003")
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT order_id, used FROM download_tokens WHERE token = ?", (token,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["order_id"] == "order-tk-003"
        assert row["used"] == 0


class TestConsumeToken:

    def test_valid_token_returns_order_id(self, init_test_db):
        """consume_token на валидный токен возвращает order_id."""
        _insert_order(init_test_db, "order-tk-010")
        token = create_token("order-tk-010")
        result = consume_token(token)
        assert result == "order-tk-010"

    def test_token_marked_used_after_consume(self, init_test_db):
        """После consume токен помечается used=TRUE в БД."""
        _insert_order(init_test_db, "order-tk-011")
        token = create_token("order-tk-011")
        consume_token(token)
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT used FROM download_tokens WHERE token = ?", (token,)
        ).fetchone()
        conn.close()
        assert row["used"] == 1

    def test_one_time_use(self, init_test_db):
        """Повторный consume_token на использованный токен → None."""
        _insert_order(init_test_db, "order-tk-012")
        token = create_token("order-tk-012")
        assert consume_token(token) == "order-tk-012"
        assert consume_token(token) is None

    def test_expired_token_returns_none(self, init_test_db):
        """Истёкший токен → None."""
        _insert_order(init_test_db, "order-tk-013")
        token = _insert_expired_token(init_test_db, "order-tk-013")
        assert consume_token(token) is None

    def test_nonexistent_token_returns_none(self, init_test_db):
        """Несуществующий токен → None."""
        assert consume_token("a" * 64) is None


class TestCleanupExpired:

    def test_cleanup_removes_expired_tokens(self, init_test_db):
        """cleanup_expired удаляет истёкшие токены."""
        _insert_order(init_test_db, "order-tk-020")
        _insert_expired_token(init_test_db, "order-tk-020")
        count = cleanup_expired()
        assert count >= 1

    def test_cleanup_removes_used_tokens(self, init_test_db):
        """cleanup_expired удаляет использованные токены."""
        _insert_order(init_test_db, "order-tk-021")
        token = create_token("order-tk-021")
        consume_token(token)
        count = cleanup_expired()
        assert count >= 1

    def test_cleanup_keeps_valid_tokens(self, init_test_db):
        """cleanup_expired не удаляет действующие токены."""
        _insert_order(init_test_db, "order-tk-022")
        create_token("order-tk-022")
        cleanup_expired()
        conn = sqlite3.connect(init_test_db)
        remaining = conn.execute(
            "SELECT COUNT(*) FROM download_tokens WHERE order_id = ? AND used = FALSE",
            ("order-tk-022",),
        ).fetchone()[0]
        conn.close()
        assert remaining == 1
