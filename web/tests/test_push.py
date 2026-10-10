"""
test_push.py
~~~~~~~~~~~~
Web Push для PWA админки: подписка устройства, отправка, хуки заказа и оплаты, service worker.

Отправка проверена по-настоящему: pywebpush шифрует сообщение (RFC 8291) и подписывает VAPID,
HTTP-слой подменён, а «устройство» (тест) расшифровывает тело своим приватным ключом.

Покрывает:
  - авторизация и выключенное состояние (нет VAPID_PRIVATE_KEY)
  - подписка: проверка host push-сервиса (защита от SSRF), ключей; upsert; лимит устройств
  - содержимое push: расшифровка, VAPID-токен (aud/sub/exp), заголовки TTL/Urgency/Topic
  - ответы push-сервиса: 201 / 404,410 (подписка удаляется) / 5xx / сеть; рассылка нескольким устройствам
  - хуки: новый заказ (в фоне), оплата (один раз), сбой push не ломает остальное
  - service worker (node), манифест, иконки, подключение в админке
"""

import asyncio
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import http_ece
import pytest
import requests
from conftest import VALID_ORDER_PAYLOAD
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from PIL import Image

from web.api.services import push_notify

ADMIN = {"Authorization": "Bearer test_admin_token_32bytes_padding_x"}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
ADMIN_DIR = Path(__file__).resolve().parents[1] / "frontend" / "admin"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node не установлен")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Device:
    """Устройство-подписчик: свои ключи P-256 и auth, как у браузера."""

    def __init__(self, endpoint: str = ENDPOINT):
        self.endpoint = endpoint
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"\x01" * 16
        self.p256dh = b64(self.private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        self.auth_b64 = b64(self.auth)

    def body(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth_b64}}

    def decrypt(self, content: bytes) -> dict:
        plain = http_ece.decrypt(content, private_key=self.private, auth_secret=self.auth)
        return json.loads(plain.decode())


class FakePushService:
    """Подмена requests.post: запоминает запросы, отвечает заданным кодом."""

    def __init__(self):
        self.calls = []
        self.status = {}          # endpoint -> код ответа (по умолчанию 201)
        self.raises = {}          # endpoint -> исключение

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "data": data, "headers": dict(headers or {}), "timeout": timeout})
        if url in self.raises:
            raise self.raises[url]
        resp = requests.Response()
        resp.status_code = self.status.get(url, 201)
        resp._content = b""
        resp.url = url
        return resp


@pytest.fixture
def vapid(monkeypatch):
    key = push_notify.generate_private_key()
    monkeypatch.setattr(push_notify, "VAPID_PRIVATE_KEY", key)
    monkeypatch.setattr(push_notify, "VAPID_SUBJECT", "https://bannerbot.ru")
    monkeypatch.setattr(push_notify, "_vapid", None)
    return key


@pytest.fixture
def service(monkeypatch):
    fake = FakePushService()
    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def subs(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM push_subscriptions ORDER BY id").fetchall()
    finally:
        conn.close()


async def subscribe(client, device: Device):
    return await client.post("/api/admin/push/subscribe", headers=ADMIN, json=device.body())


# ---------------------------------------------------------------------------
# Доступ и выключенное состояние
# ---------------------------------------------------------------------------
class TestAccess:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method, url", [
        ("GET", "/api/admin/push/config"),
        ("POST", "/api/admin/push/subscribe"),
        ("POST", "/api/admin/push/unsubscribe"),
        ("POST", "/api/admin/push/test"),
    ])
    async def test_admin_token_required(self, client, method, url):
        assert (await client.request(method, url, json={})).status_code in (401, 403)
        wrong = await client.request(method, url, json={}, headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 403

    @pytest.mark.asyncio
    async def test_disabled_without_vapid_key(self, client, monkeypatch):
        monkeypatch.setattr(push_notify, "VAPID_PRIVATE_KEY", "")
        r = await client.get("/api/admin/push/config", headers=ADMIN)
        assert r.json() == {"enabled": False, "public_key": None}
        assert (await subscribe(client, Device())).status_code == 503
        assert (await client.post("/api/admin/push/test", headers=ADMIN)).status_code == 503

    @pytest.mark.asyncio
    async def test_config_returns_uncompressed_public_key(self, client, vapid):
        r = (await client.get("/api/admin/push/config", headers=ADMIN)).json()
        raw = base64.urlsafe_b64decode(r["public_key"] + "==")
        assert r["enabled"] is True and len(raw) == 65 and raw[0] == 4

    @pytest.mark.asyncio
    async def test_disabled_sends_nothing_and_touches_nothing(self, init_test_db, service, monkeypatch):
        monkeypatch.setattr(push_notify, "VAPID_PRIVATE_KEY", "")
        monkeypatch.setattr(push_notify, "list_subscriptions", lambda: pytest.fail("в БД ходить не должен"))
        await push_notify.push_new_order("o1", 299, "1x0.5")
        await push_notify.push_order_paid("o1", 299)
        assert service.calls == []

    def test_generated_keys_are_valid_and_unique(self):
        a, b = push_notify.generate_private_key(), push_notify.generate_private_key()
        assert a != b and len(base64.urlsafe_b64decode(a + "==")) == 32


# ---------------------------------------------------------------------------
# Подписка
# ---------------------------------------------------------------------------
class TestSubscribe:

    @pytest.mark.asyncio
    async def test_saves_subscription(self, client, vapid, init_test_db):
        d = Device()
        assert (await subscribe(client, d)).status_code == 200
        (row,) = subs(init_test_db)
        assert row["endpoint"] == d.endpoint and row["p256dh"] == d.p256dh and row["auth"] == d.auth_b64

    @pytest.mark.asyncio
    async def test_extra_browser_fields_are_ignored(self, client, vapid):
        body = {**Device().body(), "expirationTime": None}
        assert (await client.post("/api/admin/push/subscribe", headers=ADMIN, json=body)).status_code == 200

    @pytest.mark.asyncio
    async def test_same_endpoint_updates_instead_of_duplicating(self, client, vapid, init_test_db):
        first, second = Device(), Device()
        await subscribe(client, first)
        await subscribe(client, second)  # тот же endpoint, новые ключи
        (row,) = subs(init_test_db)
        assert row["p256dh"] == second.p256dh

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", [
        "https://evil.example/push",                              # чужой хост
        "https://fcm.googleapis.com.evil.example/x",              # суффикс-обман
        "https://evilgoogleapis.com/x",                           # без границы по точке
        "http://fcm.googleapis.com/fcm/send/x",                   # не https
        "https://user:pw@fcm.googleapis.com/x",                   # учётные данные в адресе
        "https://fcm.googleapis.com:8443/x",                      # нестандартный порт
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "ftp://fcm.googleapis.com/x",
        "https://fcm.googleapis.com/" + "a" * 2100,
    ])
    async def test_rejects_unknown_push_service(self, client, vapid, init_test_db, endpoint):
        body = Device().body()
        body["endpoint"] = endpoint
        r = await client.post("/api/admin/push/subscribe", headers=ADMIN, json=body)
        assert r.status_code in (422, 400)
        assert subs(init_test_db) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", [
        "fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com",
        "wns2-par02p.notify.windows.com", "updates-autopush.stage.mozaws.net.push.services.mozilla.com",
    ])
    async def test_accepts_known_push_services(self, client, vapid, host):
        body = Device().body()
        body["endpoint"] = f"https://{host}/wpush/v2/x"
        assert (await client.post("/api/admin/push/subscribe", headers=ADMIN, json=body)).status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keys", [
        {"p256dh": "AAAA", "auth": "AAAAAAAAAAAAAAAAAAAAAA"},     # короткий ключ
        {"p256dh": "!!!", "auth": "AAAAAAAAAAAAAAAAAAAAAA"},
        {"p256dh": Device().p256dh, "auth": "AAAA"},              # короткий auth
        {"p256dh": Device().p256dh, "auth": ""},
    ])
    async def test_rejects_bad_keys(self, client, vapid, init_test_db, keys):
        r = await client.post("/api/admin/push/subscribe", headers=ADMIN,
                              json={"endpoint": ENDPOINT, "keys": keys})
        assert r.status_code == 422 and subs(init_test_db) == []

    @pytest.mark.asyncio
    async def test_device_limit_evicts_oldest(self, client, vapid, init_test_db, monkeypatch):
        monkeypatch.setattr(push_notify, "MAX_SUBSCRIPTIONS", 3)
        for i in range(5):
            await subscribe(client, Device(f"https://fcm.googleapis.com/fcm/send/d{i}"))
        assert [r["endpoint"][-2:] for r in subs(init_test_db)] == ["d2", "d3", "d4"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, client, vapid, init_test_db):
        d = Device()
        await subscribe(client, d)
        r = await client.post("/api/admin/push/unsubscribe", headers=ADMIN, json={"endpoint": d.endpoint})
        assert r.json() == {"ok": True, "removed": True} and subs(init_test_db) == []
        r = await client.post("/api/admin/push/unsubscribe", headers=ADMIN, json={"endpoint": d.endpoint})
        assert r.json()["removed"] is False


# ---------------------------------------------------------------------------
# Отправка: шифрование, VAPID, заголовки
# ---------------------------------------------------------------------------
def jwt_claims(authorization: str) -> dict:
    token = re.search(r"t=([\w-]+\.[\w-]+\.[\w-]+)", authorization).group(1)
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class TestDelivery:

    @pytest.mark.asyncio
    async def test_device_can_decrypt_new_order_message(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_new_order("abcdef12-0000", 299, "1x0.5")
        (call,) = service.calls
        msg = d.decrypt(call["data"])
        assert msg["title"] == "🆕 Новый заказ · 299 ₽"
        assert msg["body"] == "#ABCDEF · 1x0.5 · ожидает оплаты"
        assert msg["tag"] == "abcdef12-0000" and msg["url"] == "/admin/?order=abcdef12-0000"

    @pytest.mark.asyncio
    async def test_paid_message_replaces_by_tag(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_order_paid("abcdef12-0000", 299)
        msg = d.decrypt(service.calls[0]["data"])
        assert msg["title"] == "💳 Оплачено · 299 ₽" and msg["tag"] == "abcdef12-0000"

    @pytest.mark.asyncio
    async def test_promo_order_message(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_new_order("abcdef12-0000", 0, "1x0.5", promo_code="FREE")
        msg = d.decrypt(service.calls[0]["data"])
        assert msg["title"] == "🆓 Заказ по промокоду" and "PDF выдан" in msg["body"]

    @pytest.mark.asyncio
    async def test_banner_text_is_not_in_the_message(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_new_order("abcdef12-0000", 299, "1x0.5")
        assert "Аренда" not in json.dumps(d.decrypt(service.calls[0]["data"]), ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_request_headers_and_vapid_token(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_new_order("abcdef12-0000-4000", 299, "1x0.5")
        (call,) = service.calls
        h = {k.lower(): v for k, v in call["headers"].items()}
        assert call["url"] == ENDPOINT
        assert h["content-encoding"] == "aes128gcm" and h["urgency"] == "high"
        assert h["ttl"] == str(push_notify.PUSH_TTL)
        assert h["topic"] == "abcdef12-0000-4000"          # допустимые символы, ≤ 32
        assert call["timeout"] == push_notify.PUSH_TIMEOUT
        claims = jwt_claims(h["authorization"])
        assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "https://bannerbot.ru"
        assert claims["exp"] > 0
        assert f"k={push_notify.public_key()}" in h["authorization"]

    @pytest.mark.asyncio
    async def test_topic_is_sanitized_and_capped(self, client, vapid, service):
        d = Device()
        await subscribe(client, d)
        await push_notify.push_new_order("a/b c" + "x" * 60, 299, "1x0.5")
        topic = {k.lower(): v for k, v in service.calls[0]["headers"].items()}["topic"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", topic)

    @pytest.mark.asyncio
    async def test_all_devices_receive_and_each_decrypts_own_copy(self, client, vapid, service):
        a, b = Device("https://fcm.googleapis.com/fcm/send/a"), Device("https://web.push.apple.com/b")
        await subscribe(client, a)
        await subscribe(client, b)
        await push_notify.push_order_paid("abcdef12-0000", 299)
        by_url = {c["url"]: c for c in service.calls}
        assert a.decrypt(by_url[a.endpoint]["data"]) == b.decrypt(by_url[b.endpoint]["data"])
        with pytest.raises(Exception):
            a.decrypt(by_url[b.endpoint]["data"])   # чужим ключом не читается

    @pytest.mark.asyncio
    async def test_no_subscriptions_no_requests(self, client, vapid, service):
        await push_notify.push_new_order("o1", 299, "1x0.5")
        assert service.calls == []


class TestPushServiceReplies:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [404, 410])
    async def test_gone_subscription_is_removed(self, client, vapid, service, init_test_db, status):
        d = Device()
        await subscribe(client, d)
        service.status[d.endpoint] = status
        await push_notify.push_new_order("o1", 299, "1x0.5")
        assert subs(init_test_db) == []

    @pytest.mark.asyncio
    async def test_server_error_keeps_subscription_and_counts_failure(self, client, vapid, service, init_test_db):
        d = Device()
        await subscribe(client, d)
        service.status[d.endpoint] = 503
        await push_notify.push_new_order("o1", 299, "1x0.5")
        await push_notify.push_new_order("o2", 299, "1x0.5")
        (row,) = subs(init_test_db)
        assert row["fail_count"] == 2

    @pytest.mark.asyncio
    async def test_success_resets_failures_and_marks_time(self, client, vapid, service, init_test_db):
        d = Device()
        await subscribe(client, d)
        service.status[d.endpoint] = 500
        await push_notify.push_new_order("o1", 299, "1x0.5")
        service.status[d.endpoint] = 201
        await push_notify.push_new_order("o2", 299, "1x0.5")
        (row,) = subs(init_test_db)
        assert row["fail_count"] == 0 and row["last_ok_at"]

    @pytest.mark.asyncio
    async def test_dead_endpoint_is_dropped_after_many_failures(self, client, vapid, service, init_test_db, monkeypatch):
        monkeypatch.setattr(push_notify, "MAX_FAILS", 3)
        d = Device()
        await subscribe(client, d)
        service.status[d.endpoint] = 502
        for i in range(3):
            await push_notify.push_new_order(f"o{i}", 299, "1x0.5")
        assert subs(init_test_db) == []

    @pytest.mark.asyncio
    async def test_network_error_does_not_raise_and_keeps_other_devices_working(self, client, vapid, service, init_test_db):
        bad, good = Device("https://fcm.googleapis.com/fcm/send/bad"), Device("https://web.push.apple.com/good")
        await subscribe(client, bad)
        await subscribe(client, good)
        service.raises[bad.endpoint] = requests.ConnectionError("down")
        await push_notify.push_new_order("abcdef12-0000", 299, "1x0.5")
        assert good.decrypt([c for c in service.calls if c["url"] == good.endpoint][0]["data"])["tag"] == "abcdef12-0000"
        rows = {r["endpoint"]: r["fail_count"] for r in subs(init_test_db)}
        assert rows == {bad.endpoint: 1, good.endpoint: 0}

    @pytest.mark.asyncio
    async def test_test_endpoint_reports_counts(self, client, vapid, service):
        ok, gone = Device("https://fcm.googleapis.com/fcm/send/ok"), Device("https://web.push.apple.com/gone")
        await subscribe(client, ok)
        await subscribe(client, gone)
        service.status[gone.endpoint] = 410
        r = await client.post("/api/admin/push/test", headers=ADMIN)
        assert r.json() == {"sent": 1, "failed": 0, "removed": 1}

    @pytest.mark.asyncio
    async def test_overall_timeout_does_not_hang_the_caller(self, client, vapid, monkeypatch):
        d = Device()
        await subscribe(client, d)
        monkeypatch.setattr(push_notify, "PUSH_TIMEOUT", 0.2)

        def slow(sub, payload, topic):
            import time
            time.sleep(1.0)
            return sub["id"], "ok", None

        monkeypatch.setattr(push_notify, "_send_one", slow)
        started = asyncio.get_event_loop().time()
        await push_notify.push_new_order("o1", 299, "1x0.5")   # не бросает
        assert asyncio.get_event_loop().time() - started < 0.8


# ---------------------------------------------------------------------------
# Хуки заказа и оплаты
# ---------------------------------------------------------------------------
class TestHooks:

    @pytest.mark.asyncio
    async def test_new_order_pushes_in_background(self, client):
        with patch("web.api.routers.order.push_new_order", AsyncMock()) as push:
            r = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert r.status_code == 200
        push.assert_awaited_once()
        oid, amount, size_label, promo = push.await_args.args
        assert oid == r.json()["order_id"] and amount == 299 and size_label == "1x0.5" and promo is None

    @pytest.mark.asyncio
    async def test_push_failure_does_not_break_order_or_telegram(self, client):
        with patch("web.api.routers.order.push_new_order", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("web.api.routers.order.notify_new_order", AsyncMock(return_value=77)) as tg:
            r = await client.post("/api/order", json=VALID_ORDER_PAYLOAD)
        assert r.status_code == 200
        tg.assert_awaited_once()   # сбой push не отменяет уведомление в Telegram

    @pytest.mark.asyncio
    async def test_paid_webhook_pushes_once(self, client):
        oid = (await client.post("/api/order", json=VALID_ORDER_PAYLOAD)).json()["order_id"]
        body = json.dumps({"type": "notification", "event": "payment.succeeded",
                           "object": {"id": "yk-1", "status": "succeeded", "metadata": {"order_id": oid}}})
        verify = AsyncMock(return_value={"metadata": {"order_id": oid}, "status": "succeeded", "paid": True})
        with patch("web.api.routers.payment.verify_yookassa_payment", verify), \
             patch("web.api.routers.payment.push_order_paid", AsyncMock()) as push:
            for _ in range(2):   # повторный webhook — идемпотентный
                r = await client.post("/api/payment/callback", content=body, headers={"Content-Type": "application/json"})
                assert r.status_code == 200
            await asyncio.sleep(0.05)   # фоновая задача
        push.assert_called_once_with(oid, 299)

    @pytest.mark.asyncio
    async def test_paid_push_does_not_delay_the_webhook(self, client, vapid, monkeypatch):
        oid = (await client.post("/api/order", json=VALID_ORDER_PAYLOAD)).json()["order_id"]
        release = asyncio.Event()

        async def hang(*a, **kw):
            await release.wait()

        monkeypatch.setattr(push_notify, "_deliver", hang)
        body = json.dumps({"type": "notification", "event": "payment.succeeded",
                           "object": {"id": "yk-1", "status": "succeeded", "metadata": {"order_id": oid}}})
        verify = AsyncMock(return_value={"metadata": {"order_id": oid}, "status": "succeeded", "paid": True})
        with patch("web.api.routers.payment.verify_yookassa_payment", verify):
            r = await asyncio.wait_for(
                client.post("/api/payment/callback", content=body, headers={"Content-Type": "application/json"}), 2)
        assert r.status_code == 200          # ответили, хотя push ещё «висит»
        release.set()
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Service worker, манифест, админка
# ---------------------------------------------------------------------------
SW_HARNESS = r"""
const fs = require("fs");
const listeners = {};
const shown = [], opened = [], focused = [], navigated = [];
let clientList = JSON.parse(process.argv[3]);
global.self = {
  location: { origin: "https://bannerbot.ru" },
  addEventListener: (t, fn) => { listeners[t] = fn; },
  skipWaiting: () => {},
  clients: {
    claim: async () => {},
    matchAll: async () => clientList.map((url) => ({
      url, focus: async () => { focused.push(url); if (process.env.FOCUS_FAILS) throw new Error("not allowed"); }, navigate: async (u) => { navigated.push([url, u]); },
    })),
    openWindow: async (u) => { opened.push(u); },
  },
  registration: { showNotification: async (title, opts) => { shown.push({ title, opts }); } },
};
eval(fs.readFileSync(process.argv[1], "utf8"));
const waits = [];
const ev = (extra) => ({ waitUntil: (p) => waits.push(p), ...extra });
(async () => {
  const kind = process.argv[2];
  if (kind === "push") {
    const raw = process.argv[4];
    listeners.push(ev({ data: raw === "__none__" ? null : { json: () => JSON.parse(raw), text: () => raw } }));
  } else {
    let closed = false;
    listeners.notificationclick(ev({ notification: { close: () => { closed = true; }, data: JSON.parse(process.argv[4]) } }));
    await Promise.all(waits);
    console.log(JSON.stringify({ closed, opened, focused, navigated }));
    return;
  }
  await Promise.all(waits);
  console.log(JSON.stringify(shown));
})();
"""


def sw(kind: str, arg: str, clients=(), focus_fails=False):
    env = {**os.environ, **({"FOCUS_FAILS": "1"} if focus_fails else {})}
    out = subprocess.run(["node", "-e", SW_HARNESS, str(ADMIN_DIR / "sw.js"), kind, json.dumps(list(clients)), arg],
                         capture_output=True, text=True, timeout=20, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
class TestServiceWorker:

    def test_push_shows_notification_with_tag_and_url(self):
        (n,) = sw("push", json.dumps({"title": "T", "body": "B", "tag": "o1", "url": "/admin/?order=o1"}))
        assert n["title"] == "T" and n["opts"]["body"] == "B"
        assert n["opts"]["tag"] == "o1" and n["opts"]["renotify"] is True
        assert n["opts"]["data"]["url"] == "/admin/?order=o1"

    def test_renotify_only_together_with_tag(self):
        (n,) = sw("push", json.dumps({"title": "T", "body": "B"}))
        assert "renotify" not in n["opts"] and "tag" not in n["opts"]   # renotify без tag — TypeError в браузере

    def test_always_shows_something_even_for_garbage_payload(self):
        for raw in ("not json at all", "__none__", "{}"):
            (n,) = sw("push", raw)
            assert n["title"]       # userVisibleOnly: каждый push обязан быть показан

    def test_click_opens_window_when_admin_is_closed(self):
        r = sw("click", json.dumps({"url": "/admin/?order=o1"}))
        assert r["closed"] and r["opened"] == ["https://bannerbot.ru/admin/?order=o1"]

    def test_click_reuses_open_admin_window(self):
        r = sw("click", json.dumps({"url": "/admin/?order=o1"}), clients=["https://bannerbot.ru/admin/"])
        assert r["opened"] == [] and r["focused"] and r["navigated"][0][1] == "https://bannerbot.ru/admin/?order=o1"

    def test_click_navigates_even_if_focus_is_rejected(self):
        # на части платформ client.focus() отклоняется; ссылка из уведомления всё равно должна открыться
        r = sw("click", json.dumps({"url": "/admin/?order=o1"}), clients=["https://bannerbot.ru/admin/"], focus_fails=True)
        assert r["navigated"] and r["navigated"][0][1] == "https://bannerbot.ru/admin/?order=o1" and r["opened"] == []

    def test_click_ignores_other_tabs_of_the_site(self):
        r = sw("click", json.dumps({"url": "/admin/"}), clients=["https://bannerbot.ru/"])
        assert r["opened"] == ["https://bannerbot.ru/admin/"] and r["focused"] == []

    @pytest.mark.parametrize("url", [
        "https://evil.example/phish", "//evil.example/x", "/", "/api/admin/stats", "javascript:alert(1)", 42, None,
    ])
    def test_click_never_leaves_the_admin_area(self, url):
        r = sw("click", json.dumps({"url": url}))
        assert r["opened"] == ["https://bannerbot.ru/admin/"]


class TestStaticFiles:

    def test_manifest(self):
        m = json.loads((ADMIN_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert m["start_url"] == "/admin/" and m["scope"] == "/admin/" and m["display"] == "standalone"
        assert m["name"] and m["short_name"]
        sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes

    def test_manifest_icons_exist_with_declared_size(self):
        m = json.loads((ADMIN_DIR / "manifest.json").read_text(encoding="utf-8"))
        for icon in m["icons"]:
            assert icon["src"].startswith("/admin/")
            path = ADMIN_DIR / icon["src"].removeprefix("/admin/")
            w, h = (int(x) for x in icon["sizes"].split("x"))
            assert Image.open(path).size == (w, h), icon["src"]

    def test_admin_page_wires_pwa_and_push(self):
        html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
        assert '<link rel="manifest" href="/admin/manifest.json">' in html
        assert 'register("/admin/sw.js", { scope: "/admin/" })' in html
        for path in ("/admin/push/config", "/admin/push/subscribe", "/admin/push/unsubscribe", "/admin/push/test"):
            assert path in html
        assert 'id="push-toggle"' in html and 'id="remember-token"' in html

    def test_push_card_explains_failures_in_the_card_not_in_a_toast(self):
        # тост живёт 2,8 с и обрезается на телефоне: причина отказа должна оставаться в карточке
        html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
        assert 'id="push-note"' in html and 'id="push-diag-text"' in html
        body = html[html.index("async function pushEnable()"):html.index("async function pushToggle()")]
        assert "showToast" not in body.replace('showToast("✓ Уведомления включены", "ok")', "")
        toggle = html[html.index("async function pushToggle()"):html.index("async function pushTest()")]
        assert 'showToast("Ошибка' not in toggle

    def test_permission_is_requested_first_and_waits_are_bounded(self):
        html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
        body = html[html.index("async function pushEnable()"):html.index("async function pushToggle()")]
        # разрешение — первое ожидание в обработчике клика (пока действует жест пользователя)
        assert body.index("Notification.requestPermission()") < body.index("pushManager.subscribe")
        assert "withTimeout(Notification.requestPermission(), 15000)" in body
        assert "}), 20000)" in body                                    # подписка тоже не ждёт вечно
        assert "sub.unsubscribe()" in body                            # сервер отказал — в браузере подписку снимаем

    def test_silent_chrome_denial_is_recognised_and_permission_changes_are_picked_up(self):
        # Chrome отвечает «denied» без окна и оставляет permission == "default": нужно отдельное пояснение
        html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
        body = html[html.index("async function pushEnable()"):html.index("async function pushToggle()")]
        assert 'Notification.permission === "denied"' in body and "EMBARGO_HINT" in body
        assert "Очистить и сбросить" in html.split("const EMBARGO_HINT")[1].split(";")[0]
        # разрешили через значок сайта — включаемся сами, ссылку на PermissionStatus держим (иначе GC глушит change)
        watch = html[html.index("async function pushWatchPermission()"):html.index("async function pushInit()")]
        assert 'name: "notifications"' in watch and "_permStatus = await" in watch
        assert 'state === "granted" && !_pushOn' in watch
        # диагностика показывает, что и как быстро ответил браузер
        assert "последний запрос разрешения" in html and "Permissions API" in html

    def test_token_is_remembered_only_on_request(self):
        html = (ADMIN_DIR / "index.html").read_text(encoding="utf-8")
        assert "saveToken(token, $(\"remember-token\").checked)" in html
        assert html.count("localStorage.setItem") == 1   # единственное место: ветка remember

    def test_sw_scope_matches_location(self):
        # sw.js лежит в /admin/, поэтому его область действия не шире админки
        assert (ADMIN_DIR / "sw.js").is_file()
        assert "Service-Worker-Allowed" not in (ADMIN_DIR.parents[1] / "nginx" / "default.conf").read_text(encoding="utf-8")
