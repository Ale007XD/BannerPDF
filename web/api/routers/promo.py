"""
promo.py (router)
~~~~~~~~~~~~~~~~~
POST /api/promo/check — публичная проверка промокода для сайта: размер скидки и
цена к оплате. Ничего не списывает (списание — при создании заказа, order.py).

Ответ — снимок на момент запроса: к оформлению код может закончиться, тогда
POST /api/order вернёт 422 с причиной. Причины отказа те же, что в заказе.

Защита от перебора: лимит запросов в nginx (zone=promo_limit, nginx/default.conf).
"""

import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services import promo_store
from .order import SITE_PDF_PRICE

router = APIRouter()

_CODE_RE = re.compile(r"^[A-Z0-9]{2,20}$")   # тот же формат, что принимает POST /api/order


class PromoCheckRequest(BaseModel):
    code: str = Field(..., max_length=64)


@router.post("/promo/check")
async def check_promo(req: PromoCheckRequest):
    """
    {"code": "SALE30"} →
      {"valid": true,  "discount": 30, "price": 299, "final_price": 209, "free": false}
      {"valid": false, "message": "Промокод исчерпан"}
    Неверный формат кода — не ошибка запроса, а «не найден»: поле на сайте
    приводит ввод к A-Z0-9 само, но клиент может быть и не нашим.
    """
    code = req.code.strip().upper()
    if not _CODE_RE.match(code):
        return {"valid": False, "message": "Промокод не найден"}

    try:
        discount = promo_store.peek(code)
    except promo_store.PromoError as e:
        return {"valid": False, "message": str(e)}

    final_price = promo_store.calc_amount(SITE_PDF_PRICE, discount)
    return {
        "valid":       True,
        "discount":    discount,
        "price":       SITE_PDF_PRICE,
        "final_price": final_price,
        "free":        final_price == 0,
    }
