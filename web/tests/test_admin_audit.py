"""
test_admin_audit.py
~~~~~~~~~~~~~~~~~~~
GET /api/admin/order/{id}/audit и GET /api/admin/audit/by-payment/{payment_id}.

Покрывает:
  - авторизация: без токена / с чужим токеном нельзя
  - полный заказ: акцепт (версия, время, IP, UA), оплата, платёж ЮKassa
  - правка: текущий и оригинальный текст, окно правки
  - выдача: счётчики скачиваний, истёкшие/исчерпанные токены, токен не раскрывается целиком
  - старый заказ без акцепта: recorded=false, без ошибок
  - поиск по ID платежа ЮKassa
  - 404 для неизвестного заказа/платежа
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

CONFIG = {
    "size_key":   "1x0.5",
    "bg_color":   "Белый",
    "text_color": "Черный",
    "font":       "Golos Text",
    "text_lines": [{"text": "Аренда склада", "scale": 1.0}, {"text": "+7 914 000-00-00", "scale": 0.8}],
}
ORIGINAL = {**CONFIG, "text_lines": [{"text": "Аренда склада", "scale": 1.0}, {"text": "+7 914 000-00-0O", "scale": 0.8}]}

FULL_TOKEN_A = "a1b2c3d4" + "0" * 56   # исчерпан (3 скачивания)
FULL_TOKEN_B = "e5f6a7b8" + "1" * 56   # живой, 1 скачивание


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _insert_order(db_path: str, order_id: str, **over) -> None:
    row = {
        "status": "token_issued",
        "paid_at": _iso(-timedelta(hours=1)),
        "yookassa_payment_id": f"yk-{order_id}",
        "config_json": json.dumps(CONFIG, ensure_ascii=False),
        "offer_version": "2026-10-03",
        "accepted_at": _iso(-timedelta(hours=1, minutes=5)),
        "accepted_ip": "203.0.113.7",
        "accepted_ua": "TestBrowser/1.0",
        "amended_at": None,
        "original_config_json": None,
        **over,
    }
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO web_orders
          (id, amount_rub, size_key, promo_code, ref_code, config_json, status, created_at, paid_at,
           yookassa_payment_id, offer_version, accepted_at, accepted_ip, accepted_ua,
           amended_at, original_config_json)
        VALUES (?, 299, '1x0.5', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, row["config_json"], row["status"], _iso(-timedelta(hours=2)), row["paid_at"],
         row["yookassa_payment_id"], row["offer_version"], row["accepted_at"], row["accepted_ip"],
         row["accepted_ua"], row["amended_at"], row["original_config_json"]),
    )
    conn.commit()
    conn.close()


def _insert_token(db_path: str, token: str, order_id: str, expires: timedelta, used: bool, downloads: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO download_tokens (token, order_id, expires_at, used, downloads) VALUES (?, ?, ?, ?, ?)",
        (token, order_id, _iso(expires), used, downloads),
    )
    conn.commit()
    conn.close()


def _auth() -> dict:
    from web.api.routers import admin
    return {"Authorization": f"Bearer {admin.ADMIN_TOKEN}"}


class TestAuth:

    @pytest.mark.asyncio
    async def test_no_token_rejected(self, client, init_test_db):
        _insert_order(init_test_db, "au-001")
        resp = await client.get("/api/admin/order/au-001/audit")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, client, init_test_db):
        _insert_order(init_test_db, "au-002")
        resp = await client.get(
            "/api/admin/order/au-002/audit", headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_by_payment_requires_token_too(self, client, init_test_db):
        _insert_order(init_test_db, "au-003")
        resp = await client.get("/api/admin/audit/by-payment/yk-au-003")
        assert resp.status_code in (401, 403)


class TestAuditContent:

    @pytest.mark.asyncio
    async def test_full_order_report(self, client, init_test_db):
        _insert_order(init_test_db, "au-010")
        resp = await client.get("/api/admin/order/au-010/audit", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()

        assert body["order"]["id"] == "au-010"
        assert body["order"]["status"] == "token_issued"
        assert body["order"]["yookassa_payment_id"] == "yk-au-010"
        assert body["acceptance"] == {
            "recorded":      True,
            "offer_version": "2026-10-03",
            "accepted_at":   body["acceptance"]["accepted_at"],
            "ip":            "203.0.113.7",
            "user_agent":    "TestBrowser/1.0",
        }
        assert body["content"]["text_lines"] == ["Аренда склада", "+7 914 000-00-00"]
        assert body["content"]["original_text_lines"] is None
        assert body["amend"]["used"] is False
        assert body["amend"]["available"] is True

    @pytest.mark.asyncio
    async def test_amended_order_shows_both_texts(self, client, init_test_db):
        _insert_order(
            init_test_db, "au-011",
            config_json=json.dumps(CONFIG, ensure_ascii=False),
            original_config_json=json.dumps(ORIGINAL, ensure_ascii=False),
            amended_at=_iso(-timedelta(minutes=30)),
        )
        body = (await client.get("/api/admin/order/au-011/audit", headers=_auth())).json()
        assert body["content"]["original_text_lines"] == ["Аренда склада", "+7 914 000-00-0O"]
        assert body["content"]["text_lines"] == ["Аренда склада", "+7 914 000-00-00"]
        assert body["content"]["amended_at"] is not None
        assert body["amend"] == {
            "used": True,
            "window_ends_at": body["amend"]["window_ends_at"],
            "available": False,
        }

    @pytest.mark.asyncio
    async def test_amend_window_closed_after_24h(self, client, init_test_db):
        _insert_order(init_test_db, "au-012", paid_at=_iso(-timedelta(hours=30)))
        body = (await client.get("/api/admin/order/au-012/audit", headers=_auth())).json()
        assert body["amend"]["used"] is False
        assert body["amend"]["available"] is False

    @pytest.mark.asyncio
    async def test_legacy_order_without_acceptance(self, client, init_test_db):
        _insert_order(
            init_test_db, "au-013",
            offer_version=None, accepted_at=None, accepted_ip=None, accepted_ua=None,
        )
        body = (await client.get("/api/admin/order/au-013/audit", headers=_auth())).json()
        assert body["acceptance"]["recorded"] is False
        assert body["acceptance"]["ip"] is None

    @pytest.mark.asyncio
    async def test_unpaid_order_has_no_amend_window(self, client, init_test_db):
        _insert_order(init_test_db, "au-014", status="pending", paid_at=None)
        body = (await client.get("/api/admin/order/au-014/audit", headers=_auth())).json()
        assert body["amend"]["window_ends_at"] is None
        assert body["amend"]["available"] is False
        assert body["delivery"] == {"delivered": False, "total_downloads": 0, "tokens": []}


class TestAuditDelivery:

    @pytest.mark.asyncio
    async def test_tokens_counted_and_masked(self, client, init_test_db):
        _insert_order(init_test_db, "au-020")
        _insert_token(init_test_db, FULL_TOKEN_A, "au-020", -timedelta(minutes=40), True, 3)
        _insert_token(init_test_db, FULL_TOKEN_B, "au-020", timedelta(minutes=10), False, 1)

        resp = await client.get("/api/admin/order/au-020/audit", headers=_auth())
        body = resp.json()
        delivery = body["delivery"]

        assert delivery["delivered"] is True
        assert delivery["total_downloads"] == 4
        by_prefix = {t["token_prefix"]: t for t in delivery["tokens"]}
        assert by_prefix["a1b2c3d4…"]["exhausted"] is True
        assert by_prefix["a1b2c3d4…"]["expired"] is True
        assert by_prefix["a1b2c3d4…"]["downloads"] == 3
        assert by_prefix["e5f6a7b8…"]["exhausted"] is False
        assert by_prefix["e5f6a7b8…"]["expired"] is False

        # Полный токен — ключ к PDF — в ответ попадать не должен
        assert FULL_TOKEN_A not in resp.text
        assert FULL_TOKEN_B not in resp.text

    @pytest.mark.asyncio
    async def test_issued_but_never_downloaded(self, client, init_test_db):
        _insert_order(init_test_db, "au-021")
        _insert_token(init_test_db, FULL_TOKEN_B, "au-021", timedelta(minutes=10), False, 0)
        delivery = (await client.get("/api/admin/order/au-021/audit", headers=_auth())).json()["delivery"]
        assert delivery["delivered"] is False
        assert delivery["total_downloads"] == 0
        assert len(delivery["tokens"]) == 1


class TestLookup:

    @pytest.mark.asyncio
    async def test_unknown_order_404(self, client):
        resp = await client.get("/api/admin/order/nope/audit", headers=_auth())
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_by_payment_finds_order(self, client, init_test_db):
        _insert_order(init_test_db, "au-030", yookassa_payment_id="2f0b1c3e-000f-5000-8000-1d2e3f4a5b6c")
        resp = await client.get(
            "/api/admin/audit/by-payment/2f0b1c3e-000f-5000-8000-1d2e3f4a5b6c", headers=_auth()
        )
        assert resp.status_code == 200
        assert resp.json()["order"]["id"] == "au-030"

    @pytest.mark.asyncio
    async def test_by_payment_unknown_404(self, client):
        resp = await client.get("/api/admin/audit/by-payment/unknown-payment", headers=_auth())
        assert resp.status_code == 404
