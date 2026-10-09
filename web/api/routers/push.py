"""
push.py
~~~~~~~
Web Push для PWA админки: подписка устройства и проверочное уведомление.
Авторизация: Bearer ADMIN_TOKEN (как у остальной админки).

GET  /api/admin/push/config       — {"enabled", "public_key"} для pushManager.subscribe()
POST /api/admin/push/subscribe    — сохранить подписку устройства (PushSubscription.toJSON())
POST /api/admin/push/unsubscribe  — удалить подписку по endpoint
POST /api/admin/push/test         — отправить проверочное уведомление на все устройства
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..services import push_notify
from .admin import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/push", tags=["push"], dependencies=[Depends(require_admin)])


class Keys(BaseModel):
    p256dh: str = Field(..., max_length=200)
    auth: str = Field(..., max_length=100)


class SubscribeRequest(BaseModel):
    endpoint: str = Field(..., max_length=2100)
    keys: Keys


class UnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., max_length=2100)


def _require_enabled() -> None:
    if not push_notify.enabled():
        raise HTTPException(status_code=503, detail="Web Push не настроен: задайте VAPID_PRIVATE_KEY")


@router.get("/config")
async def push_config():
    if not push_notify.enabled():
        return {"enabled": False, "public_key": None}
    return {"enabled": True, "public_key": push_notify.public_key()}


@router.post("/subscribe")
async def push_subscribe(req: SubscribeRequest, request: Request):
    _require_enabled()
    try:
        push_notify.validate_subscription(req.endpoint, req.keys.p256dh, req.keys.auth)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    push_notify.save_subscription(
        req.endpoint, req.keys.p256dh, req.keys.auth, request.headers.get("user-agent"),
    )
    logger.info("Web Push: подписка сохранена (устройств: %d)", len(push_notify.list_subscriptions()))
    return {"ok": True}


@router.post("/unsubscribe")
async def push_unsubscribe(req: UnsubscribeRequest):
    return {"ok": True, "removed": push_notify.delete_subscription(req.endpoint)}


@router.post("/test")
async def push_test():
    _require_enabled()
    return await push_notify.send_test()
