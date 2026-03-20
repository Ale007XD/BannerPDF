"""
test_order_store.py
~~~~~~~~~~~~~~~~~~~
Тесты хранилища pending_orders (SQLite TTL-буфер).

Покрывает:
  - save_pending: сохраняет config_json, upsert при конфликте
  - get_pending: возвращает dict для актуальной записи
  - get_pending: None для истёкшей записи
  - get_pending: None для несуществующего order_id
  - delete_pending: удаляет запись
  - cleanup_expired: удаляет просроченные, оставляет актуальные
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from web.api.services.order_store import (
    cleanup_expired,
    delete_pending,
    get_pending,
    save_pending,
)

SAMPLE_CONFIG = {
    "size_key": "1x0.5",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [{"text": "Тест", "scale": 1.0}],
}


def _insert_expired_pending(db_path: str, order_id: str) -> None:
    """Вставляет запись с истёкшим TTL напрямую."""
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pending_orders (order_id, config_json, expires_at) VALUES (?, ?, ?)",
        (order_id, '{"test": true}', past),
    )
    conn.commit()
    conn.close()


class TestSavePending:

    def test_saves_config_json(self, init_test_db):
        """save_pending записывает config в pending_orders."""
        save_pending("order-ps-001", SAMPLE_CONFIG)
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT config_json FROM pending_orders WHERE order_id = ?",
            ("order-ps-001",),
        ).fetchone()
        conn.close()
        assert row is not None
        import json
        saved = json.loads(row["config_json"])
        assert saved["size_key"] == "1x0.5"

    def test_upsert_on_conflict(self, init_test_db):
        """Повторный save_pending с тем же order_id обновляет запись."""
        save_pending("order-ps-002", SAMPLE_CONFIG)
        updated_config = dict(SAMPLE_CONFIG, bg_color="Черный")
        save_pending("order-ps-002", updated_config)
        result = get_pending("order-ps-002")
        assert result["bg_color"] == "Черный"

    def test_custom_ttl(self, init_test_db):
        """save_pending принимает кастомный ttl_seconds."""
        save_pending("order-ps-003", SAMPLE_CONFIG, ttl_seconds=60)
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT expires_at FROM pending_orders WHERE order_id = ?",
            ("order-ps-003",),
        ).fetchone()
        conn.close()
        expires = datetime.fromisoformat(row["expires_at"])
        # expires_at должен быть примерно через 60 секунд
        now = datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        delta = (expires - now).total_seconds()
        assert 55 < delta < 65


class TestGetPending:

    def test_returns_config_dict(self, init_test_db):
        """get_pending возвращает dict с конфигом."""
        save_pending("order-pg-001", SAMPLE_CONFIG)
        result = get_pending("order-pg-001")
        assert isinstance(result, dict)
        assert result["size_key"] == "1x0.5"
        assert result["text_lines"][0]["text"] == "Тест"

    def test_returns_none_for_missing(self, init_test_db):
        """get_pending для несуществующего order_id → None."""
        assert get_pending("no-such-order") is None

    def test_returns_none_for_expired(self, init_test_db):
        """get_pending для истёкшей записи → None."""
        _insert_expired_pending(init_test_db, "order-pg-002")
        assert get_pending("order-pg-002") is None


class TestDeletePending:

    def test_delete_removes_entry(self, init_test_db):
        """delete_pending удаляет запись."""
        save_pending("order-pd-001", SAMPLE_CONFIG)
        delete_pending("order-pd-001")
        assert get_pending("order-pd-001") is None

    def test_delete_nonexistent_is_safe(self, init_test_db):
        """delete_pending несуществующего order_id не бросает исключений."""
        delete_pending("never-existed")  # не должно падать


class TestCleanupExpired:

    def test_removes_expired_entries(self, init_test_db):
        """cleanup_expired удаляет записи с истёкшим TTL."""
        _insert_expired_pending(init_test_db, "order-pc-001")
        count = cleanup_expired()
        assert count >= 1

    def test_keeps_active_entries(self, init_test_db):
        """cleanup_expired не трогает актуальные записи."""
        save_pending("order-pc-002", SAMPLE_CONFIG)
        cleanup_expired()
        assert get_pending("order-pc-002") is not None
