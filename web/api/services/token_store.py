"""
token_store.py
~~~~~~~~~~~~~~
Хранилище download-токенов в SQLite.
Таблица: download_tokens (token PK, order_id, expires_at, used BOOL, downloads INT)

Токен действует TTL_SECONDS и допускает до MAX_DOWNLOADS успешных выдач PDF.
Выдача фиксируется только ПОСЛЕ успешного рендера (record_download), поэтому
сбой генерации или обрыв соединения не «сжигает» оплаченную ссылку.

Не использует in-memory — переживает рестарты и работает корректно
при единственном uvicorn-воркере.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from ..db import get_db

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 900  # 15 минут
MAX_DOWNLOADS = 3        # успешных выдач PDF на один токен


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


def peek_token(token: str) -> str | None:
    """
    Проверяет токен БЕЗ изменения состояния.

    Возвращает order_id, если токен существует, не истёк и лимит выдач
    не исчерпан, иначе None.
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

    if row["used"]:
        logger.warning("Лимит выдач токена исчерпан: %s…", token[:8])
        return None

    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if _now() > expires_at:
        logger.warning("Токен истёк: %s…", token[:8])
        return None

    return row["order_id"]


def record_download(token: str) -> bool:
    """
    Атомарно фиксирует успешную выдачу PDF по токену.

    Вызывается после того, как файл уже отрендерен и готов к отправке.
    На MAX_DOWNLOADS-й выдаче токен помечается использованным.

    Возвращает False, если лимит уже исчерпан (например, параллельными
    запросами, пока шёл рендер) — тогда файл отдавать нельзя.
    """
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE download_tokens
               SET downloads = downloads + 1,
                   used      = (downloads + 1 >= ?)
             WHERE token = ? AND used = FALSE
            """,
            (MAX_DOWNLOADS, token),
        )
        recorded = cursor.rowcount == 1

    if recorded:
        logger.info("Выдача PDF зафиксирована: токен %s…", token[:8])
    else:
        logger.warning("Выдача не зафиксирована (лимит исчерпан): %s…", token[:8])
    return recorded


def consume_token(token: str) -> str | None:
    """
    УСТАРЕЛО: гасит токен сразу, до рендера. download.py больше её не
    использует (см. peek_token + record_download). Оставлена для
    обратной совместимости.

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
