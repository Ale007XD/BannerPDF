import pytest
from conftest import VALID_ORDER_PAYLOAD


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_valid_order_returns_yookassa_payment_data(self, client):
        resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert "order_id" in data
        assert "confirmation_token" in data
        assert "payment_id" in data
        assert data["amount_rub"] == 299

    @pytest.mark.asyncio
    async def test_invalid_size_key_returns_422(self, client):
        payload = dict(VALID_ORDER_PAYLOAD, size_key="99x99")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_text_lines_returns_422(self, client):
        payload = dict(VALID_ORDER_PAYLOAD, text_lines=[])
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

class TestPaymentStatus:
    @pytest.mark.asyncio
    async def test_status_of_created_order(self, client):
        create_resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        order_id = create_resp.json()["order_id"]

        status_resp = await client.get(f"/api/payment/status/{order_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "pending"