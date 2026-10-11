"""
Cloudflare Worker-релей Telegram ⇄ сайт (web/tools/cf-tg-relay.js) и согласованность портов nginx/compose.

Worker запускается в Node с подменённым fetch: проверяется, что именно он отправляет Telegram и сайту
и что отвечает вызвавшему. Без Node тесты Worker пропускаются (как и тесты service worker в test_push.py).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1]
RELAY = WEB / "tools" / "cf-tg-relay.js"
TOKEN = "123456:ABC-def_GHI"
SECRET = "s3cr3t-segment"
HOOK_SECRET = "tg-hook-secret-0123456789"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node не установлен")

HARNESS = r"""
import { pathToFileURL } from "node:url";
const [file, specJson] = process.argv.slice(-2);
const spec = JSON.parse(specJson);
const worker = (await import(pathToFileURL(file).href)).default;
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), headers: init?.headers ?? {}, body: init?.body ?? "" });
  if (spec.upstream.throw) { const e = new Error("x"); e.name = spec.upstream.throw; throw e; }
  return new Response(spec.upstream.body, { status: spec.upstream.status, headers: { "content-type": "application/json" } });
};
const req = new Request("http://relay.local" + spec.path, {
  method: spec.method, headers: spec.headers, body: spec.method === "GET" ? undefined : spec.body,
});
const resp = await worker.fetch(req, spec.env);
console.log(JSON.stringify({ status: resp.status, body: await resp.text(), calls }));
"""


def relay(path, *, method="POST", headers=None, body='{"chat_id":1,"text":"x"}', env=None,
          upstream_status=200, upstream_body='{"ok":true,"result":{"message_id":7}}', upstream_throw=None, tmp_path=None):
    """Один запрос к Worker. Возвращает {status, body, calls}: calls — то, что Worker отправил наружу."""
    spec = {
        "path": path, "method": method, "body": body,
        "headers": {"content-type": "application/json", **(headers or {})},
        "env": {"RELAY_SECRET": SECRET} if env is None else env,
        "upstream": {"status": upstream_status, "body": upstream_body, "throw": upstream_throw},
    }
    mod = tmp_path / "relay.mjs"          # Node принимает ES-модуль без package.json только с расширением .mjs
    mod.write_text(RELAY.read_text(encoding="utf-8"), encoding="utf-8")
    out = subprocess.run(["node", "--input-type=module", "-e", HARNESS, str(mod), json.dumps(spec)],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def call(tmp_path):
    return lambda path, **kw: relay(path, tmp_path=tmp_path, **kw)


@needs_node
class TestBotApiProxy:
    """сайт → Worker → Telegram"""

    def test_forwards_to_telegram_and_returns_its_answer(self, call):
        r = call(f"/{SECRET}/bot{TOKEN}/sendMessage")
        assert r["status"] == 200 and json.loads(r["body"])["result"]["message_id"] == 7
        (c,) = r["calls"]
        assert c["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"   # секретного сегмента в Telegram нет
        assert c["body"] == '{"chat_id":1,"text":"x"}'

    @pytest.mark.parametrize("method", ["sendMessage", "editMessageText", "editMessageReplyMarkup",
                                        "answerCallbackQuery", "getWebhookInfo", "setWebhook"])
    def test_allowed_methods(self, call, method):
        r = call(f"/{SECRET}/bot{TOKEN}/{method}")
        assert r["status"] == 200 and len(r["calls"]) == 1

    @pytest.mark.parametrize("method", ["getUpdates", "deleteWebhook", "getMe", "sendDocument", "forwardMessage", "logOut"])
    def test_other_methods_are_closed(self, call, method):
        r = call(f"/{SECRET}/bot{TOKEN}/{method}")
        assert r["status"] == 404 and r["calls"] == []

    @pytest.mark.parametrize("path", [
        f"/wrong/bot{TOKEN}/sendMessage",              # неверный секрет
        f"/bot{TOKEN}/sendMessage",                    # без секрета
        f"/{SECRET}/bot../sendMessage",                # мусор в токене
        f"/{SECRET}/bot{TOKEN}/sendMessage/extra",
    ])
    def test_bad_paths_404_and_nothing_leaves(self, call, path):
        r = call(path)
        assert r["status"] == 404 and r["calls"] == []

    def test_get_is_404(self, call):
        r = call(f"/{SECRET}/bot{TOKEN}/sendMessage", method="GET")
        assert r["status"] == 404 and r["calls"] == []

    def test_without_relay_secret_in_env_everything_is_404(self, call):
        r = call(f"/{SECRET}/bot{TOKEN}/sendMessage", env={})
        assert r["status"] == 404 and r["calls"] == []

    def test_telegram_down_gives_502_with_ok_false(self, call):
        r = call(f"/{SECRET}/bot{TOKEN}/sendMessage", upstream_throw="TypeError")
        assert r["status"] == 502 and json.loads(r["body"])["ok"] is False

    def test_telegram_error_passes_through_unchanged(self, call):
        r = call(f"/{SECRET}/bot{TOKEN}/sendMessage", upstream_status=400,
                 upstream_body='{"ok":false,"description":"Bad Request: chat not found"}')
        assert r["status"] == 400 and "chat not found" in r["body"]


@needs_node
class TestWebhookForward:
    """Telegram → Worker → сайт"""

    UPDATE = json.dumps({"update_id": 1, "callback_query": {"id": "c1", "from": {"id": 777}, "data": "force_token:abc"}})

    def hook(self, call, **kw):
        kw.setdefault("body", self.UPDATE)
        kw.setdefault("headers", {"x-telegram-bot-api-secret-token": HOOK_SECRET})
        return call(f"/{SECRET}/tg-hook", **kw)

    def test_forwards_body_and_secret_header_to_site_callback(self, call):
        r = self.hook(call, upstream_body='{"ok":true}')
        assert r["status"] == 200
        (c,) = r["calls"]
        assert c["url"] == "https://bannerbot.ru/api/tg/callback"
        assert c["body"] == self.UPDATE
        assert c["headers"]["x-telegram-bot-api-secret-token"] == HOOK_SECRET

    def test_without_secret_header_none_is_invented(self, call):
        r = self.hook(call, headers={})
        assert "x-telegram-bot-api-secret-token" not in r["calls"][0]["headers"]

    def test_site_rejection_is_not_masked(self, call):
        # сайт сказал 403 (неверный секрет) — Telegram должен это увидеть, а не «200 ok»
        r = self.hook(call, upstream_status=403, upstream_body='{"detail":"Неверный секрет webhook"}')
        assert r["status"] == 403

    def test_site_down_gives_502_so_telegram_retries(self, call):
        assert self.hook(call, upstream_throw="TypeError")["status"] == 502

    def test_site_timeout_gives_504(self, call):
        assert self.hook(call, upstream_throw="TimeoutError")["status"] == 504

    @pytest.mark.parametrize("path", ["/wrong/tg-hook", "/tg-hook", f"/{SECRET}/tg-hook/extra", f"/{SECRET}/tg-hook2"])
    def test_bad_paths_404_and_site_not_called(self, call, path):
        r = call(path, body=self.UPDATE)
        assert r["status"] == 404 and r["calls"] == []

    def test_get_is_404(self, call):
        r = call(f"/{SECRET}/tg-hook", method="GET")
        assert r["status"] == 404 and r["calls"] == []

    def test_only_one_fixed_site_address(self):
        # адрес сайта зашит в Worker: подставить чужой через запрос нельзя
        src = RELAY.read_text(encoding="utf-8")
        assert re.findall(r'const SITE_CALLBACK = "([^"]+)"', src) == ["https://bannerbot.ru/api/tg/callback"]


class TestPortsMatch:
    """Опубликованный в docker-compose порт без listen в nginx — тихая поломка: именно так «умер» webhook на :88."""

    def test_every_published_nginx_port_has_a_listener(self):
        compose = (WEB / "docker-compose.yml").read_text(encoding="utf-8")
        block = compose.split("\n  nginx_web:", 1)[1]
        block = re.split(r"\n  \S", block, maxsplit=1)[0]            # до следующего сервиса
        published = {int(m) for m in re.findall(r'-\s*"?\d+:(\d+)"?', block)}
        listening = {int(m) for m in re.findall(r"^\s*listen\s+(\d+)", (WEB / "nginx" / "default.conf").read_text(encoding="utf-8"), re.M)}
        assert published and published <= listening, f"опубликованы {sorted(published)}, nginx слушает {sorted(listening)}"
