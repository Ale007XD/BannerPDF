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
  - peek_token / record_download: проверка без списания, лимит MAX_DOWNLOADS
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from web.api.services.token_store import (
    MAX_DOWNLOADS,
    cleanup_expired,
    consume_token,
    create_token,
    peek_token,
    record_download,
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


def _token_row(db_path: str, token: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT used, downloads FROM download_tokens WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    return row


class TestPeekToken:

    def test_valid_token_returns_order_id_without_side_effects(self, init_test_db):
        """peek_token не меняет состояние токена."""
        _insert_order(init_test_db, "order-tk-030")
        token = create_token("order-tk-030")
        assert peek_token(token) == "order-tk-030"
        assert peek_token(token) == "order-tk-030"
        row = _token_row(init_test_db, token)
        assert row["used"] == 0
        assert row["downloads"] == 0

    def test_expired_token_returns_none(self, init_test_db):
        _insert_order(init_test_db, "order-tk-031")
        token = _insert_expired_token(init_test_db, "order-tk-031")
        assert peek_token(token) is None

    def test_nonexistent_token_returns_none(self, init_test_db):
        assert peek_token("b" * 64) is None


class TestRecordDownload:

    def test_first_download_counts_and_keeps_token_valid(self, init_test_db):
        _insert_order(init_test_db, "order-tk-040")
        token = create_token("order-tk-040")
        assert record_download(token) is True
        row = _token_row(init_test_db, token)
        assert row["downloads"] == 1
        assert row["used"] == 0
        assert peek_token(token) == "order-tk-040"

    def test_token_exhausted_after_max_downloads(self, init_test_db):
        _insert_order(init_test_db, "order-tk-041")
        token = create_token("order-tk-041")
        for _ in range(MAX_DOWNLOADS):
            assert record_download(token) is True
        row = _token_row(init_test_db, token)
        assert row["downloads"] == MAX_DOWNLOADS
        assert row["used"] == 1
        assert peek_token(token) is None

    def test_download_over_limit_is_refused_and_not_counted(self, init_test_db):
        _insert_order(init_test_db, "order-tk-042")
        token = create_token("order-tk-042")
        for _ in range(MAX_DOWNLOADS):
            record_download(token)
        assert record_download(token) is False
        assert _token_row(init_test_db, token)["downloads"] == MAX_DOWNLOADS

    def test_exhausted_token_is_cleaned_up(self, init_test_db):
        _insert_order(init_test_db, "order-tk-043")
        token = create_token("order-tk-043")
        for _ in range(MAX_DOWNLOADS):
            record_download(token)
        assert cleanup_expired() >= 1
        assert _token_row(init_test_db, token) is None
