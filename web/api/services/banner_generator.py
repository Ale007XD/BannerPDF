"""
banner_generator.py
~~~~~~~~~~~~~~~~~~~
Генерация баннеров в двух форматах:
  • JPEG-превью  — через Pillow (RGB, быстро, для сайта)
  • PDF для печати — через ReportLab (промежуточный) + Ghostscript (финальный):
      - чистый DeviceCMYK: значения красок из config.COLORS записаны напрямую,
        без конвертации и без встроенного ICC-профиля / OutputIntent
        (профиль применяет RIP типографии)
      - масштаб 1:1, страница ровно размера баннера
      - Все шрифты переведены в кривые (outlines)

Двухшаговая схема:
    ReportLab → tmp_raw.pdf → Ghostscript → print_ready.pdf → BytesIO

Локальная копия для web-контейнера.
Не зависит от бота — импортирует только из web/api/services/config.py.
"""

import io
import logging
import math
import os
import subprocess
import tempfile
from functools import lru_cache
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .config import COLORS, FONTS, MAX_TOTAL_INK_PERCENT, SAFE_ZONE_MM
from .pdf_check import COORD_LIMIT, check_print_pdf

SITE_BASE_URL: str = os.getenv("SITE_BASE_URL", "bannerbot.ru")


def _site_host(base_url: str) -> str:
    """Имя хоста для надписи на превью.

    SITE_BASE_URL — полный URL (его же используют payment.py и tg_notify.py
    для ссылок), а на баннер нужен только хост: без схемы, порта и пути.
    Значение без схемы («bannerbot.ru») тоже принимается.
    """
    raw = (base_url or "").strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
    return host or raw

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
    measure_fn=None,
) -> list[dict]:
    """
    Рассчитывает финальные размеры шрифта для каждой строки.

    measure_fn(text, size) → (width, height)
    Единицы size и возвращаемых значений определяются вызывающей стороной
    (мм для PDF-ветки, px для Pillow-превью).

    Алгоритм (два прохода):
      1. Width-fit: font_size подгоняется так, чтобы строка занимала
         effective_width (safe_width * scale) по горизонтали.
      2. Uniform scale: все строки масштабируются ОДНИМ коэффициентом,
         если суммарная высота строк превышает safe_height * FILL_RATIO.
         Один коэффициент гарантирует, что все строки остаются одинаковой
         относительной ширины (обе 100% или обе N% — но одинаково).

    Позиционирование строк — НЕ здесь. Вызывающий код (Pillow / ReportLab)
    распределяет строки пропорционально их реальным высотам с равными отступами:
        total_h  = sum(d["height"])
        padding  = (safe_height - total_h) / (n + 1)
        y_i = safe_px + padding*(i+1) + sum(h_0..h_{i-1})
    Это устраняет overflow при коротких строках с крупным шрифтом.
    """
    # Какую долю safe_height разрешаем занять строкам суммарно.
    # 0.85 оставляет 15% на равномерные отступы между строками.
    FILL_RATIO = 0.85

    details = []
    for item in text_items:
        line = item.get("text", "").strip()
        scale_modifier = item.get("scale", 1.0)
        if not line:
            continue
        effective_width = safe_width * scale_modifier

        ref_size = 100.0
        ref_w, _ = measure_fn(line, ref_size)
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

    if not details:
        return details

    # Uniform scale: если суммарная высота > safe_height * FILL_RATIO
    # масштабируем все строки одним коэффициентом.
    total_h = sum(d["height"] for d in details)
    max_total_h = safe_height * FILL_RATIO
    if total_h > max_total_h:
        scale = max_total_h / total_h
        for d in details:
            d["font_size"] *= scale
            d["height"] *= scale

    return details


# ---------------------------------------------------------------------------
# JPEG-превью (Pillow, RGB)
# ---------------------------------------------------------------------------
# Диагональная надпись в превью: доля длины диагонали, доля стороны (запас от краёв), прозрачность
_DIAG_WM_MAX_DIAG_FRACTION = 0.75
_DIAG_WM_EDGE_FRACTION = 0.90
_DIAG_WM_ALPHA = 105
_DIAG_WM_STROKE_ALPHA = 105


def _draw_diagonal_watermark(overlay: Image.Image, text: str, make_font) -> None:
    """
    Рисует надпись по диагонали баннера: от левого нижнего угла к правому верхнему.

    Угол наклона = угол диагонали (atan2(h, w)), надпись центрирована по баннеру.
    Размер шрифта подбирается так, чтобы повёрнутый текст целиком помещался
    в баннер при любых пропорциях (в том числе узких кастомных размерах).

    Белая заливка с тёмной обводкой читается на любом фоне, а alpha оставляет
    сам макет различимым, чтобы пользователь мог вычитать свой текст.

    overlay   — RGBA-слой размера баннера, рисуется на месте
    make_font — функция size -> ImageFont (шрифт передаётся снаружи)
    """
    w_px, h_px = overlay.size
    theta = math.atan2(h_px, w_px)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    diag = math.hypot(w_px, h_px)

    # Размеры текста линейны по кегле: меряем на эталонном и масштабируем.
    ref_size = 100
    ref_font = make_font(ref_size)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    bb = probe.textbbox((0, 0), text, font=ref_font)
    ref_w, ref_h = bb[2] - bb[0], bb[3] - bb[1]
    if ref_w <= 0 or ref_h <= 0:
        return

    # Три ограничения на кегль: длина вдоль диагонали и габариты повёрнутого текста.
    size = min(
        _DIAG_WM_MAX_DIAG_FRACTION * diag / ref_w,
        _DIAG_WM_EDGE_FRACTION * w_px / (ref_w * cos_t + ref_h * sin_t),
        _DIAG_WM_EDGE_FRACTION * h_px / (ref_w * sin_t + ref_h * cos_t),
    ) * ref_size
    size = max(8, int(size))

    fnt = make_font(size)
    stroke = max(1, size // 18)
    bb = probe.textbbox((0, 0), text, font=fnt, stroke_width=stroke)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (-bb[0], -bb[1]),
        text,
        font=fnt,
        fill=(255, 255, 255, _DIAG_WM_ALPHA),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, _DIAG_WM_STROKE_ALPHA),
    )

    # PIL.rotate вращает против часовой: базовая линия текста идёт из левого нижнего в правый верхний.
    rotated = layer.rotate(math.degrees(theta), expand=True, resample=Image.BICUBIC)

    # Центрируем по реальным пикселям надписи, а не по рамке слоя: у строки с выносными
    # элементами (б, д, у) рамка шире «чернил», и надпись иначе съезжала бы вниз.
    ink = rotated.getchannel("A").getbbox()
    if ink is None:
        return
    x = w_px // 2 - (ink[0] + ink[2]) // 2
    y = h_px // 2 - (ink[1] + ink[3]) // 2
    overlay.alpha_composite(rotated, (max(0, x), max(0, y)))


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

    # Пропорциональное вертикальное распределение.
    # Строки занимают ровно столько высоты, сколько у них реальных пикселей.
    # Оставшееся место делится на (n+1) равных отступов: сверху, между строками, снизу.
    n = len(details)
    total_h = sum(d["height"] for d in details)
    padding = (safe_h - total_h) / (n + 1) if n > 0 else 0

    y_cursor = safe_px + padding
    for d in details:
        fnt = ImageFont.truetype(font_path, int(d["font_size"]))
        bbox = draw.textbbox((0, 0), d["text"], font=fnt)
        text_w = bbox[2] - bbox[0]
        x = safe_px + (safe_w - text_w) / 2
        # y_cursor — верхняя граница строки (видимых пикселей).
        # Компенсируем bbox[1] чтобы верхний пиксель глифа попал точно в y_cursor.
        draw.text((x - bbox[0], y_cursor - bbox[1]), d["text"], font=fnt, fill=text_rgb)
        y_cursor += d["height"] + padding

    # --- Вотермарка ---
    # Плашка «Сделано за 3 минуты в <сайт>» в правом нижнем углу
    # + та же надпись по диагонали (см. _draw_diagonal_watermark).
    # Ширина ≈ 1/4 ширины баннера; шрифт подбирается по ширине плашки.
    # Фон: чёрный полупрозрачный; для чёрного фона — белый полупрозрачный.
    wm_text = f"Сделано за 3 минуты в {_site_host(SITE_BASE_URL)}"
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

    # Диагональная надпись поверх макета (защита превью от использования вместо оплаты)
    _draw_diagonal_watermark(overlay, wm_text, lambda sz: ImageFont.truetype(wm_font_path, sz))

    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return buf
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1024)
def _glyph_ink_x(font_path: str, char: str) -> tuple[float, float]:
    """Видимые границы одного символа по X от точки (0, 0), в долях кегля.

    Берём растеризацией: textbbox в Pillow возвращает левую границу 0 вместо
    реального левого выступа буквы, из-за чего текст съезжал вправо.
    """
    fnt = ImageFont.truetype(font_path, 1000)
    img = Image.new("L", (3000, 1600), 0)
    ImageDraw.Draw(img).text((1000, 200), char, font=fnt, fill=255)
    box = img.getbbox()
    return ((box[0] - 1000) / 1000, (box[2] - 1000) / 1000) if box else (0.0, 0.0)


@lru_cache(maxsize=4096)
def _ink_metrics(font_path: str, font_name: str, text: str) -> tuple[float, float, float, float, float]:
    """Видимые границы строки в долях кегля: (left, top, right, bottom, ascent).

    PDF-ветке нужно повторять превью: подгонка по ширине и центрирование идут по
    видимой части строки, а не по рамке метрики (иначе наклонные «!», «,» рукописного
    шрифта выходят за поля), а базовая линия учитывает нижние выносные элементы.

    По горизонтали ширина берётся у самого ReportLab (он рисует без кернинга, как и
    будет выведено в PDF), поправка только на выступ первой и последней буквы.
    По вертикали — видимые границы строки из Pillow (кернинг на них не влияет).
    """
    fnt = ImageFont.truetype(font_path, 1000)
    _, top, _, bottom = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=fnt)
    first, last = text[:1], text[-1:]
    left = _glyph_ink_x(font_path, first)[0] if first.strip() else 0.0
    right = pdfmetrics.stringWidth(text, font_name, 1000) / 1000
    if last.strip():
        right += _glyph_ink_x(font_path, last)[1] - pdfmetrics.stringWidth(last, font_name, 1000) / 1000
    return left, top / 1000, right, bottom / 1000, fnt.getmetrics()[0] / 1000


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
    # Метаданные: PDF/X требует осмысленный Title; значения ReportLab по умолчанию
    # («untitled» / «anonymous») перекрывают Info из def-файла Ghostscript.
    # Только ASCII — Title попадает ещё и в XMP.
    c.setTitle(f"Banner {width_mm}x{height_mm} mm")
    c.setAuthor("BannerBot")
    c.setCreator("BannerBot")
    c.setSubject("Print-ready banner, CMYK, outlined text")

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
        # size — в мм (единицы layout). Ширина и высота — по видимым границам букв,
        # как в Pillow-превью (см. _ink_metrics), поэтому layout совпадает с превью.
        left, top, right, bottom, _ = _ink_metrics(font_path, font_name, text)
        return (right - left) * size, (bottom - top) * size

    details = _calculate_layout(text_items, safe_w_mm, safe_h_mm, measure_fn=rl_measure)

    # font_size и height из layout в мм → переводим в pt для ReportLab.
    for d in details:
        d["font_size_pt"] = d["font_size"] * mm
        d["height_pt"] = d["height"] * mm

    # Пропорциональное вертикальное распределение — зеркало Pillow.
    # ReportLab: y=0 внизу страницы, ось Y направлена вверх.
    # Верх safe zone в RL = h_pt - safe_pt.
    # Первая строка начинается на padding_pt ниже верха safe zone.
    # total_h + padding*(n+1) = safe_h_pt → одинаковые отступы сверху, снизу и между строками.
    safe_pt = SAFE_ZONE_MM * mm
    safe_h_pt = safe_h_mm * mm
    n = len(details)
    total_h_pt = sum(d["height_pt"] for d in details)
    padding_pt = (safe_h_pt - total_h_pt) / (n + 1) if n > 0 else 0

    # y_top — верхняя граница текущей строки в RL-координатах (от низа страницы вверх).
    # Начинаем от верха safe zone, опускаемся на padding_pt.
    y_top = h_pt - safe_pt - padding_pt
    for d in details:
        size_pt = d["font_size_pt"]
        left, top, right, _bottom, ascent = _ink_metrics(font_path, font_name, d["text"])
        ink_w_pt = (right - left) * size_pt
        # Центрируем видимую часть строки (а не рамку по метрике шрифта).
        x = safe_pt + (safe_w_mm * mm - ink_w_pt) / 2 - left * size_pt

        # y_top — верхняя граница видимой части строки в RL-координатах.
        # Базовая линия ниже неё на (ascent - top): так нижние выносные элементы
        # строчных («р», «д», «у») остаются внутри строки, а не уезжают за поля.
        y_pos = y_top - (ascent - top) * size_pt

        c.setFont(font_name, size_pt)
        to = c.beginText(x, y_pos)
        to.setFont(font_name, size_pt)
        to.textLine(d["text"])
        c.drawText(to)

        # Следующая строка: опускаемся на height + padding (в RL — вычитаем).
        y_top -= d["height_pt"] + padding_pt

    c.showPage()
    c.save()
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Шаг 2: постобработка через Ghostscript
# ---------------------------------------------------------------------------
# Разрешение вывода pdfwrite. Число в потоке страницы = размер в pt × dpi / 72, а по
# умолчанию (720 dpi) координаты ×10: баннер 2000 мм даёт ~56 700 при лимите 32 767
# (PDF 1.4, PDF/A, строгие препресс-проверки). Поэтому dpi выбирается по размеру:
# максимум 720 (как у Ghostscript по умолчанию), но не больше, чем позволяет лимит
# с запасом _GS_NUMBER_BUDGET. Получается: до ~1040 мм — 720 dpi, 2000 мм — 374,
# 3000 мм — 249.
# Не занижать dpi «с запасом»: сдвиг контуров относительно эталона растёт вместе
# с размером пикселя и не монотонен. Измерено (макс. сдвиг вершин, мм): 3000×3000
# при 144 dpi — 1,95; при 249 dpi — 0,5; 2000×750 при 144 — 0,25, при 374 — 0,05.
_GS_MAX_DPI = 720
_GS_NUMBER_BUDGET = 0.9


def _output_dpi(width_mm: float, height_mm: float) -> int:
    """Максимальное dpi ≤ 720, при котором числа в потоке остаются ≤ 90% лимита 32767."""
    side_pt = max(width_mm, height_mm) * 72 / 25.4
    return int(max(72, min(_GS_MAX_DPI, COORD_LIMIT * _GS_NUMBER_BUDGET * 72 / side_pt)))


def _ghostscript_process(input_path: str, output_path: str, dpi: int = _GS_MAX_DPI) -> None:
    """
    Запускает Ghostscript: шрифты → кривые, цвет остаётся чистым DeviceCMYK.

    Ключевые флаги:
      -dPDFSETTINGS=/prepress  — настройки для допечатной подготовки
      -dNoOutputFonts          — все шрифты → кривые (outlines)
      -sColorConversionStrategy=CMYK, -dProcessColorModel=/DeviceCMYK
                               — на выходе только CMYK
      -r<dpi>                  — см. _output_dpi (числа в потоке ≤ лимита 32767)

    ICC-профиль намеренно НЕ подключается: ReportLab уже пишет DeviceCMYK,
    конвертировать нечего, и значения красок проходят без изменений (проверено:
    содержимое страницы байт-в-байт одинаково с профилем и без него для всех
    пар цветов палитры). Встраивание профиля/OutputIntent не добавило бы
    точности, но обещало бы то, что за файлом не стоит.

    TODO: если появится загрузка растровых RGB-логотипов, профиль понадобится
    снова, но только как конвертирующий (-sDefaultRGBProfile / -sOutputICCProfile
    без OutputIntent): иначе Ghostscript переведёт RGB в CMYK своим профилем по
    умолчанию и цвет станет непредсказуемым. До тех пор pdf_check считает
    растровую картинку в файле проблемой.

    ВАЖНО: вызывается только из ProcessPoolExecutor (CPU-bound операция).
    Прямой вызов из asyncio event loop запрещён.
    """
    cmd = [
        "gs",
        "-dBATCH",
        "-dNOPAUSE",
        "-dNOSAFER",
        "-sDEVICE=pdfwrite",
        "-dPDFSETTINGS=/prepress",
        "-dCompatibilityLevel=1.4",
        "-dNoOutputFonts",             # шрифты в кривые
        "-sColorConversionStrategy=CMYK",
        "-dProcessColorModel=/DeviceCMYK",
        f"-r{dpi}",
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
      - чистый DeviceCMYK, значения из config.COLORS записаны напрямую
      - без встроенного профиля и OutputIntent (профиль применяет RIP)
      - масштаб 1:1
      - шрифты переведены в кривые

    Результат самопроверяется (pdf_check): проблемы только логируются,
    заказ из-за них не падает.

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

        _ghostscript_process(raw_path, out_path, _output_dpi(data["width"], data["height"]))

        with open(out_path, "rb") as f:
            pdf_bytes = f.read()

    _log_pdf_problems(pdf_bytes, data)
    return io.BytesIO(pdf_bytes)


def _log_pdf_problems(pdf_bytes: bytes, data: dict) -> None:
    """Самопроверка готового PDF. Никогда не бросает исключений."""
    try:
        problems = check_print_pdf(
            pdf_bytes,
            width_mm=data["width"],
            height_mm=data["height"],
            max_total_ink=MAX_TOTAL_INK_PERCENT,
        )
    except Exception:  # проверка не должна ломать выдачу файла
        logger.exception("Самопроверка PDF завершилась ошибкой")
        return
    if problems:
        logger.warning("Печатный PDF %sx%s мм: %s", data["width"], data["height"], "; ".join(problems))
