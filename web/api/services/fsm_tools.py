"""
fsm_tools.py
~~~~~~~~~~~~
Инструменты (рабочие узлы) для платежного кондуктора.
"""
import logging

from ..db import get_db
from .payment import verify_yookassa_payment
from .referral_store import accrue_commission
from .tg_notify import notify_order_paid
from .token_store import create_token

logger = logging.getLogger(__name__)

async def initiate_payment_wait(**kwargs) -> str:
    """Инструмент, мгновенно уводящий VM в статус SUSPENDED."""
    return "PENDING"

async def verify_webhook_data(**kwargs) -> bool:
    """Верификация ЮКассы. Ожидает webhook_event (payload) в контексте."""
    event = kwargs.get("webhook_event", {})
    payment_id = event.get("object", {}).get("id")
    
    if not payment_id:
        return False
        
    try:
        await verify_yookassa_payment(payment_id)
        return True
    except Exception as e:
        logger.error(f"FSM: Verification failed: {e}")
        return False

async def fsm_transition_to_paid(**kwargs) -> str:
    """Перевод статуса заказа в БД."""
    order_id = kwargs["order_id"]
    with get_db() as conn:
        conn.execute("UPDATE web_orders SET status = 'paid' WHERE id = ?", (order_id,))
    return "paid"

async def create_download_token_tool(**kwargs) -> str:
    """Создание одноразового токена. Переводит заказ в token_issued."""
    order_id = kwargs["order_id"]
    create_token(order_id)
    with get_db() as conn:
        conn.execute("UPDATE web_orders SET status = 'token_issued' WHERE id = ?", (order_id,))
    return "token_issued"

async def pay_referral_tool(**kwargs) -> str:
    """Начисление реферальной комиссии."""
    order_id = kwargs["order_id"]
    with get_db() as conn:
        row = conn.execute("SELECT ref_code, amount_rub FROM web_orders WHERE id = ?", (order_id,)).fetchone()
        
    if row and row["ref_code"]:
        accrue_commission(row["ref_code"], order_id, row["amount_rub"])
    return "done"

async def send_tg_notification_tool(**kwargs) -> str:
    """Обновление сообщения в Telegram."""
    order_id = kwargs["order_id"]
    with get_db() as conn:
        row = conn.execute("SELECT amount_rub, tg_message_id FROM web_orders WHERE id = ?", (order_id,)).fetchone()
        
    if row and row["tg_message_id"]:
        await notify_order_paid(order_id, row["amount_rub"], row["tg_message_id"])
    return "done"

async def log_fraud_attempt(**kwargs) -> str:
    """Вызывается если подпись/валидация провалилась."""
    logger.warning(f"FRAUD ATTEMPT in FSM for order {kwargs.get('order_id')}")
    return "logged"