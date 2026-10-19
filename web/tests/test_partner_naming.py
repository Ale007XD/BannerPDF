"""
test_partner_naming.py
~~~~~~~~~~~~~~~~~~~~~~
Номер заказа и имя скачиваемого файла партнёрского заказа (services/partner_naming.py).
Чистые функции, без БД.
"""

import re
import uuid
from urllib.parse import unquote

import pytest

from web.api.services import partner_naming as pn


class TestOrderNumber:
    def test_format_is_three_dash_three_digits(self):
        for _ in range(200):
            assert re.fullmatch(r"\d{3}-\d{3}", pn.order_number(str(uuid.uuid4())))

    def test_is_stable_for_one_order_and_differs_between_orders(self):
        order = str(uuid.uuid4())
        assert pn.order_number(order) == pn.order_number(order)
        assert pn.order_number("a") != pn.order_number("b")

    def test_known_value_pins_the_derivation(self):
        # изменение алгоритма разорвало бы связь «номер в уже скачанном файле = номер в письме»
        assert pn.order_number("abcdef12-0000") == "353-095"

    def test_leading_zeros_are_kept(self):
        # перебор до номера с ведущим нулём: «048-291», а не «48-291»
        for i in range(5000):
            number = pn.order_number(f"order-{i}")
            if number.startswith("0"):
                assert re.fullmatch(r"0\d\d-\d{3}", number)
                return
        pytest.fail("не нашли номер с ведущим нулём")

    def test_spread_is_even_enough_to_be_unique_in_practice(self):
        # 300 заказов типографии в месяц: коллизии возможны (парадокс дней рождения), но редки
        numbers = [pn.order_number(str(uuid.uuid4())) for _ in range(300)]
        assert len(set(numbers)) >= 295


class TestClientFilename:
    def test_template(self):
        assert pn.client_filename("Реклама-Сити", "90-20-55", "x") == \
            f"Заказ {pn.order_number('x')}, скажите его в Реклама-Сити по номеру 90-20-55.pdf"

    def test_without_phone(self):
        assert pn.client_filename("Реклама-Сити", None, "x") == \
            f"Заказ {pn.order_number('x')}, скажите его в Реклама-Сити.pdf"
        assert pn.client_filename("Реклама-Сити", "  ", "x").endswith("Реклама-Сити.pdf")

    @pytest.mark.parametrize("name", ['A/B', 'A\\B', 'A:B', 'A*B', 'A?B', 'A"B', 'A<B>', 'A|B'])
    def test_forbidden_characters_never_reach_the_file_name(self, name):
        result = pn.client_filename(name, None, "x")
        assert not any(ch in result for ch in '\\/:*?"<>|')

    def test_invisible_and_bidi_characters_are_dropped(self):
        result = pn.client_filename("Ре‮клама​\x00\x1f-Сити﻿", "90-20-55", "x")
        assert "Реклама-Сити" in result
        assert not any(unicodeish in result for unicodeish in ("‮", "​", "\x00", "\x1f", "﻿"))

    def test_whitespace_is_collapsed_and_edges_trimmed(self):
        assert "в Реклама Сити по" in pn.client_filename("  Реклама \t\n  Сити. ", "90-20-55", "x")
        assert "Сити по" in pn.client_filename("Сити. . ", "90-20-55", "x")

    def test_empty_name_falls_back(self):
        assert "скажите его в типографии" in pn.client_filename("/\\:", None, "x")
        assert "скажите его в типографии" in pn.client_filename("", None, "x")

    def test_worst_case_stays_far_below_the_255_byte_file_system_limit(self):
        name = "Ж" * 200
        result = pn.client_filename(name, "9" * 200, "x")
        assert len(result.encode("utf-8")) <= 200, len(result.encode("utf-8"))
        assert result.endswith(".pdf")

    def test_always_a_pdf_and_never_starts_with_a_dot(self):
        for name in ("..", "...", "."):
            result = pn.client_filename(name, None, "x")
            assert result.endswith(".pdf") and result.startswith("Заказ ")


class TestContentDisposition:
    def test_is_ascii_and_round_trips(self):
        filename = pn.client_filename("Реклама-Сити", "90-20-55", "x")
        header = pn.content_disposition(filename, pn.ascii_fallback("x"))
        header.encode("ascii")
        assert header.startswith('attachment; filename="order_')
        assert unquote(header.split("filename*=UTF-8''", 1)[1]) == filename

    def test_percent_encoding_leaves_no_separators(self):
        header = pn.content_disposition("a;b,c d\".pdf", "order_1.pdf")
        encoded = header.split("filename*=UTF-8''", 1)[1]
        assert re.fullmatch(r"[A-Za-z0-9._~%-]+", encoded), encoded


class TestMailAttachmentName:
    def test_number_and_size_only(self):
        assert pn.mail_attachment_name("x", "2×1 м") == f"Заказ {pn.order_number('x')}, баннер 2×1 м.pdf"

    def test_size_label_is_sanitized(self):
        name = pn.mail_attachment_name("x", 'a/b"c')
        assert not any(ch in name for ch in '/"')
