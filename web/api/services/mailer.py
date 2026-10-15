"""
mailer.py
~~~~~~~~~
Отправка писем с вложением по SMTP (стандартная библиотека, новых зависимостей нет).

Переменные окружения (читаются при каждой отправке):
  SMTP_HOST       — сервер; пусто = почта отключена (is_configured() == False)
  SMTP_PORT       — по умолчанию 465 (ssl); для starttls обычно 587
  SMTP_SECURITY   — ssl (по умолчанию) | starttls | none
  SMTP_USER, SMTP_PASSWORD — логин и пароль; без SMTP_USER вход не выполняется
  SMTP_FROM       — адрес отправителя, например «BannerBot <noreply@bannerbot.ru>»;
                    домен должен иметь SPF и DKIM, иначе письма уйдут в спам
  PARTNER_MAIL_BCC — необязательно: скрытая копия каждого письма партнёру (контроль для владельца)

Отправка блокирующая, поэтому send_message() выполняет её в потоке и не держит event loop.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 20


class MailError(Exception):
    """Письмо не отправлено; текст — причина для лога и админки."""


def is_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    attachment_name: str,
    attachment_bytes: bytes,
) -> EmailMessage:
    """Текстовое письмо с одним PDF-вложением."""
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM", "").strip()
    msg["To"] = to
    msg["Subject"] = subject
    bcc = os.getenv("PARTNER_MAIL_BCC", "").strip()
    if bcc:
        # smtplib.send_message берёт Bcc в получатели и убирает заголовок из отправляемого письма
        msg["Bcc"] = bcc
    msg.set_content(body)
    msg.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype="pdf",
        filename=attachment_name,
    )
    return msg


def _send_sync(msg: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    port = int(os.getenv("SMTP_PORT", "465" if security == "ssl" else "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    context = ssl.create_default_context()

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)

    with server:
        if security == "starttls":
            server.starttls(context=context)
        if user:
            server.login(user, password)
        server.send_message(msg)


async def send_message(msg: EmailMessage) -> None:
    """Отправляет письмо. MailError при любом сбое (сеть, авторизация, отказ получателя)."""
    if not is_configured():
        raise MailError("почта не настроена (SMTP_HOST / SMTP_FROM)")
    try:
        await asyncio.to_thread(_send_sync, msg)
    except Exception as e:  # smtplib.SMTPException, OSError, ssl.SSLError, ValueError из int(port)
        raise MailError(f"{type(e).__name__}: {e}") from e
