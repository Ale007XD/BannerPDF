"""
payment.py (router)
~~~~~~~~~~~~~~~~~~~
Webhook от ЮKassa: POST /api/payment/callback

КРИТИЧЕСКИ ВАЖНО:
  verify_yookassa_webhook() вызывается ПЕРВЫМ, до любой бизнес-логики.
  Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Формат тела webhook от ЮKassa (application/json):
  {
    "type":   "notification",
    "event":  "payment.succeeded",
    "object": {
      "id":       "<yookassa_payment_id>",
      "status":   "succeeded",
      "metadata": {"order_id": "<наш_uuid>"},
      ...
    }
  }

Верификация:
  Перепроверяем платёж через GET /v3/payments/{id} к API ЮKassa.
  Не доверяем содержимому тела webhook — только ответу первоисточника.

  ЮKassa повторяет webhook каждые 2 часа (до 12 раз) если мы не вернули 2xx.
  Поэтому при ошибке верификации (сетевой сбой, таймаут) также возвращаем 200
  и логируем — иначе получим шторм повторов. Исключение: явно невалидный запрос
  (нет payment_id, нет order_id) — 400.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from ..db import get_db
from ..routers.order import OrderStatus, transition
from ..services.payment import verify_yookassa_webhook
from ..services.referral_store import accrue_commission
from ..services.token_store import create_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/payment/callback")
async def payment_callback(request: Request):
    """
    Webhook от ЮKassa при изменении статуса платежа.

    Порядок обработки (менять нельзя):
      1. Парсинг тела
      2. Извлекаем yookassa_payment_id из object.id
      3. Верификация через GET /v3/payments/{id} к API ЮKassa (P0)
      4. Извлекаем наш order_id из metadata
      5. Фильтрация: обрабатываем только event='payment.succeeded'
      6. FSM transition: pending → paid
      7. Создание download-токена
      8. FSM transition: paid → token_issued
      9. Начисление реферальной комиссии (если есть ref_code)
    """
    # --- ШАГ 1: парсинг тела ---
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    event          = payload.get("event", "")
    payment_object = payload.get("object", {})
    yookassa_id    = payment_object.get("id", "")

    if not yookassa_id:
        raise HTTPException(status_code=400, detail="Отсутствует object.id")

    # --- ШАГ 2: верификация через API ЮKassa (ПЕРВЫМ после парсинга) ---
    payment_data = await verify_yookassa_webhook(yookassa_id)

    if payment_data is None:
        # Либо статус не succeeded, либо ошибка запроса к ЮKassa.
        # Возвращаем 200 чтобы ЮKassa не спамила повторами —
        # для реальных succeeded-платежей верификация должна пройти.
        logger.warning(
            "Webhook: верификация не прошла. yookassa_id=%s event=%s IP=%s",
            yookassa_id, event,
            request.client.host if request.client else "unknown",
        )
        return {"ok": True}

    logger.info("Webhook ЮKassa: yookassa_id=%s event=%s", yookassa_id, event)

    # --- ШАГ 3: обрабатываем только успешную оплату ---
    if event != "payment.succeeded":
        logger.info("Webhook: событие %r — игнорируем", event)
        return {"ok": True}

    # --- ШАГ 4: извлекаем наш order_id из metadata ---
    metadata = payment_data.get("metadata", {})
    order_id = metadata.get("order_id", "")

    if not order_id:
        # Платёж без нашего order_id — не должен возникать в нормальной работе
        logger.error(
            "Webhook: нет order_id в metadata. yookassa_id=%s", yookassa_id
        )
        raise HTTPException(status_code=400, detail="Отсутствует order_id в metadata")

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
