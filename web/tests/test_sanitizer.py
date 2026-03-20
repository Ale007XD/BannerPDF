"""
test_sanitizer.py
~~~~~~~~~~~~~~~~~
Тесты санитайзера и валидатора конфига баннера.

Покрывает:
  - sanitize_line: управляющие символы, Unicode, длина, пробелы
  - sanitize_text_lines: фильтрация пустых, лимит строк, нормировка scale
  - validate_banner_config: все поля валидны → пустой список ошибок
  - validate_banner_config: неверные поля → соответствующие сообщения
"""

import pytest

from web.api.services.sanitizer import (
    MAX_LINE_LENGTH,
    MAX_LINES,
    sanitize_line,
    sanitize_text_lines,
    validate_banner_config,
)


class TestSanitizeLine:

    def test_removes_control_chars(self):
        """Управляющие символы удаляются."""
        result = sanitize_line("Текст\x00\x01\x1f")
        assert "\x00" not in result
        assert "Текст" in result

    def test_normalizes_multiple_spaces(self):
        """Множественные пробелы схлопываются в один."""
        result = sanitize_line("слово  слово   слово")
        assert "  " not in result
        assert result == "слово слово слово"

    def test_strips_whitespace(self):
        """Пробелы по краям обрезаются."""
        assert sanitize_line("  текст  ") == "текст"

    def test_truncates_to_max_length(self):
        """Строка обрезается до MAX_LINE_LENGTH."""
        long = "а" * (MAX_LINE_LENGTH + 50)
        result = sanitize_line(long)
        assert len(result) == MAX_LINE_LENGTH

    def test_normalizes_unicode_nfc(self):
        """Unicode нормализуется в NFC."""
        # NFD: е + combining acute (два кодпоинта)
        nfd = "е\u0301"  # е + combining acute
        result = sanitize_line(nfd)
        # NFC: ё или é — один кодпоинт
        assert len(result) == 1

    def test_empty_string_returns_empty(self):
        """Пустая строка → пустая строка."""
        assert sanitize_line("") == ""

    def test_non_string_returns_empty(self):
        """Не строка → пустая строка."""
        assert sanitize_line(None) == ""  # type: ignore
        assert sanitize_line(42) == ""    # type: ignore


class TestSanitizeTextLines:

    def test_filters_empty_lines(self):
        """Строки, пустые после очистки, фильтруются."""
        lines = [
            {"text": "Нормальный текст", "scale": 1.0},
            {"text": "\x00\x01", "scale": 1.0},  # станет пустой
            {"text": "   ", "scale": 1.0},        # только пробелы
        ]
        result = sanitize_text_lines(lines)
        assert len(result) == 1
        assert result[0]["text"] == "Нормальный текст"

    def test_limits_to_max_lines(self):
        """Количество строк ограничивается MAX_LINES."""
        lines = [{"text": f"Строка {i}", "scale": 1.0} for i in range(MAX_LINES + 5)]
        result = sanitize_text_lines(lines)
        assert len(result) <= MAX_LINES

    def test_normalizes_scale_low(self):
        """scale < 0.3 нормируется до 0.3."""
        lines = [{"text": "Текст", "scale": 0.1}]
        result = sanitize_text_lines(lines)
        assert result[0]["scale"] == 0.3

    def test_normalizes_scale_high(self):
        """scale > 1.5 нормируется до 1.5."""
        lines = [{"text": "Текст", "scale": 99.0}]
        result = sanitize_text_lines(lines)
        assert result[0]["scale"] == 1.5

    def test_preserves_valid_scale(self):
        """Корректный scale сохраняется."""
        lines = [{"text": "Текст", "scale": 0.8}]
        result = sanitize_text_lines(lines)
        assert result[0]["scale"] == 0.8

    def test_empty_input_returns_empty(self):
        """Пустой список → пустой список."""
        assert sanitize_text_lines([]) == []


class TestValidateBannerConfig:

    def _valid(self):
        return {
            "size_key": "1x0.5",
            "bg_color": "Белый",
            "text_color": "Черный",
            "font": "Golos Text",
            "text_lines": [{"text": "Тест", "scale": 1.0}],
        }

    def test_valid_config_no_errors(self):
        """Полностью валидный конфиг → пустой список ошибок."""
        errors = validate_banner_config(self._valid())
        assert errors == []

    def test_invalid_size_key(self):
        """Неизвестный size_key → ошибка."""
        cfg = self._valid()
        cfg["size_key"] = "99x99"
        errors = validate_banner_config(cfg)
        assert any("размер" in e.lower() for e in errors)

    def test_invalid_bg_color(self):
        """Неизвестный bg_color → ошибка."""
        cfg = self._valid()
        cfg["bg_color"] = "Розовый"
        errors = validate_banner_config(cfg)
        assert any("фон" in e.lower() for e in errors)

    def test_invalid_text_color(self):
        """Неизвестный text_color → ошибка."""
        cfg = self._valid()
        cfg["text_color"] = "Фиолетовый"
        errors = validate_banner_config(cfg)
        assert any("текст" in e.lower() for e in errors)

    def test_invalid_font(self):
        """Неизвестный шрифт → ошибка."""
        cfg = self._valid()
        cfg["font"] = "Comic Sans"
        errors = validate_banner_config(cfg)
        assert any("шрифт" in e.lower() for e in errors)

    def test_empty_text_lines(self):
        """Пустой список text_lines → ошибка."""
        cfg = self._valid()
        cfg["text_lines"] = []
        errors = validate_banner_config(cfg)
        assert len(errors) > 0

    def test_too_many_lines(self):
        """Больше MAX_LINES строк → ошибка."""
        cfg = self._valid()
        cfg["text_lines"] = [{"text": "x", "scale": 1.0}] * (MAX_LINES + 1)
        errors = validate_banner_config(cfg)
        assert any(str(MAX_LINES) in e for e in errors)

    def test_multiple_errors_returned(self):
        """Несколько ошибок валидации возвращаются все сразу."""
        cfg = {
            "size_key": "bad",
            "bg_color": "bad",
            "text_color": "bad",
            "font": "bad",
            "text_lines": [],
        }
        errors = validate_banner_config(cfg)
        assert len(errors) >= 4
