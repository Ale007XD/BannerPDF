import sqlite3
from datetime import datetime, timezone

from web.api.services.token_store import consume_token, create_token


def _insert_order(db_path: str, order_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) VALUES (?, 299, '1x0.5', '{}', 'paid', ?)",
        (order_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

class TestTokenStore:
    def test_tokens_are_unique(self, init_test_db):
        _insert_order(init_test_db, "order-tk-002")
        t1 = create_token("order-tk-002")
        t2 = create_token("order-tk-002")
        assert t1 != t2

    def test_valid_token_returns_order_id(self, init_test_db):
        _insert_order(init_test_db, "order-tk-010")
        token = create_token("order-tk-010")
        assert consume_token(token) == "order-tk-010"

    def test_one_time_use(self, init_test_db):
        _insert_order(init_test_db, "order-tk-012")
        token = create_token("order-tk-012")
        assert consume_token(token) == "order-tk-012"
        assert consume_token(token) is None