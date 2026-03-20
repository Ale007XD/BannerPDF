"""
token_store.py
~~~~~~~~~~~~~~
Хранилище одноразовых download-токенов в SQLite.
Таблица: download_tokens (token PK, order_id, expires_at, used BOOL)

Не использует in-memory — переживает рестарты и работает корректно
при единственном uvicorn-воркере.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from ..db import get_db

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 900  # 15 минут


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_token(order_id: str, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """
    Создаёт и сохраняет одноразовый download-токен.
    Возвращает hex-строку токена (64 символа).
    """
    token = secrets.token_hex(32)
    expires_at = _now() + timedelta(seconds=ttl_seconds)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO download_tokens (token, order_id, expires_at, used)
            VALUES (?, ?, ?, FALSE)
            """,
            (token, order_id, expires_at.isoformat()),
        )

    logger.info("Создан download-токен для заказа %s, TTL %ds", order_id, ttl_seconds)
    return token


def consume_token(token: str) -> str | None:
    """
    Проверяет токен и помечает его использованным.

    Возвращает order_id если токен валидный и не истёк,
    иначе None.

    Идемпотентен только для первого вызова — повторный вернёт None.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT order_id, expires_at, used
            FROM download_tokens
            WHERE token = ?
            """,
            (token,),
        ).fetchone()

        if row is None:
            logger.warning("Токен не найден: %s…", token[:8])
            return None

        order_id, expires_at_str, used = row["order_id"], row["expires_at"], row["used"]

        if used:
            logger.warning("Токен уже использован: %s…", token[:8])
            return None

        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if _now() > expires_at:
            logger.warning("Токен истёк: %s…", token[:8])
            return None

        # Помечаем использованным (одноразовый)
        conn.execute(
            "UPDATE download_tokens SET used = TRUE WHERE token = ?",
            (token,),
        )

    logger.info("Токен использован для заказа %s", order_id)
    return order_id


def cleanup_expired() -> int:
    """
    Удаляет просроченные и использованные токены.
    Вызывается из фонового cleanup в lifespan.
    Возвращает количество удалённых записей.
    """
    with get_db() as conn:
        cursor = conn.execute(
            """
            DELETE FROM download_tokens
            WHERE used = TRUE
               OR expires_at < ?
            """,
            (_now().isoformat(),),
        )
    count = cursor.rowcount
    if count:
        logger.info("Cleanup download_tokens: удалено %d записей", count)
    return count
