"""Политика обработки данных и фактическое поведение сайта не расходятся.

Проверяется то, что обещано на странице /privacy/: нет сторонних запросов и cookie,
счётчик не хранит идентификаторы, сроки хранения и журналы совпадают с кодом и конфигами.
"""
import importlib.util
import re
from pathlib import Path

from web.api.services.config import ORDER_PII_RETENTION_DAYS

WEB = Path(__file__).resolve().parents[1]
FRONT = WEB / "frontend"
spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)

PAGES = [p for p in FRONT.rglob("*.html")
         if "admin" not in p.parts and p.name != "404.html" and not p.name.startswith("google")]  # google*.html: файл подтверждения прав
FORBIDDEN_HOSTS = ("mc.yandex", "metrika", "googleapis", "gstatic", "googletagmanager", "google-analytics", "yookassa.ru")


def test_no_third_party_resources_are_loaded_by_pages():
    assert len(PAGES) >= 19
    everything = [p for p in FRONT.rglob("*.html")]  # включая 404 и админку
    for page in everything:
        html = page.read_text(encoding="utf-8")
        for host in FORBIDDEN_HOSTS:
            assert host not in html.lower(), (page.name, host)
        assert not re.search(r'<script[^>]+src="https?://', html), page
        assert not re.search(r'<link[^>]+href="https?://(?!bannerbot\.ru)', html), page
        assert not re.search(r'<img[^>]+src="https?://', html), page


def test_own_counter_uses_no_cookies_and_only_the_opt_out_flag_in_storage():
    code = re.sub(r"/\*.*?\*/", "", (FRONT / "kit.js").read_text(encoding="utf-8"), flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)  # строчные комментарии
    for word in ("document.cookie", "sessionStorage", "indexedDB"):
        assert word not in code, word
    # localStorage разрешён только для флага отключения счётчика «nt» (см. /privacy/)
    assert "localStorage[" not in code
    keys = set(re.findall(r'localStorage\.(?:getItem|setItem|removeItem)\("([^"]*)"', code))
    assert keys == {"nt"}
    assert len(re.findall(r"localStorage", code)) == len(re.findall(r'localStorage\.(?:getItem|setItem)\("nt"', code))


def test_every_page_links_to_the_policy():
    for page in PAGES:
        assert 'href="/privacy/"' in page.read_text(encoding="utf-8"), page


def test_retention_in_policy_matches_code():
    assert bp.ORDER_RETENTION_DAYS == ORDER_PII_RETENTION_DAYS
    days = f"{ORDER_PII_RETENTION_DAYS} дней"
    assert days in (FRONT / "privacy" / "index.html").read_text(encoding="utf-8")
    assert days in (FRONT / "index.html").read_text(encoding="utf-8")  # соглашение и «Конфиденциальность»


def test_logrotate_keeps_the_same_number_of_days():
    conf = (WEB / "ops" / "logrotate-bannerbot").read_text(encoding="utf-8")
    assert int(re.search(r"^\s*rotate (\d+)", conf, re.M).group(1)) == ORDER_PII_RETENTION_DAYS
    assert re.search(r"^\s*daily", conf, re.M)


def test_nginx_access_log_has_no_full_ip():
    conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
    fmt = conf[conf.index("log_format main"): conf.index(";", conf.index("log_format main") + 200)]
    assert "$remote_addr_anon" in fmt
    assert "$remote_addr " not in fmt and "$http_x_forwarded_for" not in fmt
    assert "map $remote_addr $remote_addr_anon" in conf


def test_stat_endpoint_gets_no_ip_and_is_not_logged():
    conf = (WEB / "nginx" / "default.conf").read_text(encoding="utf-8")
    block = conf[conf.index("location = /api/beat"): conf.index("}", conf.index("location = /api/beat"))]
    assert "access_log off;" in block
    assert 'proxy_set_header X-Real-IP "";' in block and 'proxy_set_header X-Forwarded-For "";' in block
    assert "limit_req zone=beat_limit" in block


def test_policy_page_is_in_sitemap_and_states_no_cookies():
    assert "/privacy/" in (FRONT / "sitemap.xml").read_text(encoding="utf-8")
    html = (FRONT / "privacy" / "index.html").read_text(encoding="utf-8")
    assert "не использует cookie" in html and "hostland.ru" in html
