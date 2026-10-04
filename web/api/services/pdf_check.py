"""
pdf_check.py
~~~~~~~~~~~~
Самопроверка печатного PDF без внешних зависимостей (только stdlib).

Это не PDF/X-валидатор. Проверяется контракт, который мы заявляем покупателю
(«CMYK, шрифты в кривых, масштаб 1:1, значения красок заданы напрямую»):

  • одна страница нужного размера (MediaBox, допуск 0,5 pt ≈ 0,2 мм) — масштаб 1:1
  • нет шрифтов (текст переведён в кривые)
  • нет RGB (ни /DeviceRGB, ни операторов rg/RG в потоке страницы)
  • нет встроенного профиля и OutputIntent — значения красок заданы напрямую
  • числа в потоке страницы не превышают 32767 (лимит PDF 1.4 / PDF/A)
  • суммарное покрытие красками (C+M+Y+K) не выше заданного лимита
  • Title не служебный ("untitled")

Если когда-нибудь в файл будет добавлен профиль/OutputIntent, проверка «нет
профиля» упадёт — это сигнал заодно поправить текст на сайте.
"""

import re
import zlib

COORD_LIMIT = 32767
_PT_PER_MM = 72 / 25.4
_SIZE_TOLERANCE_PT = 0.5

_MEDIABOX = re.compile(
    rb"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]"
)
_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_FONT = re.compile(rb"/Type\s*/Font(?![A-Za-z])")
_OBJ = re.compile(rb"\d+\s+\d+\s+obj\b(.*?)\bendobj", re.DOTALL)
_STREAM_START = re.compile(rb">>\s*stream\r?\n")
_NUMBER = re.compile(rb"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
_RGB_OP = re.compile(rb"(?:^|\s)(?:rg|RG)(?=\s|$)")
_CMYK_OP = re.compile(
    rb"(?:^|\s)(\d*\.?\d+)\s+(\d*\.?\d+)\s+(\d*\.?\d+)\s+(\d*\.?\d+)\s+[kK](?=\s|$)"
)
_TITLE = re.compile(rb"/Title\s*\(([^)]*)\)")


def _page_content_streams(data: bytes):
    """Разжатые потоки, похожие на содержимое страницы (не ICC, не XMP, не картинки)."""
    for obj in _OBJ.finditer(data):
        body = obj.group(1)
        start = _STREAM_START.search(body)
        end = body.rfind(b"endstream")
        if start is None or end < start.end():
            continue
        header, raw = body[: start.start() + 2], body[start.end():end]
        if b"/Subtype" in header or b"/Length1" in header or re.search(rb"/N\s+\d", header):
            continue
        if b"ASCII85Decode" in header or b"LZWDecode" in header or b"DCTDecode" in header:
            continue  # промежуточный PDF ReportLab; проверяем только финальный Flate/без фильтра
        if b"FlateDecode" in header:
            try:
                yield zlib.decompress(raw)
            except zlib.error:
                continue
        else:
            yield raw


def check_print_pdf(
    data: bytes,
    *,
    width_mm: float | None = None,
    height_mm: float | None = None,
    max_total_ink: float | None = None,
) -> list[str]:
    """Возвращает список найденных проблем; пустой список — всё в порядке."""
    problems: list[str] = []

    pages = len(_PAGE.findall(data))
    if pages != 1:
        problems.append(f"страниц в файле: {pages}, ожидалась 1")

    box = _MEDIABOX.search(data)
    if box is None:
        problems.append("не найден MediaBox")
    elif width_mm is not None and height_mm is not None:
        x0, y0, x1, y1 = (float(v) for v in box.groups())
        got_w, got_h = x1 - x0, y1 - y0
        exp_w, exp_h = width_mm * _PT_PER_MM, height_mm * _PT_PER_MM
        if abs(got_w - exp_w) > _SIZE_TOLERANCE_PT or abs(got_h - exp_h) > _SIZE_TOLERANCE_PT:
            problems.append(
                f"размер страницы {got_w:.2f}x{got_h:.2f} pt, ожидался {exp_w:.2f}x{exp_h:.2f} pt"
            )

    if _FONT.search(data):
        problems.append("в файле есть шрифты (текст не переведён в кривые)")

    if b"/DeviceRGB" in data or b"/CalRGB" in data:
        problems.append("в файле есть цветовое пространство RGB")

    max_num = 0.0
    max_ink = 0.0
    rgb_ops = False
    streams = 0
    for stream in _page_content_streams(data):
        streams += 1
        if _RGB_OP.search(stream):
            rgb_ops = True
        for c, m, y, k in _CMYK_OP.findall(stream):
            max_ink = max(max_ink, (float(c) + float(m) + float(y) + float(k)) * 100)
        for num in _NUMBER.findall(stream):
            max_num = max(max_num, abs(float(num)))
    if streams == 0:
        problems.append("не найден поток содержимого страницы (проверка цвета и чисел не выполнена)")
    if rgb_ops:
        problems.append("в потоке страницы есть RGB-операторы rg/RG")
    if max_num > COORD_LIMIT:
        problems.append(f"число в потоке страницы {max_num:g} превышает лимит {COORD_LIMIT}")

    if max_total_ink is not None and max_ink > max_total_ink + 1e-6:
        problems.append(f"суммарное покрытие красками {max_ink:g}% выше лимита {max_total_ink:g}%")

    if b"/ICCBased" in data:
        problems.append("в файле есть ICC-профиль (ICCBased): значения красок должны быть заданы напрямую")
    if b"/OutputIntents" in data or b"/DestOutputProfile" in data:
        problems.append("в файле есть OutputIntent: на сайте заявлено, что профиль не встраивается")
    if b"/GTS_PDFXVersion" in data:
        problems.append("в файле есть идентификация PDF/X, которую мы не заявляем")

    title = _TITLE.search(data)
    if title is None or title.group(1).strip().lower() in (b"", b"untitled"):
        problems.append("пустой или служебный Title")

    return problems
