"""
payment_selfwork.py (router)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Запасной роутер webhook: Сам.Эквайринг (selfwork.ru).

Активный роутер — ЮКасса (routers/payment.py).
Чтобы переключиться на Selfwork:
  1. В main.py: from .routers import payment_selfwork as payment

КРИТИЧЕСКИ ВАЖНО:
  verify_selfwork_callback() вызывается ПЕРВЫМ, до любой бизнес-логики.
  Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Формат тела webhook от selfwork (application/json):
  {
    "event":     "payment.succeeded",
    "order_id":  "<uuid>",
    "amount":    <копейки>,
    "signature": "<sha256hex>"
  }

Подпись: SHA256(order_id + amount + SELFWORK_API_KEY)
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from ..db import get_db
from ..routers.order import OrderStatus, transition
from ..services.payment import verify_selfwork_callback
from ..services.referral_store import accrue_commission
from ..services.token_store import create_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/payment/callback")
async def payment_callback(request: Request):
    """
    Webhook от selfwork при успешной оплате.

    Порядок обработки (менять нельзя):
      1. Парсинг тела (нужен для верификации)
      2. SHA256 верификация подписи из поля signature (P0)
      3. Фильтрация: обрабатываем только event='payment.succeeded'
      4. FSM transition: pending → paid
      5. Создание download-токена
      6. FSM transition: paid → token_issued
      7. Начисление реферальной комиссии (если есть ref_code)
    """
    # --- ШАГ 1: парсинг тела ---
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    order_id  = payload.get("order_id")
    amount    = payload.get("amount")      # копейки
    event     = payload.get("event")
    signature = payload.get("signature", "")

    if not order_id:
        raise HTTPException(status_code=400, detail="Отсутствует order_id")

    if amount is None:
        raise HTTPException(status_code=400, detail="Отсутствует amount")

    # --- ШАГ 2: верификация подписи (ПЕРВЫМ после парсинга, до любой логики) ---
    if not verify_selfwork_callback(order_id, int(amount), signature):
        logger.warning(
            "Webhook: неверная подпись. order_id=%s, IP=%s",
            order_id,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Неверная подпись webhook")

    logger.info("Webhook selfwork: order_id=%s event=%s", order_id, event)

    # --- ШАГ 3: обрабатываем только успешную оплату ---
    if event != "payment.succeeded":
        logger.info("Webhook: событие %r для %s — игнорируем", event, order_id)
        return {"ok": True}

    # --- ШАГ 4: FSM pending → paid ---
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

    # --- ШАГ 5: создание download-токена ---
    create_token(order_id)

    # --- ШАГ 6: FSM paid → token_issued ---
    try:
        transition(order_id, "issue_token")
    except ValueError as e:
        logger.error("FSM issue_token ошибка для %s: %s", order_id, e)
        # Не откатываем — токен создан, пользователь получит PDF через поллинг
        raise HTTPException(status_code=500, detail="Ошибка обновления статуса")

    # --- ШАГ 7: реферальная комиссия ---
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
