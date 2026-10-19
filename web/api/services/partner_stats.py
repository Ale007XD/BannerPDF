"""
partner_stats.py
~~~~~~~~~~~~~~~~
Статистика по партнёрам-типографиям за период для админки (/admin/partners/) и выгрузка в CSV.

Период задаётся пресетом (today / week / month / prev_month) или датами from..to включительно.
Границы суток считаются по рабочему часовому поясу: UTC + STATS_UTC_OFFSET_HOURS (по умолчанию +3,
Москва): «сегодня» у владельца и у типографий должно совпадать с их календарём, а не с полуночью UTC.
Выплаты и `month_report` по-прежнему считают месяц по UTC (partner_store._month_bounds): здесь отчёт
для глаз и бухгалтерии, он не двигает баланс.

В заказ попадает всё, что оплачено (статусы PAID_STATUSES) в этот период и привязано к партнёру,
в том числе бесплатные по промокоду: их вознаграждение 0 ₽, но видно, что клиент пришёл.
Персональных данных клиента здесь нет: ни текста макета, ни IP, ни платёжных данных.
"""

import csv
import io
import os
import re
from datetime import date, datetime, time, timedelta, timezone

from ..db import get_db
from . import partner_naming, partner_store
from .partner_store import PartnerError, PartnerNotFound

PERIODS = ("today", "week", "month", "prev_month")
DEFAULT_PERIOD = "month"
MAX_SPAN_DAYS = 366
DEFAULT_UTC_OFFSET_HOURS = 3
CSV_KINDS = ("orders", "days")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_YEAR, _MAX_YEAR = 2020, 2100
# Ячейка, начинающаяся с этих символов, в Excel / LibreOffice исполняется как формула
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def utc_offset_hours() -> int:
    """Рабочий часовой пояс отчёта: STATS_UTC_OFFSET_HOURS (читается при каждом вызове), по умолчанию +3."""
    try:
        hours = int(os.getenv("STATS_UTC_OFFSET_HOURS", "").strip())
    except ValueError:
        return DEFAULT_UTC_OFFSET_HOURS
    return hours if -12 <= hours <= 14 else DEFAULT_UTC_OFFSET_HOURS


def _tz() -> timezone:
    return timezone(timedelta(hours=utc_offset_hours()))


def tz_label() -> str:
    h = utc_offset_hours()
    return f"UTC{h:+d}" if h else "UTC"


# ---------------------------------------------------------------------------
# Период
# ---------------------------------------------------------------------------
def _parse_date(value: str, what: str) -> date:
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        raise PartnerError(f"{what}: дата в формате ГГГГ-ММ-ДД")
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise PartnerError(f"{what}: такой даты нет") from None
    if not _MIN_YEAR <= d.year <= _MAX_YEAR:
        raise PartnerError(f"{what}: год от {_MIN_YEAR} до {_MAX_YEAR}")
    return d


def resolve_period(
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
) -> tuple[date, date, str]:
    """
    (с какого дня, по какой день включительно, название периода). Даты имеют приоритет над пресетом.
    PartnerError при неверных данных. `now` нужен тестам.
    """
    today = (now or datetime.now(timezone.utc)).astimezone(_tz()).date()

    if date_from or date_to:
        if not (date_from and date_to):
            raise PartnerError("Для своего периода нужны обе даты: from и to")
        d_from, d_to = _parse_date(date_from, "from"), _parse_date(date_to, "to")
        if d_from > d_to:
            raise PartnerError("Дата «с» позже даты «по»")
        if (d_to - d_from).days + 1 > MAX_SPAN_DAYS:
            raise PartnerError(f"Период не длиннее {MAX_SPAN_DAYS} дней")
        return d_from, d_to, "custom"

    name = (period or DEFAULT_PERIOD).strip().lower()
    if name == "today":
        return today, today, name
    if name == "week":
        return today - timedelta(days=6), today, name
    if name == "month":
        return today.replace(day=1), today, name
    if name == "prev_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, name
    raise PartnerError("Период: today, week, month, prev_month или from + to")


def _utc_bounds(d_from: date, d_to: date) -> tuple[str, str]:
    """[начало первого дня, начало следующего за последним) в UTC как ISO-строки (так лежит paid_at)."""
    tz = _tz()
    start = datetime.combine(d_from, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def _local(iso: str) -> datetime:
    moment = datetime.fromisoformat(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_tz())


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------
def _collect(period, date_from, date_to, partner, now) -> dict:
    d_from, d_to, name = resolve_period(period, date_from, date_to, now)
    start, end = _utc_bounds(d_from, d_to)

    code = None
    if partner and partner.strip():
        code = partner_store.normalize_code(partner)
        if code is None or partner_store.get_partner(code) is None:
            raise PartnerNotFound("Партнёр не найден")

    marks = ",".join("?" * len(partner_store.PAID_STATUSES))
    where_code = " AND o.partner_code = ?" if code else ""
    extra = (code,) if code else ()

    with get_db() as conn:
        partners = [
            dict(r) for r in conn.execute("SELECT code, name, active FROM partners ORDER BY created_at, code")
        ]
        orders = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT o.id, o.partner_code AS code, COALESCE(o.paid_at, o.created_at) AS paid_at,
                       o.size_key, o.amount_rub,
                       COALESCE(a.reward_rub, 0) AS reward_rub, a.paid_out_at
                  FROM web_orders o
                  LEFT JOIN partner_accruals a ON a.order_id = o.id
                 WHERE o.partner_code IS NOT NULL
                   AND o.status IN ({marks})
                   AND COALESCE(o.paid_at, o.created_at) >= ? AND COALESCE(o.paid_at, o.created_at) < ?
                   {where_code}
                 ORDER BY COALESCE(o.paid_at, o.created_at), o.id
                """,
                (*partner_store.PAID_STATUSES, start, end, *extra),
            )
        ]
        unpaid = {
            r["partner_code"]: r["rub"]
            for r in conn.execute(
                "SELECT partner_code, COALESCE(SUM(reward_rub), 0) AS rub FROM partner_accruals "
                "WHERE paid_out_at IS NULL" + (" AND partner_code = ?" if code else "") + " GROUP BY partner_code",
                extra,
            )
        }
        mails = {
            r["partner_code"]: (r["sent"], r["failed"])
            for r in conn.execute(
                """
                SELECT partner_code,
                       COALESCE(SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS sent,
                       COALESCE(SUM(CASE WHEN sent_at IS NULL AND error IS NOT NULL THEN 1 ELSE 0 END), 0) AS failed
                  FROM partner_deliveries
                 WHERE created_at >= ? AND created_at < ?
                """ + (" AND partner_code = ?" if code else "") + " GROUP BY partner_code",
                (start, end, *extra),
            )
        }

    for o in orders:
        o["local"] = _local(o["paid_at"])
        o["day"] = o["local"].date().isoformat()

    return {
        "from": d_from, "to": d_to, "period": name, "code": code,
        "partners": partners, "orders": orders, "unpaid": unpaid, "mails": mails,
    }


def _name_of(partners: list[dict], code: str) -> str:
    return next((p["name"] for p in partners if p["code"] == code), code)


def stats(period=None, date_from=None, date_to=None, partner=None, now=None) -> dict:
    """Сводка по партнёрам за период для страницы: итоги, строка на партнёра, строка на (день, партнёр)."""
    data = _collect(period, date_from, date_to, partner, now)
    orders, partners = data["orders"], data["partners"]

    shown = [p for p in partners if not data["code"] or p["code"] == data["code"]]
    rows = []
    for p in shown:
        mine = [o for o in orders if o["code"] == p["code"]]
        sent, failed = data["mails"].get(p["code"], (0, 0))
        rows.append({
            "code": p["code"], "name": p["name"], "active": bool(p["active"]),
            "orders": len(mine),
            "free_orders": sum(1 for o in mine if o["amount_rub"] == 0),
            "revenue_rub": sum(o["amount_rub"] for o in mine),
            "reward_rub": sum(o["reward_rub"] for o in mine),
            "unpaid_rub": data["unpaid"].get(p["code"], 0),  # за всё время, а не за период
            "mails_sent": sent, "mails_failed": failed,
        })
    # Заказы партнёра, которого уже нет в таблице: не теряем их из итогов
    known = {p["code"] for p in shown}
    for code in sorted({o["code"] for o in orders} - known):
        mine = [o for o in orders if o["code"] == code]
        rows.append({
            "code": code, "name": code, "active": False, "orders": len(mine),
            "free_orders": sum(1 for o in mine if o["amount_rub"] == 0),
            "revenue_rub": sum(o["amount_rub"] for o in mine),
            "reward_rub": sum(o["reward_rub"] for o in mine),
            "unpaid_rub": data["unpaid"].get(code, 0), "mails_sent": 0, "mails_failed": 0,
        })
    rows.sort(key=lambda r: (-r["orders"], r["code"]))

    return {
        "period": data["period"],
        "from": data["from"].isoformat(),
        "to": data["to"].isoformat(),
        "tz": tz_label(),
        "partner": data["code"],
        "all_partners": [{"code": p["code"], "name": p["name"], "active": bool(p["active"])} for p in partners],
        "partners": rows,
        "totals": {
            "orders": sum(r["orders"] for r in rows),
            "free_orders": sum(r["free_orders"] for r in rows),
            "revenue_rub": sum(r["revenue_rub"] for r in rows),
            "reward_rub": sum(r["reward_rub"] for r in rows),
            "unpaid_rub": sum(r["unpaid_rub"] for r in rows),
            "mails_failed": sum(r["mails_failed"] for r in rows),
        },
        "days": _days(orders, partners),
    }


def _days(orders: list[dict], partners: list[dict]) -> list[dict]:
    acc: dict[tuple[str, str], dict] = {}
    for o in orders:
        row = acc.setdefault((o["day"], o["code"]), {
            "day": o["day"], "code": o["code"], "name": _name_of(partners, o["code"]),
            "orders": 0, "free_orders": 0, "revenue_rub": 0, "reward_rub": 0,
        })
        row["orders"] += 1
        row["free_orders"] += 1 if o["amount_rub"] == 0 else 0
        row["revenue_rub"] += o["amount_rub"]
        row["reward_rub"] += o["reward_rub"]
    return [acc[k] for k in sorted(acc)]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _cell(value):
    """Строка, похожая на формулу, получает ведущий апостроф: Excel покажет текст, а не выполнит его."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIX):
        return "'" + value
    return value


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    """UTF-8 с BOM и «;»: так русский Excel открывает файл двойным щелчком, без мастера импорта."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _ru_date(iso_day: str) -> str:
    y, m, d = iso_day.split("-")
    return f"{d}.{m}.{y}"


def export_csv(kind="orders", period=None, date_from=None, date_to=None, partner=None, now=None) -> tuple[str, bytes]:
    """(имя файла, содержимое). kind: orders — строка на заказ; days — строка на (день, партнёр) и итог."""
    if kind not in CSV_KINDS:
        raise PartnerError("Вид выгрузки: orders или days")
    data = _collect(period, date_from, date_to, partner, now)
    orders, partners = data["orders"], data["partners"]
    span = f"{data['from'].isoformat()}_{data['to'].isoformat()}"

    if kind == "orders":
        header = ["Дата и время", "Партнёр", "Код", "Номер заказа", "Размер, м", "Сумма, ₽",
                  "Начислено, ₽", "Выплачено"]
        rows = [
            [
                o["local"].strftime("%d.%m.%Y %H:%M"), _name_of(partners, o["code"]), o["code"],
                partner_naming.order_number(o["id"]), o["size_key"] or "", o["amount_rub"], o["reward_rub"],
                _local(o["paid_out_at"]).strftime("%d.%m.%Y") if o["paid_out_at"] else "",
            ]
            for o in orders
        ]
        rows.append(["ИТОГО", "", "", f"{len(orders)} шт.", "", sum(o["amount_rub"] for o in orders),
                     sum(o["reward_rub"] for o in orders), ""])
        return f"partners-orders_{span}.csv", _csv_bytes(header, rows)

    days = _days(orders, partners)
    header = ["Дата", "Партнёр", "Код", "Заказов", "Из них бесплатных", "Выручка, ₽", "Начислено, ₽"]
    rows = [[_ru_date(d["day"]), d["name"], d["code"], d["orders"], d["free_orders"], d["revenue_rub"], d["reward_rub"]]
            for d in days]
    rows.append(["ИТОГО", "", "", sum(d["orders"] for d in days), sum(d["free_orders"] for d in days),
                 sum(d["revenue_rub"] for d in days), sum(d["reward_rub"] for d in days)])
    return f"partners-days_{span}.csv", _csv_bytes(header, rows)
