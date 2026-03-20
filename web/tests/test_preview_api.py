"""
test_preview_api.py
~~~~~~~~~~~~~~~~~~~
HTTP-тесты POST /api/preview.

render_preview_base64 замокан в conftest — реальный Pillow не нужен.

Покрывает:
  - Валидный запрос → 200, preview_base64 строка, width_mm/height_mm
  - Каждый допустимый size_key
  - Невалидный size_key → 422
  - Невалидный цвет → 422
  - Невалидный шрифт → 422
  - Пустые text_lines → 422
  - Слишком длинный text → 422 (Pydantic max_length=120)
"""

import pytest  # noqa: I001


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
        """Валидный запрос → 200."""
        resp = await client.post("/api/preview", json=VALID_PREVIEW)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_has_required_fields(self, client):
        """Ответ содержит preview_base64, width_mm, height_mm."""
        resp = await client.post("/api/preview", json=VALID_PREVIEW)
        data = resp.json()
        assert "preview_base64" in data
        assert "width_mm" in data
        assert "height_mm" in data

    @pytest.mark.asyncio
    async def test_preview_base64_is_string(self, client):
        """preview_base64 — строка."""
        resp = await client.post("/api/preview", json=VALID_PREVIEW)
        assert isinstance(resp.json()["preview_base64"], str)

    @pytest.mark.asyncio
    async def test_dimensions_match_size_key(self, client):
        """width_mm и height_mm соответствуют size_key 1x0.5 (1000×500)."""
        resp = await client.post("/api/preview", json=VALID_PREVIEW)
        data = resp.json()
        assert data["width_mm"] == 1000
        assert data["height_mm"] == 500

    @pytest.mark.asyncio
    async def test_all_size_keys(self, client):
        """Все допустимые size_key возвращают 200."""
        sizes = ["3x2", "2x1", "1x0.5", "1.5x1", "1.5x0.5"]
        for size in sizes:
            payload = dict(VALID_PREVIEW, size_key=size)
            resp = await client.post("/api/preview", json=payload)
            assert resp.status_code == 200, f"size_key={size} вернул {resp.status_code}"

    @pytest.mark.asyncio
    async def test_invalid_size_key_returns_422(self, client):
        """Неизвестный size_key → 422."""
        payload = dict(VALID_PREVIEW, size_key="10x5")
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_bg_color_returns_422(self, client):
        """Неизвестный bg_color → 422."""
        payload = dict(VALID_PREVIEW, bg_color="Оранжевый")
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_font_returns_422(self, client):
        """Неизвестный шрифт → 422."""
        payload = dict(VALID_PREVIEW, font="Arial")
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_text_lines_returns_422(self, client):
        """Пустые text_lines → 422."""
        payload = dict(VALID_PREVIEW, text_lines=[])
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_text_too_long_returns_422(self, client):
        """Строка длиннее 120 символов → 422 (Pydantic max_length)."""
        payload = dict(VALID_PREVIEW, text_lines=[{"text": "а" * 121, "scale": 1.0}])
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_scale_out_of_range_returns_422(self, client):
        """scale вне [0.3, 1.5] → 422."""
        payload = dict(VALID_PREVIEW, text_lines=[{"text": "Текст", "scale": 0.1}])
        resp = await client.post("/api/preview", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_all_colors(self, client):
        """Все допустимые цвета принимаются."""
        colors = ["Белый", "Черный", "Красный", "Желтый", "Синий", "Зеленый"]
        for color in colors:
            payload = dict(VALID_PREVIEW, bg_color=color, text_color="Черный")
            resp = await client.post("/api/preview", json=payload)
            assert resp.status_code == 200, f"bg_color={color} вернул {resp.status_code}"
