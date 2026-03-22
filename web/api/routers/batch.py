"""
batch.py
~~~~~~~~
Batch-рендеринг PDF.

POST /api/v1/batch/submit          — отправить задание (CSV + параметры)
GET  /api/v1/batch/{job_id}        — статус и прогресс
GET  /api/v1/batch/{job_id}/download — скачать ZIP (когда status=ready)
"""

import csv
import io
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..services.api_key_store import increment_pdf_usage
from ..services.batch_worker import get_job_status, get_job_zip_path, submit_job
from ..services.sanitizer import validate_banner_config
from .corp_api import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/batch")

# Максимум строк в одном batch-задании
BATCH_MAX_ITEMS = 500


# ---------------------------------------------------------------------------
# POST /api/v1/batch/submit
# ---------------------------------------------------------------------------

@router.post("/submit")
async def batch_submit(
    request: Request,
    file: UploadFile = File(..., description="CSV-файл: каждая строка — отдельный баннер"),
    bg_color:    str  = Form(...),
    text_color:  str  = Form(...),
    font:        str  = Form(...),
    size_key:    str  | None = Form(None),
    width_mm:    int  | None = Form(None),
    height_mm:   int  | None = Form(None),
    api_key: dict = Depends(require_api_key),
) -> dict:
    """
    Принимает CSV-файл и общие параметры шаблона.

    Формат CSV (без заголовка):
        line1,line2,line3
        АКЦИЯ,Скидки до 70%,
        РАСПРОДАЖА,Только сегодня,Магазин

    Каждая непустая строка CSV → один PDF в итоговом ZIP.
    Пустые строки пропускаются.
    """
    # --- Парсинг CSV ---
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")  # utf-8-sig убирает BOM от Excel
    except Exception:
        raise HTTPException(status_code=422, detail="Не удалось прочитать CSV-файл")

    reader = csv.reader(io.StringIO(text))
    items: list[dict] = []

    for i, row in enumerate(reader, start=1):
        # Фильтруем полностью пустые строки
        if not any(cell.strip() for cell in row):
            continue

        text_lines = [
            {"text": cell.strip(), "scale": 1.0}
            for cell in row
            if cell.strip()
        ]
        if not text_lines:
            continue

        config = {
            "bg_color":    bg_color,
            "text_color":  text_color,
            "font":        font,
            "text_lines":  text_lines,
        }
        if size_key:
            config["size_key"] = size_key
        elif width_mm and height_mm:
            config["width_mm"]  = width_mm
            config["height_mm"] = height_mm
        else:
            raise HTTPException(
                status_code=422,
                detail="Укажите size_key ИЛИ width_mm + height_mm",
            )

        # Валидируем каждый конфиг
        errors = validate_banner_config(config)
        if errors:
            raise HTTPException(
                status_code=422,
                detail=f"Строка CSV {i}: {'; '.join(errors)}",
            )

        items.append({
            "config":   config,
            "filename": f"banner_{len(items)+1:04d}.pdf",
        })

        if len(items) > BATCH_MAX_ITEMS:
            raise HTTPException(
                status_code=422,
                detail=f"Превышен лимит строк: максимум {BATCH_MAX_ITEMS} баннеров за один запрос",
            )

    if not items:
        raise HTTPException(status_code=422, detail="CSV-файл не содержит данных")

    # --- Проверка PDF-лимита для всего задания ---
    pdf_limit = api_key["pdf_limit"]
    if pdf_limit != -1:
        remaining = pdf_limit - api_key["pdf_used"]
        if len(items) > remaining:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Недостаточно PDF в лимите: нужно {len(items)}, доступно {remaining}. "
                    "Обновите план."
                ),
            )

    # --- Создаём задачу ---
    job_id = str(uuid.uuid4())
    await submit_job(job_id, api_key["id"], items)

    logger.info(
        "batch_submit: job=%s ключ=%s %d файлов",
        job_id[:8], api_key["key_prefix"], len(items),
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "total":  len(items),
    }


# ---------------------------------------------------------------------------
# GET /api/v1/batch/{job_id}
# ---------------------------------------------------------------------------

@router.get("/{job_id}")
async def batch_status(
    job_id: str,
    api_key: dict = Depends(require_api_key),
) -> dict:
    """
    Возвращает статус и прогресс batch-задачи.
    Только владелец задачи (api_key_id совпадает) может её видеть.
    """
    job = get_job_status(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    # Проверка владельца
    if job["api_key_id"] != api_key["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа к этой задаче")

    return {
        "job_id":     job["id"],
        "status":     job["status"],        # queued|processing|ready|failed
        "total":      job["total"],
        "done":       job["done"],
        "errors":     job["errors"],        # [{file, error}, ...]
        "created_at": job["created_at"],
        "ready_at":   job.get("ready_at"),
        "expires_at": job.get("expires_at"),
    }


# ---------------------------------------------------------------------------
# GET /api/v1/batch/{job_id}/download
# ---------------------------------------------------------------------------

@router.get("/{job_id}/download")
async def batch_download(
    job_id: str,
    api_key: dict = Depends(require_api_key),
) -> FileResponse:
    """
    Возвращает ZIP-архив с PDF-файлами.
    Доступен только когда status=ready, TTL 1 час.
    После успешного скачивания — счётчик pdf_used увеличивается
    на фактическое количество сгенерированных файлов.
    """
    job = get_job_status(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if job["api_key_id"] != api_key["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа к этой задаче")

    if job["status"] == "queued" or job["status"] == "processing":
        raise HTTPException(
            status_code=202,
            detail=f"Задача ещё не готова: {job['done']}/{job['total']}",
        )

    if job["status"] == "failed" and job["done"] == 0:
        raise HTTPException(status_code=500, detail="Задача завершилась с ошибкой, файлов нет")

    zip_path = get_job_zip_path(job_id)
    if zip_path is None:
        raise HTTPException(
            status_code=410,
            detail="Архив истёк или не найден. TTL batch-архива — 1 час.",
        )

    # Увеличиваем счётчик на кол-во успешно сгенерированных PDF
    for _ in range(job["done"]):
        increment_pdf_usage(api_key["id"])

    logger.info(
        "batch_download: job=%s ключ=%s %d файлов",
        job_id[:8], api_key["key_prefix"], job["done"],
    )

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=f"banners_{job_id[:8]}.zip",
        headers={
            "X-Files-Count": str(job["done"]),
            "X-Errors-Count": str(len(job["errors"])),
        },
    )
