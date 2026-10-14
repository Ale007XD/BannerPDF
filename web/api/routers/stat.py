"""
Собственный счётчик событий: POST /api/beat (без авторизации) и чтение для админа.

Эндпоинт принимает только значения из белого списка и не получает IP посетителя:
nginx не передаёт заголовки X-Real-IP / X-Forwarded-For на этот адрес и не пишет его в access-лог.
"""

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from ..services import stat_store
from .admin import require_admin

router = APIRouter()


class StatEvent(BaseModel):
    event: str = Field(max_length=40)
    page: str = Field(max_length=80)
    source: str = Field(default="direct", max_length=16)
    detail: str = Field(default="", max_length=64)


@router.post("/beat", status_code=204)
async def post_stat(ev: StatEvent) -> Response:
    # Невалидное событие молча отбрасывается: клиенту отвечаем одинаково.
    stat_store.record(ev.event, ev.page, ev.source, ev.detail)
    return Response(status_code=204)


@router.get("/admin/events", dependencies=[Depends(require_admin)])
async def admin_events(days: int = Query(30, ge=1, le=365)):
    """Суммарные счётчики событий за период (для оценки SEO и воронки)."""
    return stat_store.summary(days)
