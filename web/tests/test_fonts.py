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

# имя шрифта в UI → (семейство в @font-face, файл woff2 в frontend/fonts)
SELF_HOSTED = {
    "Golos Text": ("Golos Text", "GolosText-Bold.woff2"),
    "Fira Sans Cond": ("Fira Sans Condensed", "FiraSansCondensed-ExtraBold.woff2"),
    "PT Sans Narrow": ("PT Sans Narrow", "PTSansNarrow-Bold.woff2"),
    "Tenor Sans": ("Tenor Sans", "TenorSans-Regular.woff2"),
    "Caveat": ("Caveat", "Caveat-Bold.woff2"),
}
CSS = (WEB / "frontend" / "style.css").read_text(encoding="utf-8")


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
def test_font_is_self_hosted_with_the_same_weight(name):
    family, woff = SELF_HOSTED[name]
    face = re.search(rf"@font-face \{{ font-family: '{re.escape(family)}'; src: url\('/fonts/{re.escape(woff)}'\)[^}}]*font-weight: (\d+);", CSS)
    assert face, name
    assert int(face.group(1)) == expected_weight(Path(FONTS[name]).name)
    assert (WEB / "frontend" / "fonts" / woff).is_file()


def test_every_font_has_a_license_text_next_to_it():
    texts = {p.name for p in (WEB / "frontend" / "fonts").glob("OFL-*.txt")}
    assert {"OFL-GolosText.txt", "OFL-FiraSansCondensed.txt", "OFL-PTSansNarrow.txt", "OFL-TenorSans.txt",
            "OFL-Caveat.txt", "OFL-Unbounded.txt", "OFL-Onest.txt"} <= texts
