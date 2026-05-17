import pytest

VALID_PREVIEW = {
    "size_key": "1x0.5",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [{"text": "Тест превью", "scale": 1.0}],
}

class TestPreviewEndpoint:
    @pytest.mark.asyncio
    async def test_valid_request_returns_200(self, client):
        resp = await client.post("/api/preview", json=VALID_PREVIEW)
        assert resp.status_code == 200
        data = resp.json()
        assert "preview_base64" in data
        assert "width_mm" in data

    @pytest.mark.asyncio
    async def test_invalid_bg_color_returns_422(self, client):
        payload = dict(VALID_PREVIEW, bg_color="Оранжевый")
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_text_too_long_returns_422(self, client):
        payload = dict(VALID_PREVIEW, text_lines=[{"text": "а" * 121, "scale": 1.0}])
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422