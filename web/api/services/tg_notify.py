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

Если переменные не заданы — все функции работают как no-op (не падают).
"""

import hashlib
import hmac
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TG_NOTIFY_TOKEN  = os.getenv("TG_NOTIFY_TOKEN", "")
TG_ADMIN_CHAT_ID = os.getenv("TG_ADMIN_CHAT_ID", "")
SITE_BASE_URL    = os.getenv("SITE_BASE_URL", "https://bannerbot.ru:8444")

_TG_API = "https://api.telegram.org/bot{token}/{method}"


def _enabled() -> bool:
    return bool(TG_NOTIFY_TOKEN and TG_ADMIN_CHAT_ID)


async def _tg_post(method: str, payload: dict) -> Optional[dict]:
    """Выполняет POST-запрос к Telegram Bot API. Возвращает None при ошибке."""
    if not _enabled():
        return None
    url = _TG_API.format(token=TG_NOTIFY_TOKEN, method=method)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("TG API %s: %s", method, data.get("description"))
            return data
    except Exception as e:
        logger.error("TG notify error (%s): %s", method, e)
        return None


async def notify_new_order(
    order_id: str,
    amount_rub: int,
    size_label: str,
    lines: list[str],
    font: str,
) -> None:
    """
    Отправляет администратору сообщение о новом заказе.

    Содержит детали заказа и inline-кнопку «✅ Выдать PDF» для force_token.
    """
    if not _enabled():
        return

    lines_text = "\n".join(f"  • {line}" for line in lines) if lines else "  (нет строк)"
    text = (
        f"🆕 <b>Новый заказ</b>\n\n"
        f"<b>ID:</b> <code>{order_id}</code>\n"
        f"<b>Размер:</b> {size_label}\n"
        f"<b>Шрифт:</b> {font}\n"
        # f"<b>Текст:</b>\n{lines_text}\n\n"
        f"<b>Сумма:</b> {amount_rub} ₽\n\n"
        f"Ожидание оплаты..."
    )

    await _tg_post("sendMessage", {
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


def verify_tg_webhook(secret_token: str, x_telegram_bot_api_secret_token: str) -> bool:
    """
    Проверяет заголовок X-Telegram-Bot-Api-Secret-Token.
    secret_token задаётся при setWebhook (поле secret_token).
    """
    return hmac.compare_digest(secret_token, x_telegram_bot_api_secret_token)
