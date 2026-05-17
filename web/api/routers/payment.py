"""
payment.py
~~~~~~~~~~
Вебхуки от ЮКассы. 
Вся бизнес-логика (FSM, верификация, выдача токенов) вынесена в payment_conductor.
"""
import logging
from fastapi import APIRouter, HTTPException, Request

from ..services.fsm_conductor import conductor_vm, payment_conductor, fsm_repo
from nano_vm.vm import TraceStatus

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/payment/callback")
async def payment_callback(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON")

    # ЮКасса передает order_id в metadata
    order_id = payload.get("object", {}).get("metadata", {}).get("order_id")
    if not order_id:
        # Для других событий возвращаем 200, чтобы ЮКасса не спамила
        return {"ok": True}

    # 1. Ищем уснувший кондуктор
    trace_id = await fsm_repo.get_trace_id_by_order(order_id)
    if not trace_id:
        logger.warning(f"Webhook received, but no FSM trace found for order: {order_id}")
        return {"ok": True} # Идемпотентность, возможно заказ уже обработан

    logger.info(f"Resuming FSM for order {order_id} (trace_id: {trace_id})")

    # 2. Будим кондуктор и передаем ему тело вебхука
    trace = await conductor_vm.resume_with_program(
        program=payment_conductor,
        trace_id=trace_id,
        webhook_event=payload  # Это станет доступно в $webhook_event
    )

    if trace.status == TraceStatus.SUCCESS:
        logger.info(f"FSM successfully completed for order {order_id}")
        return {"ok": True}
    else:
        logger.error(f"FSM halted with status {trace.status}. Error: {trace.error}")
        # Возвращаем 200, чтобы ЮКасса не слала ретраи. Ошибка останется в Trace.
        return {"ok": True}
