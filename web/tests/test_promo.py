"""
test_promo.py
~~~~~~~~~~~~~
Промокоды со скидкой: services/promo_store.py, POST /api/order, админка.

Покрывает:
  - расчёт суммы (целочисленно, скидка < 100% никогда не даёт 0)
  - атомарный резерв: параллельные запросы не применяют последнее использование дважды
  - причины отказа: не найден / отключён / истёк / исчерпан — без побочных эффектов
  - POST /api/order: скидка < 100% идёт в ЮKassa с уменьшенной суммой и списывает
    использование (раньше списывался только код со скидкой 100%)
  - возврат использования, если платёж создать не удалось (идемпотентно)
  - admin API: авторизация, создание (генерация / свой код / срок / заметка),
    валидация, дубликат, список со статусами и счётчиками, включение и отключение
  - миграция: коды, добавленные вручную до админки, продолжают работать
  - peek() и POST /api/promo/check: скидка и цена без списания, те же причины отказа,
    что у заказа, цена совпадает с суммой реального заказа
  - возврат неоплаченных использований: release_stale (по возрасту заказа), POST
    /api/order/{id}/abandon, фоновый cleanup; поздняя оплата берёт использование обратно
    (reclaim_for_order из FSM); возврат не затрагивает оплаченные заказы, в том числе
    при гонке с оплатой
"""

import asyncio
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_yookassa_succeeded_payment

from web.api.db import init_db
from web.api.services import promo_store

PAYLOAD = {
    "size_key":     "1x0.5",
    "bg_color":     "Белый",
    "text_color":   "Черный",
    "font":         "Golos Text",
    "text_lines":   [{"text": "Аренда склада +7 914 000-00-00", "scale": 100}],
    "accept_terms": True,
}

PAYMENT = {"payment_id": "yk-promo-1", "confirmation_token": "ct-promo-1"}


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_promo(db_path: str, code: str, *, uses_left: int = 3, discount: int = 30,
               uses_total: int | None = None, expires_at: str | None = None,
               active: int = 1) -> None:
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO promo_codes (code, uses_left, expires_at, discount, uses_total, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, uses_left, expires_at, discount, uses_total if uses_total is not None else uses_left,
         _iso(), active),
    )
    conn.commit()
    conn.close()


def _uses_left(db_path: str, code: str) -> int:
    conn = _db(db_path)
    value = conn.execute("SELECT uses_left FROM promo_codes WHERE code = ?", (code,)).fetchone()[0]
    conn.close()
    return value


def _orders(db_path: str) -> list[sqlite3.Row]:
    conn = _db(db_path)
    rows = conn.execute("SELECT * FROM web_orders").fetchall()
    conn.close()
    return rows


def _insert_order(db_path: str, order_id: str, promo_code: str | None, status: str = "token_issued",
                  created_at: str | None = None) -> None:
    conn = _db(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, promo_code, config_json, status, created_at) "
        "VALUES (?, 209, '1x0.5', ?, '{}', ?, ?)",
        (order_id, promo_code, status, created_at or _iso()),
    )
    conn.commit()
    conn.close()


def _auth() -> dict:
    from web.api.routers import admin
    return {"Authorization": f"Bearer {admin.ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# Расчёт суммы
# ---------------------------------------------------------------------------

class TestCalcAmount:

    @pytest.mark.parametrize("discount, expected", [
        (0, 299), (10, 269), (30, 209), (50, 149), (99, 2), (100, 0),
    ])
    def test_amount_for_discount(self, discount, expected):
        assert promo_store.calc_amount(299, discount) == expected

    def test_partial_discount_never_makes_order_free(self):
        # 99 * 1 // 100 == 0, но 0 ₽ означает бесплатный флоу без ЮKassa
        assert promo_store.calc_amount(99, 99) == 1
        assert promo_store.calc_amount(100, 99) == 1

    def test_only_full_discount_is_free(self):
        assert promo_store.calc_amount(299, 100) == 0
        assert all(promo_store.calc_amount(299, d) > 0 for d in range(0, 100))


# ---------------------------------------------------------------------------
# Резерв использования
# ---------------------------------------------------------------------------

class TestReserve:

    def test_reserve_takes_one_use_and_returns_discount(self, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        assert promo_store.reserve("SALE30") == 30
        assert _uses_left(init_test_db, "SALE30") == 2

    @pytest.mark.parametrize("kwargs, message", [
        ({"uses_left": 0},                       "исчерпан"),
        ({"active": 0},                          "отключён"),
        ({"expires_at": _iso(-timedelta(days=1))}, "истёк"),
    ])
    def test_rejected_code_is_not_charged(self, init_test_db, kwargs, message):
        _add_promo(init_test_db, "BAD", **{"uses_left": 2, **kwargs})
        before = _uses_left(init_test_db, "BAD")
        with pytest.raises(promo_store.PromoError, match=message):
            promo_store.reserve("BAD")
        assert _uses_left(init_test_db, "BAD") == before

    def test_unknown_code(self, init_test_db):
        with pytest.raises(promo_store.PromoError, match="не найден"):
            promo_store.reserve("NOSUCH")

    def test_future_expiry_is_valid(self, init_test_db):
        _add_promo(init_test_db, "LATER", expires_at=_iso(timedelta(days=1)))
        assert promo_store.reserve("LATER") == 30

    @pytest.mark.parametrize("uses, threads", [(1, 12), (5, 20)])
    def test_concurrent_reserve_never_exceeds_limit(self, init_test_db, uses, threads):
        _add_promo(init_test_db, "RACE", uses_left=uses)

        def attempt(_):
            try:
                promo_store.reserve("RACE")
                return True
            except promo_store.PromoError:
                return False

        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = list(pool.map(attempt, range(threads)))

        assert sum(results) == uses
        assert _uses_left(init_test_db, "RACE") == 0


# ---------------------------------------------------------------------------
# Возврат использования
# ---------------------------------------------------------------------------

class TestRelease:

    def test_release_code_returns_use_but_not_above_limit(self, init_test_db):
        _add_promo(init_test_db, "BACK", uses_left=3, uses_total=3)
        promo_store.reserve("BACK")
        promo_store.release_code("BACK")
        assert _uses_left(init_test_db, "BACK") == 3
        promo_store.release_code("BACK")          # лишний вызов лимит не раздувает
        assert _uses_left(init_test_db, "BACK") == 3

    def test_release_for_order_is_idempotent(self, init_test_db):
        _add_promo(init_test_db, "ONCE", uses_left=3)
        promo_store.reserve("ONCE")
        _insert_order(init_test_db, "ord-rel-1", "ONCE", status="pending")

        assert promo_store.release_for_order("ord-rel-1") is True
        assert _uses_left(init_test_db, "ONCE") == 3
        assert promo_store.release_for_order("ord-rel-1") is False
        assert _uses_left(init_test_db, "ONCE") == 3

    def test_release_for_order_without_promo_does_nothing(self, init_test_db):
        _insert_order(init_test_db, "ord-rel-2", None, status="pending")
        assert promo_store.release_for_order("ord-rel-2") is False
        assert promo_store.release_for_order("no-such-order") is False


# ---------------------------------------------------------------------------
# POST /api/order
# ---------------------------------------------------------------------------

class TestOrderWithPromo:

    @pytest.mark.asyncio
    async def test_partial_discount_charges_reduced_amount_and_takes_a_use(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        pay = AsyncMock(return_value=PAYMENT)
        with patch("web.api.routers.order.create_payment", pay):
            resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["amount_rub"] == 209
        assert "free" not in body
        assert body["confirmation_token"] == PAYMENT["confirmation_token"]
        assert pay.await_args.kwargs["amount_rub"] == 209

        (row,) = _orders(init_test_db)
        assert row["amount_rub"] == 209 and row["promo_code"] == "SALE30" and row["status"] == "pending"
        assert _uses_left(init_test_db, "SALE30") == 2

    @pytest.mark.asyncio
    async def test_full_discount_is_free_and_takes_exactly_one_use(self, client, init_test_db):
        _add_promo(init_test_db, "FREE5", uses_left=5, discount=100)
        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "FREE5"})

        assert resp.status_code == 200
        assert resp.json()["free"] is True and resp.json()["amount_rub"] == 0
        assert _uses_left(init_test_db, "FREE5") == 4

    @pytest.mark.asyncio
    async def test_code_is_case_insensitive(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=1, discount=30)
        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "sale30"})
        assert resp.status_code == 200
        assert resp.json()["amount_rub"] == 209

    @pytest.mark.asyncio
    async def test_limited_uses_run_out(self, client, init_test_db):
        _add_promo(init_test_db, "TWICE", uses_left=2, discount=50)
        for _ in range(2):
            resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "TWICE"})
            assert resp.status_code == 200
            assert resp.json()["amount_rub"] == 149

        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "TWICE"})
        assert resp.status_code == 422
        assert "исчерпан" in resp.json()["detail"]
        assert len(_orders(init_test_db)) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs, message", [
        ({"active": 0},                            "отключён"),
        ({"expires_at": _iso(-timedelta(hours=1))}, "истёк"),
        ({"uses_left": 0},                         "исчерпан"),
    ])
    async def test_unusable_code_is_rejected_without_order(self, client, init_test_db, kwargs, message):
        _add_promo(init_test_db, "NOPE", **{"uses_left": 3, **kwargs})
        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "NOPE"})
        assert resp.status_code == 422
        assert message in resp.json()["detail"]
        assert _orders(init_test_db) == []

    @pytest.mark.asyncio
    async def test_unknown_code_is_rejected(self, client, init_test_db):
        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "NOSUCH"})
        assert resp.status_code == 422
        assert "не найден" in resp.json()["detail"]
        assert _orders(init_test_db) == []

    @pytest.mark.asyncio
    async def test_order_without_promo_is_unchanged(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3)
        resp = await client.post("/api/order", json=PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["amount_rub"] == 299
        assert _uses_left(init_test_db, "SALE30") == 3

    @pytest.mark.asyncio
    async def test_failed_payment_returns_the_use(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        broken = AsyncMock(side_effect=RuntimeError("ЮKassa недоступна"))
        with patch("web.api.routers.order.create_payment", broken):
            resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})

        assert resp.status_code == 500
        assert _uses_left(init_test_db, "SALE30") == 3
        (row,) = _orders(init_test_db)
        assert row["promo_released_at"] is not None
        # повторный возврат по тому же заказу не раздувает счётчик
        assert promo_store.release_for_order(row["id"]) is False
        assert _uses_left(init_test_db, "SALE30") == 3

    @pytest.mark.asyncio
    async def test_failed_payment_without_promo_is_not_affected(self, client, init_test_db):
        broken = AsyncMock(side_effect=RuntimeError("ЮKassa недоступна"))
        with patch("web.api.routers.order.create_payment", broken):
            resp = await client.post("/api/order", json=PAYLOAD)
        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_admin_sees_promo_on_discounted_paid_order(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        push = AsyncMock()
        with patch("web.api.routers.order.push_new_order", push):
            resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})
        assert resp.status_code == 200
        oid, amount, _size, promo = push.await_args.args
        assert oid == resp.json()["order_id"] and amount == 209 and promo == "SALE30"


class TestNotificationText:

    @pytest.mark.asyncio
    async def test_push_for_discounted_order_awaits_payment(self, client):
        from web.api.services import push_notify
        with patch.object(push_notify, "_deliver", AsyncMock()) as deliver:
            await push_notify.push_new_order("abcdef12-0000", 209, "1x0.5", promo_code="SALE30")
        payload = deliver.await_args.args[0]
        assert "209" in payload["title"] and "ожидает оплаты" in payload["body"]
        assert "SALE30" in payload["body"] and "выдан автоматически" not in payload["body"]


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------

class TestAdminAuth:

    @pytest.mark.asyncio
    async def test_all_endpoints_require_token(self, client):
        for method, url, body in [
            ("POST", "/api/admin/promo", {"discount": 30, "uses": 1}),
            ("GET",  "/api/admin/promo", None),
            ("GET",  "/api/admin/promo/report", None),
            ("POST", "/api/admin/promo/ANY/active", {"active": False}),
        ]:
            resp = await client.request(method, url, json=body)
            assert resp.status_code in (401, 403), (method, url)

            resp = await client.request(method, url, json=body, headers={"Authorization": "Bearer nope"})
            assert resp.status_code == 403, (method, url)


class TestAdminCreate:

    @pytest.mark.asyncio
    async def test_generated_code(self, client, init_test_db):
        resp = await client.post("/api/admin/promo", json={"discount": 30, "uses": 3}, headers=_auth())
        assert resp.status_code == 200
        body = resp.json()

        assert len(body["code"]) == 8
        assert set(body["code"]) <= set(promo_store._CODE_ALPHABET)
        assert body["discount"] == 30 and body["uses_total"] == 3 and body["uses_left"] == 3
        assert body["active"] is True and body["expires_at"] is None

        conn = _db(init_test_db)
        row = conn.execute("SELECT * FROM promo_codes WHERE code = ?", (body["code"],)).fetchone()
        conn.close()
        assert row["uses_left"] == 3 and row["uses_total"] == 3 and row["discount"] == 30
        assert row["created_at"] is not None and row["active"] == 1

    @pytest.mark.asyncio
    async def test_generated_codes_differ(self, client):
        codes = set()
        for _ in range(5):
            r = await client.post("/api/admin/promo", json={"discount": 10, "uses": 1}, headers=_auth())
            codes.add(r.json()["code"])
        assert len(codes) == 5

    @pytest.mark.asyncio
    async def test_custom_code_expiry_and_note(self, client, init_test_db):
        resp = await client.post(
            "/api/admin/promo",
            json={"discount": 100, "uses": 5, "code": " summer26 ", "expires_in_days": 7,
                  "note": "  Иван, типография  "},
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "SUMMER26"
        assert body["note"] == "Иван, типография"
        expires = datetime.fromisoformat(body["expires_at"])
        assert timedelta(days=6, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=7)

    @pytest.mark.asyncio
    async def test_blank_code_is_generated(self, client):
        resp = await client.post("/api/admin/promo", json={"discount": 10, "uses": 1, "code": "  "}, headers=_auth())
        assert resp.status_code == 200 and len(resp.json()["code"]) == 8

    @pytest.mark.asyncio
    async def test_duplicate_code_is_conflict(self, client, init_test_db):
        body = {"discount": 30, "uses": 1, "code": "DUP1"}
        assert (await client.post("/api/admin/promo", json=body, headers=_auth())).status_code == 200
        resp = await client.post("/api/admin/promo", json={**body, "discount": 50}, headers=_auth())
        assert resp.status_code == 409
        assert _uses_left(init_test_db, "DUP1") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [
        {"discount": 0,   "uses": 1},
        {"discount": 101, "uses": 1},
        {"discount": 30,  "uses": 0},
        {"discount": 30,  "uses": 1001},
        {"discount": 30,  "uses": 1, "code": "a"},
        {"discount": 30,  "uses": 1, "code": "AB-CD"},
        {"discount": 30,  "uses": 1, "code": "A" * 21},
        {"discount": 30,  "uses": 1, "expires_in_days": 0},
        {"discount": 30,  "uses": 1, "note": "x" * 101},
        {"uses": 1},
    ])
    async def test_invalid_input_is_rejected(self, client, init_test_db, body):
        resp = await client.post("/api/admin/promo", json=body, headers=_auth())
        assert resp.status_code == 422
        conn = _db(init_test_db)
        assert conn.execute("SELECT COUNT(*) FROM promo_codes").fetchone()[0] == 0
        conn.close()

    @pytest.mark.asyncio
    async def test_issued_code_works_end_to_end(self, client, init_test_db):
        created = await client.post(
            "/api/admin/promo", json={"discount": 100, "uses": 1}, headers=_auth()
        )
        code = created.json()["code"]

        first = await client.post("/api/order", json={**PAYLOAD, "promo_code": code})
        assert first.status_code == 200 and first.json()["free"] is True

        second = await client.post("/api/order", json={**PAYLOAD, "promo_code": code})
        assert second.status_code == 422 and "исчерпан" in second.json()["detail"]


class TestAdminList:

    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        resp = await client.get("/api/admin/promo", headers=_auth())
        assert resp.status_code == 200 and resp.json() == {"promos": []}

    @pytest.mark.asyncio
    async def test_statuses_counters_and_order(self, client, init_test_db):
        _add_promo(init_test_db, "LIVE", uses_left=2, uses_total=3, discount=30)
        _add_promo(init_test_db, "EMPTY", uses_left=0, uses_total=1)
        _add_promo(init_test_db, "OLDDATE", uses_left=1, expires_at=_iso(-timedelta(days=1)))
        _add_promo(init_test_db, "OFF", uses_left=1, active=0)
        # «исчерпан» и «отключён» одновременно показывается как отключён
        _add_promo(init_test_db, "OFFEMPTY", uses_left=0, active=0)

        _insert_order(init_test_db, "o-paid", "LIVE", status="token_issued")
        _insert_order(init_test_db, "o-wait", "LIVE", status="pending")
        conn = _db(init_test_db)
        conn.execute(
            "INSERT INTO promo_codes (code, uses_left, discount) VALUES ('MANUAL', 4, 50)"
        )   # добавлен руками до админки: без created_at и uses_total
        conn.commit()
        conn.close()

        resp = await client.get("/api/admin/promo", headers=_auth())
        assert resp.status_code == 200
        promos = {p["code"]: p for p in resp.json()["promos"]}

        assert promos["LIVE"]["status"] == "active"
        assert promos["LIVE"]["uses_left"] == 2 and promos["LIVE"]["uses_total"] == 3
        assert promos["LIVE"]["used"] == 1 and promos["LIVE"]["reserved"] == 1
        assert promos["EMPTY"]["status"] == "exhausted"
        assert promos["OLDDATE"]["status"] == "expired"
        assert promos["OFF"]["status"] == "disabled" and promos["OFF"]["active"] is False
        assert promos["OFFEMPTY"]["status"] == "disabled"
        assert promos["MANUAL"]["status"] == "active"
        assert promos["MANUAL"]["uses_total"] is None and promos["MANUAL"]["created_at"] is None

        assert resp.json()["promos"][-1]["code"] == "MANUAL"   # коды без даты — в конце

    @pytest.mark.asyncio
    async def test_released_order_is_not_counted_as_reserved(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        broken = AsyncMock(side_effect=RuntimeError("down"))
        with patch("web.api.routers.order.create_payment", broken):
            await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})

        resp = await client.get("/api/admin/promo", headers=_auth())
        (promo,) = resp.json()["promos"]
        assert promo["reserved"] == 0 and promo["uses_left"] == 3


class TestAdminToggle:

    @pytest.mark.asyncio
    async def test_disable_blocks_orders_and_enable_restores(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)

        off = await client.post("/api/admin/promo/SALE30/active", json={"active": False}, headers=_auth())
        assert off.status_code == 200 and off.json() == {"code": "SALE30", "active": False}

        blocked = await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})
        assert blocked.status_code == 422 and "отключён" in blocked.json()["detail"]
        assert _uses_left(init_test_db, "SALE30") == 3

        on = await client.post("/api/admin/promo/sale30/active", json={"active": True}, headers=_auth())
        assert on.status_code == 200 and on.json()["code"] == "SALE30"

        ok = await client.post("/api/order", json={**PAYLOAD, "promo_code": "SALE30"})
        assert ok.status_code == 200 and ok.json()["amount_rub"] == 209

    @pytest.mark.asyncio
    async def test_unknown_code_is_404(self, client):
        resp = await client.post("/api/admin/promo/NOSUCH/active", json={"active": False}, headers=_auth())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Миграция: коды, добавленные вручную до админки
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
    tg_message_id       INTEGER
);
INSERT INTO promo_codes (code, uses_left, expires_at, discount) VALUES ('OLD50', 2, NULL, 50);
"""


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return cols


class TestMigration:

    def test_legacy_promo_rows_survive_and_keep_working(self, tmp_db_path):
        conn = sqlite3.connect(tmp_db_path)
        conn.executescript(LEGACY_SCHEMA)
        conn.commit()
        conn.close()
        assert "active" not in _columns(tmp_db_path, "promo_codes")

        init_db()
        init_db()   # повторный запуск безопасен

        assert {"uses_total", "created_at", "note", "active"} <= _columns(tmp_db_path, "promo_codes")
        assert "promo_released_at" in _columns(tmp_db_path, "web_orders")

        conn = _db(tmp_db_path)
        row = conn.execute("SELECT * FROM promo_codes WHERE code = 'OLD50'").fetchone()
        conn.close()
        assert (row["uses_left"], row["discount"], row["active"]) == (2, 50, 1)
        assert row["uses_total"] is None and row["created_at"] is None

        assert promo_store.reserve("OLD50") == 50
        assert _uses_left(tmp_db_path, "OLD50") == 1
        promo_store.release_code("OLD50")          # лимит неизвестен — возврат всё равно работает
        assert _uses_left(tmp_db_path, "OLD50") == 2

        (listed,) = promo_store.list_promos()
        assert listed["code"] == "OLD50" and listed["status"] == "active"


# ---------------------------------------------------------------------------
# peek() и POST /api/promo/check
# ---------------------------------------------------------------------------

class TestPeek:

    def test_peek_returns_discount_and_never_charges(self, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=2, discount=30)
        for _ in range(5):
            assert promo_store.peek("SALE30") == 30
        assert _uses_left(init_test_db, "SALE30") == 2

    @pytest.mark.parametrize("kwargs", [
        {"uses_left": 0},
        {"active": 0},
        {"expires_at": _iso(-timedelta(days=1))},
    ])
    def test_peek_and_reserve_explain_refusal_identically(self, init_test_db, kwargs):
        _add_promo(init_test_db, "BAD", **{"uses_left": 2, **kwargs})
        with pytest.raises(promo_store.PromoError) as peeked:
            promo_store.peek("BAD")
        with pytest.raises(promo_store.PromoError) as reserved:
            promo_store.reserve("BAD")
        assert str(peeked.value) == str(reserved.value)

    def test_peek_unknown_code(self, init_test_db):
        with pytest.raises(promo_store.PromoError, match="не найден"):
            promo_store.peek("NOSUCH")


class TestCheckEndpoint:

    @pytest.mark.asyncio
    async def test_partial_discount(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        resp = await client.post("/api/promo/check", json={"code": "SALE30"})
        assert resp.status_code == 200
        assert resp.json() == {"valid": True, "discount": 30, "price": 299, "final_price": 209, "free": False}

    @pytest.mark.asyncio
    async def test_full_discount_is_free(self, client, init_test_db):
        _add_promo(init_test_db, "FREE1", uses_left=1, discount=100)
        body = (await client.post("/api/promo/check", json={"code": "FREE1"})).json()
        assert body == {"valid": True, "discount": 100, "price": 299, "final_price": 0, "free": True}

    @pytest.mark.asyncio
    async def test_no_auth_required_and_input_is_normalized(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=1, discount=30)
        resp = await client.post("/api/promo/check", json={"code": "  sale30 "})
        assert resp.status_code == 200 and resp.json()["valid"] is True

    @pytest.mark.asyncio
    async def test_check_never_charges_a_use(self, client, init_test_db):
        _add_promo(init_test_db, "ONE", uses_left=1, discount=30)
        for _ in range(10):
            assert (await client.post("/api/promo/check", json={"code": "ONE"})).json()["valid"] is True
        assert _uses_left(init_test_db, "ONE") == 1
        assert _orders(init_test_db) == []
        # и единственное использование всё ещё можно потратить на заказ
        order = await client.post("/api/order", json={**PAYLOAD, "promo_code": "ONE"})
        assert order.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs, message", [
        ({"uses_left": 0},                          "исчерпан"),
        ({"active": 0},                             "отключён"),
        ({"expires_at": _iso(-timedelta(hours=1))}, "истёк"),
    ])
    async def test_unusable_code_says_why(self, client, init_test_db, kwargs, message):
        _add_promo(init_test_db, "NOPE", **{"uses_left": 3, **kwargs})
        body = (await client.post("/api/promo/check", json={"code": "NOPE"})).json()
        assert body["valid"] is False and message in body["message"]
        assert "discount" not in body and "final_price" not in body

    @pytest.mark.asyncio
    async def test_unknown_code(self, client):
        body = (await client.post("/api/promo/check", json={"code": "NOSUCH"})).json()
        assert body == {"valid": False, "message": "Промокод не найден"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["", " ", "a", "AB-CD", "КОД123", "A" * 21, "SALE 30"])
    async def test_malformed_code_is_not_found_not_an_error(self, client, code):
        resp = await client.post("/api/promo/check", json={"code": code})
        assert resp.status_code == 200
        assert resp.json() == {"valid": False, "message": "Промокод не найден"}

    @pytest.mark.asyncio
    async def test_oversized_or_missing_code_is_rejected(self, client):
        assert (await client.post("/api/promo/check", json={"code": "A" * 65})).status_code == 422
        assert (await client.post("/api/promo/check", json={})).status_code == 422

    @pytest.mark.asyncio
    async def test_price_follows_site_price(self, client, init_test_db, monkeypatch):
        from web.api.routers import promo as promo_router
        monkeypatch.setattr(promo_router, "SITE_PDF_PRICE", 500)
        _add_promo(init_test_db, "SALE30", uses_left=1, discount=30)
        body = (await client.post("/api/promo/check", json={"code": "SALE30"})).json()
        assert body["price"] == 500 and body["final_price"] == 350

    @pytest.mark.asyncio
    @pytest.mark.parametrize("discount", [10, 30, 50, 99])
    async def test_shown_price_equals_charged_amount(self, client, init_test_db, discount):
        code = f"D{discount}"
        _add_promo(init_test_db, code, uses_left=2, discount=discount)
        shown = (await client.post("/api/promo/check", json={"code": code})).json()["final_price"]
        order = await client.post("/api/order", json={**PAYLOAD, "promo_code": code})
        assert order.status_code == 200
        assert order.json()["amount_rub"] == shown

    @pytest.mark.asyncio
    async def test_code_issued_by_admin_is_checkable(self, client, init_test_db):
        created = await client.post("/api/admin/promo", json={"discount": 20, "uses": 1}, headers=_auth())
        code = created.json()["code"]
        body = (await client.post("/api/promo/check", json={"code": code})).json()
        assert body["valid"] is True and body["final_price"] == 239   # 299 * 80 // 100


# ---------------------------------------------------------------------------
# Возврат неоплаченных использований
# ---------------------------------------------------------------------------

def _order(db_path: str, order_id: str) -> sqlite3.Row:
    conn = _db(db_path)
    row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def _reserved_order(db_path: str, order_id: str, code: str, *, status: str = "pending",
                    age: timedelta = timedelta()) -> None:
    """Заказ, как его оставляет create_order: использование списано, заказ создан `age` назад."""
    promo_store.reserve(code)
    _insert_order(db_path, order_id, code, status=status, created_at=_iso(-age))


class TestReleaseStale:

    def test_returns_only_old_unpaid_orders_with_a_held_use(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=6, uses_total=6)
        hour = promo_store.RESERVE_TTL_MINUTES
        old = timedelta(minutes=hour + 1)
        _reserved_order(init_test_db, "old-pending", "SALE", age=old)                       # вернуть
        _reserved_order(init_test_db, "new-pending", "SALE", age=timedelta(minutes=5))      # ещё ждём оплату
        _reserved_order(init_test_db, "old-paid", "SALE", status="paid", age=old)           # оплачен
        _reserved_order(init_test_db, "old-issued", "SALE", status="token_issued", age=old)  # PDF выдан
        _insert_order(init_test_db, "old-nopromo", None, status="pending", created_at=_iso(-old))
        assert _uses_left(init_test_db, "SALE") == 2

        assert promo_store.release_stale() == 1

        assert _uses_left(init_test_db, "SALE") == 3
        assert _order(init_test_db, "old-pending")["promo_released_at"] is not None
        for oid in ("new-pending", "old-paid", "old-issued", "old-nopromo"):
            assert _order(init_test_db, oid)["promo_released_at"] is None, oid

    def test_is_idempotent(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE", age=timedelta(hours=3))
        assert promo_store.release_stale() == 1
        assert promo_store.release_stale() == 0
        assert _uses_left(init_test_db, "SALE") == 3

    def test_already_released_order_is_not_counted_twice(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE", age=timedelta(hours=3))
        assert promo_store.release_for_order("o1") is True        # клиент закрыл окно оплаты
        assert promo_store.release_stale() == 0                   # а таймер уже ничего не вернёт
        assert _uses_left(init_test_db, "SALE") == 3

    def test_boundary_is_the_ttl(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=3)
        ttl = promo_store.RESERVE_TTL_MINUTES
        _reserved_order(init_test_db, "young", "SALE", age=timedelta(minutes=ttl - 1))
        _reserved_order(init_test_db, "stale", "SALE", age=timedelta(minutes=ttl + 1))
        assert promo_store.release_stale() == 1
        assert _order(init_test_db, "young")["promo_released_at"] is None
        assert _order(init_test_db, "stale")["promo_released_at"] is not None

    def test_custom_threshold(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE", age=timedelta(minutes=30))
        assert promo_store.release_stale(older_than_minutes=60) == 0
        assert promo_store.release_stale(older_than_minutes=10) == 1

    def test_never_raises_the_counter_above_the_issued_limit(self, init_test_db):
        # код выдан на 1 использование, счётчик уже на максимуме (его могли поправить руками)
        _add_promo(init_test_db, "ONE", uses_left=1, uses_total=1)
        _insert_order(init_test_db, "o1", "ONE", status="pending", created_at=_iso(-timedelta(hours=3)))
        promo_store.release_stale()
        assert _uses_left(init_test_db, "ONE") == 1

    def test_ttl_is_not_shorter_than_an_hour(self):
        # ЮKassa не называет срок жизни платежа; в меньшую сторону ошибаться безопасно
        # (поздняя оплата берёт использование обратно), но не короче времени на оплату
        assert promo_store.RESERVE_TTL_MINUTES >= 60


class TestReleaseForOrderOnlyUnpaid:

    @pytest.mark.parametrize("status", ["paid", "token_issued", "expired"])
    def test_non_pending_order_keeps_its_use(self, init_test_db, status):
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE", status=status)
        assert promo_store.release_for_order("o1") is False
        assert _uses_left(init_test_db, "SALE") == 2
        assert _order(init_test_db, "o1")["promo_released_at"] is None


class TestReclaim:

    def test_late_payment_takes_the_use_back(self, init_test_db):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE")
        assert promo_store.release_for_order("o1") is True
        assert _uses_left(init_test_db, "SALE") == 3

        transition("o1", "webhook_paid")

        assert _order(init_test_db, "o1")["status"] == "paid"
        assert _order(init_test_db, "o1")["promo_released_at"] is None
        assert _uses_left(init_test_db, "SALE") == 2

    def test_normal_payment_does_not_charge_twice(self, init_test_db):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE")
        transition("o1", "webhook_paid")
        assert _uses_left(init_test_db, "SALE") == 2

    def test_repeated_paid_event_does_not_charge_twice(self, init_test_db):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE")
        promo_store.release_for_order("o1")
        transition("o1", "webhook_paid")
        transition("o1", "webhook_paid")      # ЮKassa повторяет webhook
        assert _uses_left(init_test_db, "SALE") == 2

    def test_exhausted_code_still_lets_the_paid_order_through(self, init_test_db, caplog):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "LAST", uses_left=1, uses_total=1)
        _reserved_order(init_test_db, "late", "LAST")
        promo_store.release_for_order("late")                 # окно закрыли: использование вернулось
        _reserved_order(init_test_db, "other", "LAST")        # его забрал другой клиент
        assert _uses_left(init_test_db, "LAST") == 0

        with caplog.at_level(logging.WARNING):
            transition("late", "webhook_paid")

        # деньги получены, заказ оплачен; счётчик не уходит в минус
        assert _order(init_test_db, "late")["status"] == "paid"
        assert _uses_left(init_test_db, "LAST") == 0
        assert _order(init_test_db, "late")["promo_released_at"] is not None
        assert any("LAST" in r.message and "late" in r.message and r.levelno == logging.WARNING
                   for r in caplog.records)

    def test_reclaim_failure_does_not_break_the_payment(self, init_test_db, caplog):
        from web.api.routers.order import transition
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE")
        with patch.object(promo_store, "reclaim_for_order", side_effect=RuntimeError("db locked")), \
                caplog.at_level(logging.ERROR):
            assert transition("o1", "webhook_paid") == "paid"
        assert _order(init_test_db, "o1")["status"] == "paid"
        assert any("o1" in r.message for r in caplog.records if r.levelno >= logging.ERROR)

    def test_order_without_promo_is_untouched(self, init_test_db):
        from web.api.routers.order import transition
        _insert_order(init_test_db, "o1", None, status="pending")
        assert transition("o1", "webhook_paid") == "paid"

    def test_reclaim_for_order_edge_cases(self, init_test_db):
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "o1", "SALE")
        assert promo_store.reclaim_for_order("o1") is False         # использование не возвращали
        assert promo_store.reclaim_for_order("no-such") is False
        _insert_order(init_test_db, "o2", None, status="pending")
        assert promo_store.reclaim_for_order("o2") is False
        promo_store.release_for_order("o1")
        assert promo_store.reclaim_for_order("o1") is True
        assert promo_store.reclaim_for_order("o1") is False         # второй раз ничего не берёт
        assert _uses_left(init_test_db, "SALE") == 2

    def test_abandon_racing_with_payment_never_loses_or_doubles_a_use(self, init_test_db):
        from web.api.routers.order import transition
        rounds = 15
        _add_promo(init_test_db, "RACE", uses_left=rounds, uses_total=rounds)
        for i in range(rounds):
            _reserved_order(init_test_db, f"r{i}", "RACE")

        def release_and_pay(i: int) -> None:
            gate = threading.Barrier(2)

            def pay():
                gate.wait()
                transition(f"r{i}", "webhook_paid")

            def abandon():
                gate.wait()
                promo_store.release_for_order(f"r{i}")

            threads = [threading.Thread(target=pay), threading.Thread(target=abandon)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        for i in range(rounds):
            release_and_pay(i)

        # при любом порядке оплаченный заказ держит своё использование ровно один раз
        assert _uses_left(init_test_db, "RACE") == 0
        for i in range(rounds):
            row = _order(init_test_db, f"r{i}")
            assert row["status"] == "paid" and row["promo_released_at"] is None, i


class TestAbandonEndpoint:

    @staticmethod
    async def _order_with(client, code: str) -> str:
        resp = await client.post("/api/order", json={**PAYLOAD, "promo_code": code})
        assert resp.status_code == 200, resp.text
        return resp.json()["order_id"]

    @pytest.mark.asyncio
    async def test_abandon_returns_the_use(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        order_id = await self._order_with(client, "SALE30")
        assert _uses_left(init_test_db, "SALE30") == 2

        resp = await client.post(f"/api/order/{order_id}/abandon")

        assert resp.status_code == 200 and resp.json() == {"released": True}
        assert _uses_left(init_test_db, "SALE30") == 3
        assert _order(init_test_db, order_id)["promo_released_at"] is not None

    @pytest.mark.asyncio
    async def test_repeated_abandon_is_harmless(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        order_id = await self._order_with(client, "SALE30")
        assert (await client.post(f"/api/order/{order_id}/abandon")).json() == {"released": True}
        for _ in range(3):
            resp = await client.post(f"/api/order/{order_id}/abandon")
            assert resp.status_code == 200 and resp.json() == {"released": False}
        assert _uses_left(init_test_db, "SALE30") == 3

    @pytest.mark.asyncio
    async def test_single_use_code_can_be_used_again_after_abandon(self, client, init_test_db):
        _add_promo(init_test_db, "ONCE", uses_left=1, discount=50)
        first = await self._order_with(client, "ONCE")
        blocked = await client.post("/api/order", json={**PAYLOAD, "promo_code": "ONCE"})
        assert blocked.status_code == 422                       # единственное использование занято

        await client.post(f"/api/order/{first}/abandon")

        again = await client.post("/api/order", json={**PAYLOAD, "promo_code": "ONCE"})
        assert again.status_code == 200 and again.json()["amount_rub"] == 149

    @pytest.mark.asyncio
    async def test_abandon_touches_only_its_own_order(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        a = await self._order_with(client, "SALE30")
        b = await self._order_with(client, "SALE30")
        assert _uses_left(init_test_db, "SALE30") == 1

        await client.post(f"/api/order/{a}/abandon")

        assert _uses_left(init_test_db, "SALE30") == 2
        assert _order(init_test_db, b)["promo_released_at"] is None

    @pytest.mark.asyncio
    async def test_paid_order_keeps_its_use(self, client, init_test_db):
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        order_id = await self._order_with(client, "SALE30")
        from web.api.routers.order import transition
        transition(order_id, "webhook_paid")

        resp = await client.post(f"/api/order/{order_id}/abandon")

        assert resp.status_code == 200 and resp.json() == {"released": False}
        assert _uses_left(init_test_db, "SALE30") == 2

    @pytest.mark.asyncio
    async def test_order_without_promo(self, client, init_test_db):
        resp = await client.post("/api/order", json=PAYLOAD)
        order_id = resp.json()["order_id"]
        resp = await client.post(f"/api/order/{order_id}/abandon")
        assert resp.status_code == 200 and resp.json() == {"released": False}

    @pytest.mark.asyncio
    async def test_unknown_order_is_404(self, client):
        resp = await client.post("/api/order/no-such-order/abandon")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_is_not_allowed(self, client):
        assert (await client.get("/api/order/some-id/abandon")).status_code == 405

    @pytest.mark.asyncio
    async def test_late_payment_after_abandon_reaches_the_customer(self, client, init_test_db):
        """Окно закрыли, но человек успел оплатить: PDF выдаётся, использование снова за заказом."""
        _add_promo(init_test_db, "SALE30", uses_left=3, discount=30)
        order_id = await self._order_with(client, "SALE30")
        await client.post(f"/api/order/{order_id}/abandon")
        assert _uses_left(init_test_db, "SALE30") == 3

        body = json.dumps({
            "type": "notification", "event": "payment.succeeded",
            "object": {"id": "yk-late", "status": "succeeded", "metadata": {"order_id": order_id}},
        }).encode()
        verified = AsyncMock(return_value=make_yookassa_succeeded_payment(order_id, "yk-late", 209))
        with patch("web.api.routers.payment.verify_yookassa_payment", verified):
            resp = await client.post("/api/payment/callback", content=body,
                                     headers={"Content-Type": "application/json"})

        assert resp.status_code == 200
        assert _order(init_test_db, order_id)["status"] == "token_issued"
        assert _order(init_test_db, order_id)["promo_released_at"] is None
        assert _uses_left(init_test_db, "SALE30") == 2


class TestCleanupLoop:

    @pytest.mark.asyncio
    async def test_background_cleanup_returns_stale_uses(self, client, init_test_db, monkeypatch):
        from web.api import main
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "stale", "SALE", age=timedelta(hours=3))
        _reserved_order(init_test_db, "fresh", "SALE", age=timedelta(minutes=2))
        monkeypatch.setattr(main, "CLEANUP_INTERVAL", 0)
        monkeypatch.setattr(main.batch_worker, "cleanup_old_batches", lambda: 0)

        task = asyncio.create_task(main._cleanup_loop())
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if _order(init_test_db, "stale")["promo_released_at"] is not None:
                    break
        finally:
            task.cancel()
            await task    # цикл гасит CancelledError сам

        assert _order(init_test_db, "stale")["promo_released_at"] is not None
        assert _order(init_test_db, "fresh")["promo_released_at"] is None
        assert _uses_left(init_test_db, "SALE") == 2             # 3 − 2 резерва + 1 возврат

    @pytest.mark.asyncio
    async def test_loop_survives_a_failing_release(self, client, init_test_db, monkeypatch):
        from web.api import main
        _add_promo(init_test_db, "SALE", uses_left=3)
        _reserved_order(init_test_db, "stale", "SALE", age=timedelta(hours=3))
        monkeypatch.setattr(main, "CLEANUP_INTERVAL", 0)
        monkeypatch.setattr(main.batch_worker, "cleanup_old_batches", lambda: 0)
        real = promo_store.release_stale
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real()

        monkeypatch.setattr(promo_store, "release_stale", flaky)
        task = asyncio.create_task(main._cleanup_loop())
        try:
            for _ in range(150):
                await asyncio.sleep(0.02)
                if _order(init_test_db, "stale")["promo_released_at"] is not None:
                    break
        finally:
            task.cancel()
            await task

        assert calls["n"] >= 2
        assert _order(init_test_db, "stale")["promo_released_at"] is not None
