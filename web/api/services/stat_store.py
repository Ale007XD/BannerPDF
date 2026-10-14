"""
stat_store.py
~~~~~~~~~~~~~
Счётчик посещений и действий без идентификаторов посетителя.

Принимает только событие, путь страницы, категорию источника и короткую метку,
каждое значение проверяется по белому списку или шаблону. Хранит лишь число
событий за день: IP, User-Agent и cookie не используются, поэтому связать
события одного посетителя нельзя и персональными данными они не являются.
"""

import re
from datetime import datetime, timedelta, timezone

from ..db import get_db

EVENTS = frozenset({
    "pageview", "prefill", "preview_generated", "start_order", "payment_started",
    "download_success", "example_click", "cta_click", "custom_contact_click", "custom_link_click",
    # серверные события: считаются на сервере, не зависят от браузера, блокировщиков и редиректов оплаты
    "order_paid", "file_downloaded",
})
SOURCES = frozenset({"direct", "yandex", "google", "internal", "other", "server"})

_PAGE_RE = re.compile(r"^/(?:[a-z0-9-]{1,60}/|requisites\.html)?$")
_DETAIL_RE = re.compile(r"^[A-Za-z0-9/._-]{0,64}$")

# Защита от раздувания таблицы мусорными значениями: не более стольких разных
# комбинаций в сутки. Реальных комбинаций (события × страницы × метки) на порядок меньше.
MAX_KEYS_PER_DAY = 3000


def clean_label(value: object) -> str:
    """Метка входа из браузера: возвращает её, если она допустима, иначе пустую строку (заказ из-за метки не блокируем)."""
    return value if isinstance(value, str) and _DETAIL_RE.match(value) else ""


def is_valid(event: str, page: str, source: str, detail: str) -> bool:
    return (
        event in EVENTS
        and source in SOURCES
        and bool(_PAGE_RE.match(page))
        and bool(_DETAIL_RE.match(detail))
    )


def record(event: str, page: str, source: str = "direct", detail: str = "") -> bool:
    """Увеличивает счётчик. Возвращает False, если событие отклонено."""
    if not is_valid(event, page, source, detail):
        return False
    day = datetime.now(timezone.utc).date().isoformat()
    key = (day, event, page, source, detail)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE stat_counters SET n = n + 1 "
            "WHERE day = ? AND event = ? AND page = ? AND source = ? AND detail = ?",
            key,
        )
        if cur.rowcount:
            return True
        known = conn.execute("SELECT COUNT(*) FROM stat_counters WHERE day = ?", (day,)).fetchone()[0]
        if known >= MAX_KEYS_PER_DAY:
            return False
        conn.execute(
            "INSERT INTO stat_counters (day, event, page, source, detail, n) VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(day, event, page, source, detail) DO UPDATE SET n = n + 1",
            key,
        )
    return True


def summary(days: int) -> dict:
    """Суммы за последние `days` суток: итоги по событиям и строки по (событие, страница, источник, метка)."""
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    with get_db() as conn:
        totals = {
            r["event"]: r["n"]
            for r in conn.execute(
                "SELECT event, SUM(n) AS n FROM stat_counters WHERE day >= ? GROUP BY event ORDER BY n DESC", (since,)
            )
        }
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event, page, source, detail, SUM(n) AS n FROM stat_counters WHERE day >= ? "
                "GROUP BY event, page, source, detail ORDER BY n DESC LIMIT 5000",
                (since,),
            )
        ]
    return {"since": since, "days": days, "totals": totals, "rows": rows}
