"""
batch.py
~~~~~~~~
Batch-рендеринг — заглушки 501 для МВП.
Инфраструктура (batch_worker.py) готова, эндпоинты включатся после МВП.

POST /api/v1/batch/submit
GET  /api/v1/batch/{job_id}
GET  /api/v1/batch/{job_id}/download
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1/batch")


@router.post("/submit")
async def batch_submit():
    raise HTTPException(status_code=501, detail="Batch API: не реализовано в МВП")


@router.get("/{job_id}")
async def batch_status(job_id: str):
    raise HTTPException(status_code=501, detail="Batch API: не реализовано в МВП")


@router.get("/{job_id}/download")
async def batch_download(job_id: str):
    raise HTTPException(status_code=501, detail="Batch API: не реализовано в МВП")
