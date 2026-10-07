"""
test_site_defaults.py
~~~~~~~~~~~~~~~~~~~~~
Адрес сайта по умолчанию один и тот же во всех модулях.

Раньше он расходился: bannerprintbot.ru, bannerbot.ru:8444 (порт не публикуется)
и «голый» хост. Если на проде .env что-то не переопределит, пользователь после
оплаты уходил бы на чужой домен или порт. Проверяется исходник, а не значение
из окружения: в тестах оно задано через conftest.
"""

import re
from pathlib import Path

from web.api.services import banner_generator as bg

API = Path(__file__).resolve().parents[1] / "api"
CANONICAL = "https://bannerbot.ru"


def _defaults(rel: str, var: str) -> list[str]:
    src = (API / rel).read_text(encoding="utf-8")
    return re.findall(rf'os\.getenv\("{var}",\s*"([^"]*)"\)', src)


class TestSiteBaseUrlDefaults:
    def test_url_modules_share_one_default(self):
        for rel in ("services/payment.py", "services/payment_selfwork_service.py", "services/tg_notify.py"):
            assert _defaults(rel, "SITE_BASE_URL") == [CANONICAL], rel

    def test_watermark_module_default_resolves_to_the_same_host(self):
        found = _defaults("services/banner_generator.py", "SITE_BASE_URL")
        assert len(found) == 1
        assert bg._site_host(found[0]) == bg._site_host(CANONICAL) == "bannerbot.ru"

    def test_cors_default_is_the_site(self):
        assert _defaults("main.py", "ALLOWED_ORIGINS") == [CANONICAL]

    def test_no_unpublished_port_left_in_code(self):
        for path in API.rglob("*.py"):
            assert "8444" not in path.read_text(encoding="utf-8"), path
