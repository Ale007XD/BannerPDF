"""
test_static_site.py
~~~~~~~~~~~~~~~~~~~
Статика сайта и конфиг nginx: то, что влияет на индексацию и на заявления в UI.

  - robots.txt / sitemap.xml / 404.html существуют и согласованы с nginx
    (адрес админки в robots.txt не светим; закрыта заголовком X-Robots-Tag;
    в sitemap нет зашитого lastmod, который устареет)
  - nginx не подменяет несуществующие адреса страницей конструктора (soft-404)
  - /admin/ закрыт от индексации
  - viewport не запрещает масштабирование, на странице ровно один <h1>
  - SEO-блок (title/description/OG, JSON-LD, «как это работает», FAQ): цифры в тексте
    сверяются с константами кода и с юр. страницей, чтобы текст не разошёлся с продуктом
  - каждая локальная ссылка из HTML (href/src/og:image/иконки) ведёт на существующий файл
    (OG-картинка и favicon раньше отсутствовали, а разметка на них ссылалась)
  - robots.txt: Clean-param для Яндекса; nginx: http2, gzip, кэш статики без immutable
  - на сайте нет заявлений про ICC/ISO Coated/PDF/X: файл — чистый CMYK без профиля,
    а то, что заявлено («CMYK, шрифты в кривых, масштаб 1:1, значения красок заданы
    напрямую»), проверяет test_print_pdf.py
"""

import html as htmllib
import json
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from web.api.routers.order import AMEND_WINDOW_HOURS
from web.api.services.config import MAX_DIMENSION, MIN_DIMENSION, SAFE_ZONE_MM
from web.api.services.token_store import MAX_DOWNLOADS, TOKEN_TTL_SECONDS

WEB = Path(__file__).resolve().parents[1]
FRONTEND = WEB / "frontend"
INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")
REQUISITES = (FRONTEND / "requisites.html").read_text(encoding="utf-8")
NGINX = (WEB / "nginx" / "default.conf").read_text(encoding="utf-8")
CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")

SITE = "https://bannerbot.ru"
BRAND = "BannerBot"


def _server_blocks() -> list[str]:
    """Верхнеуровневые server { … } из default.conf (вложенные location закрываются своим «}»)."""
    blocks, depth, start = [], 0, None
    for i, ch in enumerate(NGINX):
        if ch == "{":
            if depth == 0:
                start = NGINX.rfind("server", 0, i)
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blocks.append(NGINX[start: i + 1])
    return blocks


def _location_block(conf: str, header: str) -> str:
    start = conf.index(header)
    return conf[start: conf.index("}", start) + 1]


class TestRobotsAndSitemap:
    def test_robots_txt(self):
        text = (FRONTEND / "robots.txt").read_text(encoding="utf-8")
        assert "User-agent: *" in text
        assert "Disallow: /api/" in text
        assert f"Sitemap: {SITE}/sitemap.xml" in text

    def test_robots_txt_yandex_clean_param(self):
        text = (FRONTEND / "robots.txt").read_text(encoding="utf-8")
        assert "Clean-param: order_id&amend /" in text
        # параметры, с которыми главная открывается из оплаты и из письма
        assert "order_id" in (FRONTEND / "app.js").read_text(encoding="utf-8")

    def test_robots_txt_does_not_announce_admin_and_does_not_block_noindex(self):
        text = (FRONTEND / "robots.txt").read_text(encoding="utf-8")
        assert "/admin" not in text  # закрыт X-Robots-Tag в nginx; Disallow скрыл бы заголовок от бота

    def test_sitemap_is_valid_xml_with_public_pages(self):
        root = ET.fromstring((FRONTEND / "sitemap.xml").read_bytes())
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [e.text for e in root.findall("s:url/s:loc", ns)]
        assert locs[:2] == [f"{SITE}/", f"{SITE}/requisites.html"]

    def test_sitemap_has_no_hardcoded_lastmod(self):
        assert "lastmod" not in (FRONTEND / "sitemap.xml").read_text(encoding="utf-8")

    def test_sitemap_pages_exist_on_disk(self):
        assert (FRONTEND / "index.html").is_file()
        assert (FRONTEND / "requisites.html").is_file()

    def test_404_page_not_indexed(self):
        html = (FRONTEND / "404.html").read_text(encoding="utf-8")
        assert 'name="robots" content="noindex"' in html


class TestNginx:
    def test_no_spa_fallback_to_index(self):
        block = _location_block(NGINX, "location / {")
        assert "try_files $uri $uri/ =404;" in block
        assert "/index.html" not in block

    def test_custom_404_is_internal(self):
        assert "error_page 404 /404.html;" in NGINX
        assert "internal;" in _location_block(NGINX, "location = /404.html {")

    def test_admin_not_indexable(self):
        block = _location_block(NGINX, "location /admin/ {")
        assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in block

    def test_api_proxy_untouched(self):
        block = _location_block(NGINX, "location /api/ {")
        assert "proxy_pass http://api:8000;" in block
        assert "proxy_set_header X-Real-IP $remote_addr;" in block

    def test_promo_check_is_rate_limited_with_the_same_proxy_settings(self):
        promo = _location_block(NGINX, "location = /api/promo/check {")
        api = _location_block(NGINX, "location /api/ {")
        assert "limit_req zone=promo_limit burst=10 nodelay;" in promo
        assert "limit_req_status 429;" in promo
        # точный location не наследует настройки префиксного: прокси должен совпадать полностью
        proxy = lambda block: sorted(re.findall(r"^\s*(proxy_\w+ [^;]+);", block, flags=re.M))  # noqa: E731
        assert proxy(promo) == proxy(api)
        assert "limit_req" not in api   # лимиты стоят только у точных location: остальной /api/ не ограничен

    def test_promo_limit_zone_is_declared_in_http_block(self):
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        m = re.search(r"limit_req_zone \$binary_remote_addr zone=promo_limit:(\d+m) rate=(\d+)r/m;", conf)
        assert m, "зона promo_limit не объявлена в nginx.conf"
        assert int(m.group(2)) >= 10   # дебаунс на сайте: человек не должен упираться в лимит

    def test_order_creation_is_rate_limited_with_the_same_proxy_settings(self):
        order = _location_block(NGINX, "location = /api/order {")
        api = _location_block(NGINX, "location /api/ {")
        assert "limit_req zone=order_limit burst=2 nodelay;" in order
        assert "limit_req_status 429;" in order
        proxy = lambda block: sorted(re.findall(r"^\s*(proxy_\w+ [^;]+);", block, flags=re.M))  # noqa: E731
        assert proxy(order) == proxy(api)

    def test_order_limit_covers_only_order_creation(self):
        # amend/abandon/status идут по общему /api/: abandon нельзя душить, он возвращает использование промокода
        assert "location = /api/order {" in NGINX
        assert not re.search(r"location\s+[~^=]*\s*/api/order/", NGINX)
        assert len(re.findall(r"limit_req zone=order_limit", NGINX)) == 1

    def test_order_limit_zone_is_declared_and_allows_a_human(self):
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        m = re.search(r"limit_req_zone \$binary_remote_addr zone=order_limit:(\d+m) rate=(\d+)r/m;", conf)
        assert m, "зона order_limit не объявлена в nginx.conf"
        assert int(m.group(2)) >= 3   # человек оформляет заказ раз в несколько минут, но не раз в час

    def test_every_applied_limit_zone_is_declared(self):
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        declared = set(re.findall(r"limit_req_zone [^;]*zone=(\w+):", conf))
        used = set(re.findall(r"limit_req zone=(\w+)", NGINX))
        assert used <= declared, f"в default.conf применены необъявленные зоны: {used - declared}"

    def test_every_declared_limit_zone_is_applied_exactly_once(self):
        # объявленная, но не применённая зона ничего не ограничивает (так было с preview/order/download)
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        declared = re.findall(r"limit_req_zone [^;]*zone=(\w+):", conf)
        assert sorted(declared) == ["beat_limit", "download_limit", "order_limit", "preview_limit", "promo_limit"]
        for zone in declared:
            assert len(re.findall(rf"limit_req zone={zone}\b", NGINX)) == 1, zone

    def test_preview_is_rate_limited_with_the_same_proxy_settings(self):
        preview = _location_block(NGINX, "location = /api/preview {")
        api = _location_block(NGINX, "location /api/ {")
        assert "limit_req zone=preview_limit burst=10 nodelay;" in preview
        assert "limit_req_status 429;" in preview
        proxy = lambda block: sorted(re.findall(r"^\s*(proxy_\w+ [^;]+);", block, flags=re.M))  # noqa: E731
        assert proxy(preview) == proxy(api)

    def test_download_is_rate_limited_with_the_same_proxy_settings(self):
        download = _location_block(NGINX, "location /api/download/ {")
        api = _location_block(NGINX, "location /api/ {")
        assert "limit_req zone=download_limit burst=3 nodelay;" in download
        assert "limit_req_status 429;" in download
        proxy = lambda block: sorted(re.findall(r"^\s*(proxy_\w+ [^;]+);", block, flags=re.M))  # noqa: E731
        assert proxy(download) == proxy(api)

    def test_regex_locations_cannot_take_over_limited_api_paths(self):
        # regex-location побеждает обычный префиксный: проверяем, что ни один не ловит лимитируемые пути
        for pattern in re.findall(r"^\s*location\s+~\*?\s+(\S+)\s*\{", NGINX, flags=re.M):
            for path in ("/api/preview", "/api/download/abc", "/api/order", "/api/promo/check"):
                assert not re.search(pattern, path), f"{pattern} перехватывает {path}"

    def test_download_limit_does_not_touch_neighbour_paths(self):
        assert not re.search(r"location\s+[~^=]*\s*/api/(v1|order|payment|admin)", NGINX.replace("location = /api/order {", ""))
        # префикс ровно /api/download/, а не /api/download или /api/d: иначе заденет чужие пути
        assert "location /api/download/ {" in NGINX
        assert not re.search(r"location\s+/api/d\w*\s*\{", NGINX.replace("location /api/download/ {", ""))

    def test_preview_and_download_zone_rates_allow_a_human(self):
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        rate = lambda zone: int(re.search(rf"zone={zone}:\d+m rate=(\d+)r/m;", conf).group(1))  # noqa: E731
        assert rate("preview_limit") >= 30      # правки макета идут подряд, дебаунс на сайте 500 мс
        assert rate("download_limit") >= 6      # скачивание одно, плюс «Скачать ещё раз» после сбоя


    def test_http2_enabled_on_every_tls_server(self):
        servers = _server_blocks()
        tls = [b for b in servers if "listen 443 ssl;" in b]
        assert len(tls) == 2
        for block in tls:
            assert re.search(r"^\s*http2 on;", block, flags=re.M)

    def test_one_canonical_host_plain_http_redirects_in_one_hop(self):
        http = next(b for b in _server_blocks() if "listen 80;" in b)
        assert "server_name bannerbot.ru www.bannerbot.ru;" in http
        assert "return 301 https://bannerbot.ru$request_uri;" in http
        assert "$host" not in http  # иначе http://www → https://www → ещё один редирект

    def test_www_https_redirects_to_apex_keeping_path_and_query(self):
        tls = [b for b in _server_blocks() if "listen 443 ssl;" in b]
        apex, www = tls  # порядок важен: default_server для :443 — основной сайт
        assert "server_name bannerbot.ru;" in apex and "root /app/frontend;" in apex
        assert "server_name www.bannerbot.ru;" in www
        assert "return 301 https://bannerbot.ru$request_uri;" in www
        assert "root " not in www and "location" not in www
        # сертификат тот же: www должен быть в его SAN
        assert re.findall(r"ssl_certificate\s+(\S+);", www) == re.findall(r"ssl_certificate\s+(\S+);", apex)

    def test_gzip_covers_svg_and_xml(self):
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        types = re.search(r"gzip_types\s+([^;]+);", conf).group(1).split()
        for t in ("text/css", "application/javascript", "image/svg+xml", "application/xml", "text/xml"):
            assert t in types

    def test_static_cache_rules_match_only_static_files(self):
        patterns = re.findall(r"location ~ (\S+) \{", NGINX)
        assert len(patterns) == 2
        css_js, images = (re.compile(p) for p in patterns)
        for path in ("/style.css", "/app.js"):
            assert css_js.match(path)
        for path in ("/favicon.ico", "/favicon.svg", "/apple-touch-icon.png", "/static/og/og-image.jpg"):
            assert images.match(path)
        for path in ("/admin/app.js", "/api/style.css", "/api/static/og/x.jpg", "/index.html", "/static/og/../../app.js"):
            assert not css_js.match(path) and not images.match(path), path

    def test_cache_is_not_immutable(self):
        # ?v=N правится руками: забытый bump не должен залипнуть на год
        assert "immutable" not in re.sub(r"#.*", "", NGINX)
        for max_age in re.findall(r"max-age=(\d+)", NGINX):
            assert int(max_age) <= 7 * 86400


class TestIndexHtml:
    def test_viewport_allows_zoom(self):
        viewport = re.search(r'<meta name="viewport" content="([^"]*)"', INDEX).group(1)
        assert "maximum-scale" not in viewport
        assert "user-scalable=no" not in viewport.replace(" ", "")

    def test_ios_input_zoom_guard_present(self):
        # без maximum-scale iOS зумит поля со шрифтом < 16px
        assert "@media (hover: none) and (pointer: coarse)" in CSS
        guard = CSS[CSS.index("@media (hover: none) and (pointer: coarse)"):]
        assert "font-size: 16px;" in guard

    def test_exactly_one_h1_with_query_text(self):
        h1 = re.findall(r"<h1[^>]*>(.*?)</h1>", INDEX, flags=re.S)
        assert len(h1) == 1
        assert "баннер" in h1[0].lower() and "типограф" in h1[0].lower()

    def test_canonical_points_to_site_root(self):
        assert f'<link rel="canonical" href="{SITE}/"' in INDEX

    def test_preview_images_have_alt(self):
        tags = re.findall(r'<img id="(?:bs-)?preview-img"[^>]*>', INDEX)
        assert len(tags) == 2, "регэксп не нашёл оба превью (разметка изменилась?)"
        for tag in tags:
            assert re.search(r'alt="[^"]+"', tag)

    def test_file_claims_match_what_the_pdf_contains(self):
        assert "CMYK · Шрифты в кривых · Масштаб 1:1" in INDEX
        assert "Значения красок заданы напрямую" in REQUISITES
        assert "RIP вашей типографии" in REQUISITES

    def test_no_profile_or_standard_claims_on_site(self):
        for name, html in (("index.html", INDEX), ("requisites.html", REQUISITES)):
            for word in ("ICC", "ISO Coated", "ISOcoated", "PDF/X", "FOGRA"):
                assert word not in html, f"{name}: заявление «{word}» не подтверждено файлом"

    def test_css_version_bumped_for_new_rules(self):
        assert "style.css?v=22" in INDEX


# ---------------------------------------------------------------------------
# SEO-блок: мета-теги, JSON-LD, «как это работает», FAQ
# ---------------------------------------------------------------------------
TEMPLATES = json.loads((WEB / "templates.json").read_text(encoding="utf-8"))
PRICE = int(os.getenv("SITE_PDF_PRICE", "299"))


def _meta(name: str, attr: str = "name") -> str:
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', INDEX)
    assert m, f"нет <meta {attr}={name}>"
    return htmllib.unescape(m.group(1))


def _section() -> str:
    start = INDEX.index('<section class="seo-info"')
    return INDEX[start: INDEX.index("</section>", start)]


def _visible(fragment: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))


def _json_ld_all() -> list[dict]:
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', INDEX, flags=re.S)
    assert blocks, "нет JSON-LD"
    return [json.loads(b) for b in blocks]


def _json_ld() -> dict:
    return next(b for b in _json_ld_all() if b["@type"] == "WebApplication")


class TestSeoMeta:
    def test_title_targets_the_query_and_fits_the_snippet(self):
        title = re.search(r"<title>(.*?)</title>", INDEX).group(1)
        assert "макет баннера" in title.lower() and "типограф" in title.lower()
        assert f"{PRICE} ₽" in title
        assert len(title) <= 70

    def test_description_fits_the_snippet_and_states_real_limits(self):
        desc = _meta("description")
        assert len(desc) <= 170
        assert f"от {MIN_DIMENSION} до {MAX_DIMENSION} мм" in desc
        assert f"{PRICE} ₽" in desc

    def test_open_graph_and_twitter_match_title_and_description(self):
        title = re.search(r"<title>(.*?)</title>", INDEX).group(1)
        desc = _meta("description")
        assert _meta("og:title", "property") == title
        assert _meta("og:description", "property") == desc
        assert _meta("twitter:title") == title
        assert _meta("twitter:description") == desc


class TestJsonLd:
    def test_web_application_with_offer(self):
        ld = _json_ld()
        assert ld["@context"] == "https://schema.org"
        assert ld["@type"] == "WebApplication"
        assert ld["url"] == f"{SITE}/"
        assert ld["inLanguage"] == "ru"
        assert ld["offers"]["@type"] == "Offer"
        assert ld["offers"]["price"] == str(PRICE)
        assert ld["offers"]["priceCurrency"] == "RUB"

    def test_description_is_the_meta_description(self):
        assert _json_ld()["description"] == _meta("description")

    def test_website_block_gives_site_name_and_domain(self):
        site = next(b for b in _json_ld_all() if b["@type"] == "WebSite")
        assert site["name"] == BRAND
        assert site["alternateName"] == "bannerbot.ru"
        assert site["url"] == f"{SITE}/"

    def test_no_invented_ratings_or_reviews(self):
        raw = json.dumps(_json_ld())
        for key in ("aggregateRating", "review", "ratingValue", "ratingCount"):
            assert key not in raw

    def test_price_matches_visible_price(self):
        assert f'<span class="buy-price">{PRICE} ₽</span>' in INDEX


class TestSeoSection:
    def test_placed_between_main_and_footer(self):
        assert INDEX.index("</main>") < INDEX.index('<section class="seo-info"') < INDEX.index("<footer")

    def test_heading_structure(self):
        section = _section()
        assert len(re.findall(r"<h2", section)) == 3
        assert "<h1" not in section and "<h3" not in section
        labelled = re.search(r'aria-labelledby="([^"]+)"', section).group(1)
        assert f'id="{labelled}"' in section

    def test_faq_items_are_complete_and_unique(self):
        items = re.findall(r"<details class=\"seo-faq\">\s*<summary>(.*?)</summary>\s*<p>(.*?)</p>", _section(), flags=re.S)
        assert len(items) == 7
        questions = [q.strip() for q, _ in items]
        assert len(set(questions)) == len(questions)
        assert all(len(_visible(a).strip()) > 40 for _, a in items)

    def test_numbers_come_from_code_constants(self):
        """Каждое число в блоке равно константе кода: ни одно упоминание не может разойтись."""
        text = _visible(_section())

        def found(pattern: str) -> set:
            return {m.groups() if len(m.groups()) > 1 else m.group(1) for m in re.finditer(pattern, text)}

        assert found(r"от (\d+) до (\d+) мм") == {(str(MIN_DIMENSION), str(MAX_DIMENSION))}
        assert found(r"[Пп]ол(?:я|ей) (\d+) мм") == {str(SAFE_ZONE_MM)}
        assert found(r"до (\d+) строк") == {str(TEMPLATES["max_lines"])}
        assert found(r"(\d+) минут(?!ы)") == {str(TOKEN_TTL_SECONDS // 60)}
        assert found(r"до (\d+) скачиваний") == {str(MAX_DOWNLOADS)}
        assert found(r"(\d+) час") == {str(AMEND_WINDOW_HOURS)}
        # неразрывный пробел: цена не рвётся по строкам
        assert found(r"(\d+)\u00a0₽") == {str(PRICE)}

    def test_price_is_never_split_by_a_normal_space(self):
        assert f"{PRICE} ₽" not in _visible(_section())

    def test_operational_terms_match_the_legal_page(self):
        text = _visible(_section())
        legal = htmllib.unescape(re.sub(r"<[^>]+>", " ", REQUISITES))
        for phrase in (
            f"{TOKEN_TTL_SECONDS // 60} минут",
            f"до {MAX_DOWNLOADS} скачиваний",
            f"{AMEND_WINDOW_HOURS} часов",
            "НДС не облагается",
            "ЮKassa",
        ):
            assert phrase in text, f"нет в блоке: {phrase}"
            assert phrase in legal, f"нет в requisites.html: {phrase}"

    def test_standard_sizes_in_faq_exist_in_templates(self):
        available = {(s["width_mm"], s["height_mm"]) for s in TEMPLATES["sizes"]}
        for size in ((2000, 1000), (1500, 1000), (1000, 500)):
            assert size in available
        text = _visible(_section())
        for label in ("2×1 м", "1,5×1 м", "1×0,5 м"):
            assert label in text

    def test_watermark_statement_matches_preview_text(self):
        source = (WEB / "api" / "services" / "banner_generator.py").read_text(encoding="utf-8")
        assert "Сделано за 3 минуты" in source
        assert "Сделано за 3 минуты" in _visible(_section())

    def test_no_unverifiable_or_risky_claims(self):
        text = (_visible(_section()) + _meta("description") + json.dumps(_json_ld(), ensure_ascii=False)).lower()
        for word in ("анонимн", "гаранти", "любая типограф", "любой типограф", "гост", "icc", "iso coated", "pdf/x", "лучш"):
            assert word not in text, f"в тексте: «{word}»"

    def test_links_inside_section_are_real(self):
        section = _section()
        assert section.count('href="/requisites.html"') == 2
        assert 'href="mailto:alex.deloverov@gmail.com"' in section
        assert (FRONTEND / "requisites.html").is_file()


# ---------------------------------------------------------------------------
# Локальные ссылки, иконки, OG-картинка
# ---------------------------------------------------------------------------
class _Refs(HTMLParser):
    """Собирает адреса из href/src и из og:image/twitter:image/og:url."""

    def __init__(self):
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("link", "a") and a.get("href"):
            self.refs.append(a["href"])
        elif tag in ("script", "img", "source") and a.get("src"):
            self.refs.append(a["src"])
        elif tag == "meta" and a.get("content") and (
            a.get("property") in ("og:image", "og:image:secure_url", "og:url")
            or a.get("name") == "twitter:image"
        ):
            self.refs.append(a["content"])


def _local_refs(html: str) -> list[str]:
    """Адреса, которые отдаёт nginx из frontend/ (внешние, mailto, якоря и /api/ пропускаются)."""
    parser = _Refs()
    parser.feed(html)
    out = []
    for ref in parser.refs:
        ref = ref.split("#")[0].split("?")[0].strip()
        if ref.startswith(SITE):
            ref = ref[len(SITE):] or "/"
        elif not ref or re.match(r"^[a-z][a-z0-9+.-]*:", ref, flags=re.I) or ref.startswith("//"):
            continue
        if ref.startswith("/api/"):
            continue
        out.append(ref)
    return out


def _to_file(ref: str) -> Path:
    path = FRONTEND / ref.lstrip("/")
    return path / "index.html" if ref.endswith("/") or path.is_dir() else path


PAGES = {
    "index.html": INDEX,
    "requisites.html": REQUISITES,
    "404.html": (FRONTEND / "404.html").read_text(encoding="utf-8"),
}

# Статические SEO-страницы (генерируются web/seo/build_pages.py): те же проверки ссылок, иконок и бренда
SEO_PAGES = {
    f"{p.parent.name}/index.html": p.read_text(encoding="utf-8")
    for p in sorted(FRONTEND.glob("*/index.html"))
    if p.parent.name != "admin"
}
ALL_PUBLIC_PAGES = {**PAGES, **SEO_PAGES}


class TestLocalReferences:
    def test_parser_sees_the_references_it_should(self):
        refs = _local_refs(INDEX)
        assert "/static/og/og-image.jpg" in refs
        assert "style.css" in refs and "app.js" in refs
        assert len(refs) >= 10, refs

    def test_seo_pages_are_found(self):
        assert len(SEO_PAGES) >= 7, sorted(SEO_PAGES)  # иначе проверки ниже прошли бы вхолостую

    def test_every_local_reference_exists(self):
        missing = []
        for name, html in ALL_PUBLIC_PAGES.items():
            for ref in _local_refs(html):
                if not _to_file(ref).is_file():
                    missing.append(f"{name}: {ref}")
        assert not missing, missing

    def test_404_uses_only_root_absolute_paths(self):
        # 404.html отдаётся на любом адресе (/x/y), относительные пути там сломаются
        for ref in _local_refs(PAGES["404.html"]):
            assert ref.startswith("/"), ref

    def test_sitemap_urls_exist_on_disk(self):
        root = ET.fromstring((FRONTEND / "sitemap.xml").read_bytes())
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in (e.text for e in root.findall("s:url/s:loc", ns)):
            assert _to_file(loc[len(SITE):]).is_file(), loc

    def test_asset_versions_are_the_same_on_all_pages(self):
        # ?v=N правится руками: страница, забытая на старом N, получает из кэша старый файл
        for asset in ("style.css", "app.js"):
            per_page = {
                name: set(re.findall(rf"{re.escape(asset)}\?v=(\d+)", html))
                for name, html in PAGES.items()
            }
            used = {name: v for name, v in per_page.items() if v}
            assert "index.html" in used, f"{asset}: нет версии на главной"
            assert all(len(v) == 1 for v in used.values()), (asset, used)
            assert len({next(iter(v)) for v in used.values()}) == 1, (asset, used)


class TestIconsAndOgImage:
    def test_icon_links_on_all_pages(self):
        for name, html in ALL_PUBLIC_PAGES.items():
            assert 'rel="icon" href="/favicon.ico"' in html, name
            assert 'rel="icon" href="/favicon.svg"' in html, name
            assert 'rel="apple-touch-icon" href="/apple-touch-icon.png"' in html, name

    def test_favicon_ico_has_standard_sizes(self):
        with Image.open(FRONTEND / "favicon.ico") as ico:
            assert ico.format == "ICO"
            assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info["sizes"])

    def test_apple_touch_icon_is_180_png_without_alpha(self):
        with Image.open(FRONTEND / "apple-touch-icon.png") as im:
            assert im.format == "PNG" and im.size == (180, 180)
            assert im.mode in ("RGB", "L"), "iOS закрашивает прозрачность чёрным"

    def test_favicon_svg_is_well_formed(self):
        root = ET.fromstring((FRONTEND / "favicon.svg").read_bytes())
        assert root.tag.endswith("svg") and root.get("viewBox")

    def test_og_image_matches_declared_size(self):
        declared = (int(_meta("og:image:width", "property")), int(_meta("og:image:height", "property")))
        path = FRONTEND / "static" / "og" / "og-image.jpg"
        assert path.stat().st_size < 300_000, "тяжёлая картинка режется мессенджерами"
        with Image.open(path) as im:
            assert im.format == "JPEG"
            assert im.size == declared == (1200, 630)

    def test_og_and_twitter_image_are_the_same_file(self):
        assert _meta("og:image", "property") == _meta("twitter:image") == f"{SITE}/static/og/og-image.jpg"


# ---------------------------------------------------------------------------
# Бренд: везде BannerBot (домен bannerbot.ru), прежнее «BannerPrint» не должно вернуться
# ---------------------------------------------------------------------------
class TestBrand:
    ALL_PAGES = {**PAGES, "admin/index.html": (FRONTEND / "admin" / "index.html").read_text(encoding="utf-8")}

    def test_no_old_brand_in_any_page(self):
        for name, html in {**self.ALL_PAGES, **SEO_PAGES}.items():
            assert not re.search(r"banner\s*print", html, flags=re.I), name
            assert "Print</span>" not in html, f"{name}: старый логотип"

    def test_logo_reads_bannerbot_in_every_page_that_has_one(self):
        for name in ("index.html", "requisites.html", "admin/index.html"):
            assert 'Banner<span class="logo-accent">Bot</span>' in self.ALL_PAGES[name], name

    def test_footer_copyright(self):
        for name in ("index.html", "requisites.html"):
            assert f"© 2026 {BRAND}" in self.ALL_PAGES[name], name

    def test_site_name_is_the_same_everywhere(self):
        assert _meta("og:site_name", "property") == BRAND
        assert _json_ld()["name"] == BRAND
        assert BRAND in _meta("og:image:alt", "property")
        assert f"— {BRAND}</title>" in REQUISITES
        assert f"— {BRAND}</title>" in PAGES["404.html"]

    def test_customer_visible_strings_in_backend(self):
        api = WEB / "api"
        src = (api / "routers" / "order.py").read_text(encoding="utf-8")
        assert f'" — {BRAND}"' in src, "routers/order.py: описание платежа (попадает в чек)"
        gen = (api / "services" / "banner_generator.py").read_text(encoding="utf-8")
        assert f'setAuthor("{BRAND}")' in gen and f'setCreator("{BRAND}")' in gen


# ---------------------------------------------------------------------------
# Путь оплаты: SDK ЮKassa не блокирует страницу, окно оплаты открывается сразу
# ---------------------------------------------------------------------------
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
YK_SDK = "https://yookassa.ru/checkout-widget/v1/checkout-widget.js"


def _function_body(src: str, header: str) -> str:
    start = src.index(header)
    nxt = re.search(r"\n(?:async )?function |\n/\* ={10,}", src[start + len(header):])
    return src[start: start + len(header) + (nxt.start() if nxt else len(src))]


class TestPaymentPathLatency:
    def test_sdk_is_not_a_blocking_script_tag(self):
        # синхронный <script src=yookassa.ru> перед app.js держал весь конструктор
        assert "checkout-widget.js" not in INDEX
        assert not re.search(r"<script[^>]+yookassa\.ru", INDEX)

    def test_sdk_loaded_by_app_js_from_the_documented_url(self):
        assert f'const YK_SDK_URL = "{YK_SDK}";' in APP_JS
        loader = _function_body(APP_JS, "function loadYooKassaSdk()")
        assert "script.async = true" in loader
        assert "ykSdkPromise = null" in loader  # после сбоя следующая попытка грузит заново

    def test_yookassa_is_not_contacted_on_page_load(self):
        # Без намерения платить сторонний сервис не вызывается (ни preconnect, ни SDK)
        assert "yookassa.ru" not in INDEX
        app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        assert not re.search(r'addEventListener\("load",[^)]*loadYooKassaSdk', app)

    def test_sdk_loaded_on_confirm_screen_not_on_page_load(self):
        # Сторонний сервис вызываем, только когда человек собрался платить
        assert not re.search(r'addEventListener\("load",[^)]*loadYooKassaSdk', APP_JS)
        assert "loadYooKassaSdk()" in _function_body(APP_JS, "function openConfirm()")

    def test_pay_window_opens_before_the_server_answers(self):
        body = _function_body(APP_JS, "async function createOrder(payload)")
        assert body.index("showPayLoading()") < body.index("await fetch(API.order")

    def test_closing_the_window_cancels_a_late_widget(self):
        body = _function_body(APP_JS, "async function createOrder(payload)")
        assert "attempt !== payAttempt" in body
        assert "payAttempt++" in _function_body(APP_JS, "function showPayLoading(")
        widget = _function_body(APP_JS, "async function openYooKassaWidget(")
        assert widget.count("attempt !== payAttempt") >= 1 and "payAttempt++" in widget

    def test_loader_stays_until_the_payment_form_iframe_loads(self):
        widget = _function_body(APP_JS, "async function openYooKassaWidget(")
        assert 'form.querySelector("iframe")' in widget
        assert 'addEventListener("load", hideLoader' in widget
        assert "setTimeout(" in widget  # страховка, если iframe не появится

    def test_sdk_failure_is_reported_to_the_user(self):
        widget = _function_body(APP_JS, "async function openYooKassaWidget(")
        assert "Не удалось открыть форму оплаты" in widget
        assert "hideModal(el.modalPay)" in widget

    def test_loader_style_exists_and_assets_bumped(self):
        assert ".pay-loading" in CSS
        assert "app.js?v=27" in INDEX


class TestPromoCheckWiring:
    """Путь проверки промокода совпадает в фронте, роутере и nginx; ввод больше не обещает «бесплатно» любому коду."""

    def test_same_path_in_js_router_and_nginx(self):
        path = "/api/promo/check"
        assert f'promoCheck: "{path}"' in APP_JS
        assert f"location = {path} {{" in NGINX
        router = (WEB / "api" / "routers" / "promo.py").read_text(encoding="utf-8")
        assert '@router.post("/promo/check")' in router   # префикс /api — в main.py
        assert "app.include_router(promo.router" in (WEB / "api" / "main.py").read_text(encoding="utf-8")

    def test_order_payload_uses_checked_promo_code(self):
        body = _function_body(APP_JS, "function buildConfig()")
        assert "promo_code: promoCodeForOrder()" in body
        assert "promo_code: state.promoCode" not in body

    def test_input_handler_checks_on_server_instead_of_promising_free(self):
        assert "Промокод будет применён при оформлении" not in APP_JS
        handler = APP_JS[APP_JS.index("el.promoInput.addEventListener(\"input\""):]
        handler = handler[: handler.index("\n}\n")]
        assert "schedulePromoCheck()" in handler
        assert "BUY_FREE_TEXT" not in handler and "бесплатно" not in handler

    def test_free_text_only_comes_from_server_answer(self):
        body = _function_body(APP_JS, "function renderBuyButtons()")
        assert "p.free" in body

    def test_abandon_path_matches_in_js_and_router(self):
        assert 'abandon:   (id) => `/api/order/${id}/abandon`' in APP_JS
        router = (WEB / "api" / "routers" / "order.py").read_text(encoding="utf-8")
        assert '@router.post("/order/{order_id}/abandon")' in router   # префикс /api — в main.py

    def test_release_helper_is_idempotent_friendly_and_survives_network_errors(self):
        body = _function_body(APP_JS, "async function releaseOrderPromo(")
        assert "API.abandon(orderId)" in body and 'method: "POST"' in body
        assert "keepalive: true" in body                 # запрос не обрывается, если вкладку закрывают
        assert "if (!usedPromo || !orderId) return;" in body
        assert "try {" in body and "catch" in body       # не дошёл запрос — вернёт фоновая очистка
        assert body.index("await fetch(") < body.index("recheckPromo()")   # перепроверка после возврата

    def test_every_unpaid_exit_of_the_pay_flow_releases_the_promo_use(self):
        widget = _function_body(APP_JS, "async function openYooKassaWidget(")
        # SDK не загрузился, окно закрыли во время загрузки SDK, error_callback, fail, кнопка закрытия
        assert widget.count("releaseOrderPromo(orderId, usedPromo)") == 5
        order = _function_body(APP_JS, "async function createOrder(")
        assert "releaseOrderPromo(data.order_id, usedPromo)" in order        # ответ пришёл после закрытия окна
        assert "releaseOrderPromo(unpaidOrderId, usedPromo)" in order        # виджет упал при запуске

    def test_paid_and_free_paths_never_release(self):
        widget = _function_body(APP_JS, "async function openYooKassaWidget(")
        success = widget[widget.index('checkout.on("success"'):widget.index('checkout.on("fail"')]
        assert "releaseOrderPromo" not in success
        polling = _function_body(APP_JS, "function startPolling(")
        assert "releaseOrderPromo" not in polling
        order = _function_body(APP_JS, "async function createOrder(")
        free = order[order.index("if (data.free && data.download_token)"):order.index("Окно закрыли, пока создавался заказ")]
        assert "releaseOrderPromo" not in free

    def test_closing_the_loader_before_the_order_exists_does_not_guess_an_order(self):
        loader = _function_body(APP_JS, "function showPayLoading(")
        assert "releaseOrderPromo" not in loader and "recheckPromo" not in loader   # id ещё неизвестен

    def test_rate_limited_order_gets_a_readable_message(self):
        order = _function_body(APP_JS, "async function createOrder(")
        assert "resp.status === 429" in order
        assert "Слишком много попыток оформить заказ" in order
        assert order.index("resp.status === 429") < order.index("resp.json()")   # nginx отдаёт HTML, не JSON

    def test_preview_retries_on_429_and_ignores_stale_responses(self):
        body = _function_body(APP_JS, "async function fetchPreview(")
        assert "const seq = ++_previewSeq;" in body
        assert body.index("++_previewSeq") < body.index("showPlaceholder()")        # стёртый текст тоже отменяет старый ответ
        assert body.count("seq !== _previewSeq") == 3                              # после fetch, после json, в catch
        assert "resp.status === 429" in body and "PREVIEW_RETRY_DELAYS_MS" in body
        assert "retry < PREVIEW_RETRY_DELAYS_MS.length" in body
        assert "fetchPreview(retry + 1)" in body
        assert body.index("resp.status === 429") < body.index("resp.json()")        # nginx отдаёт HTML, не JSON
        assert "clearTimeout(_previewTimer)" in _function_body(APP_JS, "function schedulePreview(")

    def test_first_preview_retry_waits_for_the_nginx_limit_to_refill(self):
        delays = [int(x) for x in re.search(r"PREVIEW_RETRY_DELAYS_MS = \[([\d, ]+)\]", APP_JS).group(1).split(",")]
        conf = (WEB / "nginx" / "nginx.conf").read_text(encoding="utf-8")
        rate = int(re.search(r"zone=preview_limit:\d+m rate=(\d+)r/m;", conf).group(1))
        assert delays[0] >= 60_000 / rate        # раньше следующий токен не появится: повтор был бы снова 429
        assert delays == sorted(delays) and len(delays) >= 2

    def test_download_429_gets_its_own_message_and_keeps_retry(self):
        body = _function_body(APP_JS, "async function downloadPdf(")
        assert "e.status === 429" in body
        assert "Слишком много запросов" in body and "Скачать ещё раз" in body
        assert body.index("e.status === 404") < body.index("e.status === 429")      # 404 по-прежнему без повтора

    def test_price_markup_kept_for_seo_and_old_price_style_exists(self):
        assert f'<span class="buy-price">{PRICE} ₽</span>' in INDEX   # JSON-LD/описание берут цену отсюда
        assert ".buy-price-old" in INDEX and "line-through" in INDEX
