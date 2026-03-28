"""
corp_api.py
~~~~~~~~~~~
Корпоративный API.

POST /api/v1/render  — одиночный рендер PDF
GET  /api/v1/usage   — лимиты и статистика за период
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..services.api_key_store import increment_pdf_usage, verify_key
from ..services.renderer import get_executor, render_pdf_sync
from ..services.sanitizer import validate_banner_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

# ---------------------------------------------------------------------------
# RPM-бакеты (in-memory, некритично при рестарте)
# {key_id: [timestamp, ...]} — храним ts последних запросов в окне 60 сек
# ---------------------------------------------------------------------------
_rpm_buckets: dict[int, list[float]] = defaultdict(list)


def _check_rpm(key_id: int, rpm_limit: int) -> None:
    """Проверяет и обновляет RPM-бакет. Бросает 429 при превышении."""
    now = time.monotonic()
    window = 60.0
    bucket = _rpm_buckets[key_id]

    _rpm_buckets[key_id] = [t for t in bucket if now - t < window]

    if len(_rpm_buckets[key_id]) >= rpm_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Превышен лимит запросов: {rpm_limit} req/min",
        )

    _rpm_buckets[key_id].append(now)


# ---------------------------------------------------------------------------
# Dependency — авторизация и проверка лимитов
# ---------------------------------------------------------------------------

async def require_api_key(request: Request) -> dict:
    """
    FastAPI dependency для корп. API.
    Извлекает Bearer-токен, верифицирует ключ, проверяет лимиты.
    Возвращает строку api_keys JOIN api_plans.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer bp_live_"):
        raise HTTPException(
            status_code=401,
            detail="Требуется API-ключ: Authorization: Bearer bp_live_...",
        )

    key = auth.removeprefix("Bearer ")
    api_key = verify_key(key)

    if api_key is None:
        raise HTTPException(status_code=401, detail="Неверный или истёкший API-ключ")

    # RPM-лимит
    _check_rpm(api_key["id"], api_key["rpm_limit"])

    # PDF-лимит (-1 = безлимитный, enterprise)
    if api_key["pdf_limit"] != -1 and api_key["pdf_used"] >= api_key["pdf_limit"]:
        if api_key["plan_id"] == "trial":
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Пробный период исчерпан: использовано {api_key['pdf_used']}"
                    f" из {api_key['pdf_limit']} бесплатных генераций. "
                    "Перейдите на платный тариф."
                ),
            )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Исчерпан лимит PDF за период: {api_key['pdf_used']}/{api_key['pdf_limit']}. "
                "Обновите план или дождитесь следующего периода."
            ),
        )

    return api_key


# ---------------------------------------------------------------------------
# Схемы запроса
# ---------------------------------------------------------------------------

class TextLine(BaseModel):
    text: str = Field(..., min_length=1, max_length=120)
    scale: float = Field(1.0, ge=0.3, le=2.0)


class RenderRequest(BaseModel):
    # Типовой размер ИЛИ кастомный (width_mm + height_mm)
    size_key: str | None = None
    width_mm: int | None = Field(None, ge=100, le=3000)
    height_mm: int | None = Field(None, ge=100, le=3000)

    bg_color: str
    text_color: str
    font: str
    text_lines: list[TextLine] = Field(..., min_length=1, max_length=6)


# ---------------------------------------------------------------------------
# POST /api/v1/render
# ---------------------------------------------------------------------------

@router.post("/render")
async def corp_render(
    body: RenderRequest,
    api_key: dict = Depends(require_api_key),
) -> StreamingResponse:
    """
    Рендерит PDF-баннер и возвращает файл.
    Увеличивает счётчик pdf_used после успешного рендера.
    """
    config = body.model_dump()
    config["text_lines"] = [line.model_dump() for line in body.text_lines]

    errors = validate_banner_config(config)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    t0 = time.monotonic()
    loop = asyncio.get_event_loop()
    executor = get_executor()

    try:
        pdf_bytes: bytes = await loop.run_in_executor(
            executor, render_pdf_sync, config
        )
    except Exception as e:
        logger.error("corp_render: ошибка рендера ключ=%s: %s", api_key["key_prefix"], e)
        raise HTTPException(status_code=500, detail="Ошибка рендеринга PDF")

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    increment_pdf_usage(api_key["id"])

    logger.info(
        "corp_render: ключ=%s план=%s %dms",
        api_key["key_prefix"], api_key["plan_name"], elapsed_ms,
    )

    if body.size_key:
        filename = f"banner_{body.size_key}.pdf"
    else:
        filename = f"banner_{body.width_mm}x{body.height_mm}mm.pdf"

    pdf_used_after = api_key["pdf_used"] + 1
    pdf_limit = api_key["pdf_limit"]

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Render-Time-Ms": str(elapsed_ms),
            "X-PDF-Used": str(pdf_used_after),
            "X-PDF-Limit": str(pdf_limit) if pdf_limit != -1 else "unlimited",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/v1/usage
# ---------------------------------------------------------------------------

@router.get("/usage")
async def corp_usage(api_key: dict = Depends(require_api_key)) -> dict:
    """
    Возвращает текущее использование и лимиты ключа.
    Trial: пожизненный лимит без period_end.
    """
    pdf_limit = api_key["pdf_limit"]
    pdf_used = api_key["pdf_used"]
    is_trial = api_key["plan_id"] == "trial"

    period_start: str | None = None
    period_end: str | None = None

    if not is_trial:
        raw_period_start = api_key.get("period_start")
        if raw_period_start:
            try:
                ps = datetime.fromisoformat(raw_period_start)
                period_start = ps.date().isoformat()
                period_end = (ps + timedelta(days=30)).date().isoformat()
            except (ValueError, TypeError):
                pass

    return {
        "plan": api_key["plan_name"],
        "is_trial": is_trial,
        "pdf_used": pdf_used,
        "pdf_limit": pdf_limit,
        "pdf_remaining": max(0, pdf_limit - pdf_used) if pdf_limit != -1 else None,
        "rpm_limit": api_key["rpm_limit"],
        "period_start": period_start,
        "period_end": period_end,
        "key_prefix": api_key["key_prefix"],
        "label": api_key.get("label"),
    }
