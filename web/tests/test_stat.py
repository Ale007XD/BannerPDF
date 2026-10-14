"""Собственный счётчик событий: проверка входных значений, подсчёт, отсутствие идентификаторов."""
import sqlite3

import pytest

from web.api.services import stat_store


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT event, page, source, detail, n FROM stat_counters ORDER BY event").fetchall()
    conn.close()
    return rows


def test_record_counts_and_aggregates(set_env, init_test_db):
    assert stat_store.record("pageview", "/", "yandex", "")
    assert stat_store.record("pageview", "/", "yandex", "")
    assert stat_store.record("example_click", "/chto-napisat-na-bannere/", "internal", "open-1")
    rows = _rows(init_test_db)
    assert ("pageview", "/", "yandex", "", 2) in rows
    assert ("example_click", "/chto-napisat-na-bannere/", "internal", "open-1", 1) in rows


@pytest.mark.parametrize("args", [
    ("hack", "/", "direct", ""),                       # событие не из белого списка
    ("pageview", "/../etc/passwd", "direct", ""),      # путь вне шаблона
    ("pageview", "/a/b/", "direct", ""),               # вложенный путь
    ("pageview", "/", "facebook", ""),                 # источник не из списка
    ("pageview", "/", "direct", "<script>"),           # метка с недопустимыми символами
    ("pageview", "/", "direct", "x" * 65),             # слишком длинная метка
])
def test_invalid_events_are_rejected(set_env, init_test_db, args):
    assert stat_store.record(*args) is False
    assert _rows(init_test_db) == []


def test_distinct_keys_per_day_are_capped(set_env, init_test_db, monkeypatch):
    monkeypatch.setattr(stat_store, "MAX_KEYS_PER_DAY", 2)
    assert stat_store.record("example_click", "/", "direct", "a")
    assert stat_store.record("example_click", "/", "direct", "b")
    assert stat_store.record("example_click", "/", "direct", "c") is False
    assert stat_store.record("example_click", "/", "direct", "a")  # существующий ключ продолжает считаться


def test_table_has_no_identifier_columns(set_env, init_test_db):
    conn = sqlite3.connect(init_test_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stat_counters)")}
    conn.close()
    assert cols == {"day", "event", "page", "source", "detail", "n"}


async def test_endpoint_returns_204_and_counts(client, init_test_db):
    resp = await client.post("/api/beat", json={"event": "prefill", "page": "/", "source": "internal", "detail": "/baner-3-na-3/"})
    assert resp.status_code == 204
    assert ("prefill", "/", "internal", "/baner-3-na-3/", 1) in _rows(init_test_db)


async def test_endpoint_ignores_invalid_values_silently(client, init_test_db):
    resp = await client.post("/api/beat", json={"event": "nope", "page": "/", "source": "direct"})
    assert resp.status_code == 204
    assert _rows(init_test_db) == []


async def test_admin_summary_requires_token_and_returns_totals(client, init_test_db):
    stat_store.record("pageview", "/", "google", "")
    stat_store.record("pageview", "/", "google", "")
    assert (await client.get("/api/admin/events")).status_code in (401, 403)
    from web.api.routers import admin  # импорт после установки окружения фикстурами

    resp = await client.get("/api/admin/events?days=7", headers={"Authorization": f"Bearer {admin.ADMIN_TOKEN}"})
    assert resp.status_code == 200
    assert resp.json()["totals"]["pageview"] == 2
