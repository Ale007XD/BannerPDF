"""Шрифты: имя файла, начертание внутри файла и образец на кнопке не расходятся.

Раньше GolosText-Regular.ttf на деле был Bold: баннеры выходили жирными, а образец
на кнопке (Google Fonts, вес 400) показывал обычное начертание.
"""
import re
from pathlib import Path

import pytest
from PIL import ImageFont

from web.api.services.config import FONTS

WEB = Path(__file__).resolve().parents[1]
APP_JS = (WEB / "frontend" / "app.js").read_text(encoding="utf-8")
INDEX = (WEB / "frontend" / "index.html").read_text(encoding="utf-8")

# имя шрифта в UI → фрагмент ссылки Google Fonts с тем же начертанием, что в файле
GOOGLE_FAMILY = {
    "Golos Text": "Golos+Text:wght@700",
    "Fira Sans Cond": "Fira+Sans+Condensed:wght@800",
    "PT Sans Narrow": "PT+Sans+Narrow:wght@700",
    "Tenor Sans": "Tenor+Sans&",
    "Caveat": "Caveat:wght@700",
}


def expected_weight(filename):
    if "ExtraBold" in filename:
        return 800
    if "Bold" in filename:
        return 700
    return 400


@pytest.mark.parametrize("name", list(FONTS))
def test_file_name_matches_style_inside(name):
    path = WEB / "fonts" / Path(FONTS[name]).name
    inner = " ".join(ImageFont.truetype(str(path), 20).getname())
    for token in ("ExtraBold", "Bold"):
        assert (token in path.name) == (token in inner), (path.name, inner)


@pytest.mark.parametrize("name", list(FONTS))
def test_ui_sample_weight_matches_file(name):
    block = APP_JS[APP_JS.index("const FONT_WEIGHTS"):]
    block = block[: block.index("};")]
    found = re.search(rf'"{re.escape(name)}":\s*(\d+)', block)
    assert found, name
    assert int(found.group(1)) == expected_weight(Path(FONTS[name]).name)


@pytest.mark.parametrize("name", list(FONTS))
def test_google_fonts_link_has_the_same_weight(name):
    assert GOOGLE_FAMILY[name] in INDEX, name
