from web.api.services.sanitizer import sanitize_line, sanitize_text_lines, validate_banner_config

class TestSanitizeLine:
    def test_removes_control_chars(self):
        result = sanitize_line("Текст\x00\x01\x1f")
        assert result == "Текст"

    def test_normalizes_multiple_spaces(self):
        result = sanitize_line("слово  слово   слово")
        assert result == "слово слово слово"

class TestSanitizeTextLines:
    def test_filters_empty_lines(self):
        lines = [
            {"text": "Текст", "scale": 1.0},
            {"text": "   ", "scale": 1.0},
        ]
        assert len(sanitize_text_lines(lines)) == 1

class TestValidateBannerConfig:
    def test_valid_config_no_errors(self):
        cfg = {"size_key": "1x0.5", "bg_color": "Белый", "text_color": "Черный", "font": "Golos Text", "text_lines": [{"text": "Т", "scale": 1.0}]}
        assert validate_banner_config(cfg) == []