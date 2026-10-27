"""
partner_store.py
~~~~~~~~~~~~~~~~
Партнёры-типографии: брендированная страница конструктора, фиксированное вознаграждение
за оплаченный заказ и учёт отправки готового макета на почту типографии.

Таблицы: partners, partner_accruals, partner_deliveries (web/api/db/schema.sql).

Как это связано с реферальной программой (referral_store.py): та работает по 8-символьным
кодам бота и начисляет процент от суммы. Партнёр — отдельная сущность со своим slug
(a-z0-9-, до 32 символов), который уходит в ссылку ?partner=<code>, и фиксированной ставкой.
В заказе партнёр лежит в web_orders.partner_code; если он задан, ref_code в заказ не пишется:
у одной оплаты один получатель вознаграждения.

Инварианты:
  - начисление: одна строка на оплаченный заказ (UNIQUE order_id), reward = min(ставка, сумма заказа),
    бесплатный заказ (сумма 0) ничего не начисляет;
  - баланс не хранится: это SUM(reward_rub) по строкам с paid_out_at IS NULL;
  - выплата помечает строки одним UPDATE и считает сумму по метке этого же вызова;
  - отправка макета: одна строка на (заказ, sha256 config_json); после правки текста
    версия новая, и типография получает исправленный файл.
"""

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from email_validator import EmailNotValidError, validate_email

from ..db import get_db

logger = logging.getLogger(__name__)

DEFAULT_REWARD_RUB = 100
MAX_REWARD_RUB = 1000

# Статусы оплаченного заказа (web_orders.status): после оплаты и после выдачи download-токена
PAID_STATUSES = ("paid", "token_issued")

CODE_RE   = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Логотип: только файл из frontend/partners/ на нашем сервере. Внешние адреса не принимаем: браузер
# посетителя обратился бы к чужому хосту и выдал ему свой IP, а политика данных обещает обратное.
LOGO_RE   = re.compile(r"^/partners/[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
MONTH_RE  = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CONTROL  = re.compile(r"[\u0000-\u001f\u007f]")
# Рабочий телефон типографии: цифры, пробелы, скобки, дефисы, ведущий «+»; «90-20-55», «+7 (843) 290-20-55»
PHONE_RE  = re.compile(r"^\+?[0-9][0-9 ()\-]{3,28}[0-9]$")

# Право на отправку, занятое и не завершённое дольше этого срока (процесс упал), можно занять заново
DELIVERY_STALE_MINUTES = 10
_MAX_ERROR_LEN = 300


class PartnerError(ValueError):
    """Ошибка данных партнёра; текст безопасно показывать администратору."""


class PartnerExists(PartnerError):
    """Партнёр с таким кодом уже есть."""


class PartnerNotFound(PartnerError):
    """Партнёра с таким кодом нет."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_code(value: str | None) -> str | None:
    """Приводит код к нижнему регистру; None, если формат не подходит."""
    if not value:
        return None
    code = value.strip().lower()
    return code if CODE_RE.match(code) else None


# ---------------------------------------------------------------------------
# Партнёры
# ---------------------------------------------------------------------------
def normalize_phone(value: str | None) -> str | None:
    """Телефон типографии или None (не задан). PartnerError, если формат не подходит."""
    phone = " ".join((value or "").split())
    if not phone:
        return None
    if not PHONE_RE.match(phone):
        raise PartnerError("Телефон: 5–30 символов, цифры, пробелы, скобки, дефисы, «+» в начале (например 90-20-55)")
    return phone


def _clean_name(name: str | None) -> str:
    clean = _CONTROL.sub("", name or "").strip()
    if not 1 <= len(clean) <= 80:
        raise PartnerError("Название: от 1 до 80 символов")
    return clean


def _clean_email(email: str | None) -> str:
    try:
        return validate_email((email or "").strip(), check_deliverability=False).normalized
    except EmailNotValidError as e:
        raise PartnerError(f"Некорректный e-mail: {e}") from None


def _clean_logo(logo_url: str | None) -> str | None:
    logo = (logo_url or "").strip() or None
    if logo and not LOGO_RE.match(logo):
        raise PartnerError("Логотип: путь вида /partners/<файл> (файл лежит в web/frontend/partners/)")
    return logo


def _clean_accent(accent: str | None) -> str | None:
    clean = (accent or "").strip() or None
    if clean and not ACCENT_RE.match(clean):
        raise PartnerError("Цвет: формат #rrggbb")
    return clean.lower() if clean else None


def _clean_reward(reward_rub: int) -> int:
    if isinstance(reward_rub, bool) or not isinstance(reward_rub, int) or not 0 <= reward_rub <= MAX_REWARD_RUB:
        raise PartnerError(f"Вознаграждение: от 0 до {MAX_REWARD_RUB} ₽")
    return reward_rub


def _clean_flag(value) -> int:
    """Флажок партнёра (true/false) в 1/0 для SQLite. Число или строка вместо bool считается ошибкой."""
    if not isinstance(value, bool):
        raise PartnerError("auto_email: true или false")
    return 1 if value else 0


def create_partner(
    code: str,
    name: str,
    email: str,
    logo_url: str | None = None,
    accent: str | None = None,
    reward_rub: int = DEFAULT_REWARD_RUB,
    phone: str | None = None,
    auto_email: bool = True,
) -> dict:
    """Создаёт партнёра. PartnerError — неверные данные или код уже занят."""
    norm = normalize_code(code)
    if norm is None:
        raise PartnerError("Код партнёра: 2–32 символа, a-z, 0-9 и дефис, начинается не с дефиса")

    clean_name = _clean_name(name)
    clean_email = _clean_email(email)
    logo = _clean_logo(logo_url)
    clean_accent = _clean_accent(accent)
    reward_rub = _clean_reward(reward_rub)
    clean_phone = normalize_phone(phone)
    clean_auto = _clean_flag(auto_email)

    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO partners (code, name, email, logo_url, accent, reward_rub, phone, auto_email, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (norm, clean_name, clean_email, logo, clean_accent, reward_rub, clean_phone, clean_auto, _now()),
            )
    except sqlite3.IntegrityError:
        raise PartnerExists("Партнёр с таким кодом уже существует") from None

    logger.info("Создан партнёр %s (%s), вознаграждение %d ₽", norm, clean_name, reward_rub)
    return get_partner(norm)


# Поля, которые можно менять после создания (код не меняется: он в ссылках типографии и в заказах)
EDITABLE_FIELDS = ("name", "email", "logo_url", "accent", "reward_rub", "phone", "auto_email")


def update_partner(code: str, changes: dict) -> dict:
    """
    Меняет только переданные поля (EDITABLE_FIELDS) с теми же проверками, что при создании.
    None или пустая строка у logo_url, accent, phone убирают значение; name, email, reward_rub и auto_email обязательны.
    auto_email: True — макет уходит на почту сам при скачивании, False — только по кнопке клиента.
    Ставка влияет только на будущие начисления: уже начисленное хранит свою сумму.
    PartnerNotFound — нет такого партнёра; PartnerError — неверные данные (ничего не записывается).
    """
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise PartnerError("Нельзя изменить: " + ", ".join(unknown))
    norm = normalize_code(code)
    if norm is None or get_partner(norm) is None:
        raise PartnerNotFound("Партнёр не найден")

    cleaners = {
        "name": _clean_name, "email": _clean_email, "logo_url": _clean_logo,
        "accent": _clean_accent, "reward_rub": _clean_reward, "phone": normalize_phone,
        "auto_email": _clean_flag,
    }
    clean = {field: cleaners[field](value) for field, value in changes.items()}
    if clean:
        assignments = ", ".join(f"{field} = ?" for field in clean)   # имена полей только из EDITABLE_FIELDS
        with get_db() as conn:
            conn.execute(f"UPDATE partners SET {assignments} WHERE code = ?", (*clean.values(), norm))
        logger.info("Партнёр %s изменён: %s", norm, ", ".join(clean))
    return get_partner(norm)


def get_partner(code: str) -> dict | None:
    """Партнёр в любом состоянии (в том числе отключённый) или None."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM partners WHERE code = ?", (code,)).fetchone()
    return dict(row) if row else None


def get_active_partner(code: str | None) -> dict | None:
    """Действующий партнёр по коду (регистр не важен) или None."""
    norm = normalize_code(code)
    if norm is None:
        return None
    partner = get_partner(norm)
    return partner if partner and partner["active"] else None


def public_branding(code: str) -> dict | None:
    """Данные для брендирования страницы. Почта и ставка наружу не отдаются; телефон публичный: он в имени файла клиента."""
    partner = get_active_partner(code)
    if partner is None:
        return None
    return {
        "code":     partner["code"],
        "name":     partner["name"],
        "logo_url": partner["logo_url"],
        "accent":   partner["accent"],
        "phone":    partner["phone"],
        # как макет попадёт к типографии: сам («auto») или по кнопке клиента («button»)
        "delivery": "auto" if partner["auto_email"] else "button",
    }


def set_phone(code: str, phone: str | None) -> bool:
    """Меняет телефон партнёра (None или пусто — убрать). False, если такого партнёра нет; PartnerError — неверный формат."""
    clean = normalize_phone(phone)
    with get_db() as conn:
        cur = conn.execute("UPDATE partners SET phone = ? WHERE code = ?", (clean, code))
    return cur.rowcount == 1


def set_active(code: str, active: bool) -> bool:
    """Включает или отключает партнёра. False, если такого нет."""
    with get_db() as conn:
        cur = conn.execute("UPDATE partners SET active = ? WHERE code = ?", (1 if active else 0, code))
    return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Начисление вознаграждения
# ---------------------------------------------------------------------------
def accrue_for_order(order_id: str) -> int:
    """
    Начисляет вознаграждение партнёра за оплаченный заказ (вызывается из transition() при переходе в PAID).

    Возвращает начисленные рубли; 0, если заказ не партнёрский, бесплатный или уже начислен.
    Идемпотентно: повторный вызов по тому же заказу ничего не добавляет (UNIQUE order_id).
    Ставка берётся на момент оплаты, а сумма ограничена тем, что реально заплатил клиент.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT o.amount_rub, p.code, p.reward_rub
              FROM web_orders o
              JOIN partners p ON p.code = o.partner_code
             WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        if row is None:
            return 0

        reward = min(row["reward_rub"], row["amount_rub"])
        if reward <= 0:
            return 0

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO partner_accruals
                (partner_code, order_id, order_amount, reward_rub, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["code"], order_id, row["amount_rub"], reward, _now()),
        )
        created = cur.rowcount == 1

    if created:
        logger.info("Партнёр %s: начислено %d ₽ за заказ %s", row["code"], reward, order_id)
        return reward
    return 0


# ---------------------------------------------------------------------------
# Отчёты и выплата
# ---------------------------------------------------------------------------
def list_partners() -> list[dict]:
    """Партнёры с накопленными суммами и числом неудачных отправок (для админки)."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.code, p.name, p.email, p.phone, p.logo_url, p.accent, p.reward_rub, p.active, p.auto_email, p.created_at,
                   COUNT(a.id)                                                       AS orders,
                   COALESCE(SUM(a.reward_rub), 0)                                    AS earned_rub,
                   COALESCE(SUM(CASE WHEN a.paid_out_at IS NULL THEN a.reward_rub END), 0) AS unpaid_rub,
                   COALESCE(SUM(CASE WHEN a.paid_out_at IS NULL THEN 1 ELSE 0 END), 0)     AS unpaid_orders
              FROM partners p
              LEFT JOIN partner_accruals a ON a.partner_code = p.code
             GROUP BY p.code
             ORDER BY p.created_at DESC
            """
        ).fetchall()
        failed = {
            r["partner_code"]: r["n"]
            for r in conn.execute(
                """
                SELECT partner_code, COUNT(*) AS n FROM partner_deliveries
                 WHERE sent_at IS NULL AND error IS NOT NULL
                 GROUP BY partner_code
                """
            )
        }
    result = [dict(r) for r in rows]
    for item in result:
        item["failed_deliveries"] = failed.get(item["code"], 0)
    return result


def _month_bounds(month: str) -> tuple[str, str]:
    """[начало месяца, начало следующего) в UTC как ISO-строки (сравнение строк корректно для ISO)."""
    if not MONTH_RE.match(month):
        raise PartnerError("Месяц в формате ГГГГ-ММ")
    year, mon = int(month[:4]), int(month[5:])
    nxt_year, nxt_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return f"{year:04d}-{mon:02d}-01", f"{nxt_year:04d}-{nxt_mon:02d}-01"


def month_report(month: str) -> dict:
    """
    Сводка по партнёрам за месяц (месяц по UTC, по моменту оплаты).
    У каждого: сколько заказов и рублей начислено, из них ещё не выплачено.
    """
    start, end = _month_bounds(month)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.code, p.name,
                   COUNT(a.id)                                                       AS orders,
                   COALESCE(SUM(a.reward_rub), 0)                                    AS reward_rub,
                   COALESCE(SUM(CASE WHEN a.paid_out_at IS NULL THEN a.reward_rub END), 0) AS unpaid_rub
              FROM partners p
              JOIN partner_accruals a ON a.partner_code = p.code
             WHERE a.created_at >= ? AND a.created_at < ?
             GROUP BY p.code
             ORDER BY reward_rub DESC, p.code
            """,
            (start, end),
        ).fetchall()
    partners = [dict(r) for r in rows]
    return {
        "month": month,
        "partners": partners,
        "totals": {
            "orders":     sum(p["orders"] for p in partners),
            "reward_rub": sum(p["reward_rub"] for p in partners),
            "unpaid_rub": sum(p["unpaid_rub"] for p in partners),
        },
    }


def mark_paid_out(code: str, month: str | None = None) -> dict:
    """
    Помечает начисления партнёра выплаченными: все невыплаченные или только созданные
    до конца указанного месяца (заказы следующего месяца остаются к выплате).

    Возвращает {"orders": N, "paid_rub": S}: сумма считается по строкам, помеченным этим же
    вызовом, поэтому оплата, пришедшая между просмотром отчёта и нажатием, в неё не попадёт.
    """
    before = _month_bounds(month)[1] if month else None
    stamp = _now()
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM partners WHERE code = ?", (code,)).fetchone() is None:
            raise PartnerNotFound("Партнёр не найден")
        sql = "UPDATE partner_accruals SET paid_out_at = ? WHERE partner_code = ? AND paid_out_at IS NULL"
        params: list = [stamp, code]
        if before:
            sql += " AND created_at < ?"
            params.append(before)
        conn.execute(sql, params)
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(reward_rub), 0) AS s
              FROM partner_accruals WHERE partner_code = ? AND paid_out_at = ?
            """,
            (code, stamp),
        ).fetchone()
    logger.info("Партнёр %s: выплата %d ₽ (%d заказов)", code, row["s"], row["n"])
    return {"orders": row["n"], "paid_rub": row["s"]}


# ---------------------------------------------------------------------------
# Отправка макета на почту партнёра
# ---------------------------------------------------------------------------
def config_hash(config_json: str) -> str:
    return hashlib.sha256(config_json.encode("utf-8")).hexdigest()


def has_other_version(order_id: str, cfg_hash: str) -> bool:
    """Была ли у заказа другая версия макета (после правки текста): тогда письмо «исправленный макет»."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM partner_deliveries WHERE order_id = ? AND config_hash != ? LIMIT 1",
            (order_id, cfg_hash),
        ).fetchone()
    return row is not None


def is_delivered(order_id: str, cfg_hash: str) -> bool:
    """Ушло ли письмо с этой версией макета (sent_at задан)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM partner_deliveries WHERE order_id = ? AND config_hash = ? AND sent_at IS NOT NULL",
            (order_id, cfg_hash),
        ).fetchone()
    return row is not None


def claim_delivery(order_id: str, cfg_hash: str, partner_code: str, force: bool = False) -> bool:
    """
    Занимает право отправить эту версию макета. True — отправляем мы; False — уже отправлено
    или отправляется (параллельное скачивание).

    Занять заново можно: после сбоя (error задан), после зависания дольше DELIVERY_STALE_MINUTES,
    а при force=True (повтор из админки) всегда.
    """
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        if force:
            conn.execute(
                """
                INSERT INTO partner_deliveries (order_id, config_hash, partner_code, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(order_id, config_hash) DO UPDATE
                   SET created_at = excluded.created_at, sent_at = NULL, error = NULL,
                       attempts = attempts + 1
                """,
                (order_id, cfg_hash, partner_code, now.isoformat()),
            )
            return True

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO partner_deliveries (order_id, config_hash, partner_code, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, cfg_hash, partner_code, now.isoformat()),
        )
        if cur.rowcount == 1:
            return True

        cutoff = (now - timedelta(minutes=DELIVERY_STALE_MINUTES)).isoformat()
        cur = conn.execute(
            """
            UPDATE partner_deliveries
               SET created_at = ?, attempts = attempts + 1, error = NULL
             WHERE order_id = ? AND config_hash = ? AND sent_at IS NULL
               AND (error IS NOT NULL OR created_at < ?)
            """,
            (now.isoformat(), order_id, cfg_hash, cutoff),
        )
        return cur.rowcount == 1


DELIVERY_SCAN_LIMIT = 2000   # сколько последних оплаченных партнёрских заказов просматривать при поиске по номеру


def list_deliveries(partner: str | None = None, query: str | None = None,
                    only_failed: bool = False, limit: int = 50) -> list[dict]:
    """
    Оплаченные партнёрские заказы с состоянием письма типографии (для админки), свежие сверху.

    state: sent (ушло), failed (сбой, error задан), sending (идёт или зависло), none (клиент ещё не скачивал
    или, в режиме «кнопка» (auto_email = false), не нажал «Отправить»),
    erased (текст макета стёрт по сроку хранения: повтор невозможен).
    query — номер заказа, который назвал клиент (482-915, 482915, регистр и разделители не важны).
    PartnerError — неверные параметры.
    """
    # импорт здесь, чтобы не связывать модули при загрузке
    from .partner_naming import order_number

    if not 1 <= limit <= 200:
        raise PartnerError("limit: от 1 до 200")
    code = None
    if partner:
        code = normalize_code(partner)
        if code is None:
            raise PartnerError("Партнёр: неверный код")
    digits = "".join(ch for ch in (query or "") if ch.isdigit())
    if query and query.strip() and len(digits) != 6:
        raise PartnerError("Номер заказа: 6 цифр, например 482-915")

    sql = (
        "SELECT o.id, o.partner_code, o.size_key, o.paid_at, o.config_json, p.name AS partner_name, p.auto_email "
        "FROM web_orders o JOIN partners p ON p.code = o.partner_code "
        "WHERE o.status IN (?, ?)"
    )
    args: list = list(PAID_STATUSES)
    if code:
        sql += " AND o.partner_code = ?"
        args.append(code)
    sql += " ORDER BY COALESCE(o.paid_at, o.created_at) DESC LIMIT ?"
    args.append(DELIVERY_SCAN_LIMIT)

    with get_db() as conn:
        orders = conn.execute(sql, args).fetchall()
        ids = [o["id"] for o in orders]
        rows: dict[tuple[str, str], sqlite3.Row] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT * FROM partner_deliveries WHERE order_id IN ({marks})", chunk):
                rows[(r["order_id"], r["config_hash"])] = r

    result = []
    for o in orders:
        number = order_number(o["id"])
        if digits and number.replace("-", "") != digits:
            continue
        cfg = o["config_json"]
        d = rows.get((o["id"], config_hash(cfg))) if cfg else None
        if not cfg:
            state = "erased"
        elif d is None:
            state = "none"
        elif d["sent_at"]:
            state = "sent"
        elif d["error"]:
            state = "failed"
        else:
            state = "sending"
        if only_failed and state != "failed":
            continue
        result.append({
            "order_id": o["id"], "number": number, "partner_code": o["partner_code"],
            "partner_name": o["partner_name"], "size_key": o["size_key"], "paid_at": o["paid_at"],
            "state": state, "error": d["error"] if d and state == "failed" else None,
            "attempts": d["attempts"] if d else 0, "sent_at": d["sent_at"] if d else None,
            "can_resend": state != "erased",
            # False: типография получает макет по кнопке клиента, и state «none» значит «кнопку не нажали»
            "auto_email": bool(o["auto_email"]),
        })
        if len(result) >= limit:
            break
    return result


def mark_delivery_sent(order_id: str, cfg_hash: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE partner_deliveries SET sent_at = ?, error = NULL WHERE order_id = ? AND config_hash = ?",
            (_now(), order_id, cfg_hash),
        )


def mark_delivery_failed(order_id: str, cfg_hash: str, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE partner_deliveries SET error = ? WHERE order_id = ? AND config_hash = ? AND sent_at IS NULL",
            ((error or "ошибка")[:_MAX_ERROR_LEN], order_id, cfg_hash),
        )
