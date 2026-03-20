"""
payment.py (router)
~~~~~~~~~~~~~~~~~~~
Webhook от Tona: POST /api/payment/callback

КРИТИЧЕСКИ ВАЖНО:
  verify_tona_signature() вызывается ПЕРВЫМ, до любой бизнес-логики.
  Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from ..routers.order import transition, OrderStatus
from ..services.payment import verify_tona_signature
from ..services.token_store import create_token
from ..services.referral_store import accrue_commission
from ..db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/payment/callback")
async def payment_callback(
    request: Request,
    x_tona_signature: str = Header(..., alias="X-Tona-Signature"),
):
    """
    Webhook от Tona при успешной оплате.

    Порядок обработки (менять нельзя):
      1. HMAC-SHA256 верификация подписи (P0)
      2. Парсинг тела
      3. FSM transition: pending → paid
      4. Создание download-токена
      5. FSM transition: paid → token_issued
      6. Начисление реферальной комиссии (если есть ref_code)
    """
    # --- ШАГ 1: верификация подписи (ПЕРВЫМ, до чего угодно) ---
    raw_body = await request.body()
    if not verify_tona_signature(raw_body, x_tona_signature):
        logger.warning(
            "Webhook: неверная подпись X-Tona-Signature. IP=%s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Неверная подпись webhook")

    # --- ШАГ 2: парсинг тела ---
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    order_id = payload.get("order_id")
    status   = payload.get("status")

    if not order_id:
        raise HTTPException(status_code=400, detail="Отсутствует order_id")

    logger.info("Webhook Tona: order_id=%s status=%s", order_id, status)

    # Обрабатываем только успешную оплату
    if status != "paid":
        logger.info("Webhook: статус %s для %s — игнорируем", status, order_id)
        return {"ok": True}

    # --- ШАГ 3: FSM pending → paid ---
    try:
        transition(order_id, "webhook_paid")
    except ValueError as e:
        # Если заказ уже paid/token_issued — идемпотентно
        logger.warning("FSM webhook_paid: %s", e)
        current = _get_order_status(order_id)
        if current in (OrderStatus.PAID, OrderStatus.TOKEN_ISSUED):
            logger.info("Webhook идемпотент: заказ %s уже в статусе %s", order_id, current)
            return {"ok": True}
        raise HTTPException(status_code=422, detail=str(e))

    # --- ШАГ 4: создание download-токена ---
    token = create_token(order_id)

    # --- ШАГ 5: FSM paid → token_issued ---
    try:
        transition(order_id, "issue_token")
    except ValueError as e:
        logger.error("FSM issue_token ошибка для %s: %s", order_id, e)
        # Не откатываем — токен создан, пользователь получит PDF через поллинг
        raise HTTPException(status_code=500, detail="Ошибка обновления статуса")

    # --- ШАГ 6: реферальная комиссия ---
    with get_db() as conn:
        row = conn.execute(
            "SELECT ref_code, amount_rub FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if row and row["ref_code"]:
        try:
            accrue_commission(row["ref_code"], order_id, row["amount_rub"])
        except Exception as e:
            # Некритично — логируем, не прерываем
            logger.error("Ошибка начисления реферала для %s: %s", order_id, e)

    logger.info("Webhook обработан успешно: заказ %s → token_issued", order_id)
    return {"ok": True}


def _get_order_status(order_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    return row["status"] if row else None
