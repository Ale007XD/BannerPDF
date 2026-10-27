"""
partner.py
~~~~~~~~~~
Партнёры-типографии: брендированная страница конструктора, вознаграждение, отправка макета.

Публичное:
  GET  /api/partner/{code}  — название, логотип, цвет, рабочий телефон и способ отправки макета (delivery: auto | button)
                              для брендирования (без почты и ставки)
  POST /api/partner/send/{order_id} — кнопка «Отправить в …» в окне после оплаты: письмо с макетом
                              типографии. Работает, когда у партнёра выключена автоотправка (auto_email = false)

Админка (Bearer ADMIN_TOKEN):
  POST /api/admin/partners                                  — создать партнёра
  GET  /api/admin/partners                                  — список с суммами к выплате
  PATCH /api/admin/partners/{code}                          — изменить название, почту, логотип, цвет, ставку, телефон,
                                                              auto_email (true — письмо уходит само, false — по кнопке клиента)
  POST /api/admin/partners/{code}/active                    — заморозить (false) / восстановить (true)
  POST /api/admin/partners/{code}/phone                     — задать / убрать рабочий телефон
  GET  /api/admin/partners/report?month=ГГГГ-ММ             — сводка за месяц
  GET  /api/admin/partners/stats?period=…|from=…&to=…       — статистика за период (страница /admin/partners/)
  GET  /api/admin/partners/stats.csv?kind=orders|days&…     — выгрузка в CSV (Excel: UTF-8 с BOM, разделитель «;»)
  POST /api/admin/partners/{code}/payout                    — отметить выплаченным (всё или до конца месяца)
  GET  /api/admin/partners/deliveries?partner=&q=&only=failed&limit= — письма типографиям по заказам: ушло, сбой, не скачан
  POST /api/admin/partners/deliveries/{order_id}/resend     — повторить отправку макета на почту партнёра

Ссылка для типографии: https://bannerbot.ru/?partner=<code>
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from ..db import get_db
from ..routers.admin import require_admin
from ..services import mailer, partner_delivery, partner_stats, partner_store
from ..services.renderer import get_executor, render_pdf_sync

logger = logging.getLogger(__name__)
router = APIRouter()


def _http_error(e: partner_store.PartnerError) -> HTTPException:
    if isinstance(e, partner_store.PartnerNotFound):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, partner_store.PartnerExists):
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Публичное
# ---------------------------------------------------------------------------
@router.get("/partner/{code}")
async def partner_branding(code: str, response: Response):
    """
    Данные для брендирования страницы конструктора.
    404 для неизвестного или отключённого партнёра: страница остаётся обычной.
    delivers — уходит ли макет на почту типографии (почта настроена на сервере).
    """
    data = partner_store.public_branding(code)
    if data is None:
        raise HTTPException(status_code=404, detail="Партнёр не найден")
    data["delivers"] = mailer.is_configured()
    response.headers["Cache-Control"] = "public, max-age=300"
    return data


@router.post("/partner/send/{order_id}")
async def send_order_to_partner(order_id: str):
    """
    Кнопка «Отправить в …» в окне после оплаты: отправляет макет оплаченного партнёрского заказа на почту типографии.

    Идентификатор заказа (UUID4) тот же секрет, что у /order/{id}/amend, отдельной авторизации нет.
    Режим партнёра (auto_email) не проверяется: если админ переключил его, пока клиент смотрел окно, кнопка всё равно
    сработает. Повторы безопасны: письмо с этой версией макета уходит один раз (partner_deliveries), повторный
    запрос отвечает already=true без рендера и без письма. После бесплатной правки текста версия новая,
    и типография получает «Исправленный макет».

    404 — нет заказа; 409 — не партнёрский, не оплачен или типография отключена; 410 — текст макета стёрт по сроку
    хранения; 502 — письмо не ушло (повторить можно); 503 — почта на сервере не настроена.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, partner_code, size_key, config_json FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if not row["partner_code"]:
        raise HTTPException(status_code=409, detail="Заказ не партнёрский")
    if row["status"] not in partner_store.PAID_STATUSES:
        raise HTTPException(status_code=409, detail="Заказ не оплачен")
    partner = partner_store.get_active_partner(row["partner_code"])
    if partner is None:
        raise HTTPException(status_code=409, detail="Типография сейчас не принимает макеты")
    if not row["config_json"]:
        raise HTTPException(status_code=410, detail="Данные заказа удалены по истечении срока хранения")
    if not mailer.is_configured():
        raise HTTPException(status_code=503, detail="Отправка временно недоступна")

    cfg_hash = partner_store.config_hash(row["config_json"])
    if partner_store.is_delivered(order_id, cfg_hash):
        return {"order_id": order_id, "sent": True, "already": True}

    try:
        config = json.loads(row["config_json"])
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(get_executor(), render_pdf_sync, config)
    except Exception as e:
        logger.error("partner/send: ошибка рендеринга PDF для заказа %s: %s", order_id, e)
        raise HTTPException(status_code=500, detail="Не удалось сформировать PDF") from None

    sent = await partner_delivery.deliver_order_pdf(
        order_id, partner["code"], row["size_key"], row["config_json"], pdf_bytes,
    )
    if not sent and not partner_store.is_delivered(order_id, cfg_hash):
        # deliver_order_pdf причину пишет в лог и в partner_deliveries.error; клиенту детали SMTP не нужны
        raise HTTPException(status_code=502, detail="Не удалось отправить макет. Попробуйте ещё раз через минуту.")
    return {"order_id": order_id, "sent": True, "already": not sent}


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------
class CreatePartnerRequest(BaseModel):
    code:       str
    name:       str
    email:      str
    logo_url:   Optional[str] = None
    accent:     Optional[str] = None
    reward_rub: int = partner_store.DEFAULT_REWARD_RUB
    phone:      Optional[str] = None
    auto_email: bool = True


class UpdatePartnerRequest(BaseModel):
    # Меняются только присланные поля. null или "" у logo_url, accent, phone убирает значение
    name:       Optional[str] = None
    email:      Optional[str] = None
    logo_url:   Optional[str] = None
    accent:     Optional[str] = None
    reward_rub: Optional[int] = None
    phone:      Optional[str] = None
    auto_email: Optional[bool] = None


class ActiveRequest(BaseModel):
    active: bool


class PhoneRequest(BaseModel):
    # Пусто или null: убрать телефон (в имени файла останется только номер заказа)
    phone: Optional[str] = None


class PayoutRequest(BaseModel):
    # ГГГГ-ММ: выплатить начисления, созданные до конца этого месяца. Не задан = все невыплаченные
    month: Optional[str] = None


@router.post("/admin/partners", dependencies=[Depends(require_admin)])
async def admin_create_partner(body: CreatePartnerRequest):
    try:
        partner = partner_store.create_partner(
            body.code, body.name, body.email, body.logo_url, body.accent, body.reward_rub, body.phone, body.auto_email,
        )
    except partner_store.PartnerError as e:
        raise _http_error(e) from None
    return {**partner, "link": f"https://bannerbot.ru/?partner={partner['code']}"}


@router.patch("/admin/partners/{code}", dependencies=[Depends(require_admin)])
async def admin_update_partner(code: str, body: UpdatePartnerRequest):
    changes = {f: getattr(body, f) for f in body.model_fields_set}
    # name, email, reward_rub, auto_email обязательны: явный null не «убирает» их, а считается ошибкой
    for field in ("name", "email", "reward_rub", "auto_email"):
        if field in changes and changes[field] is None:
            raise HTTPException(status_code=422, detail=f"Поле {field} нельзя очистить")
    try:
        return partner_store.update_partner(code, changes)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None


@router.get("/admin/partners", dependencies=[Depends(require_admin)])
async def admin_list_partners():
    return {"mail_configured": mailer.is_configured(), "partners": partner_store.list_partners()}


@router.get("/admin/partners/deliveries", dependencies=[Depends(require_admin)])
async def admin_partner_deliveries(
    partner: Optional[str] = None,
    q: Optional[str] = None,
    only: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
):
    """
    Состояние письма с макетом по каждому оплаченному партнёрскому заказу (свежие сверху).
    q: номер заказа, который назвал клиент (482-915). only=failed: только сбои.
    """
    if only not in (None, "", "failed"):
        raise HTTPException(status_code=422, detail="only: failed или пусто")
    try:
        items = partner_store.list_deliveries(partner, q, only == "failed", limit)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None
    return {"mail_configured": mailer.is_configured(), "deliveries": items}


@router.get("/admin/partners/report", dependencies=[Depends(require_admin)])
async def admin_partner_report(month: Optional[str] = None):
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        return partner_store.month_report(month)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None


@router.get("/admin/partners/stats", dependencies=[Depends(require_admin)])
async def admin_partner_stats(
    period: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    partner: Optional[str] = None,
):
    """
    Заказы, выручка и вознаграждение партнёров за период. period: today | week (7 дней) | month | prev_month
    (по умолчанию month); либо свои даты from и to (ГГГГ-ММ-ДД, включительно). partner: код, чтобы смотреть одного.
    Сутки считаются по поясу STATS_UTC_OFFSET_HOURS (по умолчанию +3).
    """
    try:
        return partner_stats.stats(period, date_from, date_to, partner)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None


@router.get("/admin/partners/stats.csv", dependencies=[Depends(require_admin)])
async def admin_partner_stats_csv(
    kind: str = "orders",
    period: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    partner: Optional[str] = None,
):
    """Те же данные в CSV: kind=orders (строка на заказ) или kind=days (строка на день и партнёра)."""
    try:
        filename, content = partner_stats.export_csv(kind, period, date_from, date_to, partner)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/admin/partners/{code}/active", dependencies=[Depends(require_admin)])
async def admin_set_partner_active(code: str, body: ActiveRequest):
    norm = partner_store.normalize_code(code)
    if norm is None or not partner_store.set_active(norm, body.active):
        raise HTTPException(status_code=404, detail="Партнёр не найден")
    return {"code": norm, "active": body.active}


@router.post("/admin/partners/{code}/phone", dependencies=[Depends(require_admin)])
async def admin_set_partner_phone(code: str, body: PhoneRequest):
    norm = partner_store.normalize_code(code)
    try:
        found = norm is not None and partner_store.set_phone(norm, body.phone)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None
    if not found:
        raise HTTPException(status_code=404, detail="Партнёр не найден")
    return {"code": norm, "phone": partner_store.normalize_phone(body.phone)}


@router.post("/admin/partners/{code}/payout", dependencies=[Depends(require_admin)])
async def admin_partner_payout(code: str, body: Optional[PayoutRequest] = None):
    norm = partner_store.normalize_code(code)
    if norm is None:
        raise HTTPException(status_code=404, detail="Партнёр не найден")
    try:
        result = partner_store.mark_paid_out(norm, body.month if body else None)
    except partner_store.PartnerError as e:
        raise _http_error(e) from None
    return {"code": norm, **result}


@router.post("/admin/partners/deliveries/{order_id}/resend", dependencies=[Depends(require_admin)])
async def admin_resend_delivery(order_id: str):
    """
    Заново рендерит оплаченный партнёрский заказ и отправляет макет на почту типографии.
    Нужен, если письмо не ушло (почта была не настроена, сбой SMTP) или адрес партнёра сменился.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, partner_code, size_key, config_json FROM web_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if not row["partner_code"]:
        raise HTTPException(status_code=409, detail="Заказ не партнёрский")
    if row["status"] not in partner_store.PAID_STATUSES:
        raise HTTPException(status_code=409, detail="Заказ не оплачен")
    if not row["config_json"]:
        # services/retention.py стирает текст макета через ORDER_PII_RETENTION_DAYS после оплаты
        raise HTTPException(status_code=410, detail="Данные заказа удалены по истечении срока хранения: макет не восстановить")
    if not mailer.is_configured():
        raise HTTPException(status_code=503, detail="Почта не настроена (SMTP_HOST, SMTP_FROM)")

    try:
        config = json.loads(row["config_json"])
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(get_executor(), render_pdf_sync, config)
    except Exception as e:
        logger.error("resend: ошибка рендеринга PDF для заказа %s: %s", order_id, e)
        raise HTTPException(status_code=500, detail="Не удалось сформировать PDF") from None

    sent = await partner_delivery.deliver_order_pdf(
        order_id, row["partner_code"], row["size_key"], row["config_json"], pdf_bytes, force=True,
    )
    if not sent:
        raise HTTPException(status_code=502, detail="Письмо не отправлено: причина в логе и в списке партнёров")
    return {"order_id": order_id, "sent": True}
