"""
payment.py
~~~~~~~~~~
Интеграция с ЮKassa (yookassa.ru).

Схема работы (виджет ЮKassa, embedded-сценарий):
  1. POST /api/order — бэкенд создаёт платёж через POST /v3/payments,
     получает confirmation_token, возвращает его фронтенду.
  2. Фронтенд инициализирует YooMoneyCheckoutWidget с этим токеном —
     виджет всплывает поверх страницы, пользователь платит через СБП.
  3. ЮKassa присылает POST /api/payment/callback (уведомление о платеже).
  4. Бэкенд перепроверяет статус через GET /v3/payments/{yookassa_payment_id}
     — не доверяем телу webhook, только ответу API ЮKassa.

КРИТИЧЕСКИ ВАЖНО: verify_yookassa_webhook() вызывается ПЕРВЫМ
в /api/payment/callback, до любой бизнес-логики.
Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Верификация:
  - Перепроверяем payment_id через GET /v3/payments/{id} к API ЮKassa
  - Доверяем только ответу первоисточника, а не содержимому тела webhook

Переменные окружения:
  YOOKASSA_SHOP_ID    — ID магазина (числовой, из ЛК ЮKassa)
  YOOKASSA_SECRET_KEY — секретный ключ (sk_live_... или sk_test_...)
  SITE_PDF_PRICE      — цена в рублях
  SITE_BASE_URL       — базовый URL сайта
"""

import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

YOOKASSA_API_URL = "https://api.yookassa.ru/v3"

SITE_PDF_PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))
SITE_BASE_URL  = os.getenv("SITE_BASE_URL", "https://bannerbot.ru:8444")

# Название товара в чеке — одна позиция
ITEM_NAME = "Печатный баннер (PDF)"


def _get_credentials() -> tuple[str, str]:
    """Читает YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в момент вызова.
    Позволяет менять через os.environ в тестах без перезапуска."""
    shop_id    = os.getenv("YOOKASSA_SHOP_ID", "")
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")
    return shop_id, secret_key


async def create_payment(order_id: str, amount_rub: int, description: str) -> dict:
    """
    Создаёт платёж в ЮKassa и возвращает данные для виджета.

    Делает POST /v3/payments с Basic Auth (shop_id:secret_key).
    В metadata передаём order_id — ЮKassa вернёт его в webhook.

    Возвращает dict:
      yookassa_payment_id  — ID платежа на стороне ЮKassa
      confirmation_token   — токен для инициализации виджета на фронте

    Raises RuntimeError если YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы
    или API вернул ошибку.
    """
    shop_id, secret_key = _get_credentials()
    if not shop_id or not secret_key:
        raise RuntimeError("YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY должны быть заданы")

    # Ключ идемпотентности — UUID на каждый вызов.
    # Повторный вызов для того же order_id использует новый ключ,
    # т.к. мы не хотим получить старый failed-платёж.
    idempotence_key = str(uuid.uuid4())

    payload = {
        "amount": {
            "value":    f"{amount_rub:.2f}",
            "currency": "RUB",
        },
        "payment_method_data": {
            "type": "sbp",
        },
        "confirmation": {
            "type": "embedded",   # виджет встраивается на страницу
        },
        "capture":     True,
        "description": description,
        "metadata": {
            "order_id": order_id,  # наш ID — придёт в webhook
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{YOOKASSA_API_URL}/payments",
            json=payload,
            auth=(shop_id, secret_key),
            headers={"Idempotence-Key": idempotence_key},
        )

    if resp.status_code not in (200, 201):
        logger.error(
            "ЮKassa API ошибка при создании платежа: status=%d body=%s",
            resp.status_code, resp.text[:500],
        )
        raise RuntimeError(f"ЮKassa вернула ошибку {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    yookassa_payment_id = data["id"]
    confirmation_token  = data["confirmation"]["confirmation_token"]

    logger.info(
        "Создан платёж ЮKassa: yookassa_id=%s order_id=%s amount=%d руб.",
        yookassa_payment_id, order_id, amount_rub,
    )
    return {
        "yookassa_payment_id": yookassa_payment_id,
        "confirmation_token":  confirmation_token,
    }


async def verify_yookassa_webhook(yookassa_payment_id: str) -> dict | None:
    """
    Верифицирует webhook от ЮKassa: перепроверяет статус платежа через API.

    Не доверяем содержимому тела webhook — делаем GET /v3/payments/{id}
    и смотрим на status в ответе первоисточника.

    Возвращает словарь с данными платежа если статус 'succeeded', иначе None.
    Возвращает None при любой ошибке запроса — вызывающий код должен вернуть 200
    (чтобы ЮKassa не повторяла уведомление бесконечно) и залогировать.

    ВЫЗЫВАТЬ ПЕРВЫМ в /api/payment/callback до любой бизнес-логики.
    """
    shop_id, secret_key = _get_credentials()
    if not shop_id or not secret_key:
        logger.error("YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY не заданы — верификация невозможна")
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{YOOKASSA_API_URL}/payments/{yookassa_payment_id}",
                auth=(shop_id, secret_key),
            )
    except httpx.RequestError as e:
        logger.error("Ошибка запроса к ЮKassa при верификации %s: %s", yookassa_payment_id, e)
        return None

    if resp.status_code != 200:
        logger.error(
            "ЮKassa GET /payments/%s вернула %d: %s",
            yookassa_payment_id, resp.status_code, resp.text[:300],
        )
        return None

    data = resp.json()
    logger.info(
        "ЮKassa верификация: payment_id=%s status=%s",
        yookassa_payment_id, data.get("status"),
    )

    if data.get("status") != "succeeded":
        return None

    return data
