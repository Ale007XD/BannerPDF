"""
download.py
~~~~~~~~~~~
GET /api/download/{token} — выдача PDF по одноразовому токену.

Ограничение Nginx: 10 req/min/IP, burst=3 (защита от брутфорса токенов).
Токен действует 15 мин и допускает до MAX_DOWNLOADS успешных выдач.
Выдача фиксируется только после успешного рендера: сбой генерации или
обрыв соединения не расходуют оплаченную ссылку.
PDF рендерится в BytesIO — никаких файлов на диске.
GS вызывается через ProcessPoolExecutor (CPU-bound).
"""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response

from ..db import get_db
from ..services import partner_naming, partner_store
from ..services.conversions import record_order_event
from ..services.partner_delivery import deliver_order_pdf
from ..services.renderer import get_executor, render_pdf_sync
from ..services.token_store import peek_token, record_download

logger = logging.getLogger(__name__)
router = APIRouter()


_TOKEN_INVALID_DETAIL = "Ссылка недействительна, истекла или исчерпан лимит скачиваний"


@router.get("/download/{token}")
async def download_pdf(token: str, background: BackgroundTasks):
    """
    Выдаёт PDF по download-токену.

    1. Проверяет токен (peek_token) — без изменения состояния
    2. Достаёт config_json из web_orders (постоянное хранилище)
    3. Рендерит PDF через ProcessPoolExecutor (Ghostscript)
    4. Фиксирует выдачу (record_download) и отдаёт как attachment
    5. Партнёрский заказ: в имени файла номер заказа и телефон типографии («Заказ 482-915, скажите его
       в Реклама-Сити по номеру 90-20-55.pdf»), тот же номер в заголовке X-Order-Number для окна после оплаты;
       после ответа клиенту этот же PDF уходит на почту типографии (services/partner_delivery.py;
       сбой отправки клиента не затрагивает)

    Если шаг 3 упал — токен остаётся действительным, клиент может повторить.
    """
    order_id = peek_token(token)
    if order_id is None:
        raise HTTPException(status_code=404, detail=_TOKEN_INVALID_DETAIL)

    # Читаем конфиг из web_orders (постоянное хранение)
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_json, size_key, partner_code FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        logger.error("download: заказ %s не найден (токен был валидным)", order_id)
        raise HTTPException(status_code=404, detail="Заказ не найден")

    import json
    if not row["config_json"]:
        # Данные заказа стёрты по истечении срока хранения (см. services/retention.py)
        raise HTTPException(status_code=410, detail="Данные заказа удалены по истечении срока хранения")
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
            detail=(
                "Ошибка генерации PDF. Ссылка осталась действительной — "
                "попробуйте ещё раз или обратитесь в поддержку."
            ),
        )

    # Фиксируем выдачу только теперь, когда файл готов. False — лимит
    # исчерпан параллельными запросами, пока шёл рендер.
    if not record_download(token):
        raise HTTPException(status_code=404, detail=_TOKEN_INVALID_DETAIL)

    record_order_event("file_downloaded", row["config_json"])

    size_key = row["size_key"].replace(".", "_")
    headers = {
        "Content-Disposition": f'attachment; filename="banner_{size_key}_{order_id[:8]}.pdf"',
        "Cache-Control": "no-store",
        "X-Order-Id": order_id,
    }

    logger.info("PDF выдан: заказ=%s размер=%s bytes=%d", order_id, row["size_key"], len(pdf_bytes))

    if row["partner_code"]:
        # Отключённому партнёру имя «скажите его в …» не нужно: страница у клиента уже без бренда
        partner = partner_store.get_active_partner(row["partner_code"])
        if partner is not None:
            headers["Content-Disposition"] = partner_naming.content_disposition(
                partner_naming.client_filename(partner["name"], partner["phone"], order_id),
                partner_naming.ascii_fallback(order_id),
            )
            headers["X-Order-Number"] = partner_naming.order_number(order_id)
        background.add_task(
            deliver_order_pdf,
            order_id, row["partner_code"], row["size_key"], row["config_json"], pdf_bytes,
        )

    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
