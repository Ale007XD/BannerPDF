"""
test_partner_stats.py
~~~~~~~~~~~~~~~~~~~~~
Статистика по партнёрам за период (services/partner_stats.py, GET /api/admin/partners/stats[.csv]).

Покрывает:
  - период: сегодня / 7 дней / месяц / прошлый месяц / свои даты, границы суток по UTC+3 (и по env), ошибки ввода
  - состав: только оплаченные заказы партнёров, бесплатные видны отдельно, чужие заказы и неоплаченные не попадают
  - границы периода: начало включительно, конец исключительно, на стыке полуночи по Москве
  - «к выплате» не зависит от периода, фильтр по партнёру, письма (отправлено / сбой)
  - CSV: BOM, «;», CRLF, итоговая строка, защита от формул Excel, номер заказа как в файле клиента
  - API: токен, псевдоним from/to, коды ошибок, заголовки выгрузки
"""

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from web.api.services import partner_naming, partner_stats, partner_store
from web.api.services.partner_store import PartnerError, PartnerNotFound

# 19 октября 2026, 14:00 по Москве
NOW = datetime(2026, 10, 19, 11, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _auth() -> dict:
    from web.api.routers import admin
    return {"Authorization": f"Bearer {admin.ADMIN_TOKEN}"}


def _partner(code="reklamacity", name="Реклама-Сити", **kw):
    return partner_store.create_partner(code, name=name, email=f"{code}@example.com", **kw)


def _iso(y, m, d, hh=12, mm=0, ss=0, micro=123456) -> str:
    return datetime(y, m, d, hh, mm, ss, micro, tzinfo=timezone.utc).isoformat()


def _order(path, order_id, code="reklamacity", *, paid_at, amount=299, reward=None, status="token_issued",
           size="2x1", paid_out_at=None, created_at=None):
    """Заказ партнёра и, если reward не None, его начисление (как делает accrue_for_order)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at, partner_code) "
        "VALUES (?, ?, ?, '{}', ?, ?, ?, ?)",
        (order_id, amount, size, status, created_at or paid_at or _iso(2026, 1, 1), paid_at, code),
    )
    if reward is not None:
        conn.execute(
            "INSERT INTO partner_accruals (partner_code, order_id, order_amount, reward_rub, created_at, paid_out_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, order_id, amount, reward, paid_at or created_at, paid_out_at),
        )
    conn.commit()
    conn.close()


def _delivery(path, order_id, code, created_at, *, sent=True, error=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO partner_deliveries (order_id, config_hash, partner_code, created_at, sent_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, "h" + order_id, code, created_at, created_at if sent else None, error),
    )
    conn.commit()
    conn.close()


def _rows(csv_bytes: bytes) -> list[list[str]]:
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""), delimiter=";"))


# ---------------------------------------------------------------------------
# Период
# ---------------------------------------------------------------------------
class TestResolvePeriod:

    @pytest.mark.parametrize("period,expected", [
        ("today", ("2026-10-19", "2026-10-19")),
        ("week", ("2026-10-13", "2026-10-19")),          # 7 суток включая сегодня
        ("month", ("2026-10-01", "2026-10-19")),
        ("prev_month", ("2026-09-01", "2026-09-30")),
        (None, ("2026-10-01", "2026-10-19")),            # по умолчанию месяц
        (" MONTH ", ("2026-10-01", "2026-10-19")),
    ])
    def test_presets(self, period, expected):
        d_from, d_to, _ = partner_stats.resolve_period(period, now=NOW)
        assert (d_from.isoformat(), d_to.isoformat()) == expected

    def test_today_follows_the_moscow_calendar_not_utc(self):
        late = datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc)   # в Москве уже 20 октября, 01:30
        assert partner_stats.resolve_period("today", now=late)[0].isoformat() == "2026-10-20"
        early = datetime(2026, 10, 19, 20, 59, tzinfo=timezone.utc)  # в Москве ещё 19-е, 23:59
        assert partner_stats.resolve_period("today", now=early)[0].isoformat() == "2026-10-19"

    def test_prev_month_across_the_new_year(self):
        d_from, d_to, _ = partner_stats.resolve_period("prev_month", now=datetime(2027, 1, 5, 9, tzinfo=timezone.utc))
        assert (d_from.isoformat(), d_to.isoformat()) == ("2026-12-01", "2026-12-31")

    def test_prev_month_of_march_in_a_leap_year(self):
        d_from, d_to, _ = partner_stats.resolve_period("prev_month", now=datetime(2028, 3, 10, 9, tzinfo=timezone.utc))
        assert (d_from.isoformat(), d_to.isoformat()) == ("2028-02-01", "2028-02-29")

    def test_custom_dates_win_over_a_preset(self):
        d_from, d_to, name = partner_stats.resolve_period("today", "2026-10-01", "2026-10-05", now=NOW)
        assert (d_from.isoformat(), d_to.isoformat(), name) == ("2026-10-01", "2026-10-05", "custom")

    def test_single_day_range_is_allowed(self):
        d_from, d_to, _ = partner_stats.resolve_period(None, "2026-10-05", "2026-10-05", now=NOW)
        assert d_from == d_to

    @pytest.mark.parametrize("args", [
        ("yesterday", None, None),
        (None, "2026-10-01", None),
        (None, None, "2026-10-01"),
        (None, "2026-10-05", "2026-10-01"),               # «с» позже «по»
        (None, "2026-10-01", "2026-10-xx"),
        (None, "20261001", "20261005"),                   # date.fromisoformat принял бы, формат у нас один
        (None, "2026-W42-1", "2026-W43-1"),
        (None, "2026-02-30", "2026-03-01"),
        (None, "1999-01-01", "1999-01-02"),
        (None, "2026-01-01", "2101-01-01"),
        (None, "2026-01-01", "2027-01-02"),               # 367 суток
        (None, "' OR 1=1 --", "2026-10-01"),
    ])
    def test_bad_input_is_a_partner_error(self, args):
        with pytest.raises(PartnerError):
            partner_stats.resolve_period(*args, now=NOW)

    def test_366_days_is_the_longest_allowed(self):
        d_from, d_to, _ = partner_stats.resolve_period(None, "2026-01-01", "2027-01-01", now=NOW)
        assert (d_to - d_from).days + 1 == 366

    def test_utc_bounds_for_moscow(self):
        d = partner_stats.resolve_period("today", now=NOW)
        assert partner_stats._utc_bounds(d[0], d[1]) == ("2026-10-18T21:00:00+00:00", "2026-10-19T21:00:00+00:00")


class TestOffsetSetting:

    def test_default_is_moscow(self, monkeypatch):
        monkeypatch.delenv("STATS_UTC_OFFSET_HOURS", raising=False)
        assert partner_stats.utc_offset_hours() == 3 and partner_stats.tz_label() == "UTC+3"

    @pytest.mark.parametrize("raw,hours,label", [("0", 0, "UTC"), ("7", 7, "UTC+7"), ("-5", -5, "UTC-5"), (" 5 ", 5, "UTC+5")])
    def test_env_is_respected(self, monkeypatch, raw, hours, label):
        monkeypatch.setenv("STATS_UTC_OFFSET_HOURS", raw)
        assert partner_stats.utc_offset_hours() == hours and partner_stats.tz_label() == label

    @pytest.mark.parametrize("raw", ["", "abc", "3.5", "99", "-13", "15"])
    def test_garbage_falls_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("STATS_UTC_OFFSET_HOURS", raw)
        assert partner_stats.utc_offset_hours() == 3

    def test_offset_moves_the_day_boundary(self, monkeypatch):
        monkeypatch.setenv("STATS_UTC_OFFSET_HOURS", "7")
        late = datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)   # Бангкок: 20 октября, 01:30
        d_from, d_to, _ = partner_stats.resolve_period("today", now=late)
        assert d_from.isoformat() == "2026-10-20"
        assert partner_stats._utc_bounds(d_from, d_to)[0] == "2026-10-19T17:00:00+00:00"


# ---------------------------------------------------------------------------
# Состав и суммы
# ---------------------------------------------------------------------------
class TestStats:

    def test_counts_only_paid_partner_orders_in_the_period(self, init_test_db):
        _partner()
        _order(init_test_db, "o-in-1", paid_at=_iso(2026, 10, 19, 8), reward=100)
        _order(init_test_db, "o-in-2", paid_at=_iso(2026, 10, 19, 9), reward=100, size="1x1")
        _order(init_test_db, "o-old", paid_at=_iso(2026, 10, 18, 8), reward=100)                 # вчера
        _order(init_test_db, "o-pending", paid_at=None, status="pending", created_at=_iso(2026, 10, 19, 8))
        _order(init_test_db, "o-failed", paid_at=_iso(2026, 10, 19, 8), status="failed", reward=None)
        conn = sqlite3.connect(init_test_db)                                                        # заказ не партнёра
        conn.execute("INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at) "
                     "VALUES ('o-plain', 299, '1x1', '{}', 'paid', ?, ?)", (_iso(2026, 10, 19, 8), _iso(2026, 10, 19, 8)))
        conn.commit()
        conn.close()

        result = partner_stats.stats("today", now=NOW)
        assert result["totals"] == {"orders": 2, "free_orders": 0, "revenue_rub": 598, "reward_rub": 200,
                                    "unpaid_rub": 300, "mails_failed": 0}
        row = result["partners"][0]
        assert (row["code"], row["name"], row["orders"], row["revenue_rub"], row["reward_rub"]) == \
            ("reklamacity", "Реклама-Сити", 2, 598, 200)

    def test_both_paid_statuses_count(self, init_test_db):
        _partner()
        _order(init_test_db, "o-paid", paid_at=_iso(2026, 10, 19, 8), status="paid", reward=100)
        _order(init_test_db, "o-issued", paid_at=_iso(2026, 10, 19, 8), status="token_issued", reward=100)
        assert partner_stats.stats("today", now=NOW)["totals"]["orders"] == 2

    def test_free_orders_are_visible_but_earn_nothing(self, init_test_db):
        _partner()
        _order(init_test_db, "o-free", paid_at=_iso(2026, 10, 19, 8), amount=0, reward=None)
        _order(init_test_db, "o-full", paid_at=_iso(2026, 10, 19, 9), reward=100)
        totals = partner_stats.stats("today", now=NOW)["totals"]
        assert (totals["orders"], totals["free_orders"], totals["revenue_rub"], totals["reward_rub"]) == (2, 1, 299, 100)

    def test_unpaid_balance_is_all_time_not_the_period(self, init_test_db):
        _partner()
        _order(init_test_db, "o-sep", paid_at=_iso(2026, 9, 3, 8), reward=100)                       # давно, не выплачено
        _order(init_test_db, "o-paid-out", paid_at=_iso(2026, 8, 3, 8), reward=100, paid_out_at=_iso(2026, 9, 10))
        _order(init_test_db, "o-today", paid_at=_iso(2026, 10, 19, 8), reward=100)
        result = partner_stats.stats("today", now=NOW)
        assert result["totals"]["reward_rub"] == 100 and result["totals"]["unpaid_rub"] == 200

    def test_period_boundaries_start_inclusive_end_exclusive(self, init_test_db):
        _partner()
        # 19 октября по Москве = [18 окт 21:00:00 UTC, 19 окт 21:00:00 UTC)
        _order(init_test_db, "b-before", paid_at=datetime(2026, 10, 18, 20, 59, 59, 999999, tzinfo=timezone.utc).isoformat(), reward=100)
        _order(init_test_db, "b-start", paid_at=datetime(2026, 10, 18, 21, 0, 0, tzinfo=timezone.utc).isoformat(), reward=100)
        _order(init_test_db, "b-last", paid_at=datetime(2026, 10, 19, 20, 59, 59, 999999, tzinfo=timezone.utc).isoformat(), reward=100)
        _order(init_test_db, "b-end", paid_at=datetime(2026, 10, 19, 21, 0, 0, tzinfo=timezone.utc).isoformat(), reward=100)
        result = partner_stats.stats("today", now=NOW)
        assert result["totals"]["orders"] == 2          # b-start и b-last
        days = {(d["day"], d["orders"]) for d in result["days"]}
        assert days == {("2026-10-19", 2)}

    def test_local_day_split_in_the_daily_table(self, init_test_db):
        _partner()
        _order(init_test_db, "d-1", paid_at=_iso(2026, 10, 18, 20, 59), reward=100)   # 23:59 по Москве, 18-е
        _order(init_test_db, "d-2", paid_at=_iso(2026, 10, 18, 21, 1), reward=100)    # 00:01 по Москве, 19-е
        _order(init_test_db, "d-3", paid_at=_iso(2026, 10, 19, 5), reward=100)
        days = partner_stats.stats("week", now=NOW)["days"]
        assert [(d["day"], d["orders"], d["reward_rub"]) for d in days] == [("2026-10-18", 1, 100), ("2026-10-19", 2, 200)]

    def test_week_is_seven_days_including_today(self, init_test_db):
        _partner()
        _order(init_test_db, "w-edge", paid_at=_iso(2026, 10, 13, 5), reward=100)     # 13-е: первый день недели
        _order(init_test_db, "w-out", paid_at=_iso(2026, 10, 12, 5), reward=100)
        assert partner_stats.stats("week", now=NOW)["totals"]["orders"] == 1

    def test_month_and_prev_month(self, init_test_db):
        _partner()
        _order(init_test_db, "m-sep", paid_at=_iso(2026, 9, 30, 12), reward=100)
        _order(init_test_db, "m-oct1", paid_at=_iso(2026, 10, 1, 12), reward=100)
        _order(init_test_db, "m-oct19", paid_at=_iso(2026, 10, 19, 12), reward=100)
        assert partner_stats.stats("month", now=NOW)["totals"]["orders"] == 2
        assert partner_stats.stats("prev_month", now=NOW)["totals"]["orders"] == 1

    def test_custom_dates(self, init_test_db):
        _partner()
        for i, day in enumerate((3, 4, 5, 6)):
            _order(init_test_db, f"c-{i}", paid_at=_iso(2026, 10, day, 12), reward=100)
        result = partner_stats.stats(None, "2026-10-04", "2026-10-05", now=NOW)
        assert (result["from"], result["to"], result["period"], result["tz"]) == ("2026-10-04", "2026-10-05", "custom", "UTC+3")
        assert result["totals"]["orders"] == 2

    def test_partner_without_orders_still_listed(self, init_test_db):
        _partner()
        _partner("other", "Другая")
        _order(init_test_db, "x-1", paid_at=_iso(2026, 10, 19, 8), reward=100)
        rows = partner_stats.stats("today", now=NOW)["partners"]
        assert [(r["code"], r["orders"]) for r in rows] == [("reklamacity", 1), ("other", 0)]

    def test_partner_filter_and_unknown_partner(self, init_test_db):
        _partner()
        _partner("other", "Другая")
        _order(init_test_db, "f-1", paid_at=_iso(2026, 10, 19, 8), reward=100)
        _order(init_test_db, "f-2", "other", paid_at=_iso(2026, 10, 19, 8), reward=100)
        only = partner_stats.stats("today", partner="OTHER", now=NOW)
        assert [r["code"] for r in only["partners"]] == ["other"] and only["totals"]["orders"] == 1
        assert only["partner"] == "other" and len(only["all_partners"]) == 2      # список для выбора остаётся полным
        with pytest.raises(PartnerNotFound):
            partner_stats.stats("today", partner="nobody", now=NOW)
        with pytest.raises(PartnerNotFound):
            partner_stats.stats("today", partner="BAD CODE!", now=NOW)
        assert partner_stats.stats("today", partner="  ", now=NOW)["partner"] is None

    def test_order_without_paid_at_falls_back_to_created_at(self, init_test_db):
        _partner()
        _order(init_test_db, "n-1", paid_at=None, created_at=_iso(2026, 10, 19, 8), reward=None)
        assert partner_stats.stats("today", now=NOW)["totals"]["orders"] == 1

    def test_disabled_partner_keeps_its_history(self, init_test_db):
        _partner()
        _order(init_test_db, "h-1", paid_at=_iso(2026, 10, 19, 8), reward=100)
        partner_store.set_active("reklamacity", False)
        row = partner_stats.stats("today", now=NOW)["partners"][0]
        assert row["orders"] == 1 and row["active"] is False

    def test_mail_counters_use_the_period(self, init_test_db):
        _partner()
        _delivery(init_test_db, "m-1", "reklamacity", _iso(2026, 10, 19, 8))
        _delivery(init_test_db, "m-2", "reklamacity", _iso(2026, 10, 19, 9), sent=False, error="535 auth")
        _delivery(init_test_db, "m-3", "reklamacity", _iso(2026, 10, 19, 9), sent=False)       # в работе, не сбой
        _delivery(init_test_db, "m-4", "reklamacity", _iso(2026, 10, 10, 9), sent=False, error="old")   # вне периода
        result = partner_stats.stats("today", now=NOW)
        row = result["partners"][0]
        assert (row["mails_sent"], row["mails_failed"]) == (1, 1) and result["totals"]["mails_failed"] == 1

    def test_period_total_equals_the_sum_of_days_and_partners(self, init_test_db):
        _partner()
        _partner("other", "Другая")
        for i in range(12):
            _order(init_test_db, f"s-{i}", "other" if i % 3 == 0 else "reklamacity",
                   paid_at=_iso(2026, 10, 1 + i, 10), reward=100, amount=299 if i % 4 else 0)
        result = partner_stats.stats("month", now=NOW)
        assert sum(d["orders"] for d in result["days"]) == result["totals"]["orders"] == 12
        assert sum(d["revenue_rub"] for d in result["days"]) == result["totals"]["revenue_rub"]
        assert sum(r["reward_rub"] for r in result["partners"]) == result["totals"]["reward_rub"]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
class TestCsv:

    def test_orders_csv_layout(self, init_test_db):
        _partner(phone="90-20-55")
        _order(init_test_db, "csv-1", paid_at=_iso(2026, 10, 19, 8, 5), reward=100, size="1.5x1")
        _order(init_test_db, "csv-2", paid_at=_iso(2026, 10, 19, 9, 0), amount=0, reward=None, size="2x1")
        _order(init_test_db, "csv-3", paid_at=_iso(2026, 10, 19, 10, 0), reward=100, paid_out_at=_iso(2026, 10, 19, 11, 0))
        name, content = partner_stats.export_csv("orders", "today", now=NOW)
        assert name == "partners-orders_2026-10-19_2026-10-19.csv"
        assert content.count(b"\r\n") == 5 and b"\r\n" in content           # шапка, 3 заказа, итог; CRLF
        rows = _rows(content)
        assert rows[0] == ["Дата и время", "Партнёр", "Код", "Номер заказа", "Размер, м", "Сумма, ₽", "Начислено, ₽", "Выплачено"]
        assert rows[1] == ["19.10.2026 11:05", "Реклама-Сити", "reklamacity", partner_naming.order_number("csv-1"), "1.5x1", "299", "100", ""]
        assert rows[2][5:7] == ["0", "0"]
        assert rows[3][7] == "19.10.2026"
        assert rows[4] == ["ИТОГО", "", "", "3 шт.", "", "598", "200", ""]

    def test_order_number_is_the_one_the_client_has_in_the_file_name(self, init_test_db):
        _partner(phone="90-20-55")
        _order(init_test_db, "abcdef12-3456", paid_at=_iso(2026, 10, 19, 8), reward=100)
        rows = _rows(partner_stats.export_csv("orders", "today", now=NOW)[1])
        assert rows[1][3] == partner_naming.order_number("abcdef12-3456")
        assert rows[1][3] in partner_naming.client_filename("Реклама-Сити", "90-20-55", "abcdef12-3456")

    def test_days_csv_layout(self, init_test_db):
        _partner()
        _order(init_test_db, "dc-1", paid_at=_iso(2026, 10, 18, 22), reward=100)              # по Москве уже 19-е
        _order(init_test_db, "dc-2", paid_at=_iso(2026, 10, 19, 8), amount=0, reward=None)
        name, content = partner_stats.export_csv("days", "week", now=NOW)
        assert name == "partners-days_2026-10-13_2026-10-19.csv"
        rows = _rows(content)
        assert rows[0] == ["Дата", "Партнёр", "Код", "Заказов", "Из них бесплатных", "Выручка, ₽", "Начислено, ₽"]
        assert rows[1] == ["19.10.2026", "Реклама-Сити", "reklamacity", "2", "1", "299", "100"]
        assert rows[2] == ["ИТОГО", "", "", "2", "1", "299", "100"]

    def test_empty_period_is_a_valid_file_with_a_zero_total(self, init_test_db):
        _partner()
        rows = _rows(partner_stats.export_csv("orders", "today", now=NOW)[1])
        assert len(rows) == 2 and rows[1][0] == "ИТОГО" and rows[1][3] == "0 шт."

    @pytest.mark.parametrize("name", ["=HYPERLINK(\"http://evil\")", "+7 print", "-cmd", "@SUM(1)"])
    def test_names_that_look_like_formulas_are_neutralised(self, init_test_db, name):
        partner_store.create_partner("evil", name=name, email="e@example.com")
        _order(init_test_db, "ev-1", "evil", paid_at=_iso(2026, 10, 19, 8), reward=100)
        for kind in ("orders", "days"):
            rows = _rows(partner_stats.export_csv(kind, "today", now=NOW)[1])
            assert rows[1][1] == "'" + name, kind

    def test_separator_and_quotes_in_a_name_do_not_break_columns(self, init_test_db):
        partner_store.create_partner("qq", name='Печать; "Рога"', email="e@example.com")
        _order(init_test_db, "q-1", "qq", paid_at=_iso(2026, 10, 19, 8), reward=100)
        rows = _rows(partner_stats.export_csv("orders", "today", now=NOW)[1])
        assert rows[1][1] == 'Печать; "Рога"' and len(rows[1]) == len(rows[0])

    def test_unknown_kind_and_bad_period(self, init_test_db):
        with pytest.raises(PartnerError):
            partner_stats.export_csv("xml", "today", now=NOW)
        with pytest.raises(PartnerError):
            partner_stats.export_csv("orders", "decade", now=NOW)

    def test_csv_has_no_client_data(self, init_test_db):
        _partner()
        conn = sqlite3.connect(init_test_db)
        conn.execute("INSERT INTO web_orders (id, amount_rub, size_key, config_json, status, created_at, paid_at, partner_code, "
                     "accepted_ip, accepted_ua) VALUES ('pd-1', 299, '1x1', ?, 'paid', ?, ?, 'reklamacity', '203.0.113.9', 'Mozilla/5.0 SecretUA')",
                     (json.dumps({"text_lines": [{"text": "СЕКРЕТНЫЙ ТЕКСТ"}]}, ensure_ascii=False), _iso(2026, 10, 19, 8), _iso(2026, 10, 19, 8)))
        conn.commit()
        conn.close()
        blob = b"".join(partner_stats.export_csv(k, "today", now=NOW)[1] for k in ("orders", "days"))
        text = blob.decode("utf-8")
        for secret in ("203.0.113.9", "SecretUA", "СЕКРЕТНЫЙ", "pd-1"):
            assert secret not in text, secret


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class TestApi:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/admin/partners/stats", "/api/admin/partners/stats.csv"])
    async def test_requires_the_admin_token(self, client, init_test_db, path):
        assert (await client.get(path)).status_code in (401, 403)
        assert (await client.get(path, headers={"Authorization": "Bearer nope"})).status_code == 403

    @pytest.mark.asyncio
    async def test_json_shape_and_from_to_aliases(self, client, init_test_db):
        _partner()
        _order(init_test_db, "api-1", paid_at=_iso(2026, 10, 4, 8), reward=100)
        resp = await client.get("/api/admin/partners/stats?from=2026-10-01&to=2026-10-05", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["from"] == "2026-10-01" and body["to"] == "2026-10-05" and body["period"] == "custom"
        assert body["totals"]["orders"] == 1 and body["partners"][0]["code"] == "reklamacity"
        assert set(body) == {"period", "from", "to", "tz", "partner", "all_partners", "partners", "totals", "days"}
        assert "email" not in resp.text and "print@" not in resp.text

    @pytest.mark.asyncio
    async def test_default_period_is_the_current_month(self, client, init_test_db):
        _partner()
        body = (await client.get("/api/admin/partners/stats", headers=_auth())).json()
        assert body["period"] == "month" and body["from"].endswith("-01")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query,status", [
        ("period=decade", 422),
        ("from=2026-10-01", 422),
        ("from=2026-10-05&to=2026-10-01", 422),
        ("from=bad&to=worse", 422),
        ("partner=nobody", 404),
        ("period=today&partner=nobody", 404),
    ])
    async def test_error_codes(self, client, init_test_db, query, status):
        _partner()
        resp = await client.get(f"/api/admin/partners/stats?{query}", headers=_auth())
        assert resp.status_code == status and resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_csv_headers_and_body(self, client, init_test_db):
        _partner()
        _order(init_test_db, "api-csv", paid_at=_iso(2026, 10, 4, 8), reward=100)
        resp = await client.get("/api/admin/partners/stats.csv?kind=orders&from=2026-10-01&to=2026-10-05", headers=_auth())
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        assert resp.headers["content-disposition"] == 'attachment; filename="partners-orders_2026-10-01_2026-10-05.csv"'
        assert resp.headers["cache-control"] == "no-store"
        assert resp.content.startswith(b"\xef\xbb\xbf") and b"reklamacity" in resp.content

    @pytest.mark.asyncio
    async def test_csv_kind_days_and_errors(self, client, init_test_db):
        _partner()
        ok = await client.get("/api/admin/partners/stats.csv?kind=days&period=month", headers=_auth())
        assert ok.status_code == 200 and "partners-days_" in ok.headers["content-disposition"]
        assert (await client.get("/api/admin/partners/stats.csv?kind=json", headers=_auth())).status_code == 422
        assert (await client.get("/api/admin/partners/stats.csv?partner=nobody", headers=_auth())).status_code == 404

    @pytest.mark.asyncio
    async def test_filename_cannot_be_injected_through_the_query(self, client, init_test_db):
        _partner()
        resp = await client.get('/api/admin/partners/stats.csv?from=2026-10-01"%0d%0aX:1&to=2026-10-05', headers=_auth())
        assert resp.status_code == 422 and "x" not in resp.headers

    @pytest.mark.asyncio
    async def test_existing_admin_routes_still_work(self, client, init_test_db):
        _partner()
        assert (await client.get("/api/admin/partners", headers=_auth())).status_code == 200
        assert (await client.get("/api/admin/partners/report", headers=_auth())).status_code == 200


def test_week_window_is_exactly_seven_local_days():
    d_from, d_to, _ = partner_stats.resolve_period("week", now=NOW)
    assert (d_to - d_from) == timedelta(days=6)
