"""
order_store.py
~~~~~~~~~~~~~~
Хранилище pending_orders в SQLite.
Таблица: pending_orders (order_id PK, config_json, expires_at)

Заменяет in-memory TTLStore — переживает рестарты.
TTL 30 минут — после истечения config_json больше не нужен
(download.py читает из web_orders.config_json напрямую как fallback).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from ..db import get_db

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 1800  # 30 минут


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save_pending(order_id: str, config: dict, ttl_seconds: int = PENDING_TTL_SECONDS) -> None:
    """
    Сохраняет конфиг баннера в pending_orders до получения webhook.
    При конфликте (повторный заказ с тем же order_id) обновляет запись.
    """
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    config_json = json.dumps(config, ensure_ascii=False)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO pending_orders (order_id, config_json, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                config_json = excluded.config_json,
                expires_at  = excluded.expires_at
            """,
            (order_id, config_json, expires_at.isoformat()),
        )

    logger.info("Сохранён pending заказ %s, TTL %ds", order_id, ttl_seconds)


def get_pending(order_id: str) -> dict | None:
    """
    Возвращает config dict для order_id если запись существует и не истекла.
    Иначе None.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_json, expires_at FROM pending_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        return None

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if _now() > expires_at:
        logger.warning("pending_orders: запись для %s истекла", order_id)
        return None

    return json.loads(row["config_json"])


def delete_pending(order_id: str) -> None:
    """Удаляет запись после успешной обработки webhook."""
    with get_db() as conn:
        conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))


def cleanup_expired() -> int:
    """
    Удаляет просроченные pending_orders.
    Вызывается из фонового cleanup в lifespan.
    """
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM pending_orders WHERE expires_at < ?",
            (_now().isoformat(),),
        )
    count = cursor.rowcount
    if count:
        logger.info("Cleanup pending_orders: удалено %d записей", count)
    return count
