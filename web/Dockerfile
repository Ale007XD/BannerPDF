# ============================================================
# BannerPrint — Dockerfile
# Python 3.11 + Ghostscript + Pillow + ReportLab
# Шрифты и ICC-профили монтируются с хоста (не копируются в образ)
# ============================================================

FROM python:3.11-slim

# Системные зависимости: Ghostscript + либы для Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        libgomp1 \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-зависимости (слой кэшируется отдельно от кода)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходники
COPY api/ ./api/
COPY frontend/ ./frontend/
COPY templates.json ./templates.json

# Директории для данных и временных файлов
# /app/fonts и /profiles монтируются из хоста через docker-compose
RUN mkdir -p /app/data /app/fonts /profiles /tmp/bannerprint_batches

# Переменные окружения по умолчанию
# Секреты (TONA_*, ADMIN_TOKEN, BOT_INTERNAL_SECRET) — только из .env, не здесь
ENV WEB_DB_PATH=/app/data/banner_web.db \
    FONTS_DIR=/app/fonts \
    FRONTEND_DIR=/app/frontend \
    TEMPLATES_PATH=/app/templates.json \
    BATCH_DIR=/tmp/bannerprint_batches \
    ICC_PROFILE_PATH=/profiles/ISOcoated_v2_300_eci.icc \
    UVICORN_WORKERS=1

EXPOSE 8000

# Строго 1 worker — token_store и order_store в SQLite без внешней синхронизации
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
