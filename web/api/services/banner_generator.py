"""
banner_generator.py
~~~~~~~~~~~~~~~~~~~
Генерация баннеров в двух форматах:
  • JPEG-превью  — через Pillow (RGB, быстро, для сайта)
  • PDF для печати — через ReportLab (промежуточный) + Ghostscript (финальный):
      - PDF/X-1a совместимый
      - CMYK с ICC-профилем ISOcoated_v2_300
      - Все шрифты переведены в кривые (outlines)

Двухшаговая схема:
    ReportLab → tmp_raw.pdf → Ghostscript → print_ready.pdf → BytesIO

Локальная копия для web-контейнера.
Не зависит от бота — импортирует только из web/api/services/config.py.
"""

import io
import logging
import os
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .config import COLORS, FONTS, ICC_PROFILE_PATH, SAFE_ZONE_MM

SITE_BASE_URL: str = os.getenv("SITE_BASE_URL", "bannerprintbot.ru")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Регистрация шрифтов в ReportLab (при первом вызове)
# ---------------------------------------------------------------------------
_fonts_registered = False


def _ensure_fonts_registered() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    missing = []
    for name, path in FONTS.items():
        if not os.path.exists(path):
            missing.append(f"{name} → {path}")
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path))
        except Exception as exc:
            logger.error("Не удалось зарегистрировать шрифт %s: %s", name, exc)
            raise RuntimeError(
                f"Ошибка загрузки шрифта '{name}' из '{path}': {exc}\n"
                "Убедитесь, что файлы шрифтов находятся в папке fonts/"
            ) from exc
    if missing:
        raise FileNotFoundError(
            "Отсутствуют файлы шрифтов:\n" + "\n".join(missing)
        )
    _fonts_registered = True


# ---------------------------------------------------------------------------
# Внутренняя функция: расчёт раскладки текста
# ---------------------------------------------------------------------------
def _calculate_layout(
    text_items: list[dict],
    safe_width: float,
    safe_height: float,
    line_spacing_ratio: float = 1.2,
    measure_fn=None,
) -> list[dict]:
    """
    Рассчитывает финальные размеры шрифта для каждой строки с учётом:
      - индивидуального масштаба строки (scale)
      - ограничения по высоте (вертикальный fit)

    measure_fn(text, size) → (width, height)
    """
    details = []
    for item in text_items:
        line = item.get("text", "").strip()
        scale_modifier = item.get("scale", 1.0)
        if not line:
            continue
        effective_width = safe_width * scale_modifier

        ref_size = 100.0
        ref_w, ref_h = measure_fn(line, ref_size)
        if ref_w == 0:
            continue

        font_size = ref_size * (effective_width / ref_w)
        _, line_h = measure_fn(line, font_size)
        details.append(
            {
                "text": line,
                "font_size": font_size,
                "height": line_h,
            }
        )

    # Вертикальный fit: если не влезает — масштабируем все строки
    total_h = sum(d["height"] * line_spacing_ratio for d in details)
    if total_h > safe_height and total_h > 0:
        fit = safe_height / total_h
        for d in details:
            d["font_size"] *= fit
            d["height"] *= fit

    return details


# ---------------------------------------------------------------------------
# JPEG-превью (Pillow, RGB)
# ---------------------------------------------------------------------------
def create_preview_jpeg(data: dict) -> io.BytesIO:
    """
    Создаёт JPEG-превью баннера для отображения на сайте.

    data: {
        "width": int (мм),
        "height": int (мм),
        "bg_color": str,
        "text_color": str,
        "text_lines": [{"text": str, "scale": float}, ...],
        "font": str,
    }
    """
    width_mm: int = data["width"]
    height_mm: int = data["height"]
    bg_color_name: str = data["bg_color"]
    text_color_name: str = data["text_color"]
    text_items: list[dict] = data["text_lines"]
    font_name: str = data["font"]

    _ensure_fonts_registered()

    # Масштаб для превью: 1 пиксель = 1 мм, но ограничиваем по ширине
    max_preview_width = 1200
    scale = min(1.0, max_preview_width / width_mm)
    w_px = int(width_mm * scale)
    h_px = int(height_mm * scale)
    safe_px = SAFE_ZONE_MM * scale

    bg_rgb = COLORS[bg_color_name]["rgb"]
    text_rgb = COLORS[text_color_name]["rgb"]
    font_path = FONTS[font_name]

    image = Image.new("RGB", (w_px, h_px), bg_rgb)
    draw = ImageDraw.Draw(image)

    safe_w = w_px - 2 * safe_px
    safe_h = h_px - 2 * safe_px

    def pillow_measure(text: str, size: float):
        fnt = ImageFont.truetype(font_path, int(size))
        bbox = draw.textbbox((0, 0), text, font=fnt)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    details = _calculate_layout(text_items, safe_w, safe_h, measure_fn=pillow_measure)

    # Слотовое вертикальное распределение:
    # safe zone делится на n равных слотов, каждая строка центрируется в своём слоте.
    # Строки равномерно заполняют всю высоту от safe_px до h_px - safe_px.
    n = len(details)
    slot_h = safe_h / n if n > 0 else safe_h

    for i, d in enumerate(details):
        fnt = ImageFont.truetype(font_path, int(d["font_size"]))
        bbox = draw.textbbox((0, 0), d["text"], font=fnt)
        text_w = bbox[2] - bbox[0]
        x = safe_px + (safe_w - text_w) / 2
        # Центр слота → верхняя граница строки
        slot_top = safe_px + slot_h * i
        y = slot_top + (slot_h - d["height"]) / 2
        # Компенсируем bbox[0] и bbox[1] — offset глифа относительно origin.
        # Без этого каждая строка рисуется на bbox[1] пикселей ниже расчётной
        # позиции, что при нескольких строках накапливается в заметный сдвиг.
        draw.text((x - bbox[0], y - bbox[1]), d["text"], font=fnt, fill=text_rgb)

    # --- Вотермарка ---
    # Плашка «Сделано за 3 минуты в <сайт>» в правом нижнем углу.
    # Ширина ≈ 1/4 ширины баннера; шрифт подбирается по ширине плашки.
    # Фон: чёрный полупрозрачный; для чёрного фона — белый полупрозрачный.
    wm_text = f"Сделано за 3 минуты в {SITE_BASE_URL}"
    wm_font_path = FONTS.get("Golos Text") or next(iter(FONTS.values()))

    wm_target_w = int(w_px * 0.25)   # целевая ширина плашки = 1/4 баннера
    wm_pad_x = int(wm_target_w * 0.08)
    wm_pad_y = int(wm_target_w * 0.06)
    margin = max(4, int(safe_px * 0.5))  # отступ плашки от края

    # Подбираем размер шрифта так, чтобы текст занимал ~(target_w - 2*pad_x)
    wm_text_max_w = wm_target_w - 2 * wm_pad_x
    wm_size = max(8, int(wm_target_w * 0.08))
    for _ in range(30):
        _fnt = ImageFont.truetype(wm_font_path, wm_size)
        _bb = draw.textbbox((0, 0), wm_text, font=_fnt)
        if (_bb[2] - _bb[0]) <= wm_text_max_w:
            break
        wm_size -= 1

    wm_fnt = ImageFont.truetype(wm_font_path, wm_size)
    wm_bb = draw.textbbox((0, 0), wm_text, font=wm_fnt)
    wm_tw = wm_bb[2] - wm_bb[0]
    wm_th = wm_bb[3] - wm_bb[1]

    # Размер плашки подстраивается под реальный текст
    plate_w = wm_tw + 2 * wm_pad_x
    plate_h = wm_th + 2 * wm_pad_y

    # Позиция: правый нижний угол с отступом margin
    plate_x = w_px - plate_w - margin
    plate_y = h_px - plate_h - margin

    # Цвет плашки: чёрный фон → белая плашка, иначе → чёрная
    is_dark_bg = (bg_rgb[0] + bg_rgb[1] + bg_rgb[2]) < 128 * 3
    plate_fill = (255, 255, 255, 160) if is_dark_bg else (0, 0, 0, 160)
    text_fill  = (0, 0, 0, 220)       if is_dark_bg else (255, 255, 255, 220)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    ov_draw.rectangle(
        [plate_x, plate_y, plate_x + plate_w, plate_y + plate_h],
        fill=plate_fill,
    )
    # Текст на плашке: компенсируем bbox offset
    tx = plate_x + wm_pad_x - wm_bb[0]
    ty = plate_y + wm_pad_y - wm_bb[1]
    ov_draw.text((tx, ty), wm_text, font=wm_fnt, fill=text_fill)
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf
# ---------------------------------------------------------------------------
def _create_raw_pdf(data: dict) -> io.BytesIO:
    width_mm: int = data["width"]
    height_mm: int = data["height"]
    bg_color_name: str = data["bg_color"]
    text_color_name: str = data["text_color"]
    text_items: list[dict] = data["text_lines"]
    font_name: str = data["font"]

    _ensure_fonts_registered()

    font_path = FONTS[font_name]
    w_pt = width_mm * mm
    h_pt = height_mm * mm

    # Layout ведётся в мм (= px при scale=1) — идентично create_preview_jpeg.
    # Это гарантирует одинаковый font_size и вертикальный fit в обоих форматах.
    # После layout font_size и height переводятся в pt для ReportLab.
    safe_w_mm = float(width_mm - 2 * SAFE_ZONE_MM)
    safe_h_mm = float(height_mm - 2 * SAFE_ZONE_MM)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))

    # --- Фон ---
    bg_cmyk = COLORS[bg_color_name]["cmyk"]
    if bg_color_name == "Белый":
        # Белый фон: тонкая рамка для обозначения края (типографская метка)
        c.setStrokeColorCMYK(0, 0, 0, 0.3)
        c.setLineWidth(0.1)
        c.rect(0, 0, w_pt, h_pt, fill=0, stroke=1)
    else:
        c_v, m_v, y_v, k_v = [x / 100 for x in bg_cmyk]
        c.setFillColorCMYK(c_v, m_v, y_v, k_v)
        c.rect(0, 0, w_pt, h_pt, fill=1, stroke=0)

    # --- Текст ---
    txt_cmyk = COLORS[text_color_name]["cmyk"]
    tc, tm, ty, tk = [x / 100 for x in txt_cmyk]
    c.setFillColorCMYK(tc, tm, ty, tk)

    def rl_measure(text: str, size: float):
        # size — в мм (единицы layout). stringWidth требует pt.
        size_pt = size * mm
        # Ширина в pt → мм, чтобы совпадало с safe_w_mm.
        w_mm = pdfmetrics.stringWidth(text, font_name, size_pt) / mm
        # Высота — через Pillow textbbox (px = мм при scale=1).
        # face.ascent / 1000 * size_pt (~0.85×size_pt) завышает высоту
        # относительно реального bbox (~0.65×size), что вызывает
        # преждевременный вертикальный fit и уменьшает шрифт в PDF.
        # Pillow даёт точный визуальный bbox — layout идентичен превью.
        _fnt = ImageFont.truetype(font_path, int(size))
        _bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=_fnt)
        h_mm = _bbox[3] - _bbox[1]
        return w_mm, h_mm

    details = _calculate_layout(text_items, safe_w_mm, safe_h_mm, measure_fn=rl_measure)

    # font_size из layout в мм → переводим в pt для ReportLab.
    for d in details:
        d["font_size_pt"] = d["font_size"] * mm

    # Слотовое вертикальное распределение — зеркало Pillow.
    # ReportLab: y=0 внизу страницы, поэтому слоты считаем снизу вверх.
    #   safe_h_pt делится на n равных слотов.
    #   Строка i (0 = верхняя) → слот (n-1-i) снизу.
    safe_pt = SAFE_ZONE_MM * mm
    safe_h_pt = safe_h_mm * mm
    n = len(details)
    slot_h_pt = safe_h_pt / n if n > 0 else safe_h_pt

    # Кэшируем face для расчёта ascent/descent (одинаковый для всех строк шрифта)
    face = pdfmetrics.getFont(font_name).face

    for i, d in enumerate(details):
        size_pt = d["font_size_pt"]
        text_w = pdfmetrics.stringWidth(d["text"], font_name, size_pt)
        x = safe_pt + (safe_w_mm * mm - text_w) / 2

        # Центр слота i (строки сверху вниз: i=0 → верхний слот)
        slot_bottom = safe_pt + slot_h_pt * (n - 1 - i)
        slot_center = slot_bottom + slot_h_pt / 2

        # Центрирование по baseline через метрики шрифта.
        # В ReportLab drawString(x, y): y — это baseline.
        # Визуальный центр строки = baseline + (ascent + descent) / 2,
        # где descent отрицательный (глиф ниже baseline).
        # Приравниваем к центру слота:
        #   baseline + (ascent + descent) / 2 = slot_center
        #   baseline = slot_center - (ascent + descent) / 2
        ascent_pt  = face.ascent  / 1000 * size_pt
        descent_pt = face.descent / 1000 * size_pt  # отрицательный
        y_pos = slot_center - (ascent_pt + descent_pt) / 2

        c.setFont(font_name, size_pt)
        to = c.beginText(x, y_pos)
        to.setFont(font_name, size_pt)
        to.textLine(d["text"])
        c.drawText(to)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Шаг 2: постобработка через Ghostscript
# ---------------------------------------------------------------------------
def _ghostscript_process(input_path: str, output_path: str) -> None:
    """
    Запускает Ghostscript для конвертации в печатный PDF.

    Ключевые флаги:
      -dPDFSETTINGS=/prepress  — настройки для допечатной подготовки
      -dNoOutputFonts          — все шрифты → кривые (outlines)
      -sColorConversionStrategy=CMYK — принудительный CMYK
      -sOutputICCProfile       — встраивает ICC-профиль

    ВАЖНО: вызывается только из ProcessPoolExecutor (CPU-bound операция).
    Прямой вызов из asyncio event loop запрещён.
    """
    icc_exists = os.path.exists(ICC_PROFILE_PATH)

    cmd = [
        "gs",
        "-dBATCH",
        "-dNOPAUSE",
        "-dNOSAFER",
        "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/prepress",
        "-dCompatibilityLevel=1.4",    # PDF 1.4 — требование PDF/X-1a
        "-dNoOutputFonts",             # шрифты в кривые
        "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceCMYK",
        "-dOverrideICC",
    ]

    if icc_exists:
        cmd += [
            f"-sOutputICCProfile={ICC_PROFILE_PATH}",
            f"-sDefaultCMYKProfile={ICC_PROFILE_PATH}",
        ]
    else:
        logger.warning(
            "ICC-профиль не найден по пути %s. "
            "PDF будет сгенерирован без встроенного профиля.",
            ICC_PROFILE_PATH,
        )

    cmd += [
        f"-sOutputFile={output_path}",
        input_path,
    ]

    logger.info("Запуск Ghostscript: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error("Ghostscript stderr: %s", result.stderr)
        raise RuntimeError(
            f"Ghostscript завершился с ошибкой (код {result.returncode}):\n"
            f"{result.stderr[-2000:]}"
        )

    logger.info("Ghostscript завершён успешно → %s", output_path)


# ---------------------------------------------------------------------------
# Публичная функция: финальный PDF для типографии
# ---------------------------------------------------------------------------
def create_final_pdf(data: dict) -> io.BytesIO:
    """
    Возвращает BytesIO с PDF-файлом, готовым для передачи в типографию:
      - CMYK с ICC-профилем ISOcoated_v2_300
      - Шрифты переведены в кривые
      - PDF/X-совместимый формат

    ВАЖНО: эта функция CPU-bound из-за Ghostscript.
    Должна вызываться только через ProcessPoolExecutor.
    """
    # Шаг 1: промежуточный PDF через ReportLab
    raw_buf = _create_raw_pdf(data)

    # Шаг 2: постобработка Ghostscript
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.pdf")
        out_path = os.path.join(tmpdir, "print_ready.pdf")

        with open(raw_path, "wb") as f:
            f.write(raw_buf.getbuffer())

        _ghostscript_process(raw_path, out_path)

        with open(out_path, "rb") as f:
            result_buf = io.BytesIO(f.read())

    result_buf.seek(0)
    return result_buf
