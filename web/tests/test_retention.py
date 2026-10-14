"""Удаление персональных данных заказов по сроку хранения."""
import sqlite3
from datetime import datetime, timedelta, timezone

from web.api.services.retention import purge_order_pii


def _insert(db_path, order_id, days_ago, *, status="paid", amended=False):
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at, "
        "accepted_at, accepted_ip, accepted_ua, offer_version, original_config_json, yookassa_payment_id) "
        "VALUES (?, 299, '2x1', ?, ?, ?, ?, ?, '203.0.113.7', 'Mozilla/5.0', '2026-10-03', ?, 'pay-1')",
        (order_id, '{"text_lines":[{"text":"АРЕНДА 8-900-000-00-00","scale":1.0}]}', status, when, when, when,
         '{"text_lines":[{"text":"старый","scale":1.0}]}' if amended else None),
    )
    conn.commit()
    conn.close()


def _row(db_path, order_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM web_orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    return row


def test_old_order_loses_text_ip_and_user_agent(set_env, init_test_db):
    _insert(init_test_db, "old-1", 20, amended=True)
    assert purge_order_pii(14) == 1
    row = _row(init_test_db, "old-1")
    assert row["config_json"] == "" and row["original_config_json"] is None
    assert row["accepted_ip"] is None and row["accepted_ua"] is None


def test_non_personal_data_is_kept(set_env, init_test_db):
    _insert(init_test_db, "old-2", 30)
    purge_order_pii(14)
    row = _row(init_test_db, "old-2")
    assert (row["amount_rub"], row["size_key"], row["status"]) == (299, "2x1", "paid")
    assert row["yookassa_payment_id"] == "pay-1" and row["offer_version"] == "2026-10-03" and row["accepted_at"]


def test_fresh_order_is_untouched(set_env, init_test_db):
    _insert(init_test_db, "new-1", 2)
    assert purge_order_pii(14) == 0
    row = _row(init_test_db, "new-1")
    assert "АРЕНДА" in row["config_json"] and row["accepted_ip"] == "203.0.113.7"


def test_purge_is_idempotent(set_env, init_test_db):
    _insert(init_test_db, "old-3", 40)
    assert purge_order_pii(14) == 1
    assert purge_order_pii(14) == 0


def test_amend_window_is_shorter_than_retention():
    from web.api.routers.order import AMEND_WINDOW_HOURS
    from web.api.services.config import ORDER_PII_RETENTION_DAYS

    assert AMEND_WINDOW_HOURS / 24 < ORDER_PII_RETENTION_DAYS
    assert ORDER_PII_RETENTION_DAYS >= 10  # срок рассмотрения обращения по возврату (requisites.html)


async def test_download_of_purged_order_is_410_not_500(client, init_test_db):
    from web.api.services.token_store import create_token

    _insert(init_test_db, "old-4", 20)
    purge_order_pii(14)
    token = create_token("old-4")
    resp = await client.get(f"/api/download/{token}")
    assert resp.status_code == 410
