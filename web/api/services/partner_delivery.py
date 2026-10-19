"""
partner_delivery.py
~~~~~~~~~~~~~~~~~~~
Отправка готового макета на почту типографии-партнёра.

Вызывается из GET /api/download/{token} после успешной выдачи PDF клиенту, поэтому
типография получает тот же файл, который скачал клиент, без второго рендера.
Из админки (повтор) вызывается с force=True.

Сбой отправки никогда не влияет на клиента: он уже получил файл. Сбой пишется в лог и в
partner_deliveries.error; следующее скачивание того же заказа повторяет отправку, а
администратор может повторить её вручную (POST /api/admin/partners/deliveries/{order_id}/resend).
"""

import json
import logging

from . import mailer, partner_naming, partner_store

logger = logging.getLogger(__name__)

_MAX_LINE_LEN = 200


def _size_label(size_key: str, config: dict) -> str:
    """«2×1 м» для типового размера, «2400×900 мм» для своего."""
    if size_key and size_key != "custom":
        return f"{size_key.replace('x', '×')} м"
    w, h = config.get("width_mm"), config.get("height_mm")
    return f"{w}×{h} мм" if w and h else "свой размер"


def _text_lines(config: dict) -> list[str]:
    lines = []
    for item in config.get("text_lines", []):
        text = str(item.get("text", "")) if isinstance(item, dict) else ""
        if text:
            lines.append(text[:_MAX_LINE_LEN])
    return lines


def _compose(partner: dict, order_id: str, size_key: str, config: dict, revised: bool) -> tuple[str, str]:
    number = partner_naming.order_number(order_id)
    size = _size_label(size_key, config)
    lines = _text_lines(config)
    text_block = "\n".join(f"  {i}. {line}" for i, line in enumerate(lines, 1)) or "  —"

    kind = "Исправленный макет" if revised else "Новый макет"
    subject = f"{kind} баннера {size}: заказ {number}"

    intro = (
        "Клиент исправил текст на макете. Это обновлённая версия, она заменяет предыдущую."
        if revised
        else f"Клиент собрал и оплатил макет баннера в конструкторе на странице «{partner['name']}»."
    )
    body = (
        "Здравствуйте!\n\n"
        f"{intro}\n\n"
        f"Заказ: {number}\n"
        f"Размер: {size}\n"
        "Текст на баннере:\n"
        f"{text_block}\n\n"
        "Во вложении печатный PDF: чистый CMYK, шрифты в кривых, масштаб 1:1. "
        "Такой же файл получил клиент. В его названии и в окне после оплаты стоит этот номер заказа: "
        "если клиент позвонит, он назовёт его, а письмо найдётся по номеру в теме.\n\n"
        "Это автоматическое письмо сервиса BannerBot (bannerbot.ru), отвечать на него не нужно.\n"
    )
    return subject, body


async def deliver_order_pdf(
    order_id: str,
    partner_code: str,
    size_key: str,
    config_json: str,
    pdf_bytes: bytes,
    force: bool = False,
) -> bool:
    """
    Отправляет PDF на почту партнёра. True — письмо ушло.

    False — отправлять не нужно или не удалось: почта не настроена, партнёр отключён или удалён,
    эта версия макета уже отправлена, либо сбой SMTP. Исключения наружу не выходят.
    """
    try:
        if not mailer.is_configured():
            logger.warning("Макет заказа %s не отправлен партнёру %s: почта не настроена", order_id, partner_code)
            return False

        partner = partner_store.get_partner(partner_code)
        if partner is None or not partner["active"]:
            return False

        try:
            config = json.loads(config_json)
        except Exception:
            config = {}

        cfg_hash = partner_store.config_hash(config_json)
        revised = partner_store.has_other_version(order_id, cfg_hash)
        if not partner_store.claim_delivery(order_id, cfg_hash, partner["code"], force=force):
            return False

        subject, body = _compose(partner, order_id, size_key, config, revised)
        msg = mailer.build_message(
            to=partner["email"],
            subject=subject,
            body=body,
            attachment_name=partner_naming.mail_attachment_name(order_id, _size_label(size_key, config)),
            attachment_bytes=pdf_bytes,
        )
        try:
            await mailer.send_message(msg)
        except mailer.MailError as e:
            partner_store.mark_delivery_failed(order_id, cfg_hash, str(e))
            logger.error("Макет заказа %s (№ %s) не отправлен партнёру %s: %s", order_id, partner_naming.order_number(order_id), partner["code"], e)
            return False

        partner_store.mark_delivery_sent(order_id, cfg_hash)
        logger.info("Макет заказа %s (№ %s) отправлен партнёру %s", order_id, partner_naming.order_number(order_id), partner["code"])
        return True
    except Exception:
        # Фоновая задача: что бы ни случилось, клиента это не касается
        logger.exception("Сбой отправки макета заказа %s партнёру %s", order_id, partner_code)
        return False
