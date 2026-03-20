"""
batch_worker.py
~~~~~~~~~~~~~~~
Фоновый worker для batch-рендеринга PDF через ProcessPoolExecutor.

Архитектурное решение (закрыто):
  - asyncio.Queue(maxsize=50) принимает задачи
  - ProcessPoolExecutor(max_workers=2) обрабатывает CPU-bound GS вызовы
  - Ghostscript НИКОГДА не вызывается напрямую из event loop

Для МВП batch-эндпоинты возвращают 501.
Этот модуль реализует инфраструктуру для будущего включения.
"""

import asyncio
import logging
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import get_db
from .renderer import render_pdf_sync

logger = logging.getLogger(__name__)

BATCH_DIR     = os.getenv("BATCH_DIR", "/tmp/bannerprint_batches")
BATCH_TTL_SEC = 3600  # 1 час — после этого ZIP удаляется

# Глобальная очередь задач
_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
_executor: ProcessPoolExecutor | None = None


def init_batch_worker(executor: ProcessPoolExecutor) -> None:
    """Инициализирует executor. Вызывается из lifespan."""
    global _executor
    _executor = executor
    os.makedirs(BATCH_DIR, exist_ok=True)


async def submit_job(job_id: str, api_key_id: int, items: list[dict]) -> None:
    """
    Добавляет batch-задачу в очередь.
    items: [{"config": {...}, "filename": "banner_001.pdf"}, ...]
    """
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO batch_jobs
                (id, api_key_id, status, total, done, errors_json, created_at, expires_at)
            VALUES (?, ?, 'queued', ?, 0, '[]', ?, ?)
            """,
            (
                job_id,
                api_key_id,
                len(items),
                datetime.now(timezone.utc).isoformat(),
                (datetime.now(timezone.utc) + timedelta(seconds=BATCH_TTL_SEC)).isoformat(),
            ),
        )

    await _queue.put({"job_id": job_id, "items": items})
    logger.info("Batch-задача %s добавлена в очередь (%d файлов)", job_id, len(items))


async def run_worker() -> None:
    """
    Фоновый worker — бесконечный цикл обработки очереди.
    Запускается как asyncio.Task в lifespan FastAPI.
    """
    logger.info("Batch worker запущен")
    while True:
        try:
            task = await asyncio.wait_for(_queue.get(), timeout=5.0)
            await _process_job(task["job_id"], task["items"])
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error("Batch worker: необработанное исключение: %s", e, exc_info=True)


async def _process_job(job_id: str, items: list[dict]) -> None:
    """Обрабатывает один batch-job: рендерит все PDF, пакует в ZIP."""
    _set_status(job_id, "processing")
    loop = asyncio.get_event_loop()

    zip_path = os.path.join(BATCH_DIR, f"{job_id}.zip")
    errors = []
    done = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            filename = item.get("filename", f"banner_{done+1:04d}.pdf")
            config   = item["config"]
            try:
                # CPU-bound — через ProcessPoolExecutor
                pdf_bytes = await loop.run_in_executor(
                    _executor, render_pdf_sync, config
                )
                zf.writestr(filename, pdf_bytes)
                done += 1
            except Exception as e:
                logger.error("Batch %s: ошибка файла %s: %s", job_id, filename, e)
                errors.append({"file": filename, "error": str(e)})

            # Обновляем прогресс в БД
            _update_progress(job_id, done, errors)

    import json
    with get_db() as conn:
        conn.execute(
            """
            UPDATE batch_jobs
            SET status = ?, ready_at = ?, errors_json = ?, done = ?
            WHERE id = ?
            """,
            (
                "ready" if not errors or done > 0 else "failed",
                datetime.now(timezone.utc).isoformat(),
                json.dumps(errors, ensure_ascii=False),
                done,
                job_id,
            ),
        )

    logger.info(
        "Batch %s завершён: %d/%d успешно, %d ошибок",
        job_id, done, len(items), len(errors),
    )


def _set_status(job_id: str, status: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status = ? WHERE id = ?",
            (status, job_id),
        )


def _update_progress(job_id: str, done: int, errors: list) -> None:
    import json
    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET done = ?, errors_json = ? WHERE id = ?",
            (done, json.dumps(errors, ensure_ascii=False), job_id),
        )


def get_job_status(job_id: str) -> dict | None:
    """Возвращает статус batch-задачи."""
    import json
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM batch_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["errors"] = json.loads(d.pop("errors_json", "[]"))
    return d


def get_job_zip_path(job_id: str) -> str | None:
    """Возвращает путь к ZIP-архиву если задача готова."""
    path = os.path.join(BATCH_DIR, f"{job_id}.zip")
    if os.path.exists(path):
        return path
    return None


def cleanup_old_batches() -> int:
    """Удаляет ZIP-архивы старше TTL. Вызывается из lifespan cleanup."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=BATCH_TTL_SEC)
    removed = 0
    batch_path = Path(BATCH_DIR)
    if not batch_path.exists():
        return 0
    for f in batch_path.glob("*.zip"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except Exception as e:
                logger.warning("Не удалось удалить %s: %s", f, e)

    # Помечаем просроченные задачи в БД
    with get_db() as conn:
        conn.execute(
            "UPDATE batch_jobs SET status = 'failed' WHERE expires_at < ? AND status = 'ready'",
            (cutoff.isoformat(),),
        )

    if removed:
        logger.info("Cleanup batch: удалено %d ZIP-архивов", removed)
    return removed
