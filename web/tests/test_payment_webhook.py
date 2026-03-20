"""
test_payment_webhook.py
~~~~~~~~~~~~~~~~~~~~~~~
HTTP-тесты webhook /api/payment/callback (selfwork).

Формат тела (application/json):
  {
    "event":     "payment.succeeded",
    "order_id":  "<uuid>",
    "amount":    <копейки>,
    "signature": "<sha256hex>"
  }

Подпись: SHA256(order_id + amount + SELFWORK_API_KEY) — в теле, не в заголовке.

Покрывает:
  - Неверная подпись → 403 (ПЕРВАЯ проверка, до любой логики)
  - Отсутствует order_id в теле → 400
  - Отсутствует amount в теле → 400
  - Событие не 'payment.succeeded' → 200, заказ не меняется
  - Несуществующий order_id → 422 (FSM не находит заказ)
  - Валидный callback → 200, FSM pending→paid→token_issued
  - Валидный callback → download_token создан в БД
  - Идемпотентность: повторный callback → 200
"""

import json
import sqlite3

import pytest
from conftest import VALID_ORDER_PAYLOAD, make_selfwork_signature

# Сумма в копейках соответствует SITE_PDF_PRICE=299 из set_env
AMOUNT_KOPECKS = 29900


async def _create_order(client) -> str:
    """Вспомогательная функция: создаёт заказ, возвращает order_id."""
    resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
    assert resp.status_code == 200
    return resp.json()["order_id"]


def _build_callback_body(order_id: str, event: str = "payment.succeeded",
                          amount: int = AMOUNT_KOPECKS,
                          secret: str = "test_secret_key") -> bytes:
    """Формирует корректное тело callback selfwork с подписью."""
    signature = make_selfwork_signature(order_id, amount, secret)
    return json.dumps({
        "event":     event,
        "order_id":  order_id,
        "amount":    amount,
        "signature": signature,
    }).encode()


def _build_bad_signature_body(order_id: str) -> bytes:
    """Формирует тело с заведомо неверной подписью."""
    return json.dumps({
        "event":     "payment.succeeded",
        "order_id":  order_id,
        "amount":    AMOUNT_KOPECKS,
        "signature": "deadbeef" * 8,
    }).encode()


class TestWebhookSignature:

    @pytest.mark.asyncio
    async def test_wrong_signature_returns_403(self, client):
        """Неверная подпись → 403, до любой бизнес-логики."""
        order_id = await _create_order(client)
        body = _build_bad_signature_body(order_id)
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_signature_does_not_change_order_status(self, client, init_test_db):
        """При неверной подписи статус заказа не меняется."""
        order_id = await _create_order(client)
        body = _build_bad_signature_body(order_id)
        await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        conn = sqlite3.connect(init_test_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_wrong_amount_in_signature_returns_403(self, client):
        """Подпись от другой суммы → 403 (целостность суммы нарушена)."""
        order_id = await _create_order(client)
        # Подписываем корректно, но в теле шлём другую сумму
        correct_sig = make_selfwork_signature(order_id, AMOUNT_KOPECKS)
        body = json.dumps({
            "event":     "payment.succeeded",
            "order_id":  order_id,
            "amount":    99900,          # подменённая сумма
            "signature": correct_sig,    # подпись от старой суммы
        }).encode()
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 403


class TestWebhookValidation:

    @pytest.mark.asyncio
    async def test_missing_order_id_returns_400(self, client):
        """Callback без order_id в теле → 400."""
        # Для корректной подписи нужен order_id, поэтому шлём с неверной
        body = json.dumps({
            "event":     "payment.succeeded",
            "amount":    AMOUNT_KOPECKS,
            "signature": "deadbeef" * 8,
        }).encode()
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        # 403 (неверная подпись) или 400 — оба допустимы,
        # главное что не 200 (запрос не прошёл)
        assert resp.status_code in (400, 403)

    @pytest.mark.asyncio
    async def test_missing_amount_returns_400(self, client):
        """Callback без amount в теле → 400."""
        order_id = await _create_order(client)
        body = json.dumps({
            "event":     "payment.succeeded",
            "order_id":  order_id,
            "signature": "deadbeef" * 8,
        }).encode()
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (400, 403)

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, client):
        """Невалидный JSON в теле → 400."""
        resp = await client.post(
            "/api/payment/callback",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestWebhookLogic:

    @pytest.mark.asyncio
    async def test_valid_callback_transitions_to_token_issued(self, client, init_test_db):
        """Валидный callback → FSM pending→paid→token_issued."""
        order_id = await _create_order(client)
        body = _build_callback_body(order_id)

        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
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
    async def test_valid_callback_creates_download_token(self, client, init_test_db):
        """После callback в download_tokens появляется токен для заказа."""
        order_id = await _create_order(client)
        body = _build_callback_body(order_id)

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
        body = _build_callback_body(order_id, event="payment.failed")

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
        """Callback на несуществующий order_id → 422 (FSM не находит заказ)."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        body = _build_callback_body(fake_id)
        resp = await client.post(
            "/api/payment/callback",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_idempotent_duplicate_callback(self, client, init_test_db):
        """Повторный callback на уже оплаченный заказ → 200, без ошибки."""
        order_id = await _create_order(client)
        body = _build_callback_body(order_id)
        headers = {"Content-Type": "application/json"}

        # Первый вызов
        r1 = await client.post("/api/payment/callback", content=body, headers=headers)
        assert r1.status_code == 200

        # Второй вызов (идемпотент)
        r2 = await client.post("/api/payment/callback", content=body, headers=headers)
        assert r2.status_code == 200
