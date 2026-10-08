"""
test_order_latency.py
~~~~~~~~~~~~~~~~~~~~~
Создание заказа не ждёт то, что клиенту не нужно.

Раньше POST /api/order перед ответом ждал Telegram (api.telegram.org, новый TLS-коннект,
таймаут 10 с): окно оплаты открывалось на секунды позже. Теперь уведомление уходит
после ответа, а время по этапам видно в заголовке Server-Timing.
"""

import asyncio
import json
import re
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

PAYLOAD = {
    "size_key":     "1x0.5",
    "bg_color":     "Белый",
    "text_color":   "Черный",
    "font":         "Golos Text",
    "text_lines":   [{"text": "Аренда склада", "scale": 100}],
    "accept_terms": True,
}
PAYMENT = {"payment_id": "yk-latency-1", "confirmation_token": "ct-latency-1"}


def _tg_message_id(db_path: str):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT tg_message_id FROM web_orders").fetchone()
    conn.close()
    return row[0] if row else None


async def _post_order_raw(app, events: list[str]) -> dict:
    """Вызывает ASGI-приложение напрямую, чтобы увидеть, КОГДА уходит ответ клиенту."""
    body = json.dumps(PAYLOAD).encode()
    sent = {}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body" and not message.get("more_body"):
            events.append("response_sent")

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/api/order", "raw_path": b"/api/order", "query_string": b"",
        "root_path": "", "server": ("testserver", 80), "client": ("127.0.0.1", 5000),
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()), (b"user-agent", b"pytest")],
    }
    await app(scope, receive, send)
    return sent


class TestTelegramDoesNotDelayResponse:

    @pytest.mark.asyncio
    async def test_response_is_sent_before_telegram_finishes(self, client, init_test_db):
        events: list[str] = []  # один общий журнал: важен порядок событий

        async def slow_notify(**kwargs):
            await asyncio.sleep(0.05)  # «медленный Telegram»
            events.append("notify_done")
            return 4242

        from web.api.main import app

        with (
            patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT)),
            patch("web.api.routers.order.notify_new_order", slow_notify),
        ):
            sent = await _post_order_raw(app, events)

        assert sent["status"] == 200
        assert events == ["response_sent", "notify_done"]
        assert _tg_message_id(init_test_db) == 4242  # результат фоновой задачи сохранён

    @pytest.mark.asyncio
    async def test_telegram_failure_does_not_break_order(self, client, init_test_db):
        with (
            patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT)),
            patch("web.api.routers.order.notify_new_order", AsyncMock(side_effect=RuntimeError("tg down"))),
        ):
            resp = await client.post("/api/order", json=PAYLOAD)

        assert resp.status_code == 200
        assert resp.json()["confirmation_token"] == "ct-latency-1"
        assert _tg_message_id(init_test_db) is None

    @pytest.mark.asyncio
    async def test_notification_gets_order_details(self, client, init_test_db):
        notify = AsyncMock(return_value=None)
        with (
            patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT)),
            patch("web.api.routers.order.notify_new_order", notify),
        ):
            resp = await client.post("/api/order", json=PAYLOAD)

        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["order_id"] == resp.json()["order_id"]
        assert kwargs["amount_rub"] == 299
        assert kwargs["lines"] == ["Аренда склада"]


class TestServerTiming:

    @pytest.mark.asyncio
    async def test_paid_order_reports_yookassa_and_total(self, client):
        with patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT)):
            resp = await client.post("/api/order", json=PAYLOAD)

        assert re.fullmatch(r"yk;dur=\d+, total;dur=\d+", resp.headers["server-timing"])

    @pytest.mark.asyncio
    async def test_rejected_order_has_no_payment_timing(self, client):
        with patch("web.api.routers.order.create_payment", AsyncMock(return_value=PAYMENT)) as pay:
            resp = await client.post("/api/order", json={**PAYLOAD, "accept_terms": False})

        assert resp.status_code == 422
        pay.assert_not_awaited()
