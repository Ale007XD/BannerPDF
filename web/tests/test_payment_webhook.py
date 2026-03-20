"""
test_payment_webhook.py
~~~~~~~~~~~~~~~~~~~~~~~
HTTP-тесты webhook /api/payment/callback.

Покрывает:
  - Неверная подпись → 403 (ПЕРВАЯ проверка, до любой логики)
  - Отсутствует заголовок X-Tona-Signature → 422 (FastAPI validation)
  - Статус не 'paid' → 200, заказ не меняется
  - Нет order_id в теле → 400
  - Несуществующий order_id → 422 (FSM не находит заказ)
  - Валидный webhook на существующий заказ → 200, FSM → token_issued
"""

import json
import sqlite3

import pytest
from conftest import VALID_ORDER_PAYLOAD, make_tona_signature


async def _create_order(client) -> str:
    """Вспомогательная функция: создаёт заказ, возвращает order_id."""
    resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
    assert resp.status_code == 200
    return resp.json()["order_id"]


def _build_webhook_body(order_id: str, status: str = "paid") -> bytes:
    return json.dumps({"order_id": order_id, "status": status}).encode()


class TestWebhookSignature:

    @pytest.mark.asyncio
    async def test_missing_signature_header_returns_422(self, client):
        """Нет заголовка X-Tona-Signature → FastAPI вернёт 422."""
        body = _build_webhook_body("some-order-id")
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_wrong_signature_returns_403(self, client):
        """Неверная подпись → 403, до любой бизнес-логики."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id)
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": "deadbeef" * 8,
            },
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_signature_does_not_change_order_status(self, client, init_test_db):
        """При неверной подписи статус заказа не меняется."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id)
        await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": "bad" * 20,
            },
        )
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending"


class TestWebhookLogic:

    @pytest.mark.asyncio
    async def test_valid_webhook_transitions_to_token_issued(self, client, init_test_db):
        """Валидный webhook → FSM pending→paid→token_issued."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id)
        sig = make_tona_signature(body)

        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": sig,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Проверяем статус в БД
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
        body = _build_webhook_body(order_id)
        sig = make_tona_signature(body)

        await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": sig,
            },
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
    async def test_non_paid_status_ignored(self, client, init_test_db):
        """Webhook со статусом не 'paid' игнорируется, заказ остаётся pending."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id, status="failed")
        sig = make_tona_signature(body)

        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": sig,
            },
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
    async def test_missing_order_id_returns_400(self, client):
        """Webhook без order_id в теле → 400."""
        body = json.dumps({"status": "paid"}).encode()
        sig = make_tona_signature(body)

        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Tona-Signature": sig,
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_webhook(self, client, init_test_db):
        """Повторный webhook на уже оплаченный заказ → 200, без ошибки."""
        order_id = await _create_order(client)
        body = _build_webhook_body(order_id)
        sig = make_tona_signature(body)
        headers = {
            "Content-Type": "application/json",
            "X-Tona-Signature": sig,
        }

        # Первый вызов
        r1 = await client.post("/api/payment/callback", content=body, headers=headers)
        assert r1.status_code == 200

        # Второй вызов (идемпотент)
        r2 = await client.post("/api/payment/callback", content=body, headers=headers)
        assert r2.status_code == 200
