"""Предзаполнение конструктора из URL и согласованность списка размеров.

parsePrefill (web/frontend/prefill.js) — чистая функция, её проверяет Node
(в CI он есть; без Node тесты пропускаются).
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from web.api.services.config import BANNER_SIZES

WEB = Path(__file__).resolve().parents[1]
PREFILL = WEB / "frontend" / "prefill.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node не установлен")

TEMPLATES = json.loads((WEB / "templates.json").read_text(encoding="utf-8"))
OPTS = {
    "sizeKeys": [s["key"] for s in TEMPLATES["sizes"]],
    "colorNames": [c["name"] for c in TEMPLATES["colors"]],
    "fonts": TEMPLATES["fonts"],
    "maxLines": TEMPLATES["max_lines"],
    "min": 100,
    "max": 3000,
}
SCRIPT = (
    "const {parsePrefill} = require(process.argv[1]);"
    "console.log(JSON.stringify(parsePrefill(process.argv[2], JSON.parse(process.argv[3]))));"
)


def prefill(search):
    out = subprocess.run(
        ["node", "-e", SCRIPT, str(PREFILL), search, json.dumps(OPTS)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_templates_json_matches_config():
    """Кнопки размеров на сайте = BANNER_SIZES (раньше в шаблонах было 4 из 6)."""
    from_templates = {s["key"]: (s["width_mm"], s["height_mm"]) for s in TEMPLATES["sizes"]}
    assert from_templates == dict(BANNER_SIZES)


def test_new_sizes_present():
    assert BANNER_SIZES["3x3"] == (3000, 3000)
    assert BANNER_SIZES["1x1"] == (1000, 1000)


@needs_node
class TestParsePrefill:
    def test_size_key(self):
        assert prefill("?size=2x2")["size"] == {"key": "2x2"}

    def test_custom_size(self):
        assert prefill("?size=3000x1000")["size"] == {"w": 3000, "h": 1000}

    @pytest.mark.parametrize("value", ["3001x1000", "99x500", "abc", "2x", "1000x1000x1", "../etc"])
    def test_bad_size_is_dropped(self, value):
        assert prefill(f"?size={value}")["size"] is None

    def test_text_lines_trimmed_and_ordered(self):
        out = prefill("?text1=%20%D0%90%D0%A0%D0%95%D0%9D%D0%94%D0%90%20&text3=C&text2=")
        assert out["lines"] == ["АРЕНДА", "C"]

    def test_text_length_cap_and_control_chars(self):
        out = prefill("?text1=" + "A" * 300 + "&text2=a%00b%0Ac")
        assert len(out["lines"][0]) == 120
        assert out["lines"][1] == "abc"

    def test_lines_over_max_are_ignored(self):
        q = "&".join(f"text{i}=x{i}" for i in range(1, 9))
        assert prefill("?" + q)["lines"] == [f"x{i}" for i in range(1, 7)]

    def test_script_in_text_stays_plain_text(self):
        out = prefill("?text1=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")
        assert out["lines"] == ["<img src=x onerror=alert(1)>"]  # в DOM попадёт через value + escapeHtml

    def test_colors_and_font_only_from_lists(self):
        ok = prefill("?bg=%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D1%8B%D0%B9&color=%D0%91%D0%B5%D0%BB%D1%8B%D0%B9&font=Golos%20Text")
        assert (ok["bg"], ok["color"], ok["font"]) == ("Красный", "Белый", "Golos Text")
        bad = prefill("?bg=Фиолетовый&color=%3Cb%3E&font=Comic%20Sans")
        assert (bad["bg"], bad["color"], bad["font"]) == (None, None, None)

    def test_empty_search(self):
        assert prefill("") == {"size": None, "lines": [], "bg": None, "color": None, "font": None}


def test_wiring_in_page_and_nginx():
    index = (WEB / "frontend" / "index.html").read_text(encoding="utf-8")
    assert index.index("prefill.js") < index.index("app.js?v=")
    app = (WEB / "frontend" / "app.js").read_text(encoding="utf-8")
    init = app[app.index("async function init()"):]
    assert init.index("renderTextLines()") < init.index("applyUrlPrefill()")
    assert "prefill" in (WEB / "nginx" / "default.conf").read_text(encoding="utf-8")


@needs_node
def test_seo_cta_links_prefill_valid():
    spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)
    assert bp.CTA, "ожидаются CTA с предзаполнением"
    for slug in bp.CTA:
        href = bp.cta_href(slug)
        assert href.startswith("/?")
        out = prefill("?" + urlsplit(href).query)
        assert out["size"] is not None and out["lines"], slug
        html = (WEB / "frontend" / slug / "index.html").read_text(encoding="utf-8")
        assert f'href="{href}"' in html
    assert not re.search(r"\s", bp.cta_href("baner-arenda"))


def _build_pages():
    spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)
    return bp


def test_example_library_is_consistent():
    bp = _build_pages()
    ex = bp.examples()
    assert len(ex) >= 30
    assert len({e["id"] for e in ex}) == len(ex)
    for e in ex:
        assert 1 <= len(e["lines"]) <= TEMPLATES["max_lines"], e["id"]
        assert all(0 < len(line) <= 120 for line in e["lines"]), e["id"]
        assert e["key"] in OPTS["sizeKeys"], e["id"]
        assert e["bg"] in OPTS["colorNames"] and e["color"] in OPTS["colorNames"] and e["bg"] != e["color"]
        assert e["cap"] >= bp.TARGET_CAP_MM, f'{e["id"]}: текст не помещается даже в самый большой размер'


@needs_node
def test_every_example_link_prefills_exactly():
    bp = _build_pages()
    for e in bp.examples():
        out = prefill("?" + urlsplit(e["href"]).query)
        assert out["size"] == {"key": e["key"]}, e["id"]
        assert out["lines"] == e["lines"], e["id"]
        assert (out["bg"], out["color"]) == (e["bg"], e["color"]), e["id"]


def test_metrika_goals_on_generated_pages():
    bp = _build_pages()
    html = (WEB / "frontend" / "chto-napisat-na-bannere" / "index.html").read_text(encoding="utf-8")
    assert "ym(108388194,\"init\"" in html
    assert html.count("data-ex=") == len(bp.examples())
    assert "example_click" in html and "cta_click" in html
    assert "webvisor" not in html
