"""
test_order_amend.py
~~~~~~~~~~~~~~~~~~~
Одна бесплатная правка текста оплаченного заказа:
GET/POST /api/order/{order_id}/amend

Покрывает:
  - успех: текст заменён, оригинал сохранён, выдан новый токен, правка израсходована
  - второй вызов → 409; параллельное занятие правки → 409
  - окно 24 ч от paid_at: внутри — ок, снаружи → 410
  - неоплаченный заказ → 409; неизвестный → 404
  - число строк менять нельзя; пустая строка / тот же текст → 422 и правка НЕ расходуется
  - размер, шрифт, цвета и scale остаются из оплаченного заказа
  - новый токен отдаёт PDF с исправленным текстом (рендер видит новый config_json)
  - GET: предзаполнение формы
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from test_download_api import PDF_BYTES, _render_with

from web.api.routers.order import AMEND_WINDOW_HOURS

CONFIG = {
    "size_key":   "1x0.5",
    "bg_color":   "Белый",
    "text_color": "Черный",
    "font":       "Golos Text",
    "text_lines": [
        {"text": "Аренда склада", "scale": 1.0},
        {"text": "+7 914 000-00-0O", "scale": 0.8},
    ],
}


def _insert(db_path: str, order_id: str, *, status="token_issued", paid_ago_h=1.0, amended=False) -> None:
    now = datetime.now(timezone.utc)
    paid_at = (now - timedelta(hours=paid_ago_h)).isoformat() if status != "pending" else None
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at, amended_at) "
        "VALUES (?, 299, '1x0.5', ?, ?, ?, ?, ?)",
        (order_id, json.dumps(CONFIG, ensure_ascii=False), status, now.isoformat(), paid_at,
         now.isoformat() if amended else None),
    )
    conn.commit()
    conn.close()


def _order(db_path: str, order_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


FIXED = ["Аренда склада", "+7 914 000-00-00"]


class TestAmendSuccess:

    @pytest.mark.asyncio
    async def test_text_replaced_original_kept_token_issued(self, client, init_test_db):
        _insert(init_test_db, "am-001")
        resp = await client.post("/api/order/am-001/amend", json={"text_lines": FIXED})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["download_token"]) == 64

        row = _order(init_test_db, "am-001")
        saved = json.loads(row["config_json"])
        assert [ln["text"] for ln in saved["text_lines"]] == FIXED
        assert json.loads(row["original_config_json"]) == CONFIG
        assert row["amended_at"] is not None

    @pytest.mark.asyncio
    async def test_geometry_style_and_scale_are_preserved(self, client, init_test_db):
        _insert(init_test_db, "am-002")
        await client.post("/api/order/am-002/amend", json={"text_lines": FIXED})
        saved = json.loads(_order(init_test_db, "am-002")["config_json"])
        for key in ("size_key", "bg_color", "text_color", "font"):
            assert saved[key] == CONFIG[key]
        assert [ln["scale"] for ln in saved["text_lines"]] == [1.0, 0.8]

    @pytest.mark.asyncio
    async def test_paid_but_token_not_yet_issued_is_allowed(self, client, init_test_db):
        _insert(init_test_db, "am-003", status="paid")
        resp = await client.post("/api/order/am-003/amend", json={"text_lines": FIXED})
        assert resp.status_code == 200


class TestAmendLimits:

    @pytest.mark.asyncio
    async def test_second_amend_is_refused(self, client, init_test_db):
        _insert(init_test_db, "am-010")
        assert (await client.post("/api/order/am-010/amend", json={"text_lines": FIXED})).status_code == 200
        resp = await client.post("/api/order/am-010/amend", json={"text_lines": ["Другое", "+7 000"]})
        assert resp.status_code == 409
        saved = json.loads(_order(init_test_db, "am-010")["config_json"])
        assert [ln["text"] for ln in saved["text_lines"]] == FIXED  # вторая правка не применилась

    @pytest.mark.asyncio
    async def test_already_amended_order_is_refused(self, client, init_test_db):
        _insert(init_test_db, "am-011", amended=True)
        resp = await client.post("/api/order/am-011/amend", json={"text_lines": FIXED})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_inside_window_ok(self, client, init_test_db):
        _insert(init_test_db, "am-012", paid_ago_h=AMEND_WINDOW_HOURS - 1)
        resp = await client.post("/api/order/am-012/amend", json={"text_lines": FIXED})
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_outside_window_is_gone(self, client, init_test_db):
        _insert(init_test_db, "am-013", paid_ago_h=AMEND_WINDOW_HOURS + 1)
        resp = await client.post("/api/order/am-013/amend", json={"text_lines": FIXED})
        assert resp.status_code == 410
        assert _order(init_test_db, "am-013")["amended_at"] is None

    @pytest.mark.asyncio
    async def test_unpaid_order_refused(self, client, init_test_db):
        _insert(init_test_db, "am-014", status="pending")
        resp = await client.post("/api/order/am-014/amend", json={"text_lines": FIXED})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_order_404(self, client):
        resp = await client.post("/api/order/nope/amend", json={"text_lines": FIXED})
        assert resp.status_code == 404


class TestAmendValidation:

    @pytest.mark.asyncio
    async def test_line_count_cannot_change(self, client, init_test_db):
        _insert(init_test_db, "am-020")
        for lines in (["Только одна"], ["Раз", "Два", "Три"]):
            resp = await client.post("/api/order/am-020/amend", json={"text_lines": lines})
            assert resp.status_code == 422
        assert _order(init_test_db, "am-020")["amended_at"] is None

    @pytest.mark.asyncio
    async def test_empty_line_refused_and_amend_not_spent(self, client, init_test_db):
        _insert(init_test_db, "am-021")
        resp = await client.post("/api/order/am-021/amend", json={"text_lines": ["Аренда склада", "   "]})
        assert resp.status_code == 422
        assert _order(init_test_db, "am-021")["amended_at"] is None

    @pytest.mark.asyncio
    async def test_unchanged_text_refused_and_amend_not_spent(self, client, init_test_db):
        _insert(init_test_db, "am-022")
        same = [ln["text"] for ln in CONFIG["text_lines"]]
        resp = await client.post("/api/order/am-022/amend", json={"text_lines": same})
        assert resp.status_code == 422
        assert "не изменён" in resp.json()["detail"]
        assert _order(init_test_db, "am-022")["amended_at"] is None

    @pytest.mark.asyncio
    async def test_control_chars_are_sanitized(self, client, init_test_db):
        _insert(init_test_db, "am-023")
        resp = await client.post(
            "/api/order/am-023/amend", json={"text_lines": ["Аренда\x00 склада", "+7  914 000-00-00"]}
        )
        assert resp.status_code == 200
        saved = json.loads(_order(init_test_db, "am-023")["config_json"])
        assert [ln["text"] for ln in saved["text_lines"]] == ["Аренда склада", "+7 914 000-00-00"]

    @pytest.mark.asyncio
    async def test_too_long_line_refused(self, client, init_test_db):
        _insert(init_test_db, "am-024")
        resp = await client.post("/api/order/am-024/amend", json={"text_lines": ["Аренда склада", "x" * 500]})
        assert resp.status_code in (422,)
        assert _order(init_test_db, "am-024")["amended_at"] is None


class TestAmendInfo:

    @pytest.mark.asyncio
    async def test_get_returns_current_lines_and_deadline(self, client, init_test_db):
        _insert(init_test_db, "am-030")
        resp = await client.get("/api/order/am-030/amend")
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_amend"] is True
        assert body["text_lines"] == [ln["text"] for ln in CONFIG["text_lines"]]
        assert datetime.fromisoformat(body["deadline"]) > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_get_after_amend_is_refused(self, client, init_test_db):
        _insert(init_test_db, "am-031")
        await client.post("/api/order/am-031/amend", json={"text_lines": FIXED})
        assert (await client.get("/api/order/am-031/amend")).status_code == 409


class TestAmendThenDownload:

    @pytest.mark.asyncio
    async def test_new_token_renders_the_corrected_text(self, client, init_test_db):
        """Рендер получает config с исправленным текстом, а не оригинал."""
        _insert(init_test_db, "am-040")
        seen = []

        def capture_render(config):
            seen.append(config)
            return PDF_BYTES

        amended = await client.post("/api/order/am-040/amend", json={"text_lines": FIXED})
        token = amended.json()["download_token"]

        with _render_with(capture_render):
            resp = await client.get(f"/api/download/{token}")

        assert resp.status_code == 200
        assert [ln["text"] for ln in seen[0]["text_lines"]] == FIXED
