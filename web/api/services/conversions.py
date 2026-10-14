"""
conversions.py
~~~~~~~~~~~~~~
Серверные события воронки: оплата заказа и выдача файла.

В отличие от событий из браузера они не зависят от блокировщиков, перезагрузки страницы и
редиректа на оплату (3DS, СБП). Метка входа («entry»: страница сайта или yandex / google / direct)
приходит с заказом и хранится в его конфиге; это не персональные данные. Сбой счётчика
никогда не мешает оплате или скачиванию.
"""

import json
import logging

from . import stat_store

logger = logging.getLogger(__name__)


def record_order_event(event: str, config_json: str | None) -> None:
    """Считает серверное событие по заказу, метка входа берётся из его конфига."""
    try:
        entry = ""
        if config_json:
            entry = stat_store.clean_label(json.loads(config_json).get("entry", ""))
        stat_store.record(event, "/", "server", entry)
    except Exception:
        logger.exception("Не удалось посчитать событие %s", event)
