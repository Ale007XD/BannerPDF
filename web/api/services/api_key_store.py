"""
api_key_store.py
~~~~~~~~~~~~~~~~
Хранилище корпоративных API-ключей.
Ключ хранится как sha256(key) — сам ключ в БД не записывается.
Формат ключа: bp_live_<32 chars base64url>
"""

import hashlib
import logging
import secrets
from base64 import urlsafe_b64encode
from datetime import datetime, timezone

from ..db import get_db

logger = logging.getLogger(__name__)


def _hash_key(key: str) -> str:
    """sha256(key) как hex-строка."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """Генерирует новый API-ключ формата bp_live_<32 chars base64url>."""
    raw = secrets.token_bytes(24)  # 24 bytes → 32 chars base64url
    suffix = urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:32]
    return f"bp_live_{suffix}"


def create_api_key(plan_id: str, label: str, email: str) -> str:
    """
    Создаёт новый API-ключ, сохраняет хэш в БД.
    Возвращает сам ключ (показывается пользователю только один раз).
    """
    key = generate_key()
    key_hash = _hash_key(key)
    key_prefix = key[:12]

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO api_keys
                (key_hash, key_prefix, plan_id, label, email,
                 active, created_at, pdf_used, period_start)
            VALUES (?, ?, ?, ?, ?, TRUE, ?, 0, ?)
            """,
            (
                key_hash, key_prefix, plan_id, label, email,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    logger.info("Создан API-ключ %s… план=%s email=%s", key_prefix, plan_id, email)
    return key


def verify_key(key: str) -> dict | None:
    """
    Проверяет API-ключ.
    Возвращает строку из api_keys JOIN api_plans или None.
    """
    key_hash = _hash_key(key)
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT k.*, p.name as plan_name, p.pdf_limit, p.rpm_limit
            FROM api_keys k
            JOIN api_plans p ON p.id = k.plan_id
            WHERE k.key_hash = ? AND k.active = TRUE
            """,
            (key_hash,),
        ).fetchone()

    if row is None:
        return None

    # Проверяем срок действия если задан
    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            logger.warning("API-ключ %s… истёк", row["key_prefix"])
            return None

    return dict(row)


def increment_pdf_usage(key_id: int) -> None:
    """Увеличивает счётчик pdf_used для ключа."""
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET pdf_used = pdf_used + 1 WHERE id = ?",
            (key_id,),
        )


def list_keys() -> list[dict]:
    """Возвращает все ключи (без key_hash) для admin."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT k.id, k.key_prefix, k.plan_id, k.label, k.email,
                   k.active, k.created_at, k.expires_at,
                   k.pdf_used, p.name as plan_name, p.pdf_limit
            FROM api_keys k
            JOIN api_plans p ON p.id = k.plan_id
            ORDER BY k.created_at DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_key(key_id: int) -> None:
    """Деактивирует ключ."""
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET active = FALSE WHERE id = ?",
            (key_id,),
        )
