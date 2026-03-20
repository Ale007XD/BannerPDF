"""
test_fsm.py
~~~~~~~~~~~
Тесты FSM заказов: transition(), OrderStatus, guard-условия.

Покрывает:
  - Все допустимые переходы (webhook_paid, issue_token, ttl_expired, ttl_paid)
  - Недопустимые переходы → ValueError
  - Неизвестное событие → ValueError
  - Несуществующий заказ → ValueError
  - Идемпотентность (повторный переход в целевой статус)
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from web.api.routers.order import OrderStatus, transition


def _insert_order(db_path: str, order_id: str, status: str) -> None:
    """Вставляет тестовый заказ напрямую в БД."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO web_orders
            (id, amount_rub, size_key, config_json, status, created_at)
        VALUES (?, 299, '1x0.5', '{}', ?, ?)
        """,
        (order_id, status, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _get_status(db_path: str, order_id: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM web_orders WHERE id = ?", (order_id,)
    ).fetchone()
    conn.close()
    return row["status"]


# ---------------------------------------------------------------------------
# Допустимые переходы
# ---------------------------------------------------------------------------

class TestValidTransitions:

    def test_webhook_paid_pending_to_paid(self, init_test_db):
        """pending --[webhook_paid]--> paid"""
        _insert_order(init_test_db, "order-001", "pending")
        result = transition("order-001", "webhook_paid")
        assert result == OrderStatus.PAID
        assert _get_status(init_test_db, "order-001") == "paid"

    def test_issue_token_paid_to_token_issued(self, init_test_db):
        """paid --[issue_token]--> token_issued"""
        _insert_order(init_test_db, "order-002", "paid")
        result = transition("order-002", "issue_token")
        assert result == OrderStatus.TOKEN_ISSUED
        assert _get_status(init_test_db, "order-002") == "token_issued"

    def test_ttl_expired_pending_to_expired(self, init_test_db):
        """pending --[ttl_expired]--> expired"""
        _insert_order(init_test_db, "order-003", "pending")
        result = transition("order-003", "ttl_expired")
        assert result == OrderStatus.EXPIRED
        assert _get_status(init_test_db, "order-003") == "expired"

    def test_ttl_paid_paid_to_expired(self, init_test_db):
        """paid --[ttl_paid]--> expired"""
        _insert_order(init_test_db, "order-004", "paid")
        result = transition("order-004", "ttl_paid")
        assert result == OrderStatus.EXPIRED
        assert _get_status(init_test_db, "order-004") == "expired"

    def test_webhook_paid_sets_paid_at(self, init_test_db):
        """webhook_paid должен проставить paid_at в БД."""
        _insert_order(init_test_db, "order-005", "pending")
        transition("order-005", "webhook_paid")
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT paid_at FROM web_orders WHERE id = ?", ("order-005",)
        ).fetchone()
        conn.close()
        assert row["paid_at"] is not None


# ---------------------------------------------------------------------------
# Недопустимые переходы — должны бросать ValueError
# ---------------------------------------------------------------------------

class TestInvalidTransitions:

    def test_cannot_go_paid_to_pending(self, init_test_db):
        """paid --[ttl_expired]--> pending → недопустимо (from=pending)."""
        _insert_order(init_test_db, "order-010", "paid")
        with pytest.raises(ValueError, match="недопустимый переход"):
            transition("order-010", "ttl_expired")

    def test_cannot_go_token_issued_to_paid(self, init_test_db):
        """token_issued --[webhook_paid]--> paid → недопустимо."""
        _insert_order(init_test_db, "order-011", "token_issued")
        with pytest.raises(ValueError, match="недопустимый переход"):
            transition("order-011", "webhook_paid")

    def test_cannot_go_expired_to_paid(self, init_test_db):
        """expired --[webhook_paid]--> paid → недопустимо."""
        _insert_order(init_test_db, "order-012", "expired")
        with pytest.raises(ValueError, match="недопустимый переход"):
            transition("order-012", "webhook_paid")

    def test_unknown_event_raises(self, init_test_db):
        """Неизвестное событие → ValueError."""
        _insert_order(init_test_db, "order-013", "pending")
        with pytest.raises(ValueError, match="Неизвестное событие FSM"):
            transition("order-013", "nonexistent_event")

    def test_missing_order_raises(self, init_test_db):
        """Несуществующий order_id → ValueError."""
        with pytest.raises(ValueError, match="не найден"):
            transition("no-such-order", "webhook_paid")


# ---------------------------------------------------------------------------
# Идемпотентность
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_idempotent_already_paid(self, init_test_db):
        """Повторный webhook_paid на уже paid заказ → возвращает paid без ошибки."""
        _insert_order(init_test_db, "order-020", "paid")
        result = transition("order-020", "webhook_paid")
        assert result == OrderStatus.PAID
        # Статус не изменился
        assert _get_status(init_test_db, "order-020") == "paid"

    def test_idempotent_already_token_issued(self, init_test_db):
        """Повторный issue_token на уже token_issued заказ → возвращает token_issued."""
        _insert_order(init_test_db, "order-021", "token_issued")
        result = transition("order-021", "issue_token")
        assert result == OrderStatus.TOKEN_ISSUED
