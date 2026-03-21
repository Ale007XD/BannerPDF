"""
renderer.py
~~~~~~~~~~~
Адаптер рендерера для FastAPI.
Импортирует напрямую из локальной копии banner_generator.py.
BOT_SRC_PATH не используется — web-контейнер полностью независим.

Превью:  create_preview_jpeg() — синхронно, быстро (Pillow)
PDF:     render_pdf_sync()    — только из ProcessPoolExecutor (Ghostscript)
"""

import base64
import logging
from concurrent.futures import ProcessPoolExecutor

from .banner_generator import create_final_pdf, create_preview_jpeg
from .config import BANNER_SIZES
from .sanitizer import sanitize_text_lines, validate_banner_config

logger = logging.getLogger(__name__)

# ProcessPoolExecutor для Ghostscript (CPU-bound)
# Инициализируется в lifespan FastAPI
_executor: ProcessPoolExecutor | None = None


def set_executor(executor: ProcessPoolExecutor) -> None:
    """Устанавливает глобальный executor. Вызывается из lifespan."""
    global _executor
    _executor = executor


def get_executor() -> ProcessPoolExecutor:
    if _executor is None:
        raise RuntimeError(
            "ProcessPoolExecutor не инициализирован. "
            "Убедитесь, что set_executor() вызван в lifespan."
        )
    return _executor


def build_render_data(config: dict) -> dict:
    """
    Преобразует конфиг из API в формат для banner_generator.
    Валидирует и санитайзит входные данные.

    Поддерживает два режима задания размера:
    - Типовой:   {"size_key": "3x2", ...}
    - Кастомный: {"width_mm": 1200, "height_mm": 800, ...}
    """
    errors = validate_banner_config(config)
    if errors:
        raise ValueError("Ошибки валидации конфига: " + "; ".join(errors))

    # Резолвим размер: size_key имеет приоритет над width_mm/height_mm
    if "size_key" in config and config["size_key"] is not None:
        width_mm, height_mm = BANNER_SIZES[config["size_key"]]
    else:
        width_mm  = int(config["width_mm"])
        height_mm = int(config["height_mm"])

    clean_lines = sanitize_text_lines(config["text_lines"])

    if not clean_lines:
        raise ValueError("После санитайзинга не осталось текстовых строк")

    return {
        "width":      width_mm,
        "height":     height_mm,
        "bg_color":   config["bg_color"],
        "text_color": config["text_color"],
        "text_lines": clean_lines,
        "font":       config["font"],
    }


def render_preview_base64(config: dict) -> str:
    """
    Рендерит JPEG-превью и возвращает base64-строку.
    Синхронная операция — вызывается напрямую из asyncio через run_in_executor
    с thread executor (Pillow не CPU-bound на уровне GIL).
    """
    data = build_render_data(config)
    buf = create_preview_jpeg(data)
    return base64.b64encode(buf.read()).decode("ascii")


def render_pdf_sync(config: dict) -> bytes:
    """
    Рендерит финальный PDF и возвращает байты.
    CPU-bound (Ghostscript) — ДОЛЖНА вызываться только из ProcessPoolExecutor.

    Использование:
        loop = asyncio.get_event_loop()
        pdf_bytes = await loop.run_in_executor(get_executor(), render_pdf_sync, config)
    """
    data = build_render_data(config)
    buf = create_final_pdf(data)
    return buf.read()
