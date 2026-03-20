"""
main.py
~~~~~~~
FastAPI приложение BannerPrint.

Lifespan:
  - Инициализация БД (schema.sql)
  - Запуск ProcessPoolExecutor(max_workers=2) для Ghostscript
  - Запуск фонового cleanup просроченных токенов и заказов
  - Запуск batch worker

Uvicorn: строго 1 worker (token_store и order_store в SQLite без внешней синхронизации).
"""

import asyncio
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers import order, payment, preview, download, admin, referral, corp_api, batch
from .services import renderer, batch_worker
from .services.token_store import cleanup_expired as cleanup_tokens
from .services.order_store import cleanup_expired as cleanup_orders

logger = logging.getLogger(__name__)

ALLOWED_ORIGINS  = os.getenv("ALLOWED_ORIGINS", "https://bannerprintbot.ru").split(",")
FRONTEND_DIR     = os.getenv("FRONTEND_DIR", "/app/frontend")
CLEANUP_INTERVAL = 300  # секунд между cleanup-циклами


# ---------------------------------------------------------------------------
# Фоновый cleanup
# ---------------------------------------------------------------------------
async def _cleanup_loop():
    """Периодически удаляет просроченные токены и pending_orders."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            t = cleanup_tokens()
            p = cleanup_orders()
            b = batch_worker.cleanup_old_batches()
            if t or p or b:
                logger.info("Cleanup: токены=%d pending=%d batch_zips=%d", t, p, b)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Cleanup error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Старт
    logger.info("BannerPrint запускается...")

    # Инициализация БД
    init_db()
    logger.info("БД инициализирована")

    # ProcessPoolExecutor для Ghostscript (CPU-bound)
    executor = ProcessPoolExecutor(max_workers=2)
    renderer.set_executor(executor)
    batch_worker.init_batch_worker(executor)
    logger.info("ProcessPoolExecutor(max_workers=2) запущен")

    # Фоновые задачи
    cleanup_task = asyncio.create_task(_cleanup_loop())
    worker_task  = asyncio.create_task(batch_worker.run_worker())

    yield

    # Остановка
    cleanup_task.cancel()
    worker_task.cancel()
    await asyncio.gather(cleanup_task, worker_task, return_exceptions=True)
    executor.shutdown(wait=False)
    logger.info("BannerPrint остановлен")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BannerPrint API",
    description="Сайт-конструктор печатных баннеров",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Роутеры API
app.include_router(preview.router,  prefix="/api")
app.include_router(order.router,    prefix="/api")
app.include_router(payment.router,  prefix="/api")
app.include_router(download.router, prefix="/api")
app.include_router(admin.router,    prefix="/api")
app.include_router(referral.router, prefix="/api")
app.include_router(corp_api.router, prefix="/api")
app.include_router(batch.router,    prefix="/api")

# Health-check
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "bannerprint"}

# Статика фронтенда
import os as _os
if _os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
