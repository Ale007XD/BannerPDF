"""
retention.py
~~~~~~~~~~~~
Удаление персональных данных оплаченных заказов по истечении срока хранения.

Стираются: текст макета (config_json, original_config_json), IP-адрес и User-Agent.
Остаются данные без текста и адресов: номер заказа, сумма, размер, цвета не нужны,
даты, номер платежа ЮKassa, версия принятых условий и время принятия.
"""

from datetime import datetime, timedelta, timezone

from ..db import get_db
from .config import ORDER_PII_RETENTION_DAYS


def purge_order_pii(retention_days: int | None = None) -> int:
    """Стирает данные заказов старше срока хранения. Возвращает число обновлённых заказов."""
    days = ORDER_PII_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE web_orders
               SET config_json = '', original_config_json = NULL,
                   accepted_ip = NULL, accepted_ua = NULL
             WHERE COALESCE(paid_at, created_at) < ?
               AND (config_json != '' OR original_config_json IS NOT NULL
                    OR accepted_ip IS NOT NULL OR accepted_ua IS NOT NULL)
            """,
            (cutoff,),
        )
        return cur.rowcount
