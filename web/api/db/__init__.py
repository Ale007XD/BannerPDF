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


def _get_connection() -> sqlite3.Connection:
    db_path = os.getenv("WEB_DB_PATH", "/app/data/banner_web.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
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


# Колонки, добавленные после первого релиза схемы: (таблица, колонка, DDL).
# CREATE TABLE IF NOT EXISTS не меняет уже существующие таблицы, поэтому на
# развёрнутой БД такие колонки добавляются через ALTER TABLE при старте.
# Список только растёт, применение идемпотентно. Значения — константы кода.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("download_tokens", "downloads", "INTEGER NOT NULL DEFAULT 0"),
    ("web_orders", "offer_version", "TEXT"),
    ("web_orders", "accepted_at", "TEXT"),
    ("web_orders", "accepted_ip", "TEXT"),
    ("web_orders", "accepted_ua", "TEXT"),
)


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    """
    Инициализирует схему БД из schema.sql и донакатывает недостающие колонки.
    Вызывается из lifespan FastAPI при старте.
    Идемпотентна — безопасно вызывать при каждом запуске.
    """
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")

    conn = _get_connection()
    try:
        # Выполняем скрипт целиком (может содержать несколько операторов)
        conn.executescript(sql)
        _apply_column_migrations(conn)
        conn.commit()
    finally:
        conn.close()
