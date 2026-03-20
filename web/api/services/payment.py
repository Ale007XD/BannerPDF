"""
payment.py
~~~~~~~~~~
Верификация подписи Tona webhook и вызов API оплаты.

КРИТИЧЕСКИ ВАЖНО: verify_tona_signature() вызывается ПЕРВЫМ
в /api/payment/callback, до любой бизнес-логики.
Пропуск верификации — P0 уязвимость (подделка webhook).
"""

import hashlib
import hmac
import logging
import os

import httpx

logger = logging.getLogger(__name__)

TONA_API_BASE  = "https://api.tona.ru/v1"
TONA_API_KEY   = os.getenv("TONA_API_KEY", "")
TONA_SHOP_ID   = os.getenv("TONA_SHOP_ID", "")
TONA_SECRET    = os.getenv("TONA_WEBHOOK_SECRET", "")
SITE_PDF_PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))
SITE_BASE_URL  = os.getenv("SITE_BASE_URL", "https://bannerprintbot.ru")


def verify_tona_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Проверяет HMAC-SHA256 подпись Tona webhook.

    Алгоритм: HMAC-SHA256(raw_body, TONA_WEBHOOK_SECRET)
    Ожидается в заголовке X-Tona-Signature как hex-строка.

    Возвращает True если подпись верна, иначе False.
    ВЫЗЫВАТЬ ПЕРВЫМ перед любой обработкой тела запроса.
    """
    if not TONA_SECRET:
        logger.error("TONA_WEBHOOK_SECRET не задан — верификация невозможна")
        return False

    expected = hmac.new(
        TONA_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # Константное время сравнения — защита от timing attack
    return hmac.compare_digest(expected, signature_header.lower())


async def create_payment(order_id: str, amount_rub: int, description: str) -> dict:
    """
    Создаёт платёж в Tona.
    Возвращает dict с полями: pay_url, payment_id.

    Вызывается из POST /api/order.
    """
    payload = {
        "shop_id":     TONA_SHOP_ID,
        "order_id":    order_id,
        "amount":      amount_rub * 100,   # Tona принимает копейки
        "currency":    "RUB",
        "description": description,
        "success_url": f"{SITE_BASE_URL}/?order={order_id}",
        "fail_url":    f"{SITE_BASE_URL}/",
        "webhook_url": f"{SITE_BASE_URL}/api/payment/callback",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{TONA_API_BASE}/payments",
            json=payload,
            headers={"Authorization": f"Bearer {TONA_API_KEY}"},
        )

    if resp.status_code not in (200, 201):
        logger.error(
            "Tona API ошибка %d: %s", resp.status_code, resp.text[:500]
        )
        raise RuntimeError(
            f"Tona API вернул {resp.status_code}: {resp.text[:200]}"
        )

    data = resp.json()
    logger.info("Создан платёж Tona: order_id=%s, payment_id=%s", order_id, data.get("id"))
    return {
        "pay_url":    data["pay_url"],
        "payment_id": data.get("id"),
    }
