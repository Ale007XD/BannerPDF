"""
tg_notify.py
~~~~~~~~~~~~
Telegram-уведомления о новых заказах для администратора.

Функции:
  - notify_new_order()  — сообщение при создании заказа + inline-кнопка «Выдать PDF»
  - notify_token_issued() — подтверждение после force_token
  - handle_callback()   — обработка нажатия inline-кнопки (вызывается из /api/tg/callback)

Переменные окружения:
  TG_NOTIFY_TOKEN    — токен бота из @BotFather
  TG_ADMIN_CHAT_ID   — chat_id администратора (получить через @userinfobot)
  TG_API_BASE        — (необязательно) адрес Bot API, по умолчанию https://api.telegram.org;
                       нужен, если с сервера api.telegram.org недоступен и есть свой релей
  TG_PROXY_URL       — (необязательно) HTTP(S)-прокси для запросов к Telegram,
                       например http://user:pass@host:3128. Для socks5:// нужен
                       пакет httpx[socks] (в requirements его нет)

Если переменные не заданы — все функции работают как no-op (не падают).
"""

import hmac
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TG_NOTIFY_TOKEN  = os.getenv("TG_NOTIFY_TOKEN", "")
TG_ADMIN_CHAT_ID = os.getenv("TG_ADMIN_CHAT_ID", "")
SITE_BASE_URL    = os.getenv("SITE_BASE_URL", "https://bannerbot.ru")

TG_API_BASE      = os.getenv("TG_API_BASE", "https://api.telegram.org").rstrip("/")
TG_PROXY_URL     = os.getenv("TG_PROXY_URL", "") or None

# connect короткий: если Telegram недоступен с сервера, не держим запрос по 10 секунд
_TG_TIMEOUT = httpx.Timeout(10.0, connect=4.0)


def _enabled() -> bool:
    return bool(TG_NOTIFY_TOKEN and TG_ADMIN_CHAT_ID)


async def _tg_post(method: str, payload: dict) -> Optional[dict]:
    """Выполняет POST-запрос к Telegram Bot API. Возвращает None при ошибке."""
    if not _enabled():
        return None
    url = f"{TG_API_BASE}/bot{TG_NOTIFY_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_TG_TIMEOUT, proxy=TG_PROXY_URL) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("TG API %s: %s", method, data.get("description"))
            return data
    except Exception as e:
        # у ConnectTimeout/ConnectError текст пустой, поэтому пишем имя класса; адрес с токеном не логируем
        logger.error("TG notify error (%s): %s %s", method, type(e).__name__, e)
        return None


async def notify_new_order(
    order_id: str,
    amount_rub: int,
    size_label: str,
    lines: list[str],
    font: str,
    promo_code: Optional[str] = None,
) -> Optional[int]:
    """
    Отправляет администратору сообщение о новом заказе.
    Возвращает message_id отправленного сообщения (для последующего редактирования).
    """
    if not _enabled():
        return None

    # lines_text = "\n".join(f"  • {line}" for line in lines) if lines else "  (нет строк)"
    text = (
        f"🆕 <b>Новый заказ</b>\n\n"
        f"<b>ID:</b> <code>{order_id}</code>\n"
        f"<b>Размер:</b> {size_label}\n"
        f"<b>Шрифт:</b> {font}\n"
        # f"<b>Текст:</b>\n{lines_text}\n\n"
        f"<b>Сумма:</b> {amount_rub} ₽\n"
        + (f"<b>Промокод:</b> <code>{promo_code}</code>\n" if promo_code else "")
        + ("\n✅ PDF выдан автоматически по промокоду" if promo_code else "\nОжидание оплаты...")
    )

    data = await _tg_post("sendMessage", {
        "chat_id":    TG_ADMIN_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text":          "✅ Выдать PDF",
                    "callback_data": f"force_token:{order_id}",
                },
                {
                    "text": "🔗 Adminка",
                    "url":  f"{SITE_BASE_URL}/admin/index.html",
                },
            ]]
        },
    })

    if data and data.get("ok"):
        return data["result"]["message_id"]
    return None


async def notify_token_issued(order_id: str, chat_id: str, message_id: int) -> None:
    """
    Редактирует исходное сообщение о заказе: убирает кнопки, добавляет ✅.
    Вызывается после успешного force_token.
    """
    await _tg_post("editMessageReplyMarkup", {
        "chat_id":      chat_id,
        "message_id":   message_id,
        "reply_markup": {"inline_keyboard": []},
    })
    await _tg_post("sendMessage", {
        "chat_id":    TG_ADMIN_CHAT_ID,
        "text":       f"✅ PDF выдан для заказа <code>{order_id}</code>",
        "parse_mode": "HTML",
    })


async def notify_order_paid(order_id: str, amount_rub: int, tg_message_id: Optional[int]) -> None:
    """
    Обновляет TG-сообщение о заказе после получения оплаты через webhook ЮКасса.
    Меняет текст на «💳 Оплачено», PDF выдан автоматически.
    """
    if not _enabled() or not tg_message_id:
        return

    await _tg_post("editMessageText", {
        "chat_id":      TG_ADMIN_CHAT_ID,
        "message_id":   tg_message_id,
        "text": (
            f"💳 <b>Оплачено (автовебхук)</b>\n\n"
            f"<b>ID:</b> <code>{order_id}</code>\n"
            f"<b>Сумма:</b> {amount_rub} ₽\n\n"
            f"✅ PDF выдан автоматически"
        ),
        "parse_mode":   "HTML",
        "reply_markup": {"inline_keyboard": []},
    })


def verify_tg_webhook(secret_token: str, x_telegram_bot_api_secret_token: str) -> bool:
    """
    Проверяет заголовок X-Telegram-Bot-Api-Secret-Token.
    secret_token задаётся при setWebhook (поле secret_token).
    """
    return hmac.compare_digest(secret_token, x_telegram_bot_api_secret_token)
