import sqlite3
from datetime import datetime, timezone

import pytest

from web.api.routers.order import OrderStatus, transition


def _insert_order(db_path: str, order_id: str, status: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) VALUES (?, 299, '1x0.5', '{}', ?, ?)",
        (order_id, status, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

def _get_status(db_path: str, order_id: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row["status"]

class TestValidTransitions:
    def test_webhook_paid_pending_to_paid(self, init_test_db):
        _insert_order(init_test_db, "order-001", "pending")
        result = transition("order-001", "webhook_paid")
        assert result == OrderStatus.PAID
        assert _get_status(init_test_db, "order-001") == "paid"

    def test_issue_token_paid_to_token_issued(self, init_test_db):
        _insert_order(init_test_db, "order-002", "paid")
        result = transition("order-002", "issue_token")
        assert result == OrderStatus.TOKEN_ISSUED
        assert _get_status(init_test_db, "order-002") == "token_issued"

    def test_webhook_paid_sets_paid_at(self, init_test_db):
        _insert_order(init_test_db, "order-005", "pending")
        transition("order-005", "webhook_paid")
        conn = sqlite3.connect(init_test_db)
        row = conn.execute("SELECT paid_at FROM web_orders WHERE id = 'order-005'").fetchone()
        conn.close()
        assert row[0] is not None

class TestInvalidTransitions:
    def test_cannot_go_paid_to_pending(self, init_test_db):
        _insert_order(init_test_db, "order-010", "paid")
        with pytest.raises(ValueError, match="недопустимый переход"):
            transition("order-010", "ttl_expired")

    def test_unknown_event_raises(self, init_test_db):
        _insert_order(init_test_db, "order-013", "pending")
        with pytest.raises(ValueError, match="Неизвестное событие FSM"):
            transition("order-013", "nonexistent_event")

class TestIdempotency:
    def test_idempotent_already_paid(self, init_test_db):
        _insert_order(init_test_db, "order-020", "paid")
        assert transition("order-020", "webhook_paid") == OrderStatus.PAID
        assert _get_status(init_test_db, "order-020") == "paid"