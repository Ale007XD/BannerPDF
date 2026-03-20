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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..db import get_db

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
