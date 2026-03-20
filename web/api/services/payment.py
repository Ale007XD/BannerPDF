"""
payment.py
~~~~~~~~~~
Интеграция с платёжным провайдером Сам.Эквайринг (selfwork.ru).

Схема работы (виджет):
  1. POST /api/order — бэкенд считает signature, возвращает {order_id, signature, amount}
  2. Фронтенд формирует <form> с этими данными и вызывает виджет smzPaymentWidget —
     браузер сам отправляет запрос к selfwork /init, пользователь оплачивает во всплывающем окне
  3. Selfwork присылает POST /api/payment/callback с JSON-телом (без заголовка подписи)
  4. Бэкенд верифицирует: SHA256(order_id + amount + SELFWORK_API_KEY) == payload.signature

КРИТИЧЕСКИ ВАЖНО: verify_selfwork_callback() вызывается ПЕРВЫМ
в /api/payment/callback, до любой бизнес-логики.
Пропуск верификации — P0 уязвимость (подделка webhook = бесплатные PDF).

Переменные окружения:
  SELFWORK_SHOP_ID  — ID магазина (для формы виджета, используется фронтендом)
  SELFWORK_API_KEY  — секретный ключ для подписи
  SITE_PDF_PRICE    — цена в рублях (конвертируется в копейки для selfwork)
  SITE_BASE_URL     — базовый URL сайта
"""

import hashlib
import hmac as _hmac
import logging
import os

logger = logging.getLogger(__name__)

SELFWORK_SHOP_ID = os.getenv("SELFWORK_SHOP_ID", "")
SITE_PDF_PRICE   = int(os.getenv("SITE_PDF_PRICE", "299"))
SITE_BASE_URL    = os.getenv("SITE_BASE_URL", "https://bannerprintbot.ru")

# Название товара в чеке — одна позиция
ITEM_NAME = "Печатный баннер (PDF)"


def _get_api_key() -> str:
    """Читает SELFWORK_API_KEY в момент вызова — позволяет менять через os.environ в тестах."""
    return os.getenv("SELFWORK_API_KEY", "")


def compute_init_signature(order_id: str, amount_kopecks: int, item_name: str,
                            quantity: int, item_amount: int) -> str:
    """
    Вычисляет подпись для инициализации виджета selfwork.

    Алгоритм: SHA256(order_id + amount + item_name + quantity + item_amount + api_key)
    Все значения конкатенируются как строки без разделителей.

    amount       — полная сумма заказа в копейках (строка)
    item_amount  — сумма за единицу позиции в копейках (строка)
    quantity     — количество (строка)

    Возвращает hex-строку нижнего регистра.
    """
    api_key = _get_api_key()
    raw = (
        str(order_id)
        + str(amount_kopecks)
        + str(item_name)
        + str(quantity)
        + str(item_amount)
        + api_key
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_selfwork_callback(order_id: str, amount_kopecks: int, signature: str) -> bool:
    """
    Верифицирует подпись входящего webhook от selfwork.

    Алгоритм: SHA256(order_id + amount + api_key)
    Подпись передаётся в поле signature JSON-тела (не в заголовке).

    Возвращает True если подпись верна, иначе False.
    ВЫЗЫВАТЬ ПЕРВЫМ перед любой обработкой тела запроса.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.error("SELFWORK_API_KEY не задан — верификация невозможна")
        return False

    raw = str(order_id) + str(amount_kopecks) + api_key
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # Константное время сравнения — защита от timing attack
    return _hmac.compare_digest(expected, signature.lower())


async def create_payment(order_id: str, amount_rub: int, description: str) -> dict:
    """
    Подготавливает данные для инициализации виджета selfwork.

    НЕ делает HTTP-запрос к selfwork — запрос выполняет браузер через виджет.
    Бэкенд только вычисляет подпись и возвращает параметры для <form>.

    Возвращает dict:
      amount_kopecks — сумма в копейках
      signature      — SHA256 для формы виджета
      item_name      — название товара в чеке
      quantity       — количество (всегда 1)
    """
    amount_kopecks = amount_rub * 100
    quantity       = 1
    item_amount    = amount_kopecks  # одна позиция = вся сумма

    signature = compute_init_signature(
        order_id=order_id,
        amount_kopecks=amount_kopecks,
        item_name=ITEM_NAME,
        quantity=quantity,
        item_amount=item_amount,
    )

    logger.info(
        "Подготовлен платёж selfwork: order_id=%s, amount=%d коп.",
        order_id, amount_kopecks,
    )
    return {
        "amount_kopecks": amount_kopecks,
        "signature":      signature,
        "item_name":      ITEM_NAME,
        "quantity":       quantity,
    }
