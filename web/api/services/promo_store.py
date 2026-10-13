"""
promo_store.py
~~~~~~~~~~~~~~
Промокоды со скидкой: расчёт суммы, атомарный резерв использования, возврат.
Таблица: promo_codes (code PK, uses_left, expires_at, discount, uses_total,
created_at, note, active). Выдача кодов — через админку (routers/admin.py).

Использование резервируется при создании заказа, до обращения к ЮKassa.
Возвращается, когда заказ не будет оплачен:
  - платёж в ЮKassa создать не удалось (create_order);
  - клиент закрыл окно оплаты или платёж отклонён (POST /api/order/{id}/abandon);
  - заказ так и остался неоплаченным RESERVE_TTL_MINUTES минут (release_stale из
    фонового cleanup).
Если оплата всё же приходит по заказу с уже возвращённым использованием, оно
берётся обратно (reclaim_for_order, вызывается из FSM при переходе в PAID).

Резерв — один UPDATE с условиями в WHERE: проверка и списание не разделены,
параллельные запросы не могут применить последнее использование дважды.
peek() — та же проверка без списания (POST /api/promo/check).
"""

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from ..db import get_db

logger = logging.getLogger(__name__)

# Без 0/O и 1/I: код диктуют по телефону и вводят вручную
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_MAX_GENERATE_ATTEMPTS = 8

# Через сколько минут неоплаченный заказ перестаёт удерживать использование промокода.
# Значение выбрано нами, а не взято у ЮKassa: в документации срок «подтвердить платёж
# за определённый срок» без цифры. Ошибка в меньшую сторону не страшна: поздняя оплата
# берёт использование обратно (reclaim_for_order).
RESERVE_TTL_MINUTES = 60


class PromoError(Exception):
    """Промокод нельзя применить. Текст ошибки показывается пользователю."""


class PromoExists(Exception):
    """Код с таким именем уже есть."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def calc_amount(price_rub: int, discount: int) -> int:
    """
    Сумма к оплате в рублях после скидки в процентах.

    Целочисленная арифметика, округление вниз (в пользу клиента). Скидка меньше
    100% никогда не даёт 0: нулевая сумма означает бесплатный флоу без ЮKassa.
    """
    if discount >= 100:
        return 0
    return max(1, price_rub * (100 - max(0, discount)) // 100)


def reserve(code: str) -> int:
    """
    Атомарно списывает одно использование и возвращает размер скидки в процентах.
    Бросает PromoError, если код нельзя применить.
    """
    now_iso = _now().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE promo_codes SET uses_left = uses_left - 1
            WHERE code = ? AND uses_left > 0 AND active = 1
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (code, now_iso),
        )
        if cur.rowcount == 1:
            return conn.execute(
                "SELECT discount FROM promo_codes WHERE code = ?", (code,)
            ).fetchone()["discount"]

        # Не списали: выясняем причину, чтобы показать понятное сообщение
        raise _why_unusable(conn, code, now_iso)


def peek(code: str) -> int:
    """
    Размер скидки в процентах, если код можно применить прямо сейчас.
    Ничего не списывает: для показа цены на сайте до оформления заказа.
    Бросает PromoError с той же причиной, что и reserve().

    Ответ — снимок: к моменту заказа код может закончиться, решает reserve().
    """
    now_iso = _now().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT discount FROM promo_codes
            WHERE code = ? AND uses_left > 0 AND active = 1
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (code, now_iso),
        ).fetchone()
        if row is not None:
            return row["discount"]
        raise _why_unusable(conn, code, now_iso)


def _why_unusable(conn: sqlite3.Connection, code: str, now_iso: str) -> PromoError:
    """Причина, по которой код нельзя применить (текст показывается пользователю)."""
    row = conn.execute(
        "SELECT uses_left, expires_at, active FROM promo_codes WHERE code = ?",
        (code,),
    ).fetchone()
    if row is None:
        return PromoError("Промокод не найден")
    if not row["active"]:
        return PromoError("Промокод отключён")
    if row["expires_at"] is not None and row["expires_at"] <= now_iso:
        return PromoError("Срок действия промокода истёк")
    return PromoError("Промокод исчерпан")


def release_code(code: str) -> None:
    """
    Возвращает одно использование коду (заказ не создан). Не больше выданного лимита.
    """
    with get_db() as conn:
        conn.execute(
            """
            UPDATE promo_codes SET uses_left = uses_left + 1
            WHERE code = ? AND (uses_total IS NULL OR uses_left < uses_total)
            """,
            (code,),
        )


def release_for_order(order_id: str) -> bool:
    """
    Возвращает использование, списанное заказом. Идемпотентно: повторный вызов
    для того же заказа ничего не делает. Возвращает True, если возврат выполнен.

    Только для неоплаченных заказов (status = 'pending' проверяется в самом UPDATE):
    если оплата пришла между выбором заказа и возвратом, использование остаётся за ним.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT promo_code FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None or not row["promo_code"]:
            return False
        cur = conn.execute(
            """
            UPDATE web_orders SET promo_released_at = ?
            WHERE id = ? AND promo_released_at IS NULL AND status = 'pending'
            """,
            (_now().isoformat(), order_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            """
            UPDATE promo_codes SET uses_left = uses_left + 1
            WHERE code = ? AND (uses_total IS NULL OR uses_left < uses_total)
            """,
            (row["promo_code"],),
        )
    logger.info("Промокод %s: возвращено использование (заказ %s)", row["promo_code"], order_id)
    return True


def release_stale(older_than_minutes: int = RESERVE_TTL_MINUTES) -> int:
    """
    Возвращает использования по заказам, которые так и не оплачены дольше
    older_than_minutes. Вызывается из фонового cleanup. Возвращает число возвратов.
    """
    cutoff = (_now() - timedelta(minutes=older_than_minutes)).isoformat()
    with get_db() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                """
                SELECT id FROM web_orders
                WHERE status = 'pending' AND promo_code IS NOT NULL
                  AND promo_released_at IS NULL AND created_at < ?
                """,
                (cutoff,),
            )
        ]
    return sum(1 for order_id in ids if release_for_order(order_id))


def reclaim_for_order(order_id: str) -> bool:
    """
    Оплата пришла по заказу, у которого использование уже вернули (окно оплаты
    закрыли или прошёл час): берём использование обратно, чтобы заказ снова его
    удерживал. Если лимит за это время исчерпан, заказ остаётся оплаченным без
    использования (деньги получены, PDF выдаётся), в лог пишется предупреждение.
    Возвращает True, если использование взято обратно.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT promo_code, promo_released_at FROM web_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None or not row["promo_code"] or row["promo_released_at"] is None:
            return False
        cur = conn.execute(
            "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code = ? AND uses_left > 0",
            (row["promo_code"],),
        )
        if cur.rowcount != 1:
            logger.warning(
                "Промокод %s: заказ %s оплачен после возврата использования, а лимит уже исчерпан",
                row["promo_code"], order_id,
            )
            return False
        conn.execute("UPDATE web_orders SET promo_released_at = NULL WHERE id = ?", (order_id,))
    logger.info("Промокод %s: использование взято обратно (поздняя оплата заказа %s)", row["promo_code"], order_id)
    return True


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def create_promo(
    *,
    discount: int,
    uses: int,
    code: str | None = None,
    expires_in_days: int | None = None,
    note: str | None = None,
) -> dict:
    """
    Создаёт промокод. Без `code` генерирует случайный. Бросает PromoExists,
    если заданный код занят.
    """
    now = _now()
    expires_at = (now + timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
    note = (note or "").strip()[:100] or None

    def _insert(candidate: str) -> None:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO promo_codes
                  (code, uses_left, expires_at, discount, uses_total, created_at, note, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (candidate, uses, expires_at, discount, uses, now.isoformat(), note),
            )

    if code:
        try:
            _insert(code)
        except sqlite3.IntegrityError as e:
            raise PromoExists(code) from e
    else:
        for _ in range(_MAX_GENERATE_ATTEMPTS):
            code = _generate_code()
            try:
                _insert(code)
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("Не удалось подобрать свободный промокод")

    logger.info("Создан промокод %s: скидка %d%%, использований %d", code, discount, uses)
    return {
        "code":       code,
        "discount":   discount,
        "uses_total": uses,
        "uses_left":  uses,
        "expires_at": expires_at,
        "active":     True,
        "note":       note,
        "created_at": now.isoformat(),
    }


def promo_status(*, active: bool, uses_left: int, expires_at: str | None, now_iso: str) -> str:
    """active | disabled | expired | exhausted — в таком порядке приоритета."""
    if not active:
        return "disabled"
    if expires_at is not None and expires_at <= now_iso:
        return "expired"
    if uses_left <= 0:
        return "exhausted"
    return "active"


def list_promos() -> list[dict]:
    """
    Все коды, новые выше. `used` — оплаченные заказы с этим кодом,
    `reserved` — заказы, ожидающие оплаты, у которых использование не возвращено.
    """
    now_iso = _now().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.code, p.discount, p.uses_left, p.uses_total, p.expires_at, p.active,
                   p.note, p.created_at,
                   (SELECT COUNT(*) FROM web_orders o
                     WHERE o.promo_code = p.code
                       AND o.status IN ('paid', 'token_issued')) AS used,
                   (SELECT COUNT(*) FROM web_orders o
                     WHERE o.promo_code = p.code AND o.status = 'pending'
                       AND o.promo_released_at IS NULL) AS reserved
            FROM promo_codes p
            ORDER BY (p.created_at IS NULL), p.created_at DESC, p.code
            """
        ).fetchall()

    return [
        {
            "code":       r["code"],
            "discount":   r["discount"],
            "uses_left":  r["uses_left"],
            "uses_total": r["uses_total"],
            "expires_at": r["expires_at"],
            "active":     bool(r["active"]),
            "note":       r["note"],
            "created_at": r["created_at"],
            "used":       r["used"],
            "reserved":   r["reserved"],
            "status":     promo_status(
                active=bool(r["active"]), uses_left=r["uses_left"],
                expires_at=r["expires_at"], now_iso=now_iso,
            ),
        }
        for r in rows
    ]


def set_active(code: str, active: bool) -> bool:
    """Включает или отключает код. Возвращает False, если кода нет."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE promo_codes SET active = ? WHERE code = ?", (1 if active else 0, code)
        )
    return cur.rowcount == 1
