"""
Роутер заказов.

Содержит:
- FSM OrderStatus(str, Enum)
- transition(order_id, event) — единственная точка изменения статуса
- POST /api/order — создание заказа + подготовка данных для виджета ЮКасса
  (или прямая выдача токена при промокоде 100%)
- GET /api/payment/status/{order_id} — статус для поллинга с фронтенда
- GET /api/templates — список доступных параметров

Ответ POST /api/order (платный):
    {"order_id": "...", "amount_rub": N, "confirmation_token": "...", "payment_id": "..."}

Ответ POST /api/order (промокод 100%):
    {"order_id": "...", "amount_rub": 0, "free": true, "download_token": "..."}

ВАЖНО: прямой UPDATE status в обход transition() запрещён.
"""

import json
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from ..db import get_db
from ..services.config import BANNER_SIZES
from ..services.order_store import save_pending
from ..services.payment import create_payment
from ..services.sanitizer import sanitize_text_lines, validate_banner_config
from ..services.tg_notify import notify_new_order

logger = logging.getLogger(__name__)
router = APIRouter()

SITE_PDF_PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))

# Путь к templates.json (монтируется в /app/templates.json)
TEMPLATES_PATH = os.getenv("TEMPLATES_PATH", "/app/templates.json")

_TOKEN_TTL_MINUTES = 15

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
    "webhook_paid": (OrderStatus.PENDING, OrderStatus.PAID),
    "issue_token":  (OrderStatus.PAID,    OrderStatus.TOKEN_ISSUED),
    "ttl_expired":  (OrderStatus.PENDING, OrderStatus.EXPIRED),
    "ttl_paid":     (OrderStatus.PAID,    OrderStatus.EXPIRED),
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

        # Проверка допустимости перехода
        if current != from_status:
            raise ValueError(
                f"Недопустимый переход FSM для {order_id}: "
                f"event={event!r} требует {from_status!r}, но текущий={current!r}"
            )

        # Обновление статуса
        conn.execute(
            "UPDATE web_orders SET status = ? WHERE id = ?",
            (to_status, order_id),
        )

        # Если переход в PAID — сохраняем paid_at
        if to_status == OrderStatus.PAID:
            conn.execute(
                "UPDATE web_orders SET paid_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), order_id),
            )

    logger.info("FSM %s: %s → %s (событие=%s)", order_id, from_status, to_status, event)
    return OrderStatus(to_status)


# ---------------------------------------------------------------------------
# Pydantic-схемы
# ---------------------------------------------------------------------------
class TextLine(BaseModel):
    text:  str = Field(..., min_length=1, max_length=200)
    scale: int = Field(default=100, ge=50, le=100)


class OrderRequest(BaseModel):
    """
    Конфиг баннера для заказа.

    size_key XOR (width_mm + height_mm):
    - Либо задан size_key (шаблонный размер)
    - Либо заданы оба: width_mm и height_mm (кастомный размер)
    """
    bg_color:   str
    text_color: str
    font:       str
    text_lines: list[TextLine] = Field(..., min_length=1, max_length=6)
    size_key:   Optional[str] = None
    width_mm:   Optional[int] = Field(None, ge=100, le=3000)
    height_mm:  Optional[int] = Field(None, ge=100, le=3000)
    ref_code:   Optional[str] = None
    promo_code: Optional[str] = None

    @model_validator(mode="after")
    def check_size_xor(self):
        has_key    = self.size_key is not None
        has_custom = (self.width_mm is not None) and (self.height_mm is not None)
        if has_key == has_custom:
            raise ValueError("Укажите либо size_key, либо width_mm+height_mm (но не оба варианта)")
        return self

    @field_validator("ref_code")
    @classmethod
    def validate_ref_code(cls, v):
        if v is None:
            return v
        if not re.match(r"^[A-Z0-9]{8}$", v):
            raise ValueError("ref_code должен содержать 8 символов A-Z0-9")
        return v

    @field_validator("promo_code")
    @classmethod
    def validate_promo_code(cls, v):
        if v is None:
            return v
        cleaned = v.strip().upper()
        if not re.match(r"^[A-Z0-9]{2,20}$", cleaned):
            raise ValueError("Некорректный формат промокода")
        return cleaned


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
    Создаёт заказ и возвращает данные для оплаты.

    Платный флоу:
      1. Валидирует конфиг баннера
      2. Сохраняет заказ в web_orders (status=pending)
      3. Сохраняет config_json в pending_orders (TTL 30 мин)
      4. Создаёт платёж через API ЮКасса
      5. Возвращает {order_id, amount_rub, confirmation_token, payment_id}

    Бесплатный флоу (промокод discount=100):
      1–3. То же
      4. Пропускает ЮКасса
      5. FSM: pending → paid → token_issued
      6. Создаёт download_token (TTL 15 мин)
      7. Возвращает {order_id, amount_rub: 0, free: true, download_token}
    """
    config = {
        "bg_color":   req.bg_color,
        "text_color": req.text_color,
        "font":       req.font,
        "text_lines": [{"text": line.text, "scale": line.scale / 100} for line in req.text_lines],
    }
    if req.size_key:
        config["size_key"] = req.size_key
    else:
        config["width_mm"]  = req.width_mm
        config["height_mm"] = req.height_mm

    errors = validate_banner_config(config)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    # Санитайзим строки перед сохранением
    config["text_lines"] = sanitize_text_lines(config["text_lines"])

    # --- Валидация промокода ---
    promo_discount = 0
    applied_promo: Optional[str] = None

    if req.promo_code:
        with get_db() as conn:
            promo = conn.execute(
                "SELECT uses_left, expires_at, discount FROM promo_codes WHERE code = ?",
                (req.promo_code,),
            ).fetchone()

        if promo is None:
            raise HTTPException(status_code=422, detail="Промокод не найден")
        if promo["uses_left"] <= 0:
            raise HTTPException(status_code=422, detail="Промокод исчерпан")
        if (promo["expires_at"] is not None
                and promo["expires_at"] < datetime.now(timezone.utc).isoformat()):
            raise HTTPException(status_code=422, detail="Срок действия промокода истёк")

        promo_discount = promo["discount"]
        applied_promo  = req.promo_code

    amount_rub = max(0, int(SITE_PDF_PRICE * (100 - promo_discount) / 100))
    is_free    = amount_rub == 0

    order_id   = str(uuid.uuid4())
    config_str = json.dumps(config, ensure_ascii=False)
    size_label = req.size_key or f"{req.width_mm}×{req.height_mm} мм"
    text_lines_list = [line.text for line in req.text_lines]

    # Сохраняем заказ
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO web_orders
              (id, amount_rub, size_key, ref_code, promo_code, config_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                order_id,
                amount_rub,
                req.size_key or "custom",
                req.ref_code,
                applied_promo,
                config_str,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    # Сохраняем в pending_orders (TTL-буфер для webhook)
    save_pending(order_id, config)

    # --- Бесплатный заказ (промокод 100%) ---
    if is_free:
        # Декрементируем счётчик промокода
        with get_db() as conn:
            conn.execute(
                "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ?",
                (applied_promo,),
            )

        # FSM: pending → paid → token_issued
        transition(order_id, "webhook_paid")
        transition(order_id, "issue_token")

        # Выдаём download_token
        dl_token      = secrets.token_hex(32)
        token_expires = (
            datetime.now(timezone.utc) + timedelta(minutes=_TOKEN_TTL_MINUTES)
        ).isoformat()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO download_tokens (token, order_id, expires_at, used) VALUES (?, ?, ?, 0)",
                (dl_token, order_id, token_expires),
            )

        logger.info(
            "Бесплатный заказ %s (промокод=%s), токен выдан",
            order_id, applied_promo,
        )

        tg_message_id = await notify_new_order(
            order_id=order_id,
            amount_rub=0,
            size_label=size_label,
            lines=text_lines_list,
            font=req.font,
            promo_code=applied_promo,
        )
        if tg_message_id:
            with get_db() as conn:
                conn.execute(
                    "UPDATE web_orders SET tg_message_id = ? WHERE id = ?",
                    (tg_message_id, order_id),
                )

        return {
            "order_id":       order_id,
            "amount_rub":     0,
            "free":           True,
            "download_token": dl_token,
        }

    # --- Платный заказ ---
    try:
        payment = await create_payment(
            order_id=order_id,
            amount_rub=amount_rub,
            description=(
                "Баннер "
                + (req.size_key or f"{req.width_mm}x{req.height_mm}мм")
                + " — BannerPrint"
            ),
        )
    except Exception as e:
        logger.error("Ошибка создания платежа для заказа %s: %s", order_id, e)
        raise HTTPException(status_code=500, detail="Ошибка создания платежа. Попробуйте позже.")

    # Сохраняем yookassa_payment_id для сверки в webhook
    with get_db() as conn:
        conn.execute(
            "UPDATE web_orders SET yookassa_payment_id = ? WHERE id = ?",
            (payment["payment_id"], order_id),
        )

    logger.info(
        "Создан заказ %s, размер=%s, сумма=%d руб",
        order_id, req.size_key or f"{req.width_mm}x{req.height_mm}мм", amount_rub,
    )

    # Уведомляем администратора в Telegram (no-op если TG_NOTIFY_TOKEN не задан)
    tg_message_id = await notify_new_order(
        order_id=order_id,
        amount_rub=amount_rub,
        size_label=size_label,
        lines=text_lines_list,
        font=req.font,
    )
    if tg_message_id:
        with get_db() as conn:
            conn.execute(
                "UPDATE web_orders SET tg_message_id = ? WHERE id = ?",
                (tg_message_id, order_id),
            )

    return {
        "order_id":           order_id,
        "amount_rub":         amount_rub,
        "confirmation_token": payment["confirmation_token"],
        "payment_id":         payment["payment_id"],
    }


# ---------------------------------------------------------------------------
# GET /api/payment/status/{order_id}
# ---------------------------------------------------------------------------
@router.get("/payment/status/{order_id}")
async def payment_status(order_id: str):
    """
    Возвращает статус заказа для поллинга фронтендом.

    status:
    - pending      → оплата не получена
    - paid         → оплачен, токен генерируется
    - token_issued → токен готов, можно скачивать
    - expired      → истёк TTL

    Если status='token_issued', также возвращает download_token.
    """
    with get_db() as conn:
        order = conn.execute(
            "SELECT status FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    result = {"status": order["status"]}

    if order["status"] == OrderStatus.TOKEN_ISSUED:
        with get_db() as conn:
            token_row = conn.execute(
                "SELECT token FROM download_tokens WHERE order_id = ? AND used = 0",
                (order_id,),
            ).fetchone()

        if token_row:
            result["download_token"] = token_row["token"]

    return result
