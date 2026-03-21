"""
admin.py
~~~~~~~~
Административные эндпоинты.
Авторизация: Bearer ADMIN_TOKEN (из env).

GET  /api/admin/stats  — сводная статистика
GET  /api/admin/orders — список заказов с пагинацией
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db import get_db
from ..routers.order import transition, OrderStatus

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
_bearer = HTTPBearer()


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """Зависимость: проверяет Bearer ADMIN_TOKEN."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN не настроен")
    import hmac
    if not hmac.compare_digest(credentials.credentials, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Неверный admin-токен")
    return True


@router.get("/admin/stats", dependencies=[Depends(require_admin)])
async def admin_stats():
    """Сводная статистика по заказам и выручке."""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM web_orders").fetchone()[0]
        paid  = conn.execute(
            "SELECT COUNT(*) FROM web_orders WHERE status IN ('paid','token_issued')"
        ).fetchone()[0]
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_rub),0) FROM web_orders WHERE status IN ('paid','token_issued')"
        ).fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM web_orders WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM web_orders WHERE status = 'pending'"
        ).fetchone()[0]
        referral_debt = conn.execute(
            "SELECT COALESCE(SUM(balance_rub),0) FROM referrers"
        ).fetchone()[0]

    return {
        "total_orders":    total,
        "paid_orders":     paid,
        "pending_orders":  pending_count,
        "revenue_rub":     revenue,
        "today_orders":    today,
        "referral_debt_rub": referral_debt,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }


@router.get("/admin/orders", dependencies=[Depends(require_admin)])
async def admin_orders(
    limit:  int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None),
):
    """Список заказов с пагинацией и фильтром по статусу."""
    query  = "SELECT id, amount_rub, size_key, ref_code, status, created_at, paid_at FROM web_orders"
    params: list = []

    if status:
        query += " WHERE status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM web_orders" + (" WHERE status = ?" if status else ""),
            [status] if status else [],
        ).fetchone()[0]

    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "orders": [dict(r) for r in rows],
    }


@router.get("/admin/funnel", dependencies=[Depends(require_admin)])
async def admin_funnel():
    """Воронка конверсии по статусам."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM web_orders GROUP BY status"
        ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


@router.post("/admin/force_token/{order_id}", dependencies=[Depends(require_admin)])
async def force_token(order_id: str):
    """
    Ручная выдача download-токена после подтверждения оплаты.

    Используется при ручном флоу (без платёжного провайдера):
      1. Проверяет что заказ существует и находится в статусе PENDING
      2. Переводит PENDING → PAID → TOKEN_ISSUED через FSM (два перехода)
      3. Создаёт одноразовый download-токен TTL 15 минут
      4. Возвращает токен и прямую ссылку для скачивания

    Каждый вызов логируется. Выдача невозможна для expired-заказов.
    """
    # Проверяем существование и текущий статус заказа
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, amount_rub, size_key, created_at FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Заказ {order_id} не найден")

    current_status = row["status"]

    if current_status == OrderStatus.EXPIRED:
        raise HTTPException(
            status_code=409,
            detail=f"Заказ {order_id} истёк — выдача невозможна",
        )
    if current_status == OrderStatus.TOKEN_ISSUED:
        # Идемпотентность: токен уже выдан — возвращаем актуальный
        with get_db() as conn:
            token_row = conn.execute(
                """
                SELECT token FROM download_tokens
                WHERE order_id = ? AND used = FALSE AND expires_at > ?
                ORDER BY expires_at DESC LIMIT 1
                """,
                (order_id, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
        if token_row:
            logger.info(
                "force_token: заказ %s уже в token_issued, возвращаем существующий токен",
                order_id,
            )
            return {
                "order_id":      order_id,
                "token":         token_row["token"],
                "download_url":  f"/api/download/{token_row['token']}",
                "already_issued": True,
            }

    # FSM: PENDING → PAID (если ещё не PAID)
    if current_status == OrderStatus.PENDING:
        try:
            transition(order_id, "webhook_paid")
            logger.info("force_token: %s PENDING → PAID", order_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    # FSM: PAID → TOKEN_ISSUED
    try:
        transition(order_id, "issue_token")
        logger.info("force_token: %s PAID → TOKEN_ISSUED", order_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Создаём download-токен (TTL 15 минут, одноразовый)
    token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO download_tokens (token, order_id, expires_at, used) VALUES (?, ?, ?, FALSE)",
            (token, order_id, expires_at),
        )

    logger.info(
        "force_token: выдан токен для заказа %s | размер=%s | сумма=%d руб | expires=%s",
        order_id,
        row["size_key"],
        row["amount_rub"],
        expires_at,
    )

    return {
        "order_id":      order_id,
        "token":         token,
        "download_url":  f"/api/download/{token}",
        "expires_at":    expires_at,
        "already_issued": False,
    }
