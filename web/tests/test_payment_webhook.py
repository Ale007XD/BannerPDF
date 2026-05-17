import json
import sqlite3
from unittest.mock import AsyncMock, patch
import pytest
from conftest import VALID_ORDER_PAYLOAD, make_yookassa_succeeded_payment

YOOKASSA_PAYMENT_ID = "test_yk_payment_id_12345"

async def _create_order(client) -> str:
    resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
    return resp.json()["order_id"]

def _build_webhook_body(order_id: str, event: str = "payment.succeeded") -> bytes:
    return json.dumps({
        "type":  "notification",
        "event": event,
        "object": {
            "id":       YOOKASSA_PAYMENT_ID,
            "status":   "succeeded" if event == "payment.succeeded" else "canceled",
            "metadata": {"order_id": order_id},
        },
    }).encode()

class TestWebhookLogic:
    @pytest.mark.asyncio
    async def test_valid_webhook_transitions_to_token_issued(self, client, init_test_db):
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id)

        with patch("web.api.routers.payment.verify_yookassa_payment", AsyncMock(return_value=payment_data)):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        conn = sqlite3.connect(init_test_db)
        row = conn.execute("SELECT status FROM web_orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
        assert row[0] == "token_issued"

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_webhook(self, client, init_test_db):
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id)
        
        with patch("web.api.routers.payment.verify_yookassa_payment", AsyncMock(return_value=payment_data)):
            r1 = await client.post("/api/payment/callback", content=body, headers={"Content-Type": "application/json"})
            r2 = await client.post("/api/payment/callback", content=body, headers={"Content-Type": "application/json"})
            
        assert r1.status_code == 200
        assert r2.status_code == 200