"""
payment.py
~~~~~~~~~~
Интеграция с платёжным провайдером ЮКassa (yookassa.ru).

Схема работы (embedded виджет):
  1. POST /api/order — бэкенд создаёт платёж через POST /v3/payments к API ЮКassa,
     получает confirmation_token, возвращает {order_id, confirmation_token, amount_rub}
  2. Фронтенд инициализирует виджет YooMoneyCheckoutWidget({confirmation_token}),
     пользователь оплачивает во всплывающем окне
  3. ЮКassa присылает POST /api/payment/callback с JSON-телом (без HMAC-подписи)
  4. Бэкенд верифицирует: GET /v3/payments/{payment_id} к API ЮКassa с базовой аутентификацией,
     проверяет status='succeeded' и metadata.order_id

КРИТИЧЕСКИ ВАЖНО: verify_yookassa_payment() вызывается ПЕРВЫМ
в /api/payment/callback, до любой бизнес-логики.
Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Переменные окружения:
  YOOKASSA_SHOP_ID   — ID магазина (для базовой аутентификации, формат: 123456)
  YOOKASSA_SECRET_KEY — секретный ключ (для тестовой среды начинается с test_)
  SITE_PDF_PRICE     — цена в рублях
  SITE_BASE_URL      — базовый URL сайта
"""

import base64
import logging
import os
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
SITE_PDF_PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://bannerbot.ru:8444")

# Название товара в чеке
ITEM_NAME = "Печатный баннер (PDF)"

# Базовый URL API ЮКassa
YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


def _get_auth_header() -> str:
    """
    Формирует заголовок Authorization для базовой аутентификации ЮКassa.
    Формат: Basic base64(SHOP_ID:SECRET_KEY)
    """
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise ValueError("YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY не заданы")

    credentials = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


async def create_payment(order_id: str, amount_rub: int, description: str) -> dict:
    """
    Создаёт платёж через API ЮКassa и возвращает confirmation_token для виджета.

    POST /v3/payments
    Body:
      {
        "amount": {"value": "299.00", "currency": "RUB"},
        "confirmation": {"type": "embedded", "return_url": "..."},
        "capture": true,
        "description": "...",
        "metadata": {"order_id": "..."}
      }

    Возвращает dict:
      confirmation_token — токен для инициализации YooMoneyCheckoutWidget
      payment_id — ID платежа в ЮКassa (для верификации webhook)
    """
    url = f"{YOOKASSA_API_BASE}/payments"
    headers = {
        "Authorization": _get_auth_header(),
        "Idempotence-Key": order_id,  # защита от дублей
        "Content-Type": "application/json",
    }
    payload = {
        "amount": {
            "value": f"{amount_rub}.00",
            "currency": "RUB",
        },
        "confirmation": {
            "type": "embedded",
            "return_url": f"{SITE_BASE_URL}",  # URL возврата после оплаты (опционально)
        },
        "capture": True,  # автоматическое списание (не двухстадийное)
        "description": description or ITEM_NAME,
        "metadata": {
            "order_id": order_id,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error("ЮКassa API ошибка: %s %s", e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка создания платежа: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        logger.error("ЮКassa недоступна: %s", e)
        raise HTTPException(status_code=503, detail="Платёжный провайдер недоступен")

    payment_id = data.get("id")
    confirmation = data.get("confirmation", {})
    confirmation_token = confirmation.get("confirmation_token")

    if not confirmation_token:
        logger.error("ЮКassa не вернула confirmation_token: %s", data)
        raise HTTPException(status_code=502, detail="Некорректный ответ от ЮКassa")

    logger.info(
        "Создан платёж ЮКassa: order_id=%s payment_id=%s amount=%d руб.",
        order_id, payment_id, amount_rub,
    )
    return {
        "confirmation_token": confirmation_token,
        "payment_id": payment_id,
    }


async def verify_yookassa_payment(payment_id: str) -> dict[str, Any]:
    """
    Верифицирует платёж через GET /v3/payments/{payment_id} к API ЮКassa.

    ЮКassa не использует HMAC-подпись в webhook — единственный надёжный способ верификации.

    Возвращает dict с данными платежа из ЮКassa (status, metadata, amount).
    Бросает HTTPException(403) если:
      - платёж не найден в ЮКassa
      - status != 'succeeded'
      - paid != true

    ВЫЗЫВАТЬ ПЕРВЫМ перед любой обработкой webhook.
    """
    url = f"{YOOKASSA_API_BASE}/payments/{payment_id}"
    headers = {
        "Authorization": _get_auth_header(),
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.error("Платёж не найден в ЮКassa: payment_id=%s", payment_id)
            raise HTTPException(status_code=403, detail="Платёж не найден")
        logger.error("ЮКassa API ошибка при верификации: %s %s", e.response.status_code, e.response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка верификации платежа: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        logger.error("ЮКassa недоступна при верификации: %s", e)
        raise HTTPException(status_code=503, detail="Платёжный провайдер недоступен")

    status = data.get("status")
    paid = data.get("paid")

    if status != "succeeded" or not paid:
        logger.error(
            "Платёж не успешен: payment_id=%s status=%s paid=%s",
            payment_id, status, paid,
        )
        raise HTTPException(status_code=403, detail="Платёж не успешен")

    logger.info("Верификация ЮКassa успешна: payment_id=%s", payment_id)
    return data
