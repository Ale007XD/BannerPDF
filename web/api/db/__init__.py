"""
db/__init__.py
~~~~~~~~~~~~~~
Подключение к SQLite banner_web.db.
WAL-режим, row_factory = sqlite3.Row для доступа по имени.

Использование:
    with get_db() as conn:
        row = conn.execute("SELECT ...").fetchone()
        conn.execute("INSERT ...")   # автокоммит при выходе из контекста
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.getenv("WEB_DB_PATH", "/app/data/banner_web.db")


def _get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_db():
    """
    Контекстный менеджер для работы с БД.
    Коммитит транзакцию при выходе, откатывает при исключении.
    """
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """
    Инициализирует схему БД из schema.sql.
    Вызывается из lifespan FastAPI при старте.
    Идемпотентна — безопасно вызывать при каждом запуске.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    conn = _get_connection()
    try:
        # Выполняем скрипт целиком (может содержать несколько операторов)
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()
