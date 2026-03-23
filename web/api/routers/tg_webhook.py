"""
tg_webhook.py
~~~~~~~~~~~~~
Роутер для Telegram webhook.

POST /api/tg/callback — принимает updates от Telegram Bot API.

Обрабатывает только callback_query с данными вида:
  force_token:<order_id>

Безопасность:
  - Проверка X-Telegram-Bot-Api-Secret-Token (задаётся при setWebhook)
  - Ответ на callbackQuery через answerCallbackQuery
  - Только авторизованный chat_id может выдавать токены

Переменные окружения:
  TG_NOTIFY_TOKEN      — токен бота
  TG_ADMIN_CHAT_ID     — chat_id администратора
  TG_WEBHOOK_SECRET    — секрет для верификации webhook (задать при setWebhook)
  ADMIN_TOKEN          — для вызова force_token через внутренний вызов
"""

import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request

from ..routers.admin import do_force_token
from ..services.tg_notify import (
    TG_ADMIN_CHAT_ID,
    _tg_post,
    notify_token_issued,
    verify_tg_webhook,
)

logger = logging.getLogger(__name__)
router = APIRouter()

TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "")


@router.post("/tg/callback")
async def tg_callback(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    """
    Принимает Telegram webhook update.

    Обрабатывает нажатие inline-кнопки «Выдать PDF»:
      callback_data = "force_token:<order_id>"

    Только от TG_ADMIN_CHAT_ID.
    """
    # Верификация секрета webhook (если задан)
    if TG_WEBHOOK_SECRET:
        if not verify_tg_webhook(TG_WEBHOOK_SECRET, x_telegram_bot_api_secret_token):
            raise HTTPException(status_code=403, detail="Неверный секрет webhook")

    update = await request.json()
    logger.debug("TG update: %s", update)

    callback = update.get("callback_query")
    if not callback:
        # Игнорируем message и прочие update-типы
        return {"ok": True}

    callback_id   = callback["id"]
    from_chat_id  = str(callback["from"]["id"])
    data          = callback.get("data", "")
    message       = callback.get("message", {})
    message_id    = message.get("message_id")

    # Авторизация: только admin chat_id
    if from_chat_id != str(TG_ADMIN_CHAT_ID):
        await _tg_post("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text":              "⛔ Нет доступа",
            "show_alert":        True,
        })
        return {"ok": True}

    # Обработка force_token
    if data.startswith("force_token:"):
        order_id = data.split(":", 1)[1].strip()
        try:
            await do_force_token(order_id)
            # Убираем кнопки и отправляем подтверждение
            await notify_token_issued(order_id, from_chat_id, message_id)
            await _tg_post("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text":              "✅ PDF выдан",
            })
        except Exception as e:
            logger.error("force_token через TG для %s: %s", order_id, e)
            await _tg_post("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text":              f"❌ Ошибка: {e}",
                "show_alert":        True,
            })

    return {"ok": True}
