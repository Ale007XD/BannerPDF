"""
corp_api.py
~~~~~~~~~~~
Корпоративный API — заглушки 501 для МВП.
Роутер зарегистрирован, эндпоинты включатся после МВП.

POST /api/v1/render
GET  /api/v1/usage
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1")


@router.post("/render")
async def corp_render():
    raise HTTPException(status_code=501, detail="Корп. API: не реализовано в МВП")


@router.get("/usage")
async def corp_usage():
    raise HTTPException(status_code=501, detail="Корп. API: не реализовано в МВП")
