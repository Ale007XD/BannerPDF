"""
test_download_api.py
~~~~~~~~~~~~~~~~~~~~
GET /api/download/{token}: токен гасится только после успешного рендера.

Рендер подменяется функцией-заглушкой в ThreadPoolExecutor
(ProcessPoolExecutor требует pickle и Ghostscript).

Покрывает:
  - успешная выдача: 200, PDF, счётчик +1, токен ещё жив
  - сбой рендера: 500, токен НЕ израсходован, повтор успешен
  - лимит MAX_DOWNLOADS: следующая попытка → 404
  - гонка: лимит исчерпан параллельными запросами во время рендера → 404
  - несуществующий / истёкший токен → 404
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from conftest import VALID_CONFIG

from web.api.services.token_store import MAX_DOWNLOADS, create_token, record_download

PDF_BYTES = b"%PDF-1.4 test banner"


def _ok_render(config):
    return PDF_BYTES


def _failing_render(config):
    raise RuntimeError("render boom")


@contextmanager
def _render_with(fn):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with (
            patch("web.api.routers.download.render_pdf_sync", fn),
            patch("web.api.routers.download.get_executor", lambda: pool),
        ):
            yield
    finally:
        pool.shutdown()


def _insert_order(db_path: str, order_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at) "
        "VALUES (?, 299, '1x0.5', ?, 'token_issued', ?)",
        (order_id, json.dumps(VALID_CONFIG, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _row(db_path: str, token: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT used, downloads FROM download_tokens WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    return row


class TestDownload:

    @pytest.mark.asyncio
    async def test_success_returns_pdf_and_counts(self, client, init_test_db):
        _insert_order(init_test_db, "dl-order-001")
        token = create_token("dl-order-001")
        with _render_with(_ok_render):
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == PDF_BYTES
        row = _row(init_test_db, token)
        assert row["downloads"] == 1
        assert row["used"] == 0

    @pytest.mark.asyncio
    async def test_render_failure_does_not_burn_token(self, client, init_test_db):
        _insert_order(init_test_db, "dl-order-002")
        token = create_token("dl-order-002")

        with _render_with(_failing_render):
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 500
        row = _row(init_test_db, token)
        assert row["downloads"] == 0
        assert row["used"] == 0

        # Повтор с исправным рендером проходит по той же ссылке
        with _render_with(_ok_render):
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 200
        assert _row(init_test_db, token)["downloads"] == 1

    @pytest.mark.asyncio
    async def test_limit_exhausted_returns_404(self, client, init_test_db):
        _insert_order(init_test_db, "dl-order-003")
        token = create_token("dl-order-003")
        with _render_with(_ok_render):
            for _ in range(MAX_DOWNLOADS):
                assert (await client.get(f"/api/download/{token}")).status_code == 200
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 404
        assert _row(init_test_db, token)["downloads"] == MAX_DOWNLOADS

    @pytest.mark.asyncio
    async def test_limit_exhausted_during_render_returns_404(self, client, init_test_db):
        """Параллельные запросы выбрали лимит, пока этот рендерился → файл не отдаём."""
        _insert_order(init_test_db, "dl-order-004")
        token = create_token("dl-order-004")

        def render_while_others_finish(config):
            for _ in range(MAX_DOWNLOADS):
                record_download(token)
            return PDF_BYTES

        with _render_with(render_while_others_finish):
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 404
        assert _row(init_test_db, token)["downloads"] == MAX_DOWNLOADS

    @pytest.mark.asyncio
    async def test_unknown_token_returns_404(self, client):
        with _render_with(_ok_render):
            resp = await client.get("/api/download/" + "c" * 64)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_expired_token_returns_404(self, client, init_test_db):
        _insert_order(init_test_db, "dl-order-005")
        token = "d" * 64
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn = sqlite3.connect(init_test_db)
        conn.execute(
            "INSERT INTO download_tokens (token, order_id, expires_at, used) VALUES (?, ?, ?, FALSE)",
            (token, "dl-order-005", past),
        )
        conn.commit()
        conn.close()
        with _render_with(_ok_render):
            resp = await client.get(f"/api/download/{token}")
        assert resp.status_code == 404
