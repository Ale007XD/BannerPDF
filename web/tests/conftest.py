"""
conftest.py
~~~~~~~~~~~
Общие фикстуры для тестов BannerPrint.

Ключевые решения:
  - БД во временном файле (tmp_path per-test, WEB_DB_PATH перекрывается monkeypatch)
  - ProcessPoolExecutor мокается — GS не нужен
  - create_payment мокается — HTTP к ЮKassa не делается
  - verify_yookassa_webhook мокается — HTTP к ЮKassa не делается
  - AsyncClient из httpx для тестирования FastAPI
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Путь к тестовой БД — временный файл, чтобы между тестами не было утечек
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def tmp_db_path(tmp_path):
    """Временная SQLite БД на каждый тест."""
    return str(tmp_path / "test_banner_web.db")


@pytest.fixture(scope="function", autouse=True)
def set_env(tmp_db_path, monkeypatch):
    """
    Минимальный набор переменных окружения для тестов.
    Перекрывает реальные значения, чтобы тесты не зависели от хоста.
    """
    monkeypatch.setenv("WEB_DB_PATH", tmp_db_path)
    monkeypatch.setenv("SITE_PDF_PRICE", "299")
    monkeypatch.setenv("SITE_BASE_URL", "https://bannerprintbot.ru")
    monkeypatch.setenv("YOOKASSA_SHOP_ID", "test_shop_id")
    monkeypatch.setenv("YOOKASSA_SECRET_KEY", "test_secret_key")
    monkeypatch.setenv("ADMIN_TOKEN", "test_admin_token_32bytes_padding_x")
    monkeypatch.setenv("BOT_INTERNAL_SECRET", "test_bot_secret")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("FONTS_DIR", str(Path(__file__).parent / "fixtures" / "fonts"))
    monkeypatch.setenv("TEMPLATES_PATH", str(Path(__file__).parent / "fixtures" / "templates.json"))


@pytest.fixture(scope="function")
def init_test_db(tmp_db_path):
    """
    Инициализирует схему в тестовой БД.
    Возвращает путь к файлу БД.
    """
    # conftest.py лежит в web/tests/, schema.sql — в web/api/db/
    schema_path = Path(__file__).parent.parent / "api" / "db" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return tmp_db_path


@pytest_asyncio.fixture(scope="function")
async def client(set_env, init_test_db):
    """
    HTTPX AsyncClient с замоканными внешними зависимостями:
      - create_payment          → возвращает тестовые данные ЮKassa (без HTTP)
      - verify_yookassa_webhook → по умолчанию возвращает успешный платёж (без HTTP)
      - render_preview_base64   → возвращает строку-заглушку
      - ProcessPoolExecutor     → не запускается
    """
    # Мок create_payment — возвращает confirmation_token без HTTP к ЮKassa
    mock_payment = AsyncMock(return_value={
        "yookassa_payment_id": "test_yookassa_payment_id",
        "confirmation_token":  "test_confirmation_token",
    })

    # Заглушка превью — base64 однопиксельного JPEG
    tiny_jpeg_b64 = (
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
        "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
        "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjL/wAARCAABAAEDASIAAhEBAxEB/8QAFgABAQEAAAAAAAAAAAAAAAAABgUE/8QAIRAAAg"
        "ICAwEBAQAAAAAAAAAAAQIDBAUREiExQf/EABQBAQAAAAAAAAAAAAAAAAAAAAD/xAAUEQEA"
        "AAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCwABmSlkknJyk3JyblJScpOTcpKSlJy"
        "b//2Q=="
    )
    mock_preview = MagicMock(return_value=tiny_jpeg_b64)

    with (
        patch("web.api.routers.order.create_payment", mock_payment),
        patch("web.api.routers.preview.render_preview_base64", mock_preview),
        patch("web.api.services.renderer.ProcessPoolExecutor"),
    ):
        from web.api.main import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac


# ---------------------------------------------------------------------------
# Вспомогательные функции для тестов webhook ЮKassa
# ---------------------------------------------------------------------------

def make_yookassa_succeeded_payment(order_id: str,
                                     yookassa_payment_id: str = "test_yk_payment_id",
                                     amount_rub: int = 299) -> dict:
    """
    Возвращает словарь, имитирующий успешный ответ GET /v3/payments/{id} от ЮKassa.
    Используется для мока verify_yookassa_webhook в тестах.
    """
    return {
        "id":     yookassa_payment_id,
        "status": "succeeded",
        "paid":   True,
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "metadata": {"order_id": order_id},
    }


# ---------------------------------------------------------------------------
# Минимальный валидный конфиг баннера
# ---------------------------------------------------------------------------

VALID_CONFIG = {
    "size_key":   "1x0.5",
    "bg_color":   "Белый",
    "text_color": "Черный",
    "font":       "Golos Text",
    "text_lines": [{"text": "Тест баннер", "scale": 1.0}],
}

VALID_ORDER_PAYLOAD = {
    "size_key":   "1x0.5",
    "bg_color":   "Белый",
    "text_color": "Черный",
    "font":       "Golos Text",
    "text_lines": [{"text": "Тест баннер", "scale": 1.0}],
}
