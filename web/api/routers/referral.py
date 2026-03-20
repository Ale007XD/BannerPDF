"""
referral.py
~~~~~~~~~~~
Реферальная программа.

МВП:
  POST /api/referral/internal/create — создание ref_code (только бот)
  GET  /api/referral/stats/{code}    — статистика реферера

Заглушки 501 (после МВП):
  GET  /api/referral/admin/list
  POST /api/referral/admin/payout/{code}

Авторизация internal: X-Bot-Secret: BOT_INTERNAL_SECRET
"""

import logging
import os
import re

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ..routers.admin import require_admin
from ..services.referral_store import create_referrer, get_stats

logger = logging.getLogger(__name__)
router = APIRouter()

BOT_INTERNAL_SECRET = os.getenv("BOT_INTERNAL_SECRET", "")


def require_bot(x_bot_secret: str = Header(..., alias="X-Bot-Secret")):
    """Зависимость: проверяет X-Bot-Secret."""
    if not BOT_INTERNAL_SECRET:
        raise HTTPException(status_code=500, detail="BOT_INTERNAL_SECRET не настроен")
    import hmac
    if not hmac.compare_digest(x_bot_secret, BOT_INTERNAL_SECRET):
        raise HTTPException(status_code=403, detail="Неверный bot-секрет")
    return True


class CreateReferrerRequest(BaseModel):
    ref_code: str = Field(..., min_length=8, max_length=8)

    @classmethod
    def validate_code(cls, v: str) -> str:
        if not re.match(r"^[A-Z0-9]{8}$", v):
            raise ValueError("ref_code: 8 символов A-Z0-9")
        return v


# ---------------------------------------------------------------------------
# Internal (только бот)
# ---------------------------------------------------------------------------
@router.post("/referral/internal/create", dependencies=[Depends(require_bot)])
async def create_referral_code(req: CreateReferrerRequest):
    """
    Регистрирует новый ref_code реферера.
    Вызывается только ботом через команду /referral.
    """
    if not re.match(r"^[A-Z0-9]{8}$", req.ref_code):
        raise HTTPException(status_code=422, detail="ref_code: 8 символов A-Z0-9")

    created = create_referrer(req.ref_code)
    if not created:
        raise HTTPException(status_code=409, detail="ref_code уже существует")

    logger.info("Зарегистрирован реферер: %s", req.ref_code)
    return {"ref_code": req.ref_code, "created": True}


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
@router.get("/referral/stats/{ref_code}")
async def referral_stats(ref_code: str):
    """Публичная статистика реферера (баланс, кол-во заказов)."""
    if not re.match(r"^[A-Z0-9]{8}$", ref_code):
        raise HTTPException(status_code=422, detail="Неверный формат ref_code")

    stats = get_stats(ref_code)
    if stats is None:
        raise HTTPException(status_code=404, detail="Реферальный код не найден")

    return stats


# ---------------------------------------------------------------------------
# Admin (заглушки 501 — после МВП)
# ---------------------------------------------------------------------------
@router.get("/referral/admin/list", dependencies=[Depends(require_admin)])
async def referral_admin_list():
    raise HTTPException(status_code=501, detail="Не реализовано в МВП")


@router.post("/referral/admin/payout/{ref_code}", dependencies=[Depends(require_admin)])
async def referral_admin_payout(ref_code: str):
    raise HTTPException(status_code=501, detail="Не реализовано в МВП")
