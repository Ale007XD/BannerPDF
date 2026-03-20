"""
test_renderer_preview.py
~~~~~~~~~~~~~~~~~~~~~~~~
Unit-тесты renderer.py — build_render_data() и вспомогательная логика.

Ghostscript не нужен — тестируем только подготовку данных.
render_preview_base64 тестируется через HTTP (conftest мок).

Покрывает:
  - build_render_data: валидный конфиг → корректный dict
  - build_render_data: размеры соответствуют size_key
  - build_render_data: пустые строки после санитайзинга → ValueError
  - build_render_data: невалидный конфиг → ValueError
  - set_executor / get_executor: не инициализирован → RuntimeError
"""

import pytest

from web.api.services.renderer import build_render_data, get_executor, set_executor

VALID_CONFIG = {
    "size_key": "1x0.5",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [{"text": "Тестовый баннер", "scale": 1.0}],
}


class TestBuildRenderData:

    def test_valid_config_returns_dict(self):
        """Валидный конфиг → dict с нужными ключами."""
        data = build_render_data(VALID_CONFIG)
        assert isinstance(data, dict)
        for key in ("width", "height", "bg_color", "text_color", "text_lines", "font"):
            assert key in data, f"Отсутствует ключ: {key}"

    def test_dimensions_for_1x05(self):
        """size_key='1x0.5' → width=1000, height=500."""
        data = build_render_data(VALID_CONFIG)
        assert data["width"] == 1000
        assert data["height"] == 500

    def test_dimensions_for_3x2(self):
        """size_key='3x2' → width=3000, height=2000."""
        cfg = dict(VALID_CONFIG, size_key="3x2")
        data = build_render_data(cfg)
        assert data["width"] == 3000
        assert data["height"] == 2000

    def test_colors_passed_through(self):
        """Цвета из конфига передаются в render data."""
        cfg = dict(VALID_CONFIG, bg_color="Черный", text_color="Белый")
        data = build_render_data(cfg)
        assert data["bg_color"] == "Черный"
        assert data["text_color"] == "Белый"

    def test_font_passed_through(self):
        """Шрифт из конфига передаётся в render data."""
        data = build_render_data(VALID_CONFIG)
        assert data["font"] == "Golos Text"

    def test_text_lines_sanitized(self):
        """text_lines санитайзятся перед передачей в рендер."""
        cfg = dict(VALID_CONFIG, text_lines=[
            {"text": "  Текст  ", "scale": 1.0},
            {"text": "\x00\x01", "scale": 1.0},  # пустая после санитайзинга
        ])
        data = build_render_data(cfg)
        assert data["text_lines"][0]["text"] == "Текст"
        assert len(data["text_lines"]) == 1  # пустая строка отфильтрована

    def test_invalid_size_key_raises_value_error(self):
        """Невалидный size_key → ValueError."""
        cfg = dict(VALID_CONFIG, size_key="bad_key")
        with pytest.raises(ValueError, match="валид"):
            build_render_data(cfg)

    def test_invalid_color_raises_value_error(self):
        """Невалидный цвет → ValueError."""
        cfg = dict(VALID_CONFIG, bg_color="Розовый")
        with pytest.raises(ValueError, match="валид"):
            build_render_data(cfg)

    def test_only_empty_lines_raises_value_error(self):
        """Если после санитайзинга не осталось строк → ValueError."""
        cfg = dict(VALID_CONFIG, text_lines=[
            {"text": "\x00", "scale": 1.0},
            {"text": "   ", "scale": 1.0},
        ])
        with pytest.raises(ValueError, match="строк"):
            build_render_data(cfg)


class TestExecutor:

    def test_get_executor_without_init_raises(self):
        """get_executor() без set_executor() → RuntimeError."""
        import web.api.services.renderer as r
        original = r._executor
        r._executor = None
        try:
            with pytest.raises(RuntimeError, match="ProcessPoolExecutor"):
                get_executor()
        finally:
            r._executor = original

    def test_set_and_get_executor(self):
        """set_executor() → get_executor() возвращает тот же объект."""
        from unittest.mock import MagicMock
        mock_executor = MagicMock()
        set_executor(mock_executor)
        assert get_executor() is mock_executor
