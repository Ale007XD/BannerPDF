"""
test_payment_webhook.py
~~~~~~~~~~~~~~~~~~~~~~~
HTTP-тесты webhook /api/payment/callback (ЮKassa).

Формат тела webhook от ЮKassa (application/json):
  {
    "type":   "notification",
    "event":  "payment.succeeded",
    "object": {
      "id":       "<yookassa_payment_id>",
      "status":   "succeeded",
      "metadata": {"order_id": "<наш_uuid>"},
      ...
    }
  }

Верификация: бэкенд вызывает verify_yookassa_webhook(yookassa_id),
которая делает GET /v3/payments/{id} к API ЮKassa.
В тестах verify_yookassa_webhook мокается — HTTP не делается.

Покрывает:
  - Нет object.id в теле → 400
  - verify_yookassa_webhook вернул None (не succeeded) → 200, заказ не меняется
  - Событие не 'payment.succeeded' → 200, заказ не меняется
  - Нет order_id в metadata → 400
  - Несуществующий order_id → 422 (FSM не находит заказ)
  - Валидный webhook → 200, FSM pending→paid→token_issued
  - Валидный webhook → download_token создан в БД
  - Идемпотентность: повторный webhook → 200
  - Невалидный JSON → 400
"""

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from conftest import VALID_ORDER_PAYLOAD, make_yookassa_succeeded_payment

YOOKASSA_PAYMENT_ID = "test_yk_payment_id_12345"


async def _create_order(client) -> str:
    """Вспомогательная функция: создаёт заказ, возвращает order_id."""
    resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
    assert resp.status_code == 200
    return resp.json()["order_id"]


def _build_webhook_body(order_id: str,
                         event: str = "payment.succeeded",
                         yookassa_payment_id: str = YOOKASSA_PAYMENT_ID) -> bytes:
    """Формирует тело webhook ЮKassa."""
    return json.dumps({
        "type":  "notification",
        "event": event,
        "object": {
            "id":       yookassa_payment_id,
            "status":   "succeeded" if event == "payment.succeeded" else "canceled",
            "metadata": {"order_id": order_id},
        },
    }).encode()


def _build_webhook_no_object_id() -> bytes:
    """Тело webhook без object.id."""
    return json.dumps({
        "type":  "notification",
        "event": "payment.succeeded",
        "object": {},
    }).encode()


class TestWebhookVerification:

    @pytest.mark.asyncio
    async def test_missing_object_id_returns_400(self, client):
        """Нет object.id в теле → 400."""
        resp = await client.post(
            "/api/payment/callback",
            content=_build_webhook_no_object_id(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, client):
        """Невалидный JSON в теле → 400."""
        resp = await client.post(
            "/api/payment/callback",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_returns_none_does_not_change_order(self, client, init_test_db):
        """verify_yookassa_webhook вернул None → 200, заказ остаётся pending."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id)

        # Симулируем: ЮKassa говорит что платёж не succeeded (или ошибка сети)
        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=None),
        ):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200

        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_missing_order_id_in_metadata_returns_400(self, client):
        """Нет order_id в metadata платежа → 400."""
        order_id = await _create_order(client)
        payment_without_order_id = {
            "id":       YOOKASSA_PAYMENT_ID,
            "status":   "succeeded",
            "metadata": {},  # нет order_id
        }
        body = json.dumps({
            "type":   "notification",
            "event":  "payment.succeeded",
            "object": {"id": YOOKASSA_PAYMENT_ID},
        }).encode()

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_without_order_id),
        ):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 400


class TestWebhookLogic:

    @pytest.mark.asyncio
    async def test_valid_webhook_transitions_to_token_issued(self, client, init_test_db):
        """Валидный webhook → FSM pending→paid→token_issued."""
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id)

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_data),
        ):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "token_issued"

    @pytest.mark.asyncio
    async def test_valid_webhook_creates_download_token(self, client, init_test_db):
        """После webhook в download_tokens появляется токен для заказа."""
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id)

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_data),
        ):
            await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT token FROM download_tokens WHERE order_id = ? AND used = FALSE",
            (order_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert len(row["token"]) == 64

    @pytest.mark.asyncio
    async def test_non_succeeded_event_ignored(self, client, init_test_db):
        """Callback с событием не 'payment.succeeded' игнорируется, заказ остаётся pending."""
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id, event="payment.canceled")

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_data),
        ):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200

        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_nonexistent_order_id_returns_422(self, client):
        """Webhook с несуществующим order_id → 422 (FSM не находит заказ)."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        payment_data = make_yookassa_succeeded_payment(fake_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(fake_id)

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_data),
        ):
            resp = await client.post(
                "/api/payment/callback",
                content=body,
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_webhook(self, client, init_test_db):
        """Повторный webhook на уже оплаченный заказ → 200, без ошибки."""
        order_id = await _create_order(client)
        payment_data = make_yookassa_succeeded_payment(order_id, YOOKASSA_PAYMENT_ID)
        body = _build_webhook_body(order_id)
        headers = {"Content-Type": "application/json"}

        with patch(
            "web.api.routers.payment.verify_yookassa_webhook",
            AsyncMock(return_value=payment_data),
        ):
            r1 = await client.post("/api/payment/callback", content=body, headers=headers)
            assert r1.status_code == 200

            r2 = await client.post("/api/payment/callback", content=body, headers=headers)
            assert r2.status_code == 200
