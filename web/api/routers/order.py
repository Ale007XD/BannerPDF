"""
order.py
~~~~~~~~
Роутер заказов.

Содержит:
  - FSM OrderStatus(str, Enum)
  - transition(order_id, event) — единственная точка изменения статуса
  - POST /api/order — создание заказа + подготовка данных для виджета selfwork
  - GET  /api/payment/status/{order_id} — статус для поллинга с фронтенда
  - GET  /api/templates — список доступных параметров

ВАЖНО: прямой UPDATE status в обход transition() запрещён.

Ответ POST /api/order:
  {
    "order_id":      "<uuid>",
    "amount_kopecks": <int>,   -- сумма в копейках для формы виджета
    "signature":     "<hex>",  -- SHA256 для selfwork init
    "item_name":     "<str>",  -- название товара в чеке
    "quantity":      1
  }
"""

import json
import logging
import os
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..db import get_db
from ..services.config import BANNER_SIZES
from ..services.order_store import save_pending
from ..services.payment import create_payment
from ..services.sanitizer import sanitize_text_lines, validate_banner_config

logger = logging.getLogger(__name__)
router = APIRouter()

SITE_PDF_PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))

# Путь к templates.json (монтируется в /app/templates.json)
TEMPLATES_PATH = os.getenv("TEMPLATES_PATH", "/app/templates.json")


# ---------------------------------------------------------------------------
# FSM заказов
# ---------------------------------------------------------------------------
class OrderStatus(str, Enum):
    PENDING      = "pending"
    PAID         = "paid"
    TOKEN_ISSUED = "token_issued"
    EXPIRED      = "expired"


# Допустимые переходы: event → (from_status, to_status)
_TRANSITIONS: dict[str, tuple[str, str]] = {
    "webhook_paid": (OrderStatus.PENDING,      OrderStatus.PAID),
    "issue_token":  (OrderStatus.PAID,         OrderStatus.TOKEN_ISSUED),
    "ttl_expired":  (OrderStatus.PENDING,      OrderStatus.EXPIRED),
    "ttl_paid":     (OrderStatus.PAID,         OrderStatus.EXPIRED),
}


def transition(order_id: str, event: str) -> OrderStatus:
    """
    Выполняет переход статуса заказа по событию.

    Получает текущий статус из БД, проверяет допустимость перехода,
    обновляет статус атомарно.

    Raises ValueError если переход недопустим или заказ не найден.
    Идемпотентен: если заказ уже в целевом статусе — возвращает его без ошибки.
    """
    if event not in _TRANSITIONS:
        raise ValueError(f"Неизвестное событие FSM: {event!r}")

    from_status, to_status = _TRANSITIONS[event]

    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

        if row is None:
            raise ValueError(f"Заказ {order_id} не найден")

        current = row["status"]

        # Идемпотентность: уже в целевом статусе
        if current == to_status:
            logger.info("FSM %s: уже в статусе %s (идемпотент)", order_id, to_status)
            return OrderStatus(to_status)

        if current != from_status:
            raise ValueError(
                f"FSM: недопустимый переход для заказа {order_id}: "
                f"{current} --[{event}]--> {to_status} "
                f"(ожидается from={from_status})"
            )

        extra_fields = ""
        extra_values: list = []
        if event == "webhook_paid":
            extra_fields = ", paid_at = ?"
            extra_values = [datetime.now(timezone.utc).isoformat()]

        conn.execute(
            f"UPDATE web_orders SET status = ?{extra_fields} WHERE id = ?",
            [to_status] + extra_values + [order_id],
        )

    logger.info("FSM %s: %s --[%s]--> %s", order_id, from_status, event, to_status)
    return OrderStatus(to_status)


# ---------------------------------------------------------------------------
# Pydantic-модели
# ---------------------------------------------------------------------------
class TextLine(BaseModel):
    text:  str   = Field(..., max_length=120)
    scale: float = Field(default=1.0, ge=0.3, le=1.5)


class OrderRequest(BaseModel):
    size_key:   str            = Field(...)
    bg_color:   str            = Field(...)
    text_color: str            = Field(...)
    font:       str            = Field(...)
    text_lines: list[TextLine] = Field(..., min_length=1, max_length=6)
    ref_code:   str | None     = Field(default=None, min_length=8, max_length=8)

    @field_validator("ref_code")
    @classmethod
    def validate_ref_code(cls, v):
        if v is None:
            return v
        import re
        if not re.match(r"^[A-Z0-9]{8}$", v):
            raise ValueError("ref_code должен содержать 8 символов A-Z0-9")
        return v


# ---------------------------------------------------------------------------
# GET /api/templates
# ---------------------------------------------------------------------------
@router.get("/templates")
async def get_templates():
    """Возвращает список доступных размеров, шрифтов и цветов."""
    try:
        with open(TEMPLATES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # Fallback: генерируем из config
        from ..services.config import COLORS, FONTS
        return {
            "sizes": [
                {"key": k, "width_mm": v[0], "height_mm": v[1]}
                for k, v in BANNER_SIZES.items()
            ],
            "fonts": list(FONTS.keys()),
            "colors": [
                {"name": name, "rgb": list(data["rgb"]), "emoji": data["emoji"]}
                for name, data in COLORS.items()
            ],
            "max_lines": 6,
            "safe_zone_mm": 30,
        }


# ---------------------------------------------------------------------------
# POST /api/order
# ---------------------------------------------------------------------------
@router.post("/order")
async def create_order(req: OrderRequest):
    """
    Создаёт заказ и возвращает данные для инициализации виджета selfwork.

    1. Валидирует конфиг баннера
    2. Сохраняет заказ в web_orders (status=pending)
    3. Сохраняет config_json в pending_orders (TTL 30 мин)
    4. Вычисляет подпись selfwork
    5. Возвращает {order_id, amount_kopecks, signature, item_name, quantity}
    """
    import uuid

    config = {
        "size_key":   req.size_key,
        "bg_color":   req.bg_color,
        "text_color": req.text_color,
        "font":       req.font,
        "text_lines": [{"text": line.text, "scale": line.scale} for line in req.text_lines],
    }

    errors = validate_banner_config(config)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    # Санитайзим строки перед сохранением
    config["text_lines"] = sanitize_text_lines(config["text_lines"])

    order_id   = str(uuid.uuid4())
    amount_rub = SITE_PDF_PRICE
    config_str = json.dumps(config, ensure_ascii=False)

    # Сохраняем заказ
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO web_orders
                (id, amount_rub, size_key, ref_code, config_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                order_id,
                amount_rub,
                req.size_key,
                req.ref_code,
                config_str,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    # Сохраняем в pending_orders (TTL-буфер для webhook)
    save_pending(order_id, config)

    # Подготавливаем данные для виджета (без HTTP-запроса к selfwork)
    try:
        payment = await create_payment(
            order_id=order_id,
            amount_rub=amount_rub,
            description=f"Баннер {req.size_key} — BannerPrint",
        )
    except Exception as e:
        logger.error("Ошибка подготовки платежа для заказа %s: %s", order_id, e)
        raise HTTPException(status_code=500, detail="Ошибка подготовки платежа. Попробуйте позже.")

    logger.info("Создан заказ %s, размер=%s, сумма=%d руб", order_id, req.size_key, amount_rub)
    return {
        "order_id":       order_id,
        "amount_kopecks": payment["amount_kopecks"],
        "signature":      payment["signature"],
        "item_name":      payment["item_name"],
        "quantity":       payment["quantity"],
    }


# ---------------------------------------------------------------------------
# GET /api/payment/status/{order_id}
# ---------------------------------------------------------------------------
@router.get("/payment/status/{order_id}")
async def get_payment_status(order_id: str):
    """
    Статус заказа для поллинга с фронтенда.
    Если статус token_issued — возвращает download_token.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, amount_rub, size_key, created_at FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    result = {
        "order_id":   order_id,
        "status":     row["status"],
        "amount_rub": row["amount_rub"],
        "size_key":   row["size_key"],
    }

    if row["status"] == OrderStatus.TOKEN_ISSUED:
        # Отдаём актуальный (последний неиспользованный) токен
        with get_db() as conn:
            token_row = conn.execute(
                """
                SELECT token FROM download_tokens
                WHERE order_id = ? AND used = FALSE AND expires_at > ?
                ORDER BY expires_at DESC LIMIT 1
                """,
                (order_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()

        if token_row:
            result["download_token"] = token_row["token"]

    return result
