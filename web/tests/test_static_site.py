"""
test_static_site.py
~~~~~~~~~~~~~~~~~~~
Статика сайта и конфиг nginx: то, что влияет на индексацию и на заявления в UI.

  - robots.txt / sitemap.xml / 404.html существуют и согласованы с nginx
  - nginx не подменяет несуществующие адреса страницей конструктора (soft-404)
  - /admin/ закрыт от индексации
  - viewport не запрещает масштабирование, на странице ровно один <h1>
  - на сайте нет заявлений про ICC/ISO Coated/PDF/X: файл — чистый CMYK без профиля,
    а то, что заявлено («CMYK, шрифты в кривых, масштаб 1:1, значения красок заданы
    напрямую»), проверяет test_print_pdf.py
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

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
        assert "Disallow: /admin/" in text
        assert f"Sitemap: {SITE}/sitemap.xml" in text

    def test_sitemap_is_valid_xml_with_public_pages(self):
        root = ET.fromstring((FRONTEND / "sitemap.xml").read_bytes())
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locs = [e.text for e in root.findall("s:url/s:loc", ns)]
        assert locs == [f"{SITE}/", f"{SITE}/requisites.html"]

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
        for tag in re.findall(r'<img id="(?:bs-)?preview-img"[^>]*>', INDEX):
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
        assert "style.css?v=15" in INDEX
