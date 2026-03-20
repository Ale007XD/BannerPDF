"""
preview.py
~~~~~~~~~~
POST /api/preview — генерация JPEG-превью баннера.

Ограничение Nginx: 30 req/min/IP, burst=5.
Превью генерируется синхронно (Pillow, не CPU-bound на уровне GIL).
Результат: base64-строка в JSON (data:image/jpeg;base64,... на фронтенде).
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.renderer import render_preview_base64
from ..services.sanitizer import validate_banner_config, sanitize_text_lines

logger = logging.getLogger(__name__)
router = APIRouter()


class TextLine(BaseModel):
    text:  str   = Field(..., max_length=120)
    scale: float = Field(default=1.0, ge=0.3, le=1.5)


class PreviewRequest(BaseModel):
    size_key:   str            = Field(...)
    bg_color:   str            = Field(...)
    text_color: str            = Field(...)
    font:       str            = Field(...)
    text_lines: list[TextLine] = Field(..., min_length=1, max_length=6)


@router.post("/preview")
async def generate_preview(req: PreviewRequest):
    """
    Генерирует JPEG-превью баннера.
    Возвращает base64-строку и размеры в мм.
    """
    config = {
        "size_key":   req.size_key,
        "bg_color":   req.bg_color,
        "text_color": req.text_color,
        "font":       req.font,
        "text_lines": [{"text": l.text, "scale": l.scale} for l in req.text_lines],
    }

    errors = validate_banner_config(config)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    config["text_lines"] = sanitize_text_lines(config["text_lines"])

    try:
        # Pillow не CPU-bound — запускаем в thread executor
        loop = asyncio.get_event_loop()
        preview_b64 = await loop.run_in_executor(
            None,  # default ThreadPoolExecutor
            render_preview_base64,
            config,
        )
    except Exception as e:
        logger.error("Ошибка генерации превью: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Ошибка генерации превью")

    from ..services.config import BANNER_SIZES
    width_mm, height_mm = BANNER_SIZES[req.size_key]

    return {
        "preview_base64": preview_b64,
        "width_mm":  width_mm,
        "height_mm": height_mm,
    }
