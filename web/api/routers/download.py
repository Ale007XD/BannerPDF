"""
download.py
~~~~~~~~~~~
GET /api/download/{token} — выдача PDF по одноразовому токену.

Ограничение Nginx: 10 req/min/IP, burst=3 (защита от брутфорса токенов).
Токен одноразовый, TTL 15 мин.
PDF рендерится в BytesIO — никаких файлов на диске.
GS вызывается через ProcessPoolExecutor (CPU-bound).
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..db import get_db
from ..services.renderer import get_executor, render_pdf_sync
from ..services.token_store import consume_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/download/{token}")
async def download_pdf(token: str):
    """
    Выдаёт PDF по одноразовому download-токену.

    1. Валидирует и сжигает токен
    2. Достаёт config_json из web_orders (постоянное хранилище)
    3. Рендерит PDF через ProcessPoolExecutor (Ghostscript)
    4. Отдаёт как attachment
    """
    # Проверяем и сжигаем токен
    order_id = consume_token(token)
    if order_id is None:
        raise HTTPException(
            status_code=404,
            detail="Токен недействителен, истёк или уже использован",
        )

    # Читаем конфиг из web_orders (постоянное хранение)
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_json, size_key FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        logger.error("download: заказ %s не найден (токен был валидным)", order_id)
        raise HTTPException(status_code=404, detail="Заказ не найден")

    import json
    try:
        config = json.loads(row["config_json"])
    except Exception:
        logger.error("download: невалидный config_json для заказа %s", order_id)
        raise HTTPException(status_code=500, detail="Ошибка конфигурации заказа")

    # Рендерим PDF — CPU-bound, через ProcessPoolExecutor
    try:
        loop = asyncio.get_event_loop()
        pdf_bytes = await loop.run_in_executor(
            get_executor(),
            render_pdf_sync,
            config,
        )
    except Exception as e:
        logger.error("download: ошибка рендеринга PDF для заказа %s: %s", order_id, e)
        raise HTTPException(
            status_code=500,
            detail="Ошибка генерации PDF. Пожалуйста, обратитесь в поддержку.",
        )

    size_key = row["size_key"].replace(".", "_")
    filename = f"banner_{size_key}_{order_id[:8]}.pdf"

    logger.info("PDF выдан: заказ=%s размер=%s bytes=%d", order_id, row["size_key"], len(pdf_bytes))

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Order-Id": order_id,
        },
    )
