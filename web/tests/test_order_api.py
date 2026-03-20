"""
test_order_api.py
~~~~~~~~~~~~~~~~~
HTTP-тесты роутера заказов.

Покрывает:
  - POST /api/order: валидный запрос → 200 + {order_id, pay_url}
  - POST /api/order: невалидный size_key → 422
  - POST /api/order: невалидный ref_code → 422
  - POST /api/order: пустые text_lines → 422
  - GET /api/payment/status/{id}: статус pending
  - GET /api/payment/status/{id}: несуществующий → 404
  - GET /api/templates: 200
  - GET /api/health: 200
"""

import pytest
from conftest import VALID_ORDER_PAYLOAD


class TestHealth:

    @pytest.mark.asyncio
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestTemplates:

    @pytest.mark.asyncio
    async def test_templates_returns_200(self, client):
        """GET /api/templates возвращает 200 и структуру с sizes/fonts/colors."""
        resp = await client.get("/api/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert "sizes" in data or "fonts" in data  # fallback или файл


class TestCreateOrder:

    @pytest.mark.asyncio
    async def test_valid_order_returns_order_id_and_pay_url(self, client):
        """Валидный запрос → 200, order_id UUID, pay_url строка."""
        resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert "order_id" in data
        assert "pay_url" in data
        # order_id должен быть UUID4 (36 символов с дефисами)
        assert len(data["order_id"]) == 36
        assert data["pay_url"].startswith("https://")

    @pytest.mark.asyncio
    async def test_invalid_size_key_returns_422(self, client):
        """Неизвестный size_key → 422."""
        payload = dict(VALID_ORDER_PAYLOAD, size_key="99x99")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_bg_color_returns_422(self, client):
        """Неизвестный bg_color → 422."""
        payload = dict(VALID_ORDER_PAYLOAD, bg_color="Розовый")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_font_returns_422(self, client):
        """Неизвестный шрифт → 422."""
        payload = dict(VALID_ORDER_PAYLOAD, font="Comic Sans")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_text_lines_returns_422(self, client):
        """Пустые text_lines → 422."""
        payload = dict(VALID_ORDER_PAYLOAD, text_lines=[])
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_ref_code_format_returns_422(self, client):
        """ref_code не по формату A-Z0-9 8 символов → 422."""
        payload = dict(VALID_ORDER_PAYLOAD, ref_code="invalid!")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_ref_code_accepted(self, client):
        """Корректный ref_code 8 символов A-Z0-9 → 200."""
        payload = dict(VALID_ORDER_PAYLOAD, ref_code="ABCD1234")
        resp = await client.post("/api/order", json=payload)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_422(self, client):
        """Отсутствие обязательных полей → 422."""
        resp = await client.post("/api/order", json={"size_key": "1x0.5"})
        assert resp.status_code == 422


class TestPaymentStatus:

    @pytest.mark.asyncio
    async def test_status_of_created_order(self, client):
        """После создания заказ имеет статус pending."""
        create_resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        order_id = create_resp.json()["order_id"]

        status_resp = await client.get(f"/api/payment/status/{order_id}")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "pending"
        assert data["order_id"] == order_id
        assert data["amount_rub"] == 299

    @pytest.mark.asyncio
    async def test_status_not_found(self, client):
        """Несуществующий order_id → 404."""
        resp = await client.get("/api/payment/status/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404
