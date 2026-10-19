"""
test_admin_pages.py
~~~~~~~~~~~~~~~~~~~
Админские страницы: навигация между /admin/, /admin/stats/ и /admin/partners/ и страница статистики партнёров.

Поведение страницы (периоды, фильтр, CSV, вёрстка на телефоне) проверено в реальном Chromium на настоящем приложении;
в репозитории остаются проверки разметки и проводки, которые ломаются при правке по невнимательности.
"""

import re
from pathlib import Path

import pytest

ADMIN = Path(__file__).resolve().parents[1] / "frontend" / "admin"
PAGES = {
    "orders": ADMIN / "index.html",
    "stats": ADMIN / "stats" / "index.html",
    "partners": ADMIN / "partners" / "index.html",
}
HTML = {name: path.read_text(encoding="utf-8") for name, path in PAGES.items()}
NAV = [("/admin/", "ЗАКАЗЫ"), ("/admin/stats/", "СТАТИСТИКА"), ("/admin/partners/", "ПАРТНЁРЫ")]


def _nav(html: str) -> list[tuple[str, str, bool]]:
    block = re.search(r'<nav class="header-nav"[^>]*>(.*?)</nav>', html, re.S).group(1)
    return [(m.group(1), m.group(3), "aria-current" in m.group(2))
            for m in re.finditer(r'<a class="nav-link" href="([^"]+)"([^>]*)>([^<]+)</a>', block)]


class TestNavigation:

    @pytest.mark.parametrize("page", list(PAGES))
    def test_every_admin_page_links_to_the_other_two(self, page):
        links = _nav(HTML[page])
        assert [(href, text) for href, text, _ in links] == NAV

    @pytest.mark.parametrize("page,current", [("orders", "/admin/"), ("stats", "/admin/stats/"), ("partners", "/admin/partners/")])
    def test_exactly_the_current_page_is_marked(self, page, current):
        marked = [href for href, _, is_current in _nav(HTML[page]) if is_current]
        assert marked == [current]

    def test_main_admin_page_has_the_stats_link(self):
        # раньше на /admin/stats/ приходилось ходить вручную: ссылки с главной страницы не было
        assert 'href="/admin/stats/"' in HTML["orders"] and 'href="/admin/partners/"' in HTML["orders"]

    def test_nav_sits_inside_the_sticky_header(self):
        for page, html in HTML.items():
            header = html[html.index('<header class="site-header">'):html.index("</header>")]
            assert 'class="header-nav"' in header, page

    @pytest.mark.parametrize("page", list(PAGES))
    def test_nav_styles_exist(self, page):
        for sel in (".header-nav", ".nav-link", '.nav-link[aria-current="page"]'):
            assert sel in HTML[page], (page, sel)

    def test_stats_page_no_longer_needs_the_back_link_badge(self):
        assert "← ЗАКАЗЫ" not in HTML["stats"]


class TestPartnersPage:
    html = HTML["partners"]

    def _script(self) -> str:
        return self.html[self.html.index("<script>"):self.html.rindex("</script>")]

    def test_not_indexable_and_titled(self):
        assert '<meta name="robots" content="noindex, nofollow">' in self.html
        assert "<title>BannerBot — Партнёры</title>" in self.html

    def test_uses_the_same_token_storage_as_the_admin(self):
        key = re.search(r'const TOKEN_KEY\s*=\s*"([^"]+)"', HTML["orders"]).group(1)
        assert f'const TOKEN_KEY = "{key}";' in self.html
        assert "Authorization" in self._script() and "Bearer ${_token}" in self._script()

    def test_period_presets_match_the_api(self):
        from web.api.services.partner_stats import PERIODS
        buttons = re.findall(r'data-period="(\w+)"', self.html)
        assert set(buttons) == set(PERIODS) | {"custom"}
        assert 'data-period="month"' in re.search(r'<button[^>]*class="filter-btn active"[^>]*>', self.html).group(0)
        assert 'id="date-from"' in self.html and 'id="date-to"' in self.html and 'type="date"' in self.html

    def test_calls_the_stats_and_csv_endpoints_with_the_applied_filter(self):
        script = self._script()
        assert "/admin/partners/stats?${_applied}" in script
        assert "/admin/partners/stats.csv?kind=${kind}&${_applied}" in script
        # CSV выгружает ровно то, что на экране: параметры последнего запроса, а не текущее состояние полей
        assert "_applied = params.toString();" in script

    def test_csv_kinds_match_the_api(self):
        from web.api.services.partner_stats import CSV_KINDS
        assert set(re.findall(r'downloadCsv\("(\w+)"\)', self.html)) == set(CSV_KINDS)

    def test_csv_download_goes_through_fetch_so_the_token_is_not_in_the_url(self):
        script = self._script()
        assert "token=" not in script and "?token" not in script
        assert "URL.createObjectURL(await resp.blob())" in script and "a.download" in script

    def test_custom_period_waits_for_the_show_button_and_requires_both_dates(self):
        script = self._script()
        assert 'if (_period !== "custom") load();' in script
        assert 'Укажите обе даты' in script

    def test_dynamic_text_is_always_escaped(self):
        script = self._script()
        # название и код партнёра задаёт админ, но в разметку они попадают только через escapeHtml;
        # единственное исключение: сравнение кода с выбранным значением фильтра
        found = 0
        for m in re.finditer(r"\b(?:r|d|p)\.(?:name|code)\b", script):
            found += 1
            assert script[: m.start()].endswith("escapeHtml(") or script[m.end(): m.end() + 10].startswith(" === keep"), \
                script[m.start() - 40: m.end() + 10]
        assert found >= 6
        assert "document.write" not in script and "eval(" not in script

    def test_stale_responses_are_ignored(self):
        script = self._script()
        assert "const id = ++_reqId;" in script
        assert script.count("if (id !== _reqId) return;") == 3   # после fetch, после разбора JSON и в catch

    def test_expired_token_sends_the_user_to_the_login_page(self):
        script = self._script()
        assert script.count("resp.status === 401 || resp.status === 403") == 2   # и в статистике, и в CSV
        assert 'href="/admin/"' in self.html

    def test_phone_layout_stacks_the_partner_table(self):
        assert 'class="stack"' in self.html and "table.stack td::before" in self.html
        assert self.html.count('data-label="') >= 5
        assert "max-width: 520px" in self.html


def test_admin_pages_are_served_from_the_noindex_location():
    nginx = (Path(__file__).resolve().parents[1] / "nginx" / "default.conf").read_text(encoding="utf-8")
    block = nginx[nginx.index("location /admin/ {"):]
    block = block[: block.index("\n    }")]
    assert "X-Robots-Tag" in block and "try_files $uri $uri/ /admin/index.html" in block
