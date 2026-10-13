"""
test_router_wiring.py
~~~~~~~~~~~~~~~~~~~~~
Каждый модуль в api/routers подключён в main.py или явно назван запасным.

Раньше рядом с рабочим order.py лежал order_router.py: устаревшая копия без abandon, amend
и промокодов, которую никто не подключал и не импортировал. Правки легко попадали не в тот
файл, а тест на описание платежа проверял обе копии. Теперь новый «лишний» роутер не пройдёт.
"""

import re
from pathlib import Path

API = Path(__file__).parent.parent / "api"
MAIN = (API / "main.py").read_text(encoding="utf-8")

# Запасные модули: лежат в репозитории намеренно и в main.py не подключены. Сейчас таких нет:
# запасной провайдер Selfwork удалён (его роутер не импортировался, а фронт умел только виджет ЮKassa).
RESERVE: set[str] = set()


def _router_modules() -> list[str]:
    return sorted(p.stem for p in (API / "routers").glob("*.py") if p.stem != "__init__")


def _is_wired(module: str) -> bool:
    if re.search(rf"app\.include_router\(\s*{module}\.router\b", MAIN):
        return True
    alias = re.search(rf"from \.routers\.{module} import router as (\w+)", MAIN)
    return bool(alias and re.search(rf"app\.include_router\(\s*{alias.group(1)}\b", MAIN))


def test_every_router_module_is_wired_or_declared_reserve():
    loose = [m for m in _router_modules() if not _is_wired(m) and m not in RESERVE]
    assert not loose, (
        f"роутеры не подключены в main.py: {loose}. Подключите, удалите или, если это запасной "
        "модуль, добавьте в RESERVE с пояснением"
    )


def test_reserve_list_has_no_stale_entries():
    assert RESERVE <= set(_router_modules()), "в RESERVE назван несуществующий модуль"
    assert not [m for m in RESERVE if _is_wired(m)], "модуль из RESERVE уже подключён: уберите его из списка"


def test_wiring_detector_sees_every_active_router():
    # защита самого теста: детектор не должен считать «неподключённым» живой роутер
    for module in ("admin", "batch", "corp_api", "download", "order", "payment", "preview", "promo", "push",
                   "referral", "tg_webhook"):
        assert _is_wired(module), module


def test_duplicate_order_router_is_gone():
    assert not (API / "routers" / "order_router.py").exists()
