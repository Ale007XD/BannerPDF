"""
test_order_consent.py
~~~~~~~~~~~~~~~~~~~~~
POST /api/order требует явного акцепта (accept_terms) и фиксирует его в web_orders.

Покрывает:
  - без accept_terms / с accept_terms=false → 422, заказ не создаётся
  - с accept_terms=true → 200, в заказе: версия соглашения, время, IP, User-Agent
  - IP берётся из X-Real-IP (nginx), иначе из соединения
  - User-Agent обрезается до 300 символов
  - бесплатный заказ по промокоду тоже требует и фиксирует акцепт
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from web.api.routers.order import OFFER_VERSION

PAYLOAD = {
    "size_key":   "1x0.5",
    "bg_color":   "Белый",
    "text_color": "Черный",
    "font":       "Golos Text",
    "text_lines": [{"text": "Аренда склада +7 914 000-00-00", "scale": 100}],
}

PAYMENT = {"payment_id": "yk-test-1", "confirmation_token": "ct-test-1"}


def _orders(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM web_orders").fetchall()
    conn.close()
    return rows


def _paid_flow():
    return patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT))


class TestAcceptTermsRequired:

    @pytest.mark.asyncio
    async def test_missing_flag_is_rejected(self, client, init_test_db):
        with _paid_flow():
            resp = await client.post("/api/order", json=PAYLOAD)
        assert resp.status_code == 422
        assert "соглашени" in resp.json()["detail"].lower()
        assert _orders(init_test_db) == []

    @pytest.mark.asyncio
    async def test_false_flag_is_rejected(self, client, init_test_db):
        with _paid_flow():
            resp = await client.post("/api/order", json={**PAYLOAD, "accept_terms": False})
        assert resp.status_code == 422
        assert _orders(init_test_db) == []


class TestAcceptanceRecorded:

    @pytest.mark.asyncio
    async def test_acceptance_is_stored_with_order(self, client, init_test_db):
        before = datetime.now(timezone.utc)
        with _paid_flow():
            resp = await client.post(
                "/api/order",
                json={**PAYLOAD, "accept_terms": True},
                headers={"X-Real-IP": "203.0.113.7", "User-Agent": "TestBrowser/1.0"},
            )
        assert resp.status_code == 200
        (row,) = _orders(init_test_db)
        assert row["offer_version"] == OFFER_VERSION
        assert row["accepted_ip"] == "203.0.113.7"
        assert row["accepted_ua"] == "TestBrowser/1.0"
        assert datetime.fromisoformat(row["accepted_at"]) >= before

    @pytest.mark.asyncio
    async def test_ip_falls_back_to_connection_address(self, client, init_test_db):
        with _paid_flow():
            resp = await client.post("/api/order", json={**PAYLOAD, "accept_terms": True})
        assert resp.status_code == 200
        (row,) = _orders(init_test_db)
        assert row["accepted_ip"] == "127.0.0.1"  # адрес клиента в httpx ASGITransport

    @pytest.mark.asyncio
    async def test_user_agent_is_truncated(self, client, init_test_db):
        with _paid_flow():
            resp = await client.post(
                "/api/order",
                json={**PAYLOAD, "accept_terms": True},
                headers={"User-Agent": "U" * 1000},
            )
        assert resp.status_code == 200
        (row,) = _orders(init_test_db)
        assert len(row["accepted_ua"]) == 300

    @pytest.mark.asyncio
    async def test_free_promo_order_requires_and_records_acceptance(self, client, init_test_db):
        conn = sqlite3.connect(init_test_db)
        conn.execute(
            "INSERT INTO promo_codes (code, uses_left, discount) VALUES ('FREE100', 5, 100)"
        )
        conn.commit()
        conn.close()
        body = {**PAYLOAD, "promo_code": "FREE100"}

        resp = await client.post("/api/order", json=body)
        assert resp.status_code == 422
        assert _orders(init_test_db) == []

        resp = await client.post("/api/order", json={**body, "accept_terms": True})
        assert resp.status_code == 200
        assert resp.json()["free"] is True
        (row,) = _orders(init_test_db)
        assert row["offer_version"] == OFFER_VERSION
        assert row["accepted_at"] is not None
