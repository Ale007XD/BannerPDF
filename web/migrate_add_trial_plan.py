"""
migrate_add_trial_plan.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Добавляет тариф Trial (3 бесплатные генерации) в таблицу api_plans.
Идемпотентно: повторный запуск безопасен (INSERT OR IGNORE).

Запуск:
    docker compose exec api python migrate_add_trial_plan.py
"""

import sys

sys.path.insert(0, "/app")

from api.db import get_db

with get_db() as conn:
    conn.execute(
        """
        INSERT OR IGNORE INTO api_plans (id, name, pdf_limit, rpm_limit, price_rub)
        VALUES ('trial', 'Trial', 3, 5, 0)
        """
    )
    row = conn.execute(
        "SELECT * FROM api_plans WHERE id = 'trial'"
    ).fetchone()
    print(f"OK: план trial — {dict(row)}")
