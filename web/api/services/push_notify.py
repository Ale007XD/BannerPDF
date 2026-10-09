"""
push_notify.py
~~~~~~~~~~~~~~
Web Push админу: уведомления о заказах на телефон/компьютер через PWA админки.

Зачем. Канал, не зависящий от Telegram: сайт сам шлёт сообщение в push-сервис браузера
(FCM для Chrome/Android, Mozilla, Apple для Safari PWA, WNS для Edge), тот будит service worker
(/admin/sw.js) на устройстве, и появляется системное уведомление. Нажатие открывает заказ в админке.

Переменные окружения:
  VAPID_PRIVATE_KEY — приватный ключ VAPID (base64url, 32 байта). Пусто — Web Push выключен,
                      все функции модуля работают как no-op.
                      Сгенерировать: docker exec bannerprint_api python -m api.services.push_notify genkey
  VAPID_SUBJECT     — контакт в VAPID-токене (mailto: или https://), по умолчанию SITE_BASE_URL

Правила:
  - push_* никогда не бросают исключений и не блокируют заказ: вызываются из фоновых задач;
  - в уведомлении только ID, размер и сумма (как в Telegram), текст баннера не отправляется;
  - endpoint подписки принимается только от известных push-сервисов (иначе сайт мог бы
    быть заставлен слать запросы на произвольный адрес);
  - подписка, на которую сервис отвечает 404/410, удаляется; после MAX_FAILS подряд сбоев — тоже.
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from ..db import get_db

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
SITE_BASE_URL     = os.getenv("SITE_BASE_URL", "https://bannerbot.ru")
VAPID_SUBJECT     = os.getenv("VAPID_SUBJECT", "").strip() or SITE_BASE_URL

PUSH_TIMEOUT      = 5.0          # сек на один запрос к push-сервису
PUSH_TTL          = 24 * 3600    # сколько push-сервис хранит сообщение для выключенного устройства
MAX_SUBSCRIPTIONS = 10           # устройств админа; старые вытесняются
MAX_FAILS         = 20           # подряд неудач до удаления подписки

# Хосты push-сервисов браузеров (точное совпадение или поддомен).
ALLOWED_HOSTS = (
    "googleapis.com",              # fcm.googleapis.com: Chrome, Edge, Opera, Samsung Internet, Yandex Browser
    "push.services.mozilla.com",   # Firefox
    "push.apple.com",              # Safari / PWA на iOS и macOS
    "notify.windows.com",          # Edge на Windows (WNS)
)

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_background: set = set()
_vapid = None  # кэш объекта Vapid


def enabled() -> bool:
    return bool(VAPID_PRIVATE_KEY)


# ---------------------------------------------------------------------------
# Ключи VAPID
# ---------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    if not value or not _B64URL_RE.match(value):
        raise ValueError("не base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def generate_private_key() -> str:
    """Новый приватный ключ VAPID в формате для VAPID_PRIVATE_KEY."""
    from py_vapid import Vapid

    vapid = Vapid()
    vapid.generate_keys()
    return _b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))


def _get_vapid():
    global _vapid
    if _vapid is None:
        from py_vapid import Vapid

        _vapid = Vapid.from_string(VAPID_PRIVATE_KEY)
    return _vapid


def public_key() -> str:
    """Публичный ключ для applicationServerKey в браузере (base64url, 65 байт)."""
    from cryptography.hazmat.primitives import serialization

    raw = _get_vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(raw)


# ---------------------------------------------------------------------------
# Подписки
# ---------------------------------------------------------------------------
def validate_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    """ValueError с понятным текстом, если подписка не годится."""
    if len(endpoint) > 2000:
        raise ValueError("endpoint слишком длинный")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("endpoint должен быть https-адресом push-сервиса")
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise ValueError(f"push-сервис {host or '?'} не поддерживается")
    try:
        if len(_b64url_decode(p256dh)) != 65 or len(_b64url_decode(auth)) != 16:
            raise ValueError
    except ValueError:
        raise ValueError("некорректные ключи подписки") from None


def save_subscription(endpoint: str, p256dh: str, auth: str, user_agent: Optional[str] = None) -> None:
    """Добавляет подписку или обновляет ключи, если endpoint уже есть. Лишние старые удаляет."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                p256dh = excluded.p256dh, auth = excluded.auth,
                user_agent = excluded.user_agent, fail_count = 0
            """,
            (endpoint, p256dh, auth, (user_agent or "")[:200] or None, now),
        )
        conn.execute(
            """
            DELETE FROM push_subscriptions WHERE id NOT IN (
                SELECT id FROM push_subscriptions ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_SUBSCRIPTIONS,),
        )


def delete_subscription(endpoint: str) -> bool:
    with get_db() as conn:
        return conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).rowcount > 0


def list_subscriptions() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, endpoint, p256dh, auth, user_agent FROM push_subscriptions ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------
def _send_one(sub: dict, payload: str, topic: Optional[str]) -> tuple:
    """Блокирующая отправка одному устройству (вызывается в потоке). → (id, 'ok'|'gone'|'error', http-код)."""
    from pywebpush import WebPushException, webpush

    headers = {"Urgency": "high"}
    if topic:
        headers["Topic"] = topic  # новое сообщение с тем же topic заменяет ещё не доставленное старое
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=payload,
            vapid_private_key=_get_vapid(),
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=PUSH_TTL,
            timeout=PUSH_TIMEOUT,
            headers=headers,
        )
        return sub["id"], "ok", None
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        return sub["id"], ("gone" if status in (404, 410) else "error"), status
    except Exception as exc:  # сеть, DNS, таймаут
        logger.warning("Web Push: %s при отправке на подписку %s", type(exc).__name__, sub["id"])
        return sub["id"], "error", None


def _apply_results(results: list) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    summary = {"sent": 0, "failed": 0, "removed": 0}
    with get_db() as conn:
        for sub_id, outcome, status in results:
            if outcome == "ok":
                summary["sent"] += 1
                conn.execute(
                    "UPDATE push_subscriptions SET fail_count = 0, last_ok_at = ? WHERE id = ?", (now, sub_id)
                )
            elif outcome == "gone":
                summary["removed"] += 1
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,))
            else:
                summary["failed"] += 1
                logger.warning("Web Push: подписка %s, ответ push-сервиса %s", sub_id, status)
                conn.execute("UPDATE push_subscriptions SET fail_count = fail_count + 1 WHERE id = ?", (sub_id,))
        conn.execute("DELETE FROM push_subscriptions WHERE fail_count >= ?", (MAX_FAILS,))
    return summary


def _topic_for(order_id: Optional[str]) -> Optional[str]:
    """Topic для Web Push: base64url-символы, до 32 знаков."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", order_id or "")[:32]
    return cleaned or None


async def _deliver(message: dict, topic: Optional[str] = None) -> dict:
    """Отправляет message всем подпискам параллельно. Никогда не бросает исключений."""
    summary = {"sent": 0, "failed": 0, "removed": 0}
    if not enabled():
        return summary
    try:
        subs = list_subscriptions()
        if not subs:
            return summary
        payload = json.dumps(message, ensure_ascii=False)
        results = await asyncio.wait_for(
            asyncio.gather(*(asyncio.to_thread(_send_one, s, payload, topic) for s in subs)),
            timeout=PUSH_TIMEOUT * 2,
        )
        return _apply_results(list(results))
    except Exception as exc:
        logger.error("Web Push: сбой рассылки (%s) %s", type(exc).__name__, exc)
        return summary


def _short(order_id: str) -> str:
    return order_id[:6].upper()


async def push_new_order(order_id: str, amount_rub: int, size_label: str, promo_code: Optional[str] = None) -> None:
    if promo_code:
        title, tail = "🆓 Заказ по промокоду", "PDF выдан автоматически"
    else:
        title, tail = f"🆕 Новый заказ · {amount_rub} ₽", "ожидает оплаты"
    await _deliver(
        {"title": title, "body": f"#{_short(order_id)} · {size_label} · {tail}",
         "tag": order_id, "url": f"/admin/?order={order_id}"},
        topic=_topic_for(order_id),
    )


async def push_order_paid(order_id: str, amount_rub: int) -> None:
    await _deliver(
        {"title": f"💳 Оплачено · {amount_rub} ₽", "body": f"#{_short(order_id)} · PDF выдан автоматически",
         "tag": order_id, "url": f"/admin/?order={order_id}"},
        topic=_topic_for(order_id),
    )


async def send_test() -> dict:
    return await _deliver({"title": "🔔 Уведомления работают", "body": "Это проверочное сообщение из админки BannerBot",
                           "tag": "test", "url": "/admin/"})


def fire_and_forget(coro) -> None:
    """Запускает корутину в фоне, не задерживая ответ. Ссылка хранится, чтобы задачу не собрал GC."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


if __name__ == "__main__":
    if sys.argv[1:] == ["genkey"]:
        print(generate_private_key())
    else:
        print("usage: python -m api.services.push_notify genkey", file=sys.stderr)
        sys.exit(2)
