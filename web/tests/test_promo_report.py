"""
test_promo_report.py
~~~~~~~~~~~~~~~~~~~~
Отчёт по промокодам в админке: promo_store.promo_report() и GET /api/admin/promo/report.

Покрывает:
  - итоги и цифры по кодам: оплаченные, бесплатные, ожидающие оплаты, вернувшиеся
  - рубли скидки и выручка считаются только по оплаченным заказам
  - откуда берутся рубли: web_orders.discount_rub (цена на момент заказа), а для старых
    заказов без неё — текущая цена минус сумма заказа (и это видно в totals.estimated)
  - неиспользованные коды: ни оплаты, ни заказа, удерживающего использование
  - последние заказы: порядок, состояние, лимит, фильтр по коду
  - POST /api/order записывает discount_rub; смена цены отчёт не меняет
  - поздняя оплата после возврата использования не даёт двойной записи
  - миграция: колонка добавляется на развёрнутую БД, старые заказы остаются в отчёте
  - доступ только с токеном админа
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from web.api.db import init_db
from web.api.services import promo_store

PRICE = 299

PAYLOAD = {
    "size_key":     "1x0.5",
    "bg_color":     "Белый",
    "text_color":   "Черный",
    "font":         "Golos Text",
    "text_lines":   [{"text": "Аренда склада +7 914 000-00-00", "scale": 100}],
    "accept_terms": True,
}

_UNSET = object()   # discount_rub не передан: NULL, как у заказов до появления колонки


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _auth() -> dict:
    from web.api.routers import admin
    return {"Authorization": f"Bearer {admin.ADMIN_TOKEN}"}


def _add_promo(db_path: str, code: str, *, discount: int = 30, uses_left: int = 5,
               uses_total: int | None = None, active: int = 1,
               expires_at: str | None = None, created_at: str | None = None) -> None:
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO promo_codes (code, uses_left, expires_at, discount, uses_total, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, uses_left, expires_at, discount, uses_total if uses_total is not None else uses_left,
         created_at or _iso(), active),
    )
    conn.commit()
    conn.close()


def _put_order(db_path: str, order_id: str, code: str | None, status: str, amount: int, *,
               discount_rub=_UNSET, created_at: str | None = None, paid_at: str | None = None,
               released: bool = False, size_key: str = "1x0.5", ref_code: str | None = None) -> None:
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, ref_code, promo_code, config_json, status, "
        "created_at, paid_at, promo_released_at, discount_rub) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)",
        (order_id, amount, size_key, ref_code, code, status, created_at or _iso(), paid_at,
         _iso() if released else None, None if discount_rub is _UNSET else discount_rub),
    )
    conn.commit()
    conn.close()


def _row(db_path: str, order_id: str) -> sqlite3.Row:
    conn = _db(db_path)
    row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def _report(**kwargs) -> dict:
    return promo_store.promo_report(list_price_rub=kwargs.pop("price", PRICE), **kwargs)


def _by_code(report: dict) -> dict:
    return {c["code"]: c for c in report["codes"]}


# ---------------------------------------------------------------------------
# Пустая база
# ---------------------------------------------------------------------------

class TestEmpty:

    @pytest.mark.asyncio
    async def test_endpoint_on_empty_db(self, client):
        resp = await client.get("/api/admin/promo/report", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["codes"] == [] and body["recent"] == []
        assert body["price_rub"] == PRICE and body["recent_limit"] == 50
        assert set(body["totals"].values()) == {0}

    def test_orders_without_promo_are_not_in_the_report(self, init_test_db):
        _put_order(init_test_db, "plain", None, "token_issued", PRICE, discount_rub=0)
        report = _report()
        assert report["recent"] == [] and set(report["totals"].values()) == {0}

    def test_code_without_orders_is_unused(self, init_test_db):
        _add_promo(init_test_db, "NEW")
        report = _report()
        (code,) = report["codes"]
        assert code["unused"] is True and code["last_used_at"] is None
        assert (code["used"], code["reserved"], code["abandoned"], code["free"]) == (0, 0, 0, 0)
        assert (code["revenue_rub"], code["discount_rub"]) == (0, 0)
        assert report["totals"]["unused"] == 1 and report["totals"]["unused_active"] == 1


# ---------------------------------------------------------------------------
# Итоги и цифры по кодам
# ---------------------------------------------------------------------------

class TestTotals:

    def _seed(self, db):
        _add_promo(db, "SALE30", discount=30, uses_left=1, uses_total=5)
        _add_promo(db, "FREE", discount=100, uses_left=0, uses_total=1)
        _add_promo(db, "IDLE", discount=50, uses_left=3)
        _add_promo(db, "WAITING", discount=20, uses_left=2, uses_total=3)
        _add_promo(db, "GIVENUP", discount=10, uses_left=3)

        # SALE30: два оплаченных, один с выданным PDF, один ждёт оплаты, один вернулся
        _put_order(db, "s1", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso(-timedelta(days=2)))
        _put_order(db, "s2", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso(-timedelta(days=1)))
        _put_order(db, "s3", "SALE30", "token_issued", 209, discount_rub=90, paid_at=_iso(-timedelta(hours=1)))
        _put_order(db, "s4", "SALE30", "pending", 209, discount_rub=90)
        _put_order(db, "s5", "SALE30", "pending", 209, discount_rub=90, released=True)
        # FREE: бесплатный заказ, скидка = вся цена
        _put_order(db, "f1", "FREE", "token_issued", 0, discount_rub=PRICE, paid_at=_iso())
        # WAITING: только заказ в ожидании оплаты
        _put_order(db, "w1", "WAITING", "pending", 239, discount_rub=60)
        # GIVENUP: заказ был, но использование вернулось
        _put_order(db, "g1", "GIVENUP", "pending", 269, discount_rub=30, released=True)

    def test_per_code_numbers(self, init_test_db):
        self._seed(init_test_db)
        codes = _by_code(_report())

        sale = codes["SALE30"]
        assert (sale["used"], sale["reserved"], sale["abandoned"], sale["free"]) == (3, 1, 1, 0)
        assert sale["revenue_rub"] == 3 * 209 and sale["discount_rub"] == 3 * 90
        assert sale["unused"] is False

        free = codes["FREE"]
        assert (free["used"], free["free"]) == (1, 1)
        assert free["revenue_rub"] == 0 and free["discount_rub"] == PRICE
        assert free["status"] == "exhausted" and free["unused"] is False

    def test_totals_are_the_sum_of_codes(self, init_test_db):
        self._seed(init_test_db)
        report = _report()
        t = report["totals"]
        assert t["codes"] == 5 and t["active"] == 4          # FREE исчерпан
        assert t["applied"] == 4 and t["free"] == 1
        assert t["reserved"] == 2 and t["abandoned"] == 2
        assert t["revenue_rub"] == 3 * 209
        assert t["discount_rub"] == 3 * 90 + PRICE
        for key in ("applied", "free", "reserved", "abandoned", "revenue_rub", "discount_rub", "estimated"):
            assert t[key] == sum(c[key if key != "applied" else "used"] for c in report["codes"]), key

    def test_unused_means_no_payment_and_no_held_use(self, init_test_db):
        self._seed(init_test_db)
        report = _report()
        codes = _by_code(report)
        # WAITING держит использование (клиент может оплатить), поэтому «не использован» он не считается
        assert codes["WAITING"]["unused"] is False
        assert codes["IDLE"]["unused"] is True
        # у GIVENUP заказ был, но использование вернулось и оплаты нет
        assert codes["GIVENUP"]["unused"] is True and codes["GIVENUP"]["abandoned"] == 1
        assert report["totals"]["unused"] == 2 and report["totals"]["unused_active"] == 2

    def test_unused_active_excludes_dead_codes(self, init_test_db):
        _add_promo(init_test_db, "LIVE")
        _add_promo(init_test_db, "OFF", active=0)
        _add_promo(init_test_db, "OLD", expires_at=_iso(-timedelta(days=1)))
        t = _report()["totals"]
        assert t["unused"] == 3 and t["unused_active"] == 1 and t["active"] == 1

    def test_money_counts_only_paid_orders(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "wait", "SALE30", "pending", 209, discount_rub=90)
        _put_order(init_test_db, "gone", "SALE30", "pending", 209, discount_rub=90, released=True)
        _put_order(init_test_db, "exp", "SALE30", "expired", 209, discount_rub=90)
        sale = _by_code(_report())["SALE30"]
        assert sale["revenue_rub"] == 0 and sale["discount_rub"] == 0
        assert (sale["used"], sale["reserved"], sale["abandoned"]) == (0, 1, 2)

    def test_expired_order_is_abandoned_not_reserved(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "exp", "SALE30", "expired", 209, discount_rub=90)
        sale = _by_code(_report())["SALE30"]
        assert sale["reserved"] == 0 and sale["abandoned"] == 1 and sale["unused"] is True

    def test_counters_match_list_promos(self, init_test_db):
        self._seed(init_test_db)
        listed = {p["code"]: p for p in promo_store.list_promos()}
        reported = _by_code(_report())
        assert list(reported) == list(listed)               # тот же порядок
        for code, p in listed.items():
            assert reported[code] == {**reported[code], **p}, code   # поля списка не расходятся

    def test_last_used_is_the_latest_payment(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        early, late = _iso(-timedelta(days=3)), _iso(-timedelta(hours=2))
        _put_order(init_test_db, "a", "SALE30", "paid", 209, discount_rub=90, paid_at=early)
        _put_order(init_test_db, "b", "SALE30", "paid", 209, discount_rub=90, paid_at=late)
        _put_order(init_test_db, "c", "SALE30", "pending", 209, discount_rub=90)   # не оплачен: не считается
        assert _by_code(_report())["SALE30"]["last_used_at"] == late

    def test_last_used_falls_back_to_created_at(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        created = _iso(-timedelta(days=1))
        _put_order(init_test_db, "a", "SALE30", "paid", 209, discount_rub=90, created_at=created)
        assert _by_code(_report())["SALE30"]["last_used_at"] == created

    def test_orders_of_other_codes_do_not_leak(self, init_test_db):
        _add_promo(init_test_db, "AAA")
        _add_promo(init_test_db, "BBB")
        _put_order(init_test_db, "a", "AAA", "paid", 209, discount_rub=90, paid_at=_iso())
        codes = _by_code(_report())
        assert codes["AAA"]["used"] == 1 and codes["BBB"]["used"] == 0 and codes["BBB"]["unused"] is True


# ---------------------------------------------------------------------------
# Откуда берутся рубли скидки
# ---------------------------------------------------------------------------

class TestDiscountRubles:

    def test_recorded_discount_survives_a_price_change(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "o1", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso())
        for price in (299, 500, 100):
            report = _report(price=price)
            assert report["totals"]["discount_rub"] == 90, price
            assert report["totals"]["estimated"] == 0
            assert report["recent"][0]["discount_rub"] == 90

    def test_legacy_order_uses_the_current_price_and_is_flagged(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "old", "SALE30", "token_issued", 209, paid_at=_iso())     # discount_rub NULL
        _put_order(init_test_db, "new", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso())

        report = _report(price=299)
        assert report["totals"]["discount_rub"] == 90 + 90 and report["totals"]["estimated"] == 1

        report = _report(price=500)       # цену подняли: новый заказ не меняется, старый — оценка
        assert report["totals"]["discount_rub"] == 90 + (500 - 209)
        assert report["totals"]["estimated"] == 1
        assert _by_code(report)["SALE30"]["estimated"] == 1

    def test_legacy_free_order_counts_the_full_price(self, init_test_db):
        _add_promo(init_test_db, "FREE", discount=100)
        _put_order(init_test_db, "old", "FREE", "token_issued", 0, paid_at=_iso())
        assert _report(price=299)["totals"]["discount_rub"] == 299

    def test_legacy_discount_is_never_negative(self, init_test_db):
        # цену снизили ниже суммы старого заказа: отрицательной скидки не бывает
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "old", "SALE30", "paid", 209, paid_at=_iso())
        report = _report(price=150)
        assert report["totals"]["discount_rub"] == 0 and report["recent"][0]["discount_rub"] == 0

    def test_estimate_is_counted_only_for_paid_orders(self, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "old-wait", "SALE30", "pending", 209)
        assert _report()["totals"]["estimated"] == 0


# ---------------------------------------------------------------------------
# Последние заказы
# ---------------------------------------------------------------------------

class TestRecent:

    def _seed(self, db):
        _add_promo(db, "AAA")
        _add_promo(db, "BBB")
        base = datetime.now(timezone.utc)
        for i, (oid, code, status, released) in enumerate([
            ("o1", "AAA", "token_issued", False),
            ("o2", "BBB", "pending", False),
            ("o3", "AAA", "pending", True),
            ("o4", "AAA", "paid", False),
            ("o5", "BBB", "expired", False),
        ]):
            _put_order(db, oid, code, status, 209, discount_rub=90, released=released,
                       created_at=(base + timedelta(minutes=i)).isoformat(),
                       paid_at=(base + timedelta(minutes=i, seconds=30)).isoformat()
                       if status in ("paid", "token_issued") else None,
                       size_key=f"size{i}", ref_code=f"ref{i}" if i % 2 else None)

    def test_newest_first_with_state(self, init_test_db):
        self._seed(init_test_db)
        recent = _report()["recent"]
        assert [r["order_id"] for r in recent] == ["o5", "o4", "o3", "o2", "o1"]
        assert {r["order_id"]: r["state"] for r in recent} == {
            "o1": "applied", "o2": "reserved", "o3": "released", "o4": "applied", "o5": "released",
        }

    def test_item_fields(self, init_test_db):
        self._seed(init_test_db)
        item = next(r for r in _report()["recent"] if r["order_id"] == "o2")
        assert item["code"] == "BBB" and item["status"] == "pending"
        assert item["amount_rub"] == 209 and item["discount_rub"] == 90
        assert item["size_key"] == "size1" and item["ref_code"] == "ref1"
        assert item["paid_at"] is None and item["created_at"]
        # ровно эти поля: ни IP, ни User-Agent, ни платёжных данных, ни конфига баннера
        assert set(item) == {"order_id", "code", "state", "status", "amount_rub", "discount_rub",
                             "size_key", "ref_code", "created_at", "paid_at"}

    def test_recent_has_no_personal_data(self, init_test_db):
        self._seed(init_test_db)
        conn = _db(init_test_db)
        conn.execute("UPDATE web_orders SET accepted_ip = '203.0.113.7', accepted_ua = 'UA-secret', "
                     "yookassa_payment_id = 'pay-secret', config_json = '{\"text\": \"secret\"}'")
        conn.commit()
        conn.close()
        dumped = repr(_report())
        for secret in ("203.0.113.7", "UA-secret", "pay-secret", "secret"):
            assert secret not in dumped

    def test_limit(self, init_test_db):
        self._seed(init_test_db)
        report = _report(limit=2)
        assert [r["order_id"] for r in report["recent"]] == ["o5", "o4"] and report["recent_limit"] == 2

    @pytest.mark.parametrize("limit, expected", [(0, 1), (-5, 1), (10_000, 200)])
    def test_limit_is_clamped(self, init_test_db, limit, expected):
        assert _report(limit=limit)["recent_limit"] == expected

    def test_filter_by_code(self, init_test_db):
        self._seed(init_test_db)
        report = _report(code="BBB")
        assert [r["order_id"] for r in report["recent"]] == ["o5", "o2"]
        # цифры по кодам и итоги от фильтра не зависят
        assert len(report["codes"]) == 2 and report["totals"]["applied"] == 2

    def test_filter_by_unknown_code(self, init_test_db):
        self._seed(init_test_db)
        report = _report(code="NOSUCH")
        assert report["recent"] == [] and len(report["codes"]) == 2

    def test_filter_is_a_parameter_not_sql(self, init_test_db):
        self._seed(init_test_db)
        assert _report(code="X' OR '1'='1")["recent"] == []

    def test_all_orders_with_a_code_are_listed_even_if_the_code_row_is_gone(self, init_test_db):
        # заказ ссылается на код, которого нет в promo_codes (удалён руками): в recent он виден
        _put_order(init_test_db, "orphan", "GONE", "paid", 209, discount_rub=90, paid_at=_iso())
        report = _report()
        assert [r["order_id"] for r in report["recent"]] == ["orphan"] and report["codes"] == []


# ---------------------------------------------------------------------------
# Эндпоинт
# ---------------------------------------------------------------------------

class TestEndpoint:

    @pytest.mark.asyncio
    async def test_requires_admin_token(self, client):
        resp = await client.get("/api/admin/promo/report")
        assert resp.status_code in (401, 403)
        resp = await client.get("/api/admin/promo/report", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_code_filter_is_normalized(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "o1", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso())
        resp = await client.get("/api/admin/promo/report", params={"code": "  sale30 "}, headers=_auth())
        assert [r["order_id"] for r in resp.json()["recent"]] == ["o1"]

    @pytest.mark.asyncio
    async def test_blank_code_means_no_filter(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "o1", "SALE30", "paid", 209, discount_rub=90, paid_at=_iso())
        resp = await client.get("/api/admin/promo/report", params={"code": "   "}, headers=_auth())
        assert resp.status_code == 200 and len(resp.json()["recent"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"limit": "x"}, {"code": "A" * 21}])
    async def test_invalid_params_are_rejected(self, client, params):
        resp = await client.get("/api/admin/promo/report", params=params, headers=_auth())
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_price_comes_from_the_site_price(self, client, init_test_db, monkeypatch):
        from web.api.routers import admin
        monkeypatch.setattr(admin, "SITE_PDF_PRICE", 500)
        _add_promo(init_test_db, "SALE30")
        _put_order(init_test_db, "old", "SALE30", "paid", 209, paid_at=_iso())     # без discount_rub
        body = (await client.get("/api/admin/promo/report", headers=_auth())).json()
        assert body["price_rub"] == 500 and body["totals"]["discount_rub"] == 291
        assert body["totals"]["estimated"] == 1

    @pytest.mark.asyncio
    async def test_report_route_does_not_clash_with_toggle(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30")
        off = await client.post("/api/admin/promo/SALE30/active", json={"active": False}, headers=_auth())
        assert off.status_code == 200
        report = (await client.get("/api/admin/promo/report", headers=_auth())).json()
        assert _by_code(report)["SALE30"]["status"] == "disabled"


# ---------------------------------------------------------------------------
# Заказы через API: discount_rub записывается при создании
# ---------------------------------------------------------------------------

class TestOrderRecordsDiscount:

    @pytest.mark.asyncio
    async def test_free_partial_and_plain_orders(self, client, init_test_db):
        _add_promo(init_test_db, "FREE", discount=100, uses_left=1)
        _add_promo(init_test_db, "SALE30", discount=30, uses_left=3)

        free = (await client.post("/api/order", json={**PAYLOAD, "promo_code": "FREE"})).json()
        part = (await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})).json()
        plain = (await client.post("/api/order", json=PAYLOAD)).json()

        assert _row(init_test_db, free["order_id"])["discount_rub"] == PRICE
        assert _row(init_test_db, part["order_id"])["discount_rub"] == PRICE - 209
        assert _row(init_test_db, plain["order_id"])["discount_rub"] == 0

        report = (await client.get("/api/admin/promo/report", headers=_auth())).json()
        codes = _by_code(report)
        # бесплатный заказ уже оплачен, частичный ждёт оплаты в ЮKassa
        assert codes["FREE"]["used"] == 1 and codes["FREE"]["discount_rub"] == PRICE
        assert codes["SALE30"]["used"] == 0 and codes["SALE30"]["reserved"] == 1
        assert codes["SALE30"]["discount_rub"] == 0
        assert report["totals"]["discount_rub"] == PRICE and report["totals"]["estimated"] == 0
        assert {r["order_id"]: r["state"] for r in report["recent"]} == {
            free["order_id"]: "applied", part["order_id"]: "reserved",
        }

    @pytest.mark.asyncio
    async def test_price_change_does_not_rewrite_history(self, client, init_test_db, monkeypatch):
        from web.api.routers import admin
        from web.api.routers import order as order_router
        _add_promo(init_test_db, "FREE", discount=100, uses_left=2)

        first = (await client.post("/api/order", json={**PAYLOAD, "promo_code": "FREE"})).json()

        monkeypatch.setattr(order_router, "SITE_PDF_PRICE", 500)
        monkeypatch.setattr(admin, "SITE_PDF_PRICE", 500)
        second = (await client.post("/api/order", json={**PAYLOAD, "promo_code": "FREE"})).json()

        assert _row(init_test_db, first["order_id"])["discount_rub"] == 299
        assert _row(init_test_db, second["order_id"])["discount_rub"] == 500
        assert (await client.get("/api/admin/promo/report", headers=_auth())).json()["totals"]["discount_rub"] == 799

    @pytest.mark.asyncio
    async def test_failed_payment_leaves_the_code_unused(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3)
        broken = AsyncMock(side_effect=RuntimeError("down"))
        with patch("web.api.routers.order.create_payment", broken):
            await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})

        report = (await client.get("/api/admin/promo/report", headers=_auth())).json()
        sale = _by_code(report)["SALE30"]
        assert sale["unused"] is True and sale["abandoned"] == 1 and sale["reserved"] == 0
        assert report["totals"]["discount_rub"] == 0

    @pytest.mark.asyncio
    async def test_abandon_moves_order_from_reserved_to_abandoned(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3)
        order = (await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})).json()
        before = _by_code((await client.get("/api/admin/promo/report", headers=_auth())).json())["SALE30"]
        assert (before["reserved"], before["abandoned"], before["unused"]) == (1, 0, False)

        assert (await client.post(f"/api/order/{order['order_id']}/abandon")).status_code == 200

        report = (await client.get("/api/admin/promo/report", headers=_auth())).json()
        after = _by_code(report)["SALE30"]
        assert (after["reserved"], after["abandoned"], after["unused"]) == (0, 1, True)
        assert report["recent"][0]["state"] == "released"

    def test_late_payment_is_counted_once(self, init_test_db):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "SALE30", uses_left=3)
        _put_order(init_test_db, "o1", "SALE30", "pending", 209, discount_rub=90)
        promo_store.reserve("SALE30")
        promo_store.release_for_order("o1")         # окно оплаты закрыли
        assert _by_code(_report())["SALE30"]["abandoned"] == 1

        transition("o1", "webhook_paid")             # а клиент всё-таки оплатил

        report = _report()
        sale = _by_code(report)["SALE30"]
        assert (sale["used"], sale["abandoned"], sale["reserved"]) == (1, 0, 0)
        assert sale["discount_rub"] == 90 and report["recent"][0]["state"] == "applied"

    def test_paid_order_is_applied_even_if_the_use_could_not_be_taken_back(self, init_test_db):
        # лимит исчерпан, пока заказ считался вернувшимся; деньги получены — заказ учтён как оплаченный
        from web.api.routers.order import transition
        _add_promo(init_test_db, "LAST", uses_left=1, uses_total=1)
        _put_order(init_test_db, "late", "LAST", "pending", 209, discount_rub=90)
        promo_store.reserve("LAST")
        promo_store.release_for_order("late")
        _put_order(init_test_db, "other", "LAST", "pending", 209, discount_rub=90)
        promo_store.reserve("LAST")

        transition("late", "webhook_paid")

        last = _by_code(_report())["LAST"]
        assert (last["used"], last["reserved"], last["abandoned"]) == (1, 1, 0)
        assert last["discount_rub"] == 90


# ---------------------------------------------------------------------------
# Миграция
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE promo_codes (
    code       TEXT    PRIMARY KEY,
    uses_left  INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    discount   INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE web_orders (
    id                  TEXT    PRIMARY KEY,
    amount_rub          INTEGER NOT NULL,
    size_key            TEXT    NOT NULL,
    ref_code            TEXT,
    promo_code          TEXT,
    config_json         TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    created_at          TEXT    NOT NULL,
    paid_at             TEXT,
    yookassa_payment_id TEXT,
    tg_message_id       INTEGER,
    promo_released_at   TEXT
);
INSERT INTO promo_codes (code, uses_left, expires_at, discount) VALUES ('OLD50', 1, NULL, 50);
INSERT INTO web_orders (id, amount_rub, size_key, promo_code, config_json, status, created_at, paid_at)
VALUES ('old-1', 150, '1x0.5', 'OLD50', '{}', 'token_issued', '2026-01-01T10:00:00+00:00', '2026-01-01T10:05:00+00:00');
"""


class TestMigration:

    def test_column_is_added_and_old_orders_stay_in_the_report(self, tmp_db_path):
        conn = sqlite3.connect(tmp_db_path)
        conn.executescript(LEGACY_SCHEMA)
        conn.commit()
        conn.close()
        assert "discount_rub" not in {r[1] for r in sqlite3.connect(tmp_db_path).execute(
            "PRAGMA table_info(web_orders)")}

        init_db()
        init_db()   # повторный запуск безопасен

        conn = _db(tmp_db_path)
        assert "discount_rub" in {r[1] for r in conn.execute("PRAGMA table_info(web_orders)")}
        assert conn.execute("SELECT discount_rub FROM web_orders WHERE id = 'old-1'").fetchone()[0] is None
        conn.close()

        report = promo_store.promo_report(list_price_rub=PRICE)
        (code,) = report["codes"]
        assert code["code"] == "OLD50" and code["used"] == 1
        assert code["discount_rub"] == PRICE - 150 and code["estimated"] == 1
        assert report["recent"][0]["paid_at"] == "2026-01-01T10:05:00+00:00"


# ---------------------------------------------------------------------------
# Страница админки: связка разметки, скрипта и маршрута (jsdom в репозитории нет)
# ---------------------------------------------------------------------------

class TestAdminPageWiring:

    @staticmethod
    def _html() -> str:
        from pathlib import Path
        return (Path(__file__).parent.parent / "frontend" / "admin" / "index.html").read_text(encoding="utf-8")

    def test_every_element_the_script_looks_up_exists(self):
        import re
        html = self._html()
        ids = set(re.findall(r'\bid="([\w-]+)"', html))
        looked_up = set(re.findall(r'\$\("([\w-]+)"\)', html))
        assert looked_up, "не нашли ни одного $(\"id\") — регэксп устарел"
        # неверный id даёт null и роняет скрипт при загрузке страницы, то есть всю админку, а не один блок
        assert looked_up - ids == set()

    def test_report_ids_are_unique(self):
        import re
        ids = re.findall(r'\bid="(promo-[\w-]+)"', self._html())
        assert len(ids) == len(set(ids))

    def test_script_requests_the_route_the_api_serves(self):
        from web.api.routers import admin
        assert "/admin/promo/report" in {r.path for r in admin.router.routes}
        html = self._html()
        assert 'const API_BASE     = "/api";' in html      # nginx отдаёт API под /api/
        assert "${API_BASE}/admin/promo/report?" in html

    def test_view_switch_does_not_reuse_the_orders_filter_class(self):
        # обработчик фильтра заказов снимает active со ВСЕХ .filter-btn на странице:
        # переключатель «Коды | Отчёт» с этим классом переставал бы подсвечиваться
        import re
        for button in re.findall(r'<button[^>]*promo-view-btn[^>]*>', self._html()):
            assert "filter-btn" not in button
