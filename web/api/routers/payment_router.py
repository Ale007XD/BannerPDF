"""
payment.py (router)
~~~~~~~~~~~~~~~~~~~
Webhook от ЮКassa: POST /api/payment/callback

КРИТИЧЕСКИ ВАЖНО:
  verify_yookassa_payment() вызывается ПЕРВЫМ, до любой бизнес-логики.
  Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Формат тела webhook от ЮKassa (application/json):
  {
    "type": "notification",
    "event": "payment.succeeded",
    "object": {
      "id": "<yookassa_payment_id>",
      "status": "succeeded",
      "paid": true,
      "amount": {"value": "299.00", "currency": "RUB"},
      "metadata": {"order_id": "<uuid>"}
    }
  }

Верификация: GET /v3/payments/{payment_id} к API ЮКassa с базовой аутентификацией.
У ЮКassa нет HMAC-подписи в теле — единственный надёжный способ верификации.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from ..db import get_db
from ..routers.order import OrderStatus, transition
from ..services.payment import verify_yookassa_payment
from ..services.referral_store import accrue_commission
from ..services.token_store import create_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/payment/callback")
async def payment_callback(request: Request):
    """
    Webhook от ЮКassa при успешной оплате.

    Порядок обработки (менять нельзя):
      1. Парсинг тела (нужен для верификации)
      2. Верификация через GET /v3/payments/{payment_id} к API ЮКassa (P0)
      3. Фильтрация: обрабатываем только event='payment.succeeded'
      4. Сохранение yookassa_payment_id в БД
      5. FSM transition: pending → paid
      6. Создание download-токена
      7. FSM transition: paid → token_issued
      8. Начисление реферальной комиссии (если есть ref_code)
    """
    # --- ШАГ 1: парсинг тела ---
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    event = payload.get("event")
    obj = payload.get("object", {})
    payment_id = obj.get("id")
    metadata = obj.get("metadata", {})
    order_id = metadata.get("order_id")

    if not payment_id:
        raise HTTPException(status_code=400, detail="Отсутствует object.id (payment_id)")

    if not order_id:
        raise HTTPException(status_code=400, detail="Отсутствует metadata.order_id")

    # --- ШАГ 2: верификация через API ЮКassa (ПЕРВЫМ после парсинга, до любой логики) ---
    try:
        verified_data = await verify_yookassa_payment(payment_id)
    except HTTPException as e:
        logger.warning(
            "Webhook ЮКassa: верификация не прошла. payment_id=%s, order_id=%s, IP=%s, error=%s",
            payment_id,
            order_id,
            request.client.host if request.client else "unknown",
            e.detail,
        )
        raise

    # Проверяем, что order_id из вебхука совпадает с order_id в ЮКassa
    verified_order_id = verified_data.get("metadata", {}).get("order_id")
    if verified_order_id != order_id:
        logger.error(
            "Webhook ЮКassa: несовпадение order_id. webhook=%s, yookassa=%s",
            order_id, verified_order_id,
        )
        raise HTTPException(status_code=403, detail="order_id не совпадает с данными ЮКassa")

    logger.info("Webhook ЮКassa: payment_id=%s order_id=%s event=%s", payment_id, order_id, event)

    # --- ШАГ 3: обрабатываем только успешную оплату ---
    if event != "payment.succeeded":
        logger.info("Webhook: событие %r для %s — игнорируем", event, order_id)
        return {"ok": True}

    # --- ШАГ 4: сохранение yookassa_payment_id ---
    with get_db() as conn:
        conn.execute(
            "UPDATE web_orders SET yookassa_payment_id = ? WHERE id = ?",
            (payment_id, order_id),
        )
        conn.commit()

    # --- ШАГ 5: FSM pending → paid ---
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

    # --- ШАГ 6: создание download-токена ---
    create_token(order_id)

    # --- ШАГ 7: FSM paid → token_issued ---
    try:
        transition(order_id, "issue_token")
    except ValueError as e:
        logger.error("FSM issue_token ошибка для %s: %s", order_id, e)
        # Не откатываем — токен создан, пользователь получит PDF через поллинг
        raise HTTPException(status_code=500, detail="Ошибка обновления статуса")

    # --- ШАГ 8: реферальная комиссия ---
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
