"""
test_order_api.py
~~~~~~~~~~~~~~~~~
HTTP-тесты роутера заказов.

Покрывает:
  - POST /api/order: валидный запрос → 200 + {order_id, amount_kopecks, signature, ...}
  - POST /api/order: невалидный size_key → 422
  - POST /api/order: невалидный bg_color → 422
  - POST /api/order: невалидный font → 422
  - POST /api/order: пустые text_lines → 422
  - POST /api/order: невалидный ref_code → 422
  - POST /api/order: корректный ref_code → 200
  - POST /api/order: отсутствуют обязательные поля → 422
  - GET /api/payment/status/{id}: статус pending + amount_rub
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
    async def test_valid_order_returns_selfwork_payment_data(self, client):
        """Валидный запрос → 200, order_id UUID, данные для виджета selfwork."""
        resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()

        # order_id — UUID4 (36 символов с дефисами)
        assert "order_id" in data
        assert len(data["order_id"]) == 36

        # Поля для selfwork виджета (вместо pay_url)
        assert "amount_kopecks" in data
        assert "signature" in data
        assert "item_name" in data
        assert "quantity" in data

        # Нет pay_url — selfwork виджет не требует
        assert "pay_url" not in data

        # Сумма: 299 руб = 29900 коп
        assert data["amount_kopecks"] == 29900
        assert data["quantity"] == 1

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
        """После создания заказ имеет статус pending, amount_rub=299."""
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
