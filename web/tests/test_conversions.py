"""Серверные события воронки: оплата и выдача файла считаются без участия браузера."""
import json
import sqlite3

import pytest

from web.api.services import stat_store
from web.api.services.conversions import record_order_event


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT event, page, source, detail, n FROM stat_counters").fetchall()
    conn.close()
    return rows


def test_event_uses_entry_from_order_config(set_env, init_test_db):
    record_order_event("order_paid", json.dumps({"entry": "/baner-arenda/", "font": "Golos Text"}))
    assert ("order_paid", "/", "server", "/baner-arenda/", 1) in _rows(init_test_db)


def test_missing_or_broken_config_still_counts_without_label(set_env, init_test_db):
    record_order_event("order_paid", None)
    record_order_event("file_downloaded", "не json")  # сбой разбора не должен ничего ломать
    record_order_event("order_paid", json.dumps({"entry": "<script>"}))  # недопустимая метка отбрасывается
    rows = _rows(init_test_db)
    assert ("order_paid", "/", "server", "", 2) in rows


@pytest.mark.parametrize("value", ["/baner-3-na-3/", "yandex", "direct", ""])
def test_clean_label_accepts_valid_values(value):
    assert stat_store.clean_label(value) == value


@pytest.mark.parametrize("value", ["<img>", "x" * 65, "a b", None, 5, ["x"]])
def test_clean_label_rejects_invalid_values(value):
    assert stat_store.clean_label(value) == ""


def test_order_request_never_fails_because_of_the_label():
    from web.api.routers.order import OrderRequest

    base = {"bg_color": "Красный", "text_color": "Белый", "font": "Golos Text",
            "text_lines": [{"text": "АРЕНДА", "scale": 100}], "size_key": "2x1"}
    assert OrderRequest(**base, entry="/baner-arenda/").entry == "/baner-arenda/"
    assert OrderRequest(**base, entry="<script>alert(1)</script>").entry == ""
    assert OrderRequest(**base).entry == ""


def test_paid_transition_counts_order_with_its_entry(set_env, init_test_db):
    from datetime import datetime, timezone

    from web.api.routers.order import transition

    conn = sqlite3.connect(init_test_db)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) "
        "VALUES ('cv-1', 299, '2x1', ?, 'pending', ?)",
        (json.dumps({"entry": "/chto-napisat-na-bannere/"}), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    transition("cv-1", "webhook_paid")
    transition("cv-1", "webhook_paid")  # повторное событие (идемпотентность) не считается второй раз
    assert ("order_paid", "/", "server", "/chto-napisat-na-bannere/", 1) in _rows(init_test_db)


def _set_entry(db_path, order_id, entry):
    conn = sqlite3.connect(db_path)
    config = json.loads(conn.execute("SELECT config_json FROM web_orders WHERE id = ?", (order_id,)).fetchone()[0])
    config["entry"] = entry
    conn.execute("UPDATE web_orders SET config_json = ? WHERE id = ?", (json.dumps(config), order_id))
    conn.commit()
    conn.close()


async def test_successful_download_is_counted_with_entry(client, init_test_db):
    from test_download_api import _insert_order, _ok_render, _render_with

    from web.api.services.token_store import create_token

    _insert_order(init_test_db, "cv-dl-1")
    _set_entry(init_test_db, "cv-dl-1", "/baner-3-na-3/")
    token = create_token("cv-dl-1")
    with _render_with(_ok_render):
        resp = await client.get(f"/api/download/{token}")
    assert resp.status_code == 200
    assert ("file_downloaded", "/", "server", "/baner-3-na-3/", 1) in _rows(init_test_db)


async def test_failed_render_is_not_counted_as_download(client, init_test_db):
    from test_download_api import _failing_render, _insert_order, _render_with

    from web.api.services.token_store import create_token

    _insert_order(init_test_db, "cv-dl-2")
    token = create_token("cv-dl-2")
    with _render_with(_failing_render):
        resp = await client.get(f"/api/download/{token}")
    assert resp.status_code == 500
    assert _rows(init_test_db) == []
