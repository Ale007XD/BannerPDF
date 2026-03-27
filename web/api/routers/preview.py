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
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..services.config import BANNER_SIZES
from ..services.renderer import render_preview_base64
from ..services.sanitizer import sanitize_text_lines, validate_banner_config

logger = logging.getLogger(__name__)
router = APIRouter()


class TextLine(BaseModel):
    text:  str   = Field(..., max_length=120)
    scale: int   = Field(default=100, ge=50, le=100)


class PreviewRequest(BaseModel):
    size_key:   Optional[str] = None
    width_mm:   Optional[int] = None
    height_mm:  Optional[int] = None

    bg_color:   str            = Field(...)
    text_color: str            = Field(...)
    font:       str            = Field(...)
    text_lines: list[TextLine] = Field(..., min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_size(self):
        if not self.size_key:
            if self.width_mm is None or self.height_mm is None:
                raise ValueError("Either size_key or width_mm+height_mm must be provided")
        return self


@router.post("/preview")
async def generate_preview(req: PreviewRequest):
    """
    Генерирует JPEG-превью баннера.
    Возвращает base64-строку и размеры в мм.
    """
    config = {
        "bg_color":   req.bg_color,
        "text_color": req.text_color,
        "font":       req.font,
        "text_lines": [{"text": item.text, "scale": item.scale / 100} for item in req.text_lines],
    }

    # Размер — два режима
    if req.size_key:
        config["size_key"] = req.size_key
    else:
        config["width_mm"]  = req.width_mm
        config["height_mm"] = req.height_mm

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

    # Возвращаем реальные размеры в мм
    if req.size_key:
        width_mm, height_mm = BANNER_SIZES[req.size_key]
    else:
        width_mm  = req.width_mm
        height_mm = req.height_mm

    return {
        "preview_base64": preview_b64,
        "width_mm":  width_mm,
        "height_mm": height_mm,
    }
