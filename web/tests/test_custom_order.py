"""Страница заказа макета с картинкой: каналы связи, цели Метрики, ссылки из конструктора."""
import importlib.util
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)
PAGE = (WEB / "frontend" / "zakaz-banner-s-kartinkoy" / "index.html").read_text(encoding="utf-8")
INDEX = (WEB / "frontend" / "index.html").read_text(encoding="utf-8")


def test_contact_links_present_with_goal_attributes():
    assert set(bp.CONTACTS) == {"telegram", "max", "avito"}
    for key, (_name, url) in bp.CONTACTS.items():
        assert url.startswith("https://")
        assert f'data-contact="{key}" href="{url}" target="_blank" rel="noopener"' in PAGE, key


def test_goal_handler_covers_contacts():
    assert "custom_contact_click" in PAGE and "a[data-contact]" in PAGE


def test_page_states_price_and_collects_no_contacts():
    assert "999" in PAGE and "1 499" in PAGE
    assert "<form" not in PAGE and "<input" not in PAGE


def test_constructor_and_birthday_page_link_to_order_page():
    assert 'href="/zakaz-banner-s-kartinkoy/"' in INDEX
    assert "custom_link_click" in INDEX
    bday = (WEB / "frontend" / "baner-na-den-rozhdeniya" / "index.html").read_text(encoding="utf-8")
    assert bday.count("/zakaz-banner-s-kartinkoy/") >= 2
