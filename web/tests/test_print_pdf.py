"""
test_print_pdf.py
~~~~~~~~~~~~~~~~~
Печатный PDF: то, что обещано на сайте, должно быть в файле.

Заявление на сайте: «CMYK, шрифты в кривых, масштаб 1:1, значения красок
заданы напрямую (профиль применяет RIP типографии)». Тесты проверяют каждое слово:

Без Ghostscript (всегда):
  - палитра: чистый K100 для чёрного, суммарная краска ≤ лимита
  - pdf_check на синтетических PDF: каждая проверка ловит свою проблему

С Ghostscript (пропускаются, если gs не установлен; в CI он ставится):
  - только CMYK, нет шрифтов, нет RGB, нет профиля/OutputIntent/PDF/X
  - значения красок проходят без изменений для всех цветов палитры
  - чёрный текст — чистый K100, без «богатого чёрного»
  - масштаб 1:1, числа в потоке ≤ 32767 — в том числе на 3000×3000 мм
  - Title/Author осмысленные
"""

import logging
import re
import shutil
from pathlib import Path

import pytest

from web.api.services import banner_generator as bg
from web.api.services.config import COLORS, FONTS, MAX_TOTAL_INK_PERCENT
from web.api.services.pdf_check import (
    _CMYK_OP,
    COORD_LIMIT,
    _page_content_streams,
    check_print_pdf,
)

REPO_FONTS = Path(__file__).resolve().parents[1] / "fonts"

needs_gs = pytest.mark.skipif(shutil.which("gs") is None, reason="Ghostscript не установлен")


@pytest.fixture
def repo_fonts(monkeypatch):
    """Шрифты из репозитория (в CI нет /app/fonts)."""
    for name, path in FONTS.items():
        monkeypatch.setitem(FONTS, name, str(REPO_FONTS / Path(path).name))


def _order(width=2000, height=750, bg_color="Красный", text_color="Белый"):
    return {
        "width": width, "height": height,
        "bg_color": bg_color, "text_color": text_color, "font": "Golos Text",
        "text_lines": [{"text": "ВАША РЕКЛАМА", "scale": 1.0}, {"text": "814000000", "scale": 0.8}],
    }


def _cmyk_ops(pdf: bytes) -> set[tuple[float, float, float, float]]:
    """Все CMYK-операторы k/K из потока страницы, значения в долях 0..1."""
    ops = set()
    for stream in _page_content_streams(pdf):
        for c, m, y, k in _CMYK_OP.findall(stream):
            ops.add((float(c), float(m), float(y), float(k)))
    return ops


def _as_fractions(cmyk) -> tuple[float, float, float, float]:
    return tuple(round(v / 100, 6) for v in cmyk)


# ---------------------------------------------------------------------------
# Палитра: K100 и лимит краски
# ---------------------------------------------------------------------------
class TestPalette:
    def test_black_is_pure_k100(self):
        assert tuple(COLORS["Черный"]["cmyk"]) == (0, 0, 0, 100)

    @pytest.mark.parametrize("name", list(COLORS))
    def test_total_ink_within_limit(self, name):
        assert sum(COLORS[name]["cmyk"]) <= MAX_TOTAL_INK_PERCENT

    @pytest.mark.parametrize("name", list(COLORS))
    def test_values_in_range(self, name):
        assert all(0 <= v <= 100 for v in COLORS[name]["cmyk"])


# ---------------------------------------------------------------------------
# Выбор dpi для Ghostscript
# ---------------------------------------------------------------------------
class TestOutputDpi:
    @pytest.mark.parametrize("size,expected", [
        ((100, 100), 720),      # малые размеры остаются на умолчании Ghostscript
        ((1000, 500), 720),
        ((2000, 750), 374),
        ((3000, 2000), 249),
        ((3000, 3000), 249),
    ])
    def test_known_sizes(self, size, expected):
        assert bg._output_dpi(*size) == expected

    def test_uses_longest_side(self):
        assert bg._output_dpi(3000, 100) == bg._output_dpi(100, 3000) == bg._output_dpi(3000, 3000)

    def test_never_above_default_and_non_increasing(self):
        prev = 10_000
        for side in range(100, 3001, 50):
            dpi = bg._output_dpi(side, 100)
            assert 72 <= dpi <= 720
            assert dpi <= prev
            prev = dpi

    def test_numbers_stay_within_budget_for_every_size(self):
        for side in range(100, 3001, 25):
            max_number = side * 72 / 25.4 * bg._output_dpi(side, side) / 72
            assert max_number <= COORD_LIMIT * 0.9 + 1e-6, side


# ---------------------------------------------------------------------------
# pdf_check на синтетических PDF
# ---------------------------------------------------------------------------
GOOD_STREAM = b"q 1 0 0 1 0 0 cm\n0 1 1 0 k\n0 0 5669.29 2125.98 re\nf\nQ\n"


def _mini_pdf(
    stream: bytes = GOOD_STREAM,
    media: str = "0 0 5669.29 2125.98",
    pages: int = 1,
    output_intent: bool = False,
    icc_based: bool = False,
    pdfx_id: bool = False,
    title: bytes = b"(Banner 2000x750 mm)",
    font: bool = False,
    device_rgb: bool = False,
    form_stream: bytes | None = None,
    image: bool = False,
    objstm: bool = False,
) -> bytes:
    catalog = b"/Type /Catalog /Pages 2 0 R"
    if output_intent:
        catalog += (
            b" /OutputIntents [ << /Type /OutputIntent /S /GTS_PDFX"
            b" /OutputConditionIdentifier (FOGRA39L) /DestOutputProfile 9 0 R >> ]"
        )
    resources = b""
    if font:
        resources += b" /Resources << /Font << /F1 8 0 R >> >>"
    if device_rgb:
        resources += b" /ColorSpace << /CS0 /DeviceRGB >>"
    if icc_based:
        resources += b" /ColorSpace << /CS0 [/ICCBased 9 0 R] >>"
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [" + media.encode() + b"]"
        + resources + b" /Contents 4 0 R >>"
    )
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj\n<< " + catalog + b" >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n" + page + b"\nendobj\n",
    ]
    for i in range(pages - 1):
        parts.append(f"{10 + i} 0 obj\n".encode() + page + b"\nendobj\n")
    parts.append(
        b"4 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"endstream\nendobj\n"
    )
    if font:
        parts.append(b"8 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
    if form_stream is not None:
        parts.append(
            b"6 0 obj\n<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Length "
            + str(len(form_stream)).encode() + b" >>\nstream\n" + form_stream + b"endstream\nendobj\n"
        )
    if image:
        parts.append(
            b"7 0 obj\n<< /Type /XObject /Subtype /Image /Width 1 /Height 1"
            b" /ColorSpace /DeviceCMYK /BitsPerComponent 8 /Length 4 >>\nstream\n"
            b"\x00\x00\x00\x00\nendstream\nendobj\n"
        )
    if objstm:
        parts.append(b"11 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Length 4 >>\nstream\n1 0 \nendstream\nendobj\n")
    info = b"5 0 obj\n<< /Title " + title
    if pdfx_id:
        info += b" /GTS_PDFXVersion (PDF/X-3:2002)"
    parts.append(info + b" >>\nendobj\n")
    parts.append(b"%%EOF\n")
    return b"".join(parts)


class TestPdfCheck:
    def test_good_pdf_has_no_problems(self):
        assert check_print_pdf(_mini_pdf(), width_mm=2000, height_mm=750, max_total_ink=240) == []

    def test_wrong_page_size(self):
        problems = check_print_pdf(_mini_pdf(), width_mm=1000, height_mm=750)
        assert any("размер страницы" in p for p in problems)

    def test_size_tolerance_is_sub_millimetre(self):
        pdf = _mini_pdf(media="0 0 5669.5 2126.2")  # +0,2 pt
        assert check_print_pdf(pdf, width_mm=2000, height_mm=750) == []

    def test_two_pages(self):
        problems = check_print_pdf(_mini_pdf(pages=2), width_mm=2000, height_mm=750)
        assert any("страниц в файле: 2" in p for p in problems)

    def test_fonts_detected(self):
        problems = check_print_pdf(_mini_pdf(font=True), width_mm=2000, height_mm=750)
        assert any("шрифты" in p for p in problems)

    def test_rgb_colorspace_detected(self):
        problems = check_print_pdf(_mini_pdf(device_rgb=True), width_mm=2000, height_mm=750)
        assert any("RGB" in p for p in problems)

    def test_rgb_operator_detected(self):
        stream = b"1 0 0 rg\n0 0 10 10 re\nf\n"
        problems = check_print_pdf(_mini_pdf(stream=stream), width_mm=2000, height_mm=750)
        assert any("rg/RG" in p for p in problems)

    def test_number_over_limit_detected(self):
        stream = b"0 0 56692.9 21259.8 re\nf\n"
        problems = check_print_pdf(_mini_pdf(stream=stream), width_mm=2000, height_mm=750)
        assert any(str(COORD_LIMIT) in p for p in problems)

    def test_number_at_limit_is_ok(self):
        stream = f"0 0 {COORD_LIMIT} 100 re\nf\n".encode()
        assert check_print_pdf(_mini_pdf(stream=stream), width_mm=2000, height_mm=750) == []

    def test_output_intent_is_reported(self):
        problems = check_print_pdf(_mini_pdf(output_intent=True), width_mm=2000, height_mm=750)
        assert any("OutputIntent" in p for p in problems)

    def test_icc_profile_is_reported(self):
        problems = check_print_pdf(_mini_pdf(icc_based=True), width_mm=2000, height_mm=750)
        assert any("ICC" in p for p in problems)

    def test_pdfx_id_is_reported(self):
        problems = check_print_pdf(_mini_pdf(pdfx_id=True), width_mm=2000, height_mm=750)
        assert any("PDF/X" in p for p in problems)

    @pytest.mark.parametrize("title", [b"(untitled)", b"()", b"(Untitled)"])
    def test_service_title_detected(self, title):
        problems = check_print_pdf(_mini_pdf(title=title), width_mm=2000, height_mm=750)
        assert any("Title" in p for p in problems)

    def test_no_content_stream_is_reported_not_silently_passed(self):
        pdf = _mini_pdf().replace(b"4 0 obj", b"4 0 xxx")
        problems = check_print_pdf(pdf, width_mm=2000, height_mm=750)
        assert any("поток содержимого" in p for p in problems)

    def test_rgb_inside_form_xobject_detected(self):
        pdf = _mini_pdf(form_stream=b"1 0 0 rg\n0 0 5 5 re\nf\n")
        problems = check_print_pdf(pdf, width_mm=2000, height_mm=750)
        assert any("rg/RG" in p for p in problems)

    def test_ink_inside_form_xobject_counted(self):
        pdf = _mini_pdf(form_stream=b"1 1 1 1 k\n0 0 5 5 re\nf\n")
        problems = check_print_pdf(pdf, width_mm=2000, height_mm=750, max_total_ink=240)
        assert any("суммарное покрытие" in p for p in problems)

    def test_clean_form_xobject_is_fine(self):
        pdf = _mini_pdf(form_stream=b"0 0 0 1 k\n0 0 5 5 re\nf\n")
        assert check_print_pdf(pdf, width_mm=2000, height_mm=750, max_total_ink=240) == []

    def test_raster_image_is_reported(self):
        problems = check_print_pdf(_mini_pdf(image=True), width_mm=2000, height_mm=750)
        assert any("растровое изображение" in p for p in problems)

    def test_object_streams_are_reported_not_silently_blind(self):
        problems = check_print_pdf(_mini_pdf(objstm=True), width_mm=2000, height_mm=750)
        assert any("object streams" in p for p in problems)

    def test_total_ink_over_limit_detected(self):
        stream = b"0.6 0.4 0.4 1 k\n0 0 10 10 re\nf\n"  # «богатый чёрный», 240%+
        pdf = _mini_pdf(stream=stream)
        problems = check_print_pdf(pdf, width_mm=2000, height_mm=750, max_total_ink=200)
        assert any("суммарное покрытие" in p for p in problems)

    def test_total_ink_at_limit_is_ok(self):
        stream = b"1 1 0 0 k\n0 0 10 10 re\nf\n"  # ровно 200%
        pdf = _mini_pdf(stream=stream)
        assert check_print_pdf(pdf, width_mm=2000, height_mm=750, max_total_ink=200) == []

    def test_total_ink_not_checked_without_limit(self):
        stream = b"1 1 1 1 k\n0 0 10 10 re\nf\n"
        assert check_print_pdf(_mini_pdf(stream=stream), width_mm=2000, height_mm=750) == []

    def test_stroke_cmyk_counts_too(self):
        stream = b"1 1 1 1 K\n0 0 10 10 re\nS\n"
        problems = check_print_pdf(_mini_pdf(stream=stream), width_mm=2000, height_mm=750, max_total_ink=240)
        assert any("суммарное покрытие" in p for p in problems)


# ---------------------------------------------------------------------------
# Реальный Ghostscript
# ---------------------------------------------------------------------------
@needs_gs
class TestFinalPdfWithGhostscript:
    def test_clean_cmyk_without_profile(self, repo_fonts):
        pdf = bg.create_final_pdf(_order()).getvalue()
        assert pdf.startswith(b"%PDF-")
        assert b"/OutputIntents" not in pdf
        assert b"/ICCBased" not in pdf
        assert b"/DeviceRGB" not in pdf
        assert b"/GTS_PDFXVersion" not in pdf
        assert _cmyk_ops(pdf)  # цвет задан CMYK-операторами
        assert check_print_pdf(
            pdf, width_mm=2000, height_mm=750, max_total_ink=MAX_TOTAL_INK_PERCENT
        ) == []

    @pytest.mark.parametrize(
        "size", [(100, 100), (1000, 500), (2000, 750), (3000, 2000), (3000, 3000), (3000, 300)]
    )
    def test_scale_1_to_1_and_numbers_within_limit(self, repo_fonts, size):
        w, h = size
        pdf = bg.create_final_pdf(_order(width=w, height=h)).getvalue()
        assert check_print_pdf(pdf, width_mm=w, height_mm=h) == []
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert float(box.group(1)) == pytest.approx(w * 72 / 25.4, abs=0.01)
        assert float(box.group(2)) == pytest.approx(h * 72 / 25.4, abs=0.01)

    @pytest.mark.parametrize("name", [n for n in COLORS if n != "Белый"])
    def test_background_values_pass_through_unchanged(self, repo_fonts, name):
        text = "Белый" if name != "Белый" else "Черный"
        pdf = bg.create_final_pdf(_order(bg_color=name, text_color=text)).getvalue()
        assert _as_fractions(COLORS[name]["cmyk"]) in _cmyk_ops(pdf)

    @pytest.mark.parametrize("name", list(COLORS))
    def test_text_values_pass_through_unchanged(self, repo_fonts, name):
        bg_name = "Желтый" if name != "Желтый" else "Синий"
        pdf = bg.create_final_pdf(_order(bg_color=bg_name, text_color=name)).getvalue()
        assert _as_fractions(COLORS[name]["cmyk"]) in _cmyk_ops(pdf)

    def test_black_text_is_pure_k100_not_rich_black(self, repo_fonts):
        pdf = bg.create_final_pdf(_order(bg_color="Желтый", text_color="Черный")).getvalue()
        ops = _cmyk_ops(pdf)
        assert (0.0, 0.0, 0.0, 1.0) in ops
        # ни одной комбинации K=1 с примесью C/M/Y
        assert not [o for o in ops if o[3] == 1.0 and any(o[:3])]

    def test_metadata_is_meaningful(self, repo_fonts):
        pdf = bg.create_final_pdf(_order()).getvalue()
        assert re.search(rb"/Title\s*\(Banner 2000x750 mm\)", pdf)
        assert re.search(rb"/Author\s*\(BannerPrint\)", pdf)
        assert b"untitled" not in pdf
        assert b"anonymous" not in pdf

    def test_self_check_failure_does_not_break_delivery(self, repo_fonts, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("checker bug")

        monkeypatch.setattr(bg, "check_print_pdf", boom)
        pdf = bg.create_final_pdf(_order()).getvalue()
        assert pdf.startswith(b"%PDF-")

    def test_problems_are_logged_not_raised(self, repo_fonts, monkeypatch, caplog):
        monkeypatch.setattr(bg, "check_print_pdf", lambda *a, **k: ["тестовая проблема"])
        with caplog.at_level(logging.WARNING, logger=bg.logger.name):
            pdf = bg.create_final_pdf(_order()).getvalue()
        assert pdf.startswith(b"%PDF-")
        assert any("тестовая проблема" in r.getMessage() for r in caplog.records)
