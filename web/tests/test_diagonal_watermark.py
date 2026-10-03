"""
test_diagonal_watermark.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Диагональная надпись в превью: от левого нижнего угла к правому верхнему.

Покрывает (геометрия, шрифт Pillow по умолчанию — файлов шрифтов не нужно):
  - направление надписи совпадает с диагональю баннера (главная ось закрашенных пикселей)
  - надпись по центру и целиком внутри баннера при любых пропорциях
  - пустой текст ничего не рисует
Покрывает (интеграция, шрифты из репозитория):
  - create_preview_jpeg рисует надпись: превью отличается от превью без неё
  - углы вдали от диагонали не затронуты
  - печатный PDF надпись не получает (_create_raw_pdf не вызывает хелпер)
"""

import math
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageFont

from web.api.services import banner_generator as bg
from web.api.services.config import FONTS

REPO_FONTS = Path(__file__).resolve().parents[1] / "fonts"
TEXT = "Made in bannerbot.ru"


def _default_font(size: int):
    return ImageFont.load_default(size)


def _opaque_points(overlay: Image.Image) -> list[tuple[int, int]]:
    alpha = overlay.getchannel("A")
    w, _ = alpha.size
    return [(i % w, i // w) for i, a in enumerate(alpha.getdata()) if a > 0]


def _principal_angle_deg(points: list[tuple[int, int]]) -> float:
    """Угол главной оси облака точек к оси X; ось Y перевёрнута (вверх = плюс)."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(-y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points) / n
    syy = sum((-y - my) ** 2 for _, y in points) / n
    sxy = sum((x - mx) * (-y - my) for x, y in points) / n
    return math.degrees(0.5 * math.atan2(2 * sxy, sxx - syy))


def _draw(w: int, h: int, text: str = TEXT) -> Image.Image:
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bg._draw_diagonal_watermark(overlay, text, _default_font)
    return overlay


class TestGeometry:

    @pytest.mark.parametrize("w,h", [(1000, 500), (1000, 1000), (1200, 800)])
    def test_text_follows_banner_diagonal(self, w, h):
        points = _opaque_points(_draw(w, h))
        assert points, "надпись не нарисована"
        expected = math.degrees(math.atan2(h, w))
        # Положительный угол = рост вверх вправо, т.е. из левого нижнего в правый верхний
        assert _principal_angle_deg(points) == pytest.approx(expected, abs=3)

    @pytest.mark.parametrize("w,h", [(1000, 500), (1000, 1000), (300, 3000), (100, 3000), (1200, 120), (1200, 300)])
    def test_fits_inside_with_margin_and_is_centered(self, w, h):
        overlay = _draw(w, h)
        left, top, right, bottom = overlay.getchannel("A").getbbox()
        # не касается краёв
        assert left > 0 and top > 0 and right < w and bottom < h
        # по центру баннера
        assert (left + right) / 2 == pytest.approx(w / 2, abs=3)
        assert (top + bottom) / 2 == pytest.approx(h / 2, abs=3)

    def test_empty_text_draws_nothing(self):
        overlay = _draw(1000, 500, text="")
        assert overlay.getchannel("A").getbbox() is None

    def test_visible_but_not_opaque(self):
        """Надпись полупрозрачная: макет под ней остаётся читаемым."""
        alphas = {a for a in _draw(1000, 500).getchannel("A").getdata() if a}
        assert max(alphas) < 200


@pytest.fixture
def repo_fonts(monkeypatch):
    """Подменяет пути шрифтов на файлы из репозитория (в CI нет /app/fonts)."""
    for name, path in FONTS.items():
        monkeypatch.setitem(FONTS, name, str(REPO_FONTS / Path(path).name))


def _data(w=2000, h=1000):
    return {
        "width": w, "height": h,
        "bg_color": "Красный", "text_color": "Белый", "font": "PT Sans Narrow",
        "text_lines": [{"text": "ВАША РЕКЛАМА", "scale": 0.9}, {"text": "814000000", "scale": 1.0}],
    }


class TestIntegration:

    def test_preview_contains_diagonal_text(self, repo_fonts, monkeypatch):
        with_wm = Image.open(bg.create_preview_jpeg(_data())).convert("RGB")

        monkeypatch.setattr(bg, "_draw_diagonal_watermark", lambda *a, **k: None)
        without_wm = Image.open(bg.create_preview_jpeg(_data())).convert("RGB")

        assert with_wm.size == without_wm.size
        diff = ImageChops.difference(with_wm, without_wm)
        assert diff.getbbox() is not None, "диагональная надпись не попала в превью"

        w, h = with_wm.size
        # верхний левый угол далеко от диагонали и плашки — без изменений
        assert diff.crop((0, 0, w // 10, h // 10)).getbbox() is None
        # диагональ проходит через центр
        assert diff.crop((w * 2 // 5, h * 2 // 5, w * 3 // 5, h * 3 // 5)).getbbox() is not None

    def test_print_pdf_has_no_watermark(self, repo_fonts, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("вотермарк попал в печатный PDF")

        monkeypatch.setattr(bg, "_draw_diagonal_watermark", boom)
        pdf = bg._create_raw_pdf(_data())
        assert pdf.getvalue().startswith(b"%PDF")
