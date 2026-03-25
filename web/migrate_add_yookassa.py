#!/usr/bin/env python3
"""
migrate_add_yookassa.py
~~~~~~~~~~~~~~~~~~~~~~~
Миграция БД: добавление колонки yookassa_payment_id в таблицу web_orders.

Запуск на сервере:
  docker compose exec api python migrate_add_yookassa.py

Безопасно для повторного запуска (проверка IF NOT EXISTS в SQLite 3.35+).
"""

import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

WEB_DB_PATH = os.getenv("WEB_DB_PATH", "/app/data/banner_web.db")


def migrate():
    """Добавляет колонку yookassa_payment_id в web_orders, если её нет."""
    if not os.path.exists(WEB_DB_PATH):
        logger.error("БД не найдена: %s", WEB_DB_PATH)
        sys.exit(1)

    logger.info("Подключение к БД: %s", WEB_DB_PATH)
    conn = sqlite3.connect(WEB_DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        # Проверка версии SQLite (для IF NOT EXISTS в ALTER TABLE нужен 3.35+)
        version = conn.execute("SELECT sqlite_version()").fetchone()[0]
        logger.info("Версия SQLite: %s", version)

        # Проверка наличия колонки
        cursor = conn.execute("PRAGMA table_info(web_orders)")
        columns = [row["name"] for row in cursor.fetchall()]

        if "yookassa_payment_id" in columns:
            logger.info("Колонка yookassa_payment_id уже существует — пропускаем миграцию")
            return

        # Добавление колонки
        logger.info("Добавление колонки yookassa_payment_id...")
        conn.execute("""
            ALTER TABLE web_orders
            ADD COLUMN yookassa_payment_id TEXT DEFAULT NULL
        """)
        conn.commit()
        logger.info("✅ Миграция завершена успешно")

    except sqlite3.Error as e:
        logger.error("Ошибка миграции: %s", e)
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
