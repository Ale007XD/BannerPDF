"""
referral_store.py
~~~~~~~~~~~~~~~~~
Хранилище реферальной программы.
Таблицы: referrers, referrals

Сайт хранит: ref_code (8 A-Z0-9) + balance_rub — не ПД (152-ФЗ).
Сайт не знает tg_id владельца кода — намеренно.
Ref_code создаётся только ботом через POST /api/referral/internal/create.

Механика: 15% от суммы заказа на баланс реферера.
"""

import logging
from datetime import datetime, timezone

from ..db import get_db

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.15  # 15%


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_referrer(ref_code: str) -> bool:
    """
    Регистрирует новый ref_code.
    Возвращает True если создан, False если уже существует.
    """
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO referrers (ref_code, balance_rub, created_at) VALUES (?, 0, ?)",
                (ref_code, _now()),
            )
        logger.info("Создан реферер: %s", ref_code)
        return True
    except Exception:
        logger.warning("Реферер %s уже существует", ref_code)
        return False


def get_referrer(ref_code: str) -> dict | None:
    """Возвращает данные реферера или None если не найден."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT ref_code, balance_rub, created_at FROM referrers WHERE ref_code = ?",
            (ref_code,),
        ).fetchone()
    return dict(row) if row else None


def accrue_commission(ref_code: str, order_id: str, order_amount_rub: int) -> None:
    """
    Начисляет 15% комиссию рефереру.
    Идемпотентно — повторный вызов с тем же order_id игнорируется
    (UNIQUE order_id в таблице referrals).
    """
    commission = int(order_amount_rub * COMMISSION_RATE)
    if commission <= 0:
        return

    with get_db() as conn:
        # Проверяем что ref_code существует
        referrer = conn.execute(
            "SELECT ref_code FROM referrers WHERE ref_code = ?",
            (ref_code,),
        ).fetchone()

        if referrer is None:
            logger.warning("accrue_commission: ref_code %s не найден", ref_code)
            return

        try:
            conn.execute(
                """
                INSERT INTO referrals
                    (referrer_id, order_id, order_amount, commission, created_at, paid_out)
                VALUES (?, ?, ?, ?, ?, FALSE)
                """,
                (ref_code, order_id, order_amount_rub, commission, _now()),
            )
            conn.execute(
                "UPDATE referrers SET balance_rub = balance_rub + ? WHERE ref_code = ?",
                (commission, ref_code),
            )
            logger.info(
                "Начислено %d руб рефереру %s (заказ %s)", commission, ref_code, order_id
            )
        except Exception:
            # UNIQUE constraint — заказ уже был начислен
            logger.warning(
                "accrue_commission: заказ %s уже начислен рефереру %s", order_id, ref_code
            )


def get_stats(ref_code: str) -> dict | None:
    """Статистика реферера для GET /api/referral/stats/{code}."""
    with get_db() as conn:
        referrer = conn.execute(
            "SELECT ref_code, balance_rub, created_at FROM referrers WHERE ref_code = ?",
            (ref_code,),
        ).fetchone()

        if referrer is None:
            return None

        total_orders = conn.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?",
            (ref_code,),
        ).fetchone()["cnt"]

    return {
        "ref_code":     referrer["ref_code"],
        "balance_rub":  referrer["balance_rub"],
        "total_orders": total_orders,
        "created_at":   referrer["created_at"],
    }


def list_referrers() -> list[dict]:
    """Список всех рефереров для admin."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_code, r.balance_rub, r.created_at,
                   COUNT(rl.id) as total_orders,
                   COALESCE(SUM(rl.commission), 0) as total_commission
            FROM referrers r
            LEFT JOIN referrals rl ON rl.referrer_id = r.ref_code
            GROUP BY r.ref_code
            ORDER BY r.created_at DESC
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def mark_paid_out(ref_code: str) -> int:
    """
    Помечает все неоплаченные начисления как выплаченные.
    Обнуляет balance_rub.
    Возвращает сумму выплаты в рублях.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT balance_rub FROM referrers WHERE ref_code = ?",
            (ref_code,),
        ).fetchone()

        if row is None:
            return 0

        amount = row["balance_rub"]
        if amount <= 0:
            return 0

        conn.execute(
            "UPDATE referrals SET paid_out = TRUE WHERE referrer_id = ? AND paid_out = FALSE",
            (ref_code,),
        )
        conn.execute(
            "UPDATE referrers SET balance_rub = 0 WHERE ref_code = ?",
            (ref_code,),
        )

    logger.info("Выплачено %d руб рефереру %s", amount, ref_code)
    return amount
