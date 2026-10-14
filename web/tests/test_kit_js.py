"""kit.js (собственный счётчик): поведение в «браузере» проверяет Node на заглушке window/document."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1] / "frontend" / "kit.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node не установлен")

RUNNER = r"""
const vm = require("vm"), fs = require("fs");
const cfg = JSON.parse(process.argv[2]);
const beats = [], store = Object.assign({}, cfg.storage || {});
const sandbox = {
  location: { pathname: cfg.path || "/", hostname: "bannerbot.ru", search: cfg.search || "" },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  navigator: Object.assign({ userAgent: "Mozilla/5.0 (Windows NT 10.0) Chrome/154", sendBeacon: (u, b) => { beats.push(u); return true; } }, cfg.nav || {}),
  document: { referrer: cfg.referrer || "", addEventListener: () => {} },
  Blob, URL, JSON, fetch: () => {},
};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
console.log(JSON.stringify({ beats: beats.length, entry: sandbox.trackEntry(), store }));
"""


def run(**cfg):
    out = subprocess.run(["node", "-e", RUNNER, str(KIT), json.dumps(cfg)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@needs_node
class TestCounting:
    def test_normal_visit_is_counted_once_as_pageview(self):
        assert run()["beats"] == 1

    @pytest.mark.parametrize("ua", [
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
        "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/154.0 Safari/537.36",
        "Mozilla/5.0 Chrome-Lighthouse",
    ])
    def test_robots_are_not_counted(self, ua):
        assert run(nav={"userAgent": ua})["beats"] == 0

    def test_automation_dnt_and_gpc_are_respected(self):
        assert run(nav={"webdriver": True})["beats"] == 0
        assert run(nav={"doNotTrack": "1"})["beats"] == 0
        assert run(nav={"globalPrivacyControl": True})["beats"] == 0

    def test_real_browser_names_are_not_mistaken_for_robots(self):
        for ua in ("Mozilla/5.0 (Linux; Android 13) YaBrowser/24.1 Mobile Safari/537.36",
                   "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) YandexSearch/24.5"):
            assert run(nav={"userAgent": ua})["beats"] == 1

    def test_owner_opt_out_flag_is_set_and_remembered(self):
        first = run(search="?notrack=1")
        assert first["beats"] == 0 and first["store"] == {"nt": "1"}
        assert run(storage={"nt": "1"})["beats"] == 0


@needs_node
class TestEntryLabel:
    def test_entry_is_the_source_for_external_visits(self):
        assert run(referrer="https://yandex.ru/search/?text=x")["entry"] == "yandex"
        assert run(referrer="https://www.google.com/")["entry"] == "google"
        assert run(referrer="")["entry"] == "direct"
        assert run(referrer="https://example.org/")["entry"] == "other"

    def test_entry_is_the_page_for_internal_navigation(self):
        assert run(referrer="https://bannerbot.ru/baner-arenda/")["entry"] == "/baner-arenda/"

    def test_entry_is_empty_when_counting_is_off(self):
        assert run(nav={"doNotTrack": "1"}, referrer="https://yandex.ru/")["entry"] == ""
