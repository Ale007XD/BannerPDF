"""Сгенерированные SEO-страницы: актуальны, не дублируются, без непроверенных заявлений."""
import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)
FRONT = WEB / "frontend"


def test_generated_files_are_up_to_date():
    for p in bp.PAGES:
        assert (FRONT / p["slug"] / "index.html").read_text(encoding="utf-8") == bp.render(p), p["slug"]


def test_each_page_is_unique_and_well_formed():
    titles, descs = set(), set()
    for p in bp.PAGES:
        html = bp.render(p)
        assert len(re.findall(r"<h1>", html)) == 1
        assert f'<link rel="canonical" href="{bp.SITE}/{p["slug"]}/">' in html
        assert p["title"] not in titles and p["desc"] not in descs
        titles.add(p["title"])
        descs.add(p["desc"])
        assert len(p["desc"]) <= 200
        for w in ("ICC", "ISO Coated", "ISOcoated", "PDF/X-1a", "FOGRA", "ГОСТ"):
            assert w not in html, f'{p["slug"]}: «{w}»'
        assert "{{" not in html


def test_related_links_resolve_and_sitemap_lists_pages():
    slugs = {p["slug"] for p in bp.PAGES}
    for p in bp.PAGES:
        assert set(p["related"]) <= slugs
    root = ET.fromstring((FRONT / "sitemap.xml").read_bytes())
    locs = {e.text for e in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc")}
    assert {f"{bp.SITE}/{s}/" for s in slugs} <= locs


def test_eyelet_formula():
    assert bp.eyelets(2000, 1000, 500) == 12
