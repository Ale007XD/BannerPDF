"""
test_batch_charge.py
~~~~~~~~~~~~~~~~~~~~
Лимит PDF за batch-задачу списывается один раз — при первой выдаче ZIP.

Рендер не нужен: задачи и архивы создаются напрямую (GS не требуется).
"""

import asyncio
import sqlite3
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from web.api.db import init_db
from web.api.services import batch_worker
from web.api.services.api_key_store import charge_batch_job, create_api_key


def _used(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT pdf_used FROM api_keys").fetchone()[0]
    finally:
        conn.close()


def _make_job(db_path, batch_dir, key_id, *, total, done, status="ready", with_zip=True):
    """Создаёт строку batch_jobs и (опционально) ZIP с `done` файлами."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO batch_jobs (id, api_key_id, status, total, done, errors_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
        (job_id, key_id, status, total, done, now.isoformat(), (now + timedelta(hours=1)).isoformat()),
    )
    conn.commit()
    conn.close()
    if with_zip:
        with zipfile.ZipFile(batch_dir / f"{job_id}.zip", "w") as zf:
            for i in range(done):
                zf.writestr(f"banner_{i + 1:04d}.pdf", b"%PDF-1.4\n%%EOF\n")
    return job_id


@pytest.fixture
def batch_env(client, tmp_db_path, tmp_path, monkeypatch):
    """Ключ Business + каталог для ZIP во временной папке."""
    monkeypatch.setattr(batch_worker, "BATCH_DIR", str(tmp_path))
    key = create_api_key(plan_id="business", label="t", email="t@example.com")
    conn = sqlite3.connect(tmp_db_path)
    key_id = conn.execute("SELECT id FROM api_keys").fetchone()[0]
    conn.close()
    return {"client": client, "db": tmp_db_path, "dir": tmp_path,
            "headers": {"Authorization": f"Bearer {key}"}, "key_id": key_id}


async def _download(env, job_id):
    return await env["client"].get(f"/api/v1/batch/{job_id}/download", headers=env["headers"])


async def test_first_download_charges_done_files_once(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=4, done=4)
    assert _used(e["db"]) == 0

    r1 = await _download(e, job)
    assert r1.status_code == 200
    assert r1.headers["x-files-count"] == "4"
    assert _used(e["db"]) == 4

    r2 = await _download(e, job)  # повторная выдача того же архива
    assert r2.status_code == 200
    assert _used(e["db"]) == 4, "повторное скачивание не должно списывать лимит"


async def test_charge_is_done_not_total(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=5, done=3)  # 2 файла завершились ошибкой
    assert (await _download(e, job)).status_code == 200
    assert _used(e["db"]) == 3


async def test_not_ready_job_is_not_charged(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=4, done=1, status="processing", with_zip=False)
    r = await _download(e, job)
    assert r.status_code == 202
    assert _used(e["db"]) == 0


async def test_expired_archive_is_not_charged(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=2, done=2, with_zip=False)  # ZIP удалён по TTL
    r = await _download(e, job)
    assert r.status_code == 410
    assert _used(e["db"]) == 0


async def test_each_job_is_charged_separately(batch_env):
    e = batch_env
    a = _make_job(e["db"], e["dir"], e["key_id"], total=2, done=2)
    b = _make_job(e["db"], e["dir"], e["key_id"], total=3, done=3)
    for job in (a, b, a, b):
        assert (await _download(e, job)).status_code == 200
    assert _used(e["db"]) == 5


async def test_parallel_downloads_charge_once(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=6, done=6)
    results = await asyncio.gather(*(_download(e, job) for _ in range(5)))
    assert all(r.status_code == 200 for r in results)
    assert _used(e["db"]) == 6


def test_charge_batch_job_claims_only_once(batch_env):
    e = batch_env
    job = _make_job(e["db"], e["dir"], e["key_id"], total=3, done=3)
    assert charge_batch_job(job, e["key_id"], 3) is True
    assert charge_batch_job(job, e["key_id"], 3) is False
    assert _used(e["db"]) == 3


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="DROP COLUMN нужен SQLite ≥ 3.35")
def test_init_db_adds_charged_at_to_legacy_batch_jobs(tmp_db_path):
    init_db()
    conn = sqlite3.connect(tmp_db_path)
    conn.execute("ALTER TABLE batch_jobs DROP COLUMN charged_at")  # имитируем БД до этого релиза
    conn.commit()
    assert "charged_at" not in {r[1] for r in conn.execute("PRAGMA table_info(batch_jobs)")}
    conn.close()

    init_db()

    conn = sqlite3.connect(tmp_db_path)
    assert "charged_at" in {r[1] for r in conn.execute("PRAGMA table_info(batch_jobs)")}
    conn.close()
