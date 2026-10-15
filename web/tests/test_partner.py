"""
test_partner.py
~~~~~~~~~~~~~~~
Партнёры-типографии: брендирование, вознаграждение 100 ₽ за оплаченный заказ,
отправка готового макета на почту типографии.

Покрывает:
  - создание партнёра: нормализация и валидация кода, почты, логотипа, цвета, ставки
  - публичный GET /api/partner/{code}: только название, логотип, цвет; почты и ставки нет
  - привязка заказа: partner_code сохраняется, ref_code бота при этом не пишется,
    мусорный / неизвестный / отключённый партнёр не ломает покупку
  - начисление: вебхук ЮKassa, ручная выдача (force_token), идемпотентность,
    потолок по сумме заказа, бесплатный заказ ничего не начисляет
  - отчёт за месяц, граница месяцев, выплата (всё / до конца месяца)
  - отправка макета при скачивании: тот же PDF, один раз на версию, после правки текста новое письмо,
    сбой SMTP не трогает клиента и повторяется, почта не настроена
  - повтор отправки из админки
  - SMTP-слой: ssl / starttls / none, вход, обёртка ошибок
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from conftest import VALID_CONFIG, VALID_ORDER_PAYLOAD, make_yookassa_succeeded_payment

from web.api.services import mailer, partner_delivery, partner_store, promo_store
from web.api.services.partner_store import PartnerError, PartnerExists, PartnerNotFound
from web.api.services.token_store import create_token

PDF_BYTES = b"%PDF-1.4 partner banner"
VERIFY = "web.api.routers.payment.verify_yookassa_payment"
SEND = "web.api.services.mailer.send_message"


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _auth() -> dict:
    from web.api.routers import admin
    return {"Authorization": f"Bearer {admin.ADMIN_TOKEN}"}


def _db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _partner(code="reklamacity", **kw) -> dict:
    args = {"name": "Реклама-Сити", "email": "print@reklamacity.example"}
    args.update(kw)
    return partner_store.create_partner(code, **args)


def _insert_order(path, order_id, *, partner_code=None, amount=299, status="token_issued",
                  config=None, size_key="1x0.5"):
    conn = _db(path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at, partner_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, amount, size_key, json.dumps(config or VALID_CONFIG, ensure_ascii=False), status,
         datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), partner_code),
    )
    conn.commit()
    conn.close()


def _accruals(path) -> list[sqlite3.Row]:
    conn = _db(path)
    rows = conn.execute("SELECT * FROM partner_accruals ORDER BY id").fetchall()
    conn.close()
    return rows


def _insert_accrual(path, code, order_id, reward, created_at, paid_out_at=None):
    conn = _db(path)
    conn.execute(
        "INSERT INTO partner_accruals (partner_code, order_id, order_amount, reward_rub, created_at, paid_out_at) "
        "VALUES (?, ?, 299, ?, ?, ?)",
        (code, order_id, reward, created_at, paid_out_at),
    )
    conn.commit()
    conn.close()


def _order_row(path, order_id) -> sqlite3.Row:
    conn = _db(path)
    row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


@contextmanager
def _render_with(module: str, fn):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with (
            patch(f"web.api.routers.{module}.render_pdf_sync", fn),
            patch(f"web.api.routers.{module}.get_executor", lambda: pool),
        ):
            yield
    finally:
        pool.shutdown()


def _ok_render(config):
    return PDF_BYTES


@pytest.fixture
def mail_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("SMTP_FROM", "BannerBot <noreply@test.invalid>")
    monkeypatch.delenv("PARTNER_MAIL_BCC", raising=False)


@pytest.fixture
def no_mail_env(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)


def _body(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def _attachment(msg):
    return next(iter(msg.iter_attachments()))


# ---------------------------------------------------------------------------
# Создание партнёра
# ---------------------------------------------------------------------------
class TestCreatePartner:

    def test_creates_and_normalizes(self, init_test_db):
        p = partner_store.create_partner(
            "  RekLama-City ", "  Реклама-Сити ", " Print@ReklamaCity.Example ",
            logo_url="/partners/reklamacity.svg", accent="#E31E24",
        )
        assert p["code"] == "reklama-city"
        assert p["name"] == "Реклама-Сити"
        assert p["email"] == "Print@reklamacity.example"  # домен нормализуется, локальная часть как есть
        assert p["accent"] == "#e31e24"
        assert p["reward_rub"] == 100 and p["active"] == 1

    @pytest.mark.parametrize("code", ["", "a", "-ab", "ab cd", "ab_cd", "x" * 33, "../etc", "реклама"])
    def test_rejects_bad_code(self, init_test_db, code):
        with pytest.raises(PartnerError):
            partner_store.create_partner(code, "Имя", "a@b.example")

    def test_duplicate_code_case_insensitive(self, init_test_db):
        _partner("print1")
        with pytest.raises(PartnerExists):
            _partner("PRINT1")

    @pytest.mark.parametrize("email", ["", "no-at-sign", "a@", "@b.example", "a b@c.example"])
    def test_rejects_bad_email(self, init_test_db, email):
        with pytest.raises(PartnerError):
            partner_store.create_partner("print1", "Имя", email)

    @pytest.mark.parametrize("logo", [
        "javascript:alert(1)", "data:image/svg+xml;base64,AAAA", "http://x.example/l.png",
        "/partners/../admin/index.html", "/partners/.hidden", "/other/l.png", "/partners/", "/partners/a/b.png",
        # внешние адреса не принимаем: браузер посетителя выдал бы чужому хосту свой IP (политика /privacy/)
        "https://cdn.example/l.png", "https://x.example/a b.png", 'https://x.example/"onerror=1',
    ])
    def test_rejects_bad_logo(self, init_test_db, logo):
        with pytest.raises(PartnerError):
            partner_store.create_partner("print1", "Имя", "a@b.example", logo_url=logo)

    @pytest.mark.parametrize("logo", ["/partners/a.svg", "/partners/Logo_1-x.png", "/partners/rc.v2.png"])
    def test_accepts_good_logo(self, init_test_db, logo):
        assert partner_store.create_partner("print1", "Имя", "a@b.example", logo_url=logo)["logo_url"] == logo

    @pytest.mark.parametrize("accent", ["red", "#fff", "#12345g", "e31e24", "#e31e2400"])
    def test_rejects_bad_accent(self, init_test_db, accent):
        with pytest.raises(PartnerError):
            partner_store.create_partner("print1", "Имя", "a@b.example", accent=accent)

    @pytest.mark.parametrize("reward", [-1, 1001])
    def test_rejects_reward_out_of_range(self, init_test_db, reward):
        with pytest.raises(PartnerError):
            partner_store.create_partner("print1", "Имя", "a@b.example", reward_rub=reward)

    def test_rejects_empty_or_overlong_name(self, init_test_db):
        for name in ("", "   ", "x" * 81):
            with pytest.raises(PartnerError):
                partner_store.create_partner("print1", name, "a@b.example")

    def test_control_chars_stripped_from_name(self, init_test_db):
        p = partner_store.create_partner("print1", "Ре\nкл\x00ама", "a@b.example")
        assert p["name"] == "Реклама"


# ---------------------------------------------------------------------------
# Публичное брендирование
# ---------------------------------------------------------------------------
class TestPublicBranding:

    @pytest.mark.asyncio
    async def test_returns_only_public_fields(self, client, init_test_db, mail_env):
        _partner(logo_url="/partners/rc.svg", accent="#e31e24")
        resp = await client.get("/api/partner/reklamacity")
        assert resp.status_code == 200
        assert resp.json() == {
            "code": "reklamacity", "name": "Реклама-Сити", "logo_url": "/partners/rc.svg",
            "accent": "#e31e24", "delivers": True,
        }
        raw = resp.text
        assert "print@reklamacity.example" not in raw and "reward" not in raw
        assert resp.headers["cache-control"] == "public, max-age=300"

    @pytest.mark.asyncio
    async def test_delivers_false_without_mail(self, client, init_test_db, no_mail_env):
        _partner()
        assert (await client.get("/api/partner/reklamacity")).json()["delivers"] is False

    @pytest.mark.asyncio
    async def test_code_is_case_insensitive(self, client, init_test_db):
        _partner()
        assert (await client.get("/api/partner/ReklamaCity")).status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_and_disabled_are_404(self, client, init_test_db):
        _partner()
        assert (await client.get("/api/partner/nobody")).status_code == 404
        assert (await client.get("/api/partner/..%2Fetc")).status_code == 404
        partner_store.set_active("reklamacity", False)
        assert (await client.get("/api/partner/reklamacity")).status_code == 404


# ---------------------------------------------------------------------------
# Привязка заказа к партнёру
# ---------------------------------------------------------------------------
class TestOrderAttribution:

    @pytest.mark.asyncio
    async def test_partner_code_saved_and_normalized(self, client, init_test_db):
        _partner()
        resp = await client.post("/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": " ReklamaCity "})
        assert resp.status_code == 200
        assert _order_row(init_test_db, resp.json()["order_id"])["partner_code"] == "reklamacity"

    @pytest.mark.asyncio
    async def test_partner_wins_over_referral_code(self, client, init_test_db):
        _partner()
        resp = await client.post(
            "/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity", "ref_code": "ABCD1234"},
        )
        row = _order_row(init_test_db, resp.json()["order_id"])
        assert row["partner_code"] == "reklamacity" and row["ref_code"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["nobody", "../../x", "<script>", "", "x" * 100, "реклама"])
    async def test_unknown_or_junk_partner_does_not_break_order(self, client, init_test_db, junk):
        _partner()
        resp = await client.post(
            "/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": junk, "ref_code": "ABCD1234"},
        )
        assert resp.status_code == 200
        row = _order_row(init_test_db, resp.json()["order_id"])
        assert row["partner_code"] is None and row["ref_code"] == "ABCD1234"

    @pytest.mark.asyncio
    async def test_disabled_partner_not_attached(self, client, init_test_db):
        _partner()
        partner_store.set_active("reklamacity", False)
        resp = await client.post("/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity"})
        assert resp.status_code == 200
        assert _order_row(init_test_db, resp.json()["order_id"])["partner_code"] is None

    @pytest.mark.asyncio
    async def test_order_without_partner_unchanged(self, client, init_test_db):
        resp = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert _order_row(init_test_db, resp.json()["order_id"])["partner_code"] is None


# ---------------------------------------------------------------------------
# Начисление вознаграждения
# ---------------------------------------------------------------------------
class TestAccrual:

    def test_accrues_fixed_reward_once(self, init_test_db):
        _partner()
        _insert_order(init_test_db, "o-1", partner_code="reklamacity")
        assert partner_store.accrue_for_order("o-1") == 100
        assert partner_store.accrue_for_order("o-1") == 0  # идемпотентно
        rows = _accruals(init_test_db)
        assert len(rows) == 1
        assert (rows[0]["partner_code"], rows[0]["order_amount"], rows[0]["reward_rub"]) == ("reklamacity", 299, 100)
        assert rows[0]["paid_out_at"] is None

    def test_reward_capped_by_what_client_paid(self, init_test_db):
        _partner()
        _insert_order(init_test_db, "o-1", partner_code="reklamacity", amount=59)
        assert partner_store.accrue_for_order("o-1") == 59

    def test_free_order_accrues_nothing(self, init_test_db):
        _partner()
        _insert_order(init_test_db, "o-1", partner_code="reklamacity", amount=0)
        assert partner_store.accrue_for_order("o-1") == 0
        assert _accruals(init_test_db) == []

    def test_non_partner_and_unknown_orders_accrue_nothing(self, init_test_db):
        _partner()
        _insert_order(init_test_db, "o-1")
        assert partner_store.accrue_for_order("o-1") == 0
        assert partner_store.accrue_for_order("no-such-order") == 0

    def test_zero_reward_partner_accrues_nothing(self, init_test_db):
        _partner(reward_rub=0)
        _insert_order(init_test_db, "o-1", partner_code="reklamacity")
        assert partner_store.accrue_for_order("o-1") == 0

    def test_custom_reward_rate(self, init_test_db):
        _partner(reward_rub=150)
        _insert_order(init_test_db, "o-1", partner_code="reklamacity")
        assert partner_store.accrue_for_order("o-1") == 150

    def test_disabled_partner_still_gets_reward_for_orders_it_brought(self, init_test_db):
        _partner()
        _insert_order(init_test_db, "o-1", partner_code="reklamacity")
        partner_store.set_active("reklamacity", False)
        assert partner_store.accrue_for_order("o-1") == 100

    @pytest.mark.asyncio
    async def test_webhook_payment_accrues(self, client, init_test_db):
        _partner()
        created = await client.post("/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity"})
        order_id = created.json()["order_id"]
        assert _accruals(init_test_db) == []  # до оплаты вознаграждения нет

        body = json.dumps({
            "type": "notification", "event": "payment.succeeded",
            "object": {"id": "yk-1", "status": "succeeded", "metadata": {"order_id": order_id}},
        }).encode()
        with patch(VERIFY, AsyncMock(return_value=make_yookassa_succeeded_payment(order_id, "yk-1"))):
            for _ in range(2):  # повторный вебхук не удваивает начисление
                resp = await client.post(
                    "/api/payment/callback", content=body, headers={"Content-Type": "application/json"},
                )
                assert resp.status_code == 200
        rows = _accruals(init_test_db)
        assert [(r["order_id"], r["reward_rub"]) for r in rows] == [(order_id, 100)]

    @pytest.mark.asyncio
    async def test_manual_force_token_accrues(self, client, init_test_db):
        _partner()
        created = await client.post("/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity"})
        order_id = created.json()["order_id"]
        resp = await client.post(f"/api/admin/force_token/{order_id}", headers=_auth())
        assert resp.status_code == 200
        assert [r["reward_rub"] for r in _accruals(init_test_db)] == [100]

    @pytest.mark.asyncio
    async def test_unpaid_order_accrues_nothing(self, client, init_test_db):
        _partner()
        await client.post("/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity"})
        assert _accruals(init_test_db) == []

    @pytest.mark.asyncio
    async def test_free_promo_order_accrues_nothing(self, client, init_test_db):
        _partner()
        promo_store.create_promo(discount=100, uses=1, code="FREE100")
        resp = await client.post(
            "/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity", "promo_code": "FREE100"},
        )
        assert resp.json()["free"] is True
        assert _accruals(init_test_db) == []

    @pytest.mark.asyncio
    async def test_discounted_order_reward_never_exceeds_payment(self, client, init_test_db):
        _partner()
        promo_store.create_promo(discount=80, uses=1, code="SALE80")
        created = await client.post(
            "/api/order", json={**VALID_ORDER_PAYLOAD, "partner_code": "reklamacity", "promo_code": "SALE80"},
        )
        order_id = created.json()["order_id"]
        paid = created.json()["amount_rub"]
        assert 0 < paid < 100
        await client.post(f"/api/admin/force_token/{order_id}", headers=_auth())
        assert [r["reward_rub"] for r in _accruals(init_test_db)] == [paid]


# ---------------------------------------------------------------------------
# Отчёт и выплата
# ---------------------------------------------------------------------------
class TestReportAndPayout:

    def test_month_bounds_including_year_rollover(self):
        assert partner_store._month_bounds("2026-10") == ("2026-10-01", "2026-11-01")
        assert partner_store._month_bounds("2026-12") == ("2026-12-01", "2027-01-01")
        for bad in ("2026-13", "2026-00", "26-10", "2026-1", "", "2026/10"):
            with pytest.raises(PartnerError):
                partner_store._month_bounds(bad)

    def test_report_respects_month_boundaries(self, init_test_db):
        _partner("a1")
        _partner("b2", name="Вторая", email="b@b.example")
        _insert_accrual(init_test_db, "a1", "o1", 100, "2026-09-30T23:59:59+00:00")
        _insert_accrual(init_test_db, "a1", "o2", 100, "2026-10-01T00:00:00+00:00")
        _insert_accrual(init_test_db, "a1", "o3", 100, "2026-10-31T23:59:59.999999+00:00", paid_out_at="2026-11-02")
        _insert_accrual(init_test_db, "a1", "o4", 100, "2026-11-01T00:00:00+00:00")
        _insert_accrual(init_test_db, "b2", "o5", 50, "2026-10-15T10:00:00+00:00")

        report = partner_store.month_report("2026-10")
        by_code = {p["code"]: p for p in report["partners"]}
        assert by_code["a1"]["orders"] == 2 and by_code["a1"]["reward_rub"] == 200 and by_code["a1"]["unpaid_rub"] == 100
        assert by_code["b2"]["orders"] == 1 and by_code["b2"]["reward_rub"] == 50
        assert report["totals"] == {"orders": 3, "reward_rub": 250, "unpaid_rub": 150}

    def test_empty_month(self, init_test_db):
        _partner()
        assert partner_store.month_report("2026-10") == {
            "month": "2026-10", "partners": [], "totals": {"orders": 0, "reward_rub": 0, "unpaid_rub": 0},
        }

    def test_payout_all_unpaid_then_nothing_left(self, init_test_db):
        _partner()
        _insert_accrual(init_test_db, "reklamacity", "o1", 100, "2026-10-01T00:00:00+00:00")
        _insert_accrual(init_test_db, "reklamacity", "o2", 100, "2026-11-05T00:00:00+00:00")
        _insert_accrual(init_test_db, "reklamacity", "o3", 100, "2026-10-02T00:00:00+00:00", paid_out_at="2026-10-20")
        assert partner_store.mark_paid_out("reklamacity") == {"orders": 2, "paid_rub": 200}
        assert partner_store.mark_paid_out("reklamacity") == {"orders": 0, "paid_rub": 0}
        assert all(r["paid_out_at"] for r in _accruals(init_test_db))

    def test_payout_through_month_leaves_later_orders(self, init_test_db):
        _partner()
        _insert_accrual(init_test_db, "reklamacity", "o1", 100, "2026-10-31T23:59:59+00:00")
        _insert_accrual(init_test_db, "reklamacity", "o2", 100, "2026-11-01T00:00:00+00:00")
        assert partner_store.mark_paid_out("reklamacity", "2026-10") == {"orders": 1, "paid_rub": 100}
        rows = {r["order_id"]: r["paid_out_at"] for r in _accruals(init_test_db)}
        assert rows["o1"] is not None and rows["o2"] is None

    def test_payout_does_not_touch_other_partners(self, init_test_db):
        _partner("a1")
        _partner("b2", name="Вторая", email="b@b.example")
        _insert_accrual(init_test_db, "a1", "o1", 100, "2026-10-01T00:00:00+00:00")
        _insert_accrual(init_test_db, "b2", "o2", 100, "2026-10-01T00:00:00+00:00")
        partner_store.mark_paid_out("a1")
        rows = {r["order_id"]: r["paid_out_at"] for r in _accruals(init_test_db)}
        assert rows["o1"] is not None and rows["o2"] is None

    def test_payout_unknown_partner(self, init_test_db):
        with pytest.raises(PartnerNotFound):
            partner_store.mark_paid_out("nobody")

    def test_list_partners_balances(self, init_test_db):
        _partner()
        _insert_accrual(init_test_db, "reklamacity", "o1", 100, "2026-10-01T00:00:00+00:00", paid_out_at="2026-10-20")
        _insert_accrual(init_test_db, "reklamacity", "o2", 100, "2026-10-02T00:00:00+00:00")
        _insert_accrual(init_test_db, "reklamacity", "o3", 70, "2026-10-03T00:00:00+00:00")
        item = partner_store.list_partners()[0]
        assert (item["orders"], item["earned_rub"], item["unpaid_rub"], item["unpaid_orders"]) == (3, 270, 170, 2)
        assert item["failed_deliveries"] == 0


# ---------------------------------------------------------------------------
# Админ-API
# ---------------------------------------------------------------------------
class TestAdminApi:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,path", [
        ("POST", "/api/admin/partners"),
        ("GET", "/api/admin/partners"),
        ("GET", "/api/admin/partners/report"),
        ("POST", "/api/admin/partners/x1/active"),
        ("POST", "/api/admin/partners/x1/payout"),
        ("POST", "/api/admin/partners/deliveries/o1/resend"),
    ])
    async def test_every_admin_endpoint_requires_token(self, client, init_test_db, method, path):
        resp = await client.request(method, path)
        assert resp.status_code in (401, 403)
        resp = await client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_list_toggle(self, client, init_test_db, mail_env):
        resp = await client.post("/api/admin/partners", headers=_auth(), json={
            "code": "ReklamaCity", "name": "Реклама-Сити", "email": "print@reklamacity.example",
            "logo_url": "/partners/rc.svg", "accent": "#E31E24",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "reklamacity" and body["reward_rub"] == 100
        assert body["link"] == "https://bannerbot.ru/?partner=reklamacity"

        listing = (await client.get("/api/admin/partners", headers=_auth())).json()
        assert listing["mail_configured"] is True
        assert [p["code"] for p in listing["partners"]] == ["reklamacity"]
        assert listing["partners"][0]["email"] == "print@reklamacity.example"

        resp = await client.post("/api/admin/partners/reklamacity/active", headers=_auth(), json={"active": False})
        assert resp.json() == {"code": "reklamacity", "active": False}
        assert (await client.get("/api/partner/reklamacity")).status_code == 404

    @pytest.mark.asyncio
    async def test_create_errors(self, client, init_test_db):
        good = {"code": "print1", "name": "Имя", "email": "a@b.example"}
        assert (await client.post("/api/admin/partners", headers=_auth(), json=good)).status_code == 200
        assert (await client.post("/api/admin/partners", headers=_auth(), json=good)).status_code == 409
        bad = await client.post("/api/admin/partners", headers=_auth(), json={**good, "code": "-bad"})
        assert bad.status_code == 422
        for logo in ("javascript:1", "https://cdn.example/l.png"):
            bad = await client.post("/api/admin/partners", headers=_auth(), json={**good, "code": "p2", "logo_url": logo})
            assert bad.status_code == 422, logo

    @pytest.mark.asyncio
    async def test_toggle_unknown_is_404(self, client, init_test_db):
        for code in ("nobody", "-bad"):
            resp = await client.post(f"/api/admin/partners/{code}/active", headers=_auth(), json={"active": True})
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_report_and_payout_flow(self, client, init_test_db):
        _partner()
        _insert_accrual(init_test_db, "reklamacity", "o1", 100, "2026-10-05T00:00:00+00:00")
        _insert_accrual(init_test_db, "reklamacity", "o2", 100, "2026-11-05T00:00:00+00:00")

        rep = (await client.get("/api/admin/partners/report?month=2026-10", headers=_auth())).json()
        assert rep["totals"] == {"orders": 1, "reward_rub": 100, "unpaid_rub": 100}

        resp = await client.post("/api/admin/partners/reklamacity/payout", headers=_auth(), json={"month": "2026-10"})
        assert resp.json() == {"code": "reklamacity", "orders": 1, "paid_rub": 100}
        rep = (await client.get("/api/admin/partners/report?month=2026-10", headers=_auth())).json()
        assert rep["totals"]["unpaid_rub"] == 0

        # без тела: всё оставшееся
        resp = await client.post("/api/admin/partners/reklamacity/payout", headers=_auth())
        assert resp.json()["paid_rub"] == 100

    @pytest.mark.asyncio
    async def test_report_defaults_to_current_month_and_validates(self, client, init_test_db):
        resp = await client.get("/api/admin/partners/report", headers=_auth())
        assert resp.json()["month"] == datetime.now(timezone.utc).strftime("%Y-%m")
        assert (await client.get("/api/admin/partners/report?month=2026-13", headers=_auth())).status_code == 422

    @pytest.mark.asyncio
    async def test_payout_errors(self, client, init_test_db):
        _partner()
        assert (await client.post("/api/admin/partners/nobody/payout", headers=_auth())).status_code == 404
        bad = await client.post("/api/admin/partners/reklamacity/payout", headers=_auth(), json={"month": "oct"})
        assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Отправка макета при скачивании
# ---------------------------------------------------------------------------
class TestDeliveryOnDownload:

    async def _download(self, client, order_id: str):
        token = create_token(order_id)
        with _render_with("download", _ok_render):
            return await client.get(f"/api/download/{token}")

    @pytest.mark.asyncio
    async def test_partner_gets_the_same_pdf_the_client_downloaded(self, client, init_test_db, mail_env, monkeypatch):
        monkeypatch.setenv("PARTNER_MAIL_BCC", "owner@bannerbot.example")
        _partner()
        _insert_order(init_test_db, "abcdef12-0000", partner_code="reklamacity")
        with patch(SEND, new=AsyncMock()) as send:
            resp = await self._download(client, "abcdef12-0000")
        assert resp.status_code == 200 and resp.content == PDF_BYTES

        send.assert_awaited_once()
        msg = send.await_args.args[0]
        assert msg["To"] == "print@reklamacity.example"
        assert msg["From"] == "BannerBot <noreply@test.invalid>"
        assert msg["Bcc"] == "owner@bannerbot.example"
        assert msg["Subject"] == "Новый макет баннера 1×0.5 м: заказ abcdef12"
        att = _attachment(msg)
        assert att.get_content() == PDF_BYTES
        assert att.get_filename() == "banner_1x0_5_abcdef12.pdf"
        assert att.get_content_type() == "application/pdf"
        body = _body(msg)
        assert "Реклама-Сити" in body and "abcdef12" in body and "Тест баннер" in body

        conn = _db(init_test_db)
        row = conn.execute("SELECT * FROM partner_deliveries").fetchone()
        conn.close()
        assert row["sent_at"] and row["error"] is None and row["partner_code"] == "reklamacity"

    @pytest.mark.asyncio
    async def test_same_version_is_sent_once(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0001", partner_code="reklamacity")
        with patch(SEND, new=AsyncMock()) as send:
            await self._download(client, "ord-0001")
            await self._download(client, "ord-0001")
        assert send.await_count == 1

    @pytest.mark.asyncio
    async def test_amended_text_sends_revised_version(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0002", partner_code="reklamacity")
        with patch(SEND, new=AsyncMock()) as send:
            await self._download(client, "ord-0002")
            fixed = {**VALID_CONFIG, "text_lines": [{"text": "Исправленный текст", "scale": 1.0}]}
            conn = _db(init_test_db)
            conn.execute("UPDATE web_orders SET config_json = ? WHERE id = ?",
                         (json.dumps(fixed, ensure_ascii=False), "ord-0002"))
            conn.commit()
            conn.close()
            await self._download(client, "ord-0002")
        assert send.await_count == 2
        second = send.await_args_list[1].args[0]
        assert second["Subject"].startswith("Исправленный макет")
        assert "Исправленный текст" in _body(second) and "заменяет предыдущую" in _body(second)

    @pytest.mark.asyncio
    async def test_non_partner_order_sends_nothing(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0003")
        with patch(SEND, new=AsyncMock()) as send:
            resp = await self._download(client, "ord-0003")
        assert resp.status_code == 200 and send.await_count == 0

    @pytest.mark.asyncio
    async def test_mail_not_configured_download_still_works_and_no_claim(self, client, init_test_db, no_mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0004", partner_code="reklamacity")
        resp = await self._download(client, "ord-0004")
        assert resp.status_code == 200 and resp.content == PDF_BYTES
        conn = _db(init_test_db)
        assert conn.execute("SELECT COUNT(*) FROM partner_deliveries").fetchone()[0] == 0
        conn.close()

    @pytest.mark.asyncio
    async def test_disabled_partner_gets_no_mail(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0005", partner_code="reklamacity")
        partner_store.set_active("reklamacity", False)
        with patch(SEND, new=AsyncMock()) as send:
            resp = await self._download(client, "ord-0005")
        assert resp.status_code == 200 and send.await_count == 0

    @pytest.mark.asyncio
    async def test_smtp_failure_does_not_affect_client_and_is_retried(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0006", partner_code="reklamacity")
        with patch(SEND, new=AsyncMock(side_effect=[mailer.MailError("OSError: connection refused"), None])) as send:
            first = await self._download(client, "ord-0006")
            assert first.status_code == 200 and first.content == PDF_BYTES

            conn = _db(init_test_db)
            row = conn.execute("SELECT * FROM partner_deliveries").fetchone()
            conn.close()
            assert row["sent_at"] is None and "connection refused" in row["error"]
            assert partner_store.list_partners()[0]["failed_deliveries"] == 1

            second = await self._download(client, "ord-0006")  # следующее скачивание повторяет отправку
            assert second.status_code == 200
        assert send.await_count == 2
        conn = _db(init_test_db)
        row = conn.execute("SELECT * FROM partner_deliveries").fetchone()
        conn.close()
        assert row["sent_at"] and row["error"] is None and row["attempts"] == 2
        assert partner_store.list_partners()[0]["failed_deliveries"] == 0

    @pytest.mark.asyncio
    async def test_erased_order_is_gone_and_nothing_is_sent(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-0008", partner_code="reklamacity")
        conn = _db(init_test_db)
        conn.execute("UPDATE web_orders SET config_json = '' WHERE id = 'ord-0008'")
        conn.commit()
        conn.close()
        with patch(SEND, new=AsyncMock()) as send:
            resp = await self._download(client, "ord-0008")
        assert resp.status_code == 410 and send.await_count == 0
        conn = _db(init_test_db)
        assert conn.execute("SELECT COUNT(*) FROM partner_deliveries").fetchone()[0] == 0
        conn.close()

    @pytest.mark.asyncio
    async def test_entry_label_in_config_does_not_leak_into_the_mail(self, client, init_test_db, mail_env):
        # main добавляет в config_json метку входа для статистики: в письмо типографии она не попадает
        _partner()
        _insert_order(init_test_db, "ord-0009", partner_code="reklamacity", config={**VALID_CONFIG, "entry": "yandex"})
        with patch(SEND, new=AsyncMock()) as send:
            await self._download(client, "ord-0009")
        msg = send.await_args.args[0]
        assert "yandex" not in _body(msg) and "yandex" not in msg["Subject"]

    @pytest.mark.asyncio
    async def test_custom_size_label(self, client, init_test_db, mail_env):
        _partner()
        custom = {**VALID_CONFIG, "width_mm": 2400, "height_mm": 900}
        custom.pop("size_key")
        _insert_order(init_test_db, "ord-0007", partner_code="reklamacity", config=custom, size_key="custom")
        with patch(SEND, new=AsyncMock()) as send:
            await self._download(client, "ord-0007")
        msg = send.await_args.args[0]
        assert "2400×900 мм" in msg["Subject"] and "Размер: 2400×900 мм" in _body(msg)


# ---------------------------------------------------------------------------
# Права на отправку (claim)
# ---------------------------------------------------------------------------
class TestClaimDelivery:

    def test_second_claim_refused_while_in_flight_or_sent(self, init_test_db):
        _partner()
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is True
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is False  # отправка идёт
        partner_store.mark_delivery_sent("o1", "h1")
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is False  # уже отправлено

    def test_failed_delivery_can_be_claimed_again(self, init_test_db):
        _partner()
        partner_store.claim_delivery("o1", "h1", "reklamacity")
        partner_store.mark_delivery_failed("o1", "h1", "x" * 500)
        conn = _db(init_test_db)
        assert len(conn.execute("SELECT error FROM partner_deliveries").fetchone()["error"]) == 300
        conn.close()
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is True

    def test_stale_claim_is_taken_over_fresh_is_not(self, init_test_db):
        _partner()
        partner_store.claim_delivery("o1", "h1", "reklamacity")
        old = (datetime.now(timezone.utc) - timedelta(minutes=partner_store.DELIVERY_STALE_MINUTES + 1)).isoformat()
        conn = _db(init_test_db)
        conn.execute("UPDATE partner_deliveries SET created_at = ?", (old,))
        conn.commit()
        conn.close()
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is True
        assert partner_store.claim_delivery("o1", "h1", "reklamacity") is False  # теперь занято свежо

    def test_force_always_claims_and_resets(self, init_test_db):
        _partner()
        partner_store.claim_delivery("o1", "h1", "reklamacity")
        partner_store.mark_delivery_sent("o1", "h1")
        assert partner_store.claim_delivery("o1", "h1", "reklamacity", force=True) is True
        conn = _db(init_test_db)
        row = conn.execute("SELECT * FROM partner_deliveries").fetchone()
        conn.close()
        assert row["sent_at"] is None and row["attempts"] == 2

    def test_versions_are_independent(self, init_test_db):
        _partner()
        h1, h2 = partner_store.config_hash('{"a":1}'), partner_store.config_hash('{"a":2}')
        assert h1 != h2
        partner_store.claim_delivery("o1", h1, "reklamacity")
        assert partner_store.has_other_version("o1", h1) is False
        assert partner_store.has_other_version("o1", h2) is True
        assert partner_store.claim_delivery("o1", h2, "reklamacity") is True


# ---------------------------------------------------------------------------
# Повтор из админки
# ---------------------------------------------------------------------------
class TestResend:

    @pytest.mark.asyncio
    async def test_resend_sends_even_if_already_delivered(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-1001", partner_code="reklamacity")
        cfg = json.dumps(VALID_CONFIG, ensure_ascii=False)
        partner_store.claim_delivery("ord-1001", partner_store.config_hash(cfg), "reklamacity")
        partner_store.mark_delivery_sent("ord-1001", partner_store.config_hash(cfg))
        with patch(SEND, new=AsyncMock()) as send, _render_with("partner", _ok_render):
            resp = await client.post("/api/admin/partners/deliveries/ord-1001/resend", headers=_auth())
        assert resp.status_code == 200 and resp.json() == {"order_id": "ord-1001", "sent": True}
        assert send.await_count == 1 and _attachment(send.await_args.args[0]).get_content() == PDF_BYTES

    @pytest.mark.asyncio
    async def test_resend_guards(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "plain", )
        _insert_order(init_test_db, "unpaid", partner_code="reklamacity", status="pending")
        url = "/api/admin/partners/deliveries/{}/resend"
        with patch(SEND, new=AsyncMock()) as send, _render_with("partner", _ok_render):
            assert (await client.post(url.format("nope"), headers=_auth())).status_code == 404
            assert (await client.post(url.format("plain"), headers=_auth())).status_code == 409
            assert (await client.post(url.format("unpaid"), headers=_auth())).status_code == 409
        assert send.await_count == 0

    @pytest.mark.asyncio
    async def test_resend_after_retention_erased_the_order_is_410(self, client, init_test_db, mail_env):
        # services/retention.py стирает текст макета через ORDER_PII_RETENTION_DAYS: восстановить файл нечем
        _partner()
        _insert_order(init_test_db, "ord-0999", partner_code="reklamacity")
        conn = _db(init_test_db)
        conn.execute("UPDATE web_orders SET config_json = '' WHERE id = 'ord-0999'")
        conn.commit()
        conn.close()
        with patch(SEND, new=AsyncMock()) as send, _render_with("partner", _ok_render):
            resp = await client.post("/api/admin/partners/deliveries/ord-0999/resend", headers=_auth())
        assert resp.status_code == 410 and send.await_count == 0

    @pytest.mark.asyncio
    async def test_resend_without_mail_is_503(self, client, init_test_db, no_mail_env):
        _partner()
        _insert_order(init_test_db, "ord-1002", partner_code="reklamacity")
        resp = await client.post("/api/admin/partners/deliveries/ord-1002/resend", headers=_auth())
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_resend_reports_smtp_failure(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-1003", partner_code="reklamacity")
        with patch(SEND, new=AsyncMock(side_effect=mailer.MailError("boom"))), _render_with("partner", _ok_render):
            resp = await client.post("/api/admin/partners/deliveries/ord-1003/resend", headers=_auth())
        assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_resend_render_failure_is_500(self, client, init_test_db, mail_env):
        _partner()
        _insert_order(init_test_db, "ord-1004", partner_code="reklamacity")

        def boom(config):
            raise RuntimeError("gs died")

        with patch(SEND, new=AsyncMock()) as send, _render_with("partner", boom):
            resp = await client.post("/api/admin/partners/deliveries/ord-1004/resend", headers=_auth())
        assert resp.status_code == 500 and send.await_count == 0


# ---------------------------------------------------------------------------
# Письмо и SMTP-слой
# ---------------------------------------------------------------------------
class FakeSMTP:
    instances: list = []
    fail_on_send = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        if FakeSMTP.fail_on_send:
            raise OSError("network down")
        self.calls.append(("send", msg))


class FakeSMTPSSL(FakeSMTP):
    """smtplib.SMTP_SSL: отдельный класс, чтобы тест отличал TLS с первого байта от обычного соединения."""


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_on_send = False
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


class TestMailer:

    def test_is_configured_needs_host_and_from(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("SMTP_FROM", raising=False)
        assert mailer.is_configured() is False
        monkeypatch.setenv("SMTP_HOST", "smtp.example")
        assert mailer.is_configured() is False
        monkeypatch.setenv("SMTP_FROM", "a@example")
        assert mailer.is_configured() is True

    def test_build_message_bcc_only_when_set(self, mail_env, monkeypatch):
        kw = dict(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=PDF_BYTES)
        assert mailer.build_message(**kw)["Bcc"] is None
        monkeypatch.setenv("PARTNER_MAIL_BCC", "owner@example")
        assert mailer.build_message(**kw)["Bcc"] == "owner@example"

    @pytest.mark.asyncio
    async def test_ssl_default_port_and_login(self, mail_env, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_USER", "u")
        monkeypatch.setenv("SMTP_PASSWORD", "p")
        msg = mailer.build_message(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=b"x")
        await mailer.send_message(msg)
        (srv,) = fake_smtp.instances
        assert isinstance(srv, FakeSMTPSSL), "порт 465 требует TLS с первого байта (SMTP_SSL)"
        assert (srv.host, srv.port, srv.timeout) == ("smtp.test.invalid", 465, mailer.SMTP_TIMEOUT_SECONDS)
        assert [c[0] for c in srv.calls] == ["login", "send"] and srv.calls[0][1:] == ("u", "p")

    @pytest.mark.asyncio
    async def test_starttls_order(self, mail_env, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_SECURITY", "starttls")
        monkeypatch.setenv("SMTP_USER", "u")
        msg = mailer.build_message(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=b"x")
        await mailer.send_message(msg)
        (srv,) = fake_smtp.instances
        assert not isinstance(srv, FakeSMTPSSL), "starttls начинается с обычного соединения"
        assert srv.port == 587
        assert [c[0] for c in srv.calls] == ["starttls", "login", "send"]

    @pytest.mark.asyncio
    async def test_none_security_without_user_skips_login(self, mail_env, fake_smtp, monkeypatch):
        monkeypatch.setenv("SMTP_SECURITY", "none")
        monkeypatch.setenv("SMTP_PORT", "25")
        monkeypatch.delenv("SMTP_USER", raising=False)
        msg = mailer.build_message(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=b"x")
        await mailer.send_message(msg)
        (srv,) = fake_smtp.instances
        assert not isinstance(srv, FakeSMTPSSL)
        assert srv.port == 25 and [c[0] for c in srv.calls] == ["send"]

    @pytest.mark.asyncio
    async def test_errors_are_wrapped(self, mail_env, fake_smtp):
        fake_smtp.fail_on_send = True
        msg = mailer.build_message(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=b"x")
        with pytest.raises(mailer.MailError, match="OSError: network down"):
            await mailer.send_message(msg)

    @pytest.mark.asyncio
    async def test_not_configured_raises(self, no_mail_env):
        with pytest.raises(mailer.MailError, match="не настроена"):
            await mailer.send_message(object())

    @pytest.mark.asyncio
    async def test_bad_port_is_a_mail_error_not_a_crash(self, mail_env, monkeypatch):
        monkeypatch.setenv("SMTP_PORT", "abc")
        msg = mailer.build_message(to="p@example", subject="S", body="B", attachment_name="x.pdf", attachment_bytes=b"x")
        with pytest.raises(mailer.MailError):
            await mailer.send_message(msg)

    @pytest.mark.asyncio
    async def test_deliver_never_raises(self, init_test_db, mail_env):
        _partner()
        with patch.object(partner_store, "claim_delivery", side_effect=RuntimeError("db locked")):
            ok = await partner_delivery.deliver_order_pdf(
                "o1", "reklamacity", "1x1", json.dumps(VALID_CONFIG), PDF_BYTES,
            )
        assert ok is False
