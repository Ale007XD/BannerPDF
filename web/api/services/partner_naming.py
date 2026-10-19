"""
partner_naming.py
~~~~~~~~~~~~~~~~~
Номер заказа, который клиент называет типографии, и имя скачиваемого файла партнёрского заказа.

Клиент анонимен: ни имени, ни телефона, ни почты мы не просим. Связь с типографией держится на
двух вещах, которые и так есть: номер заказа и публичный телефон самой типографии. Оба видны
в имени файла («Заказ 482-915, скажите его в Реклама-Сити по номеру 90-20-55.pdf»), в окне
после оплаты и в письме типографии, поэтому новых персональных данных клиента не появляется.

Номер выводится из order_id (sha256), а не хранится: схема БД не меняется, а номер в файле, письме
и окне совпадает по построению. Номер не секрет и ничего не открывает: доступ к макету даёт
только одноразовый download-токен.
"""

import hashlib
import re
import unicodedata
from urllib.parse import quote

NUMBER_DIGITS = 6

# Символы, недопустимые в именах файлов Windows/macOS/Linux, плюс управляющие
_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')
# Категории Unicode: управляющие, форматирующие (в том числе RTL-override, которым прячут расширение),
# суррогаты, частное использование, не назначенные
_DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn"}

_MAX_NAME_LEN  = 40
_MAX_PHONE_LEN = 30
_FALLBACK_NAME = "типографии"


def order_number(order_id: str) -> str:
    """Читаемый номер заказа вида «482-915»: 6 цифр из sha256(order_id), стабильно для одного заказа."""
    digest = hashlib.sha256(str(order_id).encode("utf-8")).hexdigest()
    n = int(digest[:8], 16) % 10 ** NUMBER_DIGITS
    s = f"{n:0{NUMBER_DIGITS}d}"
    return f"{s[:3]}-{s[3:]}"


def _clean(value: str | None, limit: int) -> str:
    """Безопасный для имени файла фрагмент: без запрещённых и невидимых символов, один пробел, не длиннее limit."""
    text = "".join(" " if ch.isspace() else ch for ch in str(value or "") if unicodedata.category(ch) not in _DROP_CATEGORIES)
    text = _FORBIDDEN.sub(" ", text)
    text = " ".join(text.split())
    return text[:limit].strip(" .")


def client_filename(partner_name: str, phone: str | None, order_id: str) -> str:
    """
    «Заказ 482-915, скажите его в Реклама-Сити по номеру 90-20-55.pdf».
    Без телефона: «Заказ 482-915, скажите его в Реклама-Сити.pdf».
    """
    name = _clean(partner_name, _MAX_NAME_LEN) or _FALLBACK_NAME
    tel = _clean(phone, _MAX_PHONE_LEN)
    text = f"Заказ {order_number(order_id)}, скажите его в {name}"
    if tel:
        text += f" по номеру {tel}"
    return f"{text}.pdf"


def ascii_fallback(order_id: str) -> str:
    """Имя для клиентов без поддержки RFC 5987 (filename*): «order_482-915.pdf»."""
    return f"order_{order_number(order_id)}.pdf"


def content_disposition(filename: str, fallback: str) -> str:
    """
    attachment с именем в UTF-8 по RFC 5987/6266: заголовок остаётся ASCII (так его отдаёт и Starlette),
    современные браузеры берут filename*, остальные, как и раньше, обычный filename.
    """
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def mail_attachment_name(order_id: str, size_label: str) -> str:
    """Имя вложения для самой типографии: номер заказа и размер, без её же названия и телефона."""
    return f"Заказ {order_number(order_id)}, баннер {_clean(size_label, 30)}.pdf"
