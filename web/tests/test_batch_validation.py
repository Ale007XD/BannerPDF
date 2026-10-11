"""
test_batch_validation.py
~~~~~~~~~~~~~~~~~~~~~~~~
Проверка CSV в POST /api/v1/batch/submit: длина ячейки, размер файла, кодировка.

Постановка задачи в очередь мокается — рендер и GS не нужны.
"""

from unittest.mock import AsyncMock, patch

import pytest

from web.api.routers import batch as batch_router
from web.api.services.api_key_store import create_api_key

FORM = {
    "size_key": "1x0.5",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
}


@pytest.fixture
def env(client):
    """Ключ Business + замоканная очередь (submit_job не ставит ничего в работу)."""
    key = create_api_key(plan_id="business", label="t", email="t@example.com")
    with patch("web.api.routers.batch.submit_job", AsyncMock()) as submit:
        yield {"client": client, "headers": {"Authorization": f"Bearer {key}"}, "submit": submit}


async def _submit(env, content: bytes):
    return await env["client"].post(
        "/api/v1/batch/submit",
        headers=env["headers"],
        data=FORM,
        files={"file": ("banners.csv", content, "text/csv")},
    )


def _csv(*rows: str) -> bytes:
    return ("\r\n".join(rows) + "\r\n").encode("utf-8")


# --- длина ячейки -----------------------------------------------------------

async def test_cell_of_exactly_120_chars_is_accepted(env):
    r = await _submit(env, _csv("Д" * 120))
    assert r.status_code == 200
    assert r.json()["total"] == 1
    env["submit"].assert_awaited_once()


async def test_cell_of_121_chars_is_rejected_with_position(env):
    r = await _submit(env, _csv("короткая", "а," + "Д" * 121))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Строка CSV 2, ячейка 2 (121 симв.)" in detail
    assert "120" in detail
    env["submit"].assert_not_awaited()  # задача не создана


async def test_length_is_counted_in_characters_not_bytes(env):
    # 120 кириллических символов = 240 байт — должно проходить
    r = await _submit(env, _csv("Ё" * 120))
    assert r.status_code == 200


async def test_spaces_around_cell_do_not_count(env):
    r = await _submit(env, _csv('"' + " " * 10 + "x" * 120 + " " * 10 + '"'))
    assert r.status_code == 200


async def test_row_number_counts_blank_lines(env):
    r = await _submit(env, _csv("ok", "", "Д" * 130))
    assert r.status_code == 422
    assert "Строка CSV 3, ячейка 1 (130 симв.)" in r.json()["detail"]


async def test_many_long_cells_report_first_ten_and_the_rest(env):
    r = await _submit(env, _csv(*["Д" * 121] * 12))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail.count("симв.)") == 10
    assert "и ещё 2" in detail
    assert "Строка CSV 10," in detail and "Строка CSV 11," not in detail


# --- размер файла -----------------------------------------------------------

async def test_file_over_limit_is_rejected_with_413(env, monkeypatch):
    monkeypatch.setattr(batch_router, "BATCH_MAX_FILE_BYTES", 100)
    r = await _submit(env, b"a\n" * 51)  # 102 байта
    assert r.status_code == 413
    assert "слишком большой" in r.json()["detail"]
    env["submit"].assert_not_awaited()


async def test_file_exactly_at_limit_is_accepted(env, monkeypatch):
    monkeypatch.setattr(batch_router, "BATCH_MAX_FILE_BYTES", 100)
    r = await _submit(env, b"a\n" * 50)  # ровно 100 байт
    assert r.status_code == 200
    assert r.json()["total"] == 50


def test_default_limit_is_below_nginx_default_of_1_mib():
    # nginx без client_max_body_size режет тело на 1 МиБ; наш JSON-ответ должен срабатывать раньше
    assert batch_router.BATCH_MAX_FILE_BYTES < 1024 * 1024


def test_max_valid_csv_fits_the_limit():
    # 500 строк × 6 ячеек × 120 кириллических символов (2 байта) + запятые и CRLF
    worst = 500 * (6 * 120 * 2 + 5 + 2)
    assert worst < batch_router.BATCH_MAX_FILE_BYTES


# --- кодировка --------------------------------------------------------------

async def test_non_utf8_file_gets_clear_message(env):
    r = await _submit(env, "Привет,Мир\r\n".encode("cp1251"))
    assert r.status_code == 422
    assert "UTF-8" in r.json()["detail"]
    env["submit"].assert_not_awaited()
