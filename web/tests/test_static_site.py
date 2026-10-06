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
  - на сайте нет заявлений про ICC/ISO Coated/PDF/X: файл — чистый CMYK без профиля,
    а то, что заявлено («CMYK, шрифты в кривых, масштаб 1:1, значения красок заданы
    напрямую»), проверяет test_print_pdf.py
"""

import html as htmllib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

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


def _location_block(conf: str, header: str) -> str:
    start = conf.index(header)
    return conf[start: conf.index("}", start) + 1]


class TestRobotsAndSitemap:
    def test_robots_txt(self):
        text = (FRONTEND / "robots.txt").read_text(encoding="utf-8")
        assert "User-agent: *" in text
        assert "Disallow: /api/" in text
        assert f"Sitemap: {SITE}/sitemap.xml" in text

    def test_robots_txt_does_not_announce_admin_and_does_not_block_noindex(self):
        text = (FRONTEND / "robots.txt").read_text(encoding="utf-8")
        assert "/admin" not in text  # закрыт X-Robots-Tag в nginx; Disallow скрыл бы заголовок от бота

    def test_sitemap_is_valid_xml_with_public_pages(self):
        root = ET.fromstring((FRONTEND / "sitemap.xml").read_bytes())
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [e.text for e in root.findall("s:url/s:loc", ns)]
        assert locs == [f"{SITE}/", f"{SITE}/requisites.html"]

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
        assert "style.css?v=16" in INDEX


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


def _json_ld() -> dict:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', INDEX, flags=re.S)
    assert m, "нет JSON-LD"
    return json.loads(m.group(1))


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
