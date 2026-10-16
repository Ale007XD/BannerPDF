"""Брендированная страница типографии: partner.js, проводка в index.html / app.js / nginx, политика данных.

Чистые функции partner.js проверяет Node (в CI он есть; без Node тесты пропускаются).
Поведение в браузере (брендинг, 404, таймаут, XSS в названии) проверяется отдельно в jsdom, в репо его нет.
"""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1]
FRONT = WEB / "frontend"
PARTNER_JS = FRONT / "partner.js"
JS = PARTNER_JS.read_text(encoding="utf-8")
INDEX = (FRONT / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONT / "app.js").read_text(encoding="utf-8")
CSS = (FRONT / "style.css").read_text(encoding="utf-8")
NGINX = (WEB / "nginx" / "default.conf").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node не установлен")


def call(fn: str, *args):
    script = "const P = require(process.argv[1]); console.log(JSON.stringify(P[process.argv[2]](...JSON.parse(process.argv[3]))));"
    out = subprocess.run(["node", "-e", script, str(PARTNER_JS), fn, json.dumps(list(args))],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@needs_node
class TestPureFunctions:
    @pytest.mark.parametrize("search,expected", [
        ("?partner=reklamacity", "reklamacity"),
        ("?partner=ReklamaCity&size=2x1", "reklamacity"),
        ("?size=2x1&partner=%20print-1%20", "print-1"),
        ("", None),
        ("?size=2x1", None),
        ("?partner=", None),
        ("?partner=a", None),
        ("?partner=-ab", None),
        ("?partner=ab_cd", None),
        ("?partner=%3Cscript%3E", None),
        ("?partner=..%2Fetc", None),
        ("?partner=" + "x" * 33, None),
        ("?partner=реклама", None),
    ])
    def test_extract_code(self, search, expected):
        assert call("extractCode", search) == expected

    @pytest.mark.parametrize("search,expected", [
        ("?partner=reklamacity", ""),
        ("?partner=reklamacity&size=2x1", "?size=2x1"),
        ("?size=2x1&partner=reklamacity&text1=%D0%9F", "?size=2x1&text1=%D0%9F"),
        ("?size=2x1", "?size=2x1"),
        ("", ""),
    ])
    def test_strip_param_keeps_prefill(self, search, expected):
        assert call("stripParam", search) == expected

    def test_short_order_id_matches_the_email(self):
        # то же правило, что в письме и имени вложения: order_id[:8]
        assert call("shortOrderId", "abcdef12-3456-7890-abcd-ef1234567890") == "abcdef12"
        assert call("shortOrderId", None) == ""
        assert call("shortOrderId", "abc") == "abc"

    def test_code_regex_matches_the_server(self):
        from web.api.services.partner_store import CODE_RE
        m = re.search(r"var CODE_RE = (/.*/);", JS)
        assert m and m.group(1) == "/" + CODE_RE.pattern + "/"

    def test_accent_regex_matches_the_server(self):
        from web.api.services.partner_store import ACCENT_RE
        m = re.search(r"var ACCENT_RE = (/.*/);", JS)
        assert m and m.group(1) == "/" + ACCENT_RE.pattern + "/"


class TestSafety:
    def test_no_html_injection_primitives(self):
        # название и логотип приходят с сервера: только textContent и атрибуты
        code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
        for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert word not in code, word

    def test_storage_is_only_the_tab_session_code(self):
        code = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
        assert "document.cookie" not in code and "localStorage" not in code and "indexedDB" not in code
        assert set(re.findall(r'sessionStorage\.\w+\(([A-Z_]+)', code)) == {"STORAGE_KEY"}
        assert 'STORAGE_KEY = "bb_partner"' in code

    def test_order_is_attributed_only_after_branding_is_shown(self):
        # без брендинга клиент не видел уведомление о передаче макета: партнёр в заказ не попадает
        assert "return api.code && api.brand ? api.code : undefined;" in JS
        assert re.search(r"\.catch\(function \(\) \{[^}]*api\.code = null;[^}]*api\.brand = null;", JS, re.S)

    def test_logo_failure_falls_back_to_name(self):
        assert 'addEventListener("error", showName)' in JS

    def test_request_has_a_timeout(self):
        assert "AbortController" in JS and "FETCH_TIMEOUT_MS" in JS


class TestWiring:
    def test_partner_js_loaded_blocking_in_head_before_the_page(self):
        head = INDEX[: INDEX.index("</head>")]
        assert '<script src="partner.js?v=1"></script>' in head  # блокирующий: логотип BannerBot не мигает
        assert "defer" not in re.search(r'<script src="partner\.js[^>]*>', head).group(0)

    def test_dom_hooks_exist(self):
        for hook in ('id="site-logo"', 'id="partner-strip"', 'id="confirm-partner"', 'id="success-text"'):
            assert hook in INDEX, hook
        # плашка продавца и уведомление скрыты, пока партнёра нет
        assert re.search(r'class="partner-strip hidden" id="partner-strip"', INDEX)
        assert re.search(r'class="confirm-partner hidden" id="confirm-partner"', INDEX)

    def test_notice_sits_between_the_layout_and_the_consent_checkbox(self):
        confirm = INDEX[INDEX.index('id="modal-confirm"'):]
        assert confirm.index('id="confirm-lines"') < confirm.index('id="confirm-partner"') < confirm.index('id="confirm-check"')

    def test_partner_code_is_read_when_pay_is_clicked_not_when_config_is_built(self):
        # пока уведомление не показано, заказ не помечается партнёром; превью код партнёра не нужен
        build = APP_JS[APP_JS.index("function buildConfig()"):]
        build = build[: build.index("\n}\n")]
        assert "partner_code" not in build
        pay = APP_JS[APP_JS.index('el.confirmPay.addEventListener("click"'):]
        pay = pay[: pay.index("\n});")]
        assert "partner_code: window.Partner ? window.Partner.orderCode() : undefined" in pay

    def test_success_modal_is_decorated(self):
        body = APP_JS[APP_JS.index("async function downloadPdf("):]
        body = body[: body.index("\n}\n")]
        assert "window.Partner.decorateSuccess(state.orderId)" in body
        assert body.index("decorateSuccess") < body.index("showModal(el.modalSuccess)")

    def test_css_rules_exist(self):
        for sel in ("html.partner-pending .site-header .logo", ".logo--partner", ".partner-strip", ".confirm-partner"):
            assert sel in CSS, sel

    def test_logo_is_scaled_to_a_fixed_height(self):
        # max-height не увеличивает SVG с маленьким родным размером (CorelDRAW задаёт мм): логотип 49x14 px нечитаем
        rule = CSS[CSS.index(".logo--partner img {"):]
        rule = rule[: rule.index("}")]
        assert re.search(r"(?<![-\w])height:\s*28px", rule), rule
        assert "max-height" not in re.sub(r"/\*.*?\*/", "", rule, flags=re.S), rule
        assert "object-fit: contain" in rule  # при max-width логотип не растягивается

    def test_partner_svg_logos_are_utf8_and_inert(self):
        # CorelDRAW сохраняет SVG в UTF-16: Chromium такой файл в <img> читает, остальные браузеры не обязаны
        import xml.etree.ElementTree as ET
        svgs = sorted((FRONT / "partners").glob("*.svg"))
        assert svgs, "нет ни одного SVG-логотипа"
        for path in svgs:
            raw = path.read_bytes()
            assert not raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")), f"{path.name}: BOM, нужен UTF-8 без BOM"
            text = raw.decode("utf-8")
            assert re.search(r'<\?xml[^>]*encoding="UTF-8"', text), f"{path.name}: в объявлении XML нет encoding=UTF-8"
            ET.fromstring(raw)  # well-formed
            low = text.lower()
            for bad in ("<script", "<foreignobject", "<image", "javascript:", "href=", "data:"):
                assert bad not in low, f"{path.name}: {bad}"
            assert not re.search(r"\son\w+\s*=", low), f"{path.name}: обработчик on*"

    def test_nginx_caches_partner_js_like_other_static(self):
        assert re.search(r"location ~ \^/\([^)]*partner\\\.js[^)]*\)\$ \{", NGINX)

    def test_logo_directory_is_served_from_our_own_host(self):
        assert (FRONT / "partners").is_dir()


class TestPrivacyDisclosure:
    """Типография-партнёр получает текст макета: политика данных и окно «Конфиденциальность» это называют."""

    def _policy(self) -> str:
        return (FRONT / "privacy" / "index.html").read_text(encoding="utf-8")

    def test_policy_names_the_partner_as_a_recipient(self):
        text = self._policy()
        assert "Типография-партнёр" in text and "sessionStorage" in text
        assert "IP-адрес, User-Agent и платёжные данные не передаются" in text

    def test_policy_page_is_regenerated_from_the_generator(self):
        spec = importlib.util.spec_from_file_location("build_pages", WEB / "seo" / "build_pages.py")
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        page = next(p for p in bp.PAGES if p["slug"] == "privacy")
        assert bp.render(page) == self._policy(), "privacy/index.html устарел: python web/seo/build_pages.py"

    def test_privacy_modal_mentions_the_transfer(self):
        modal = INDEX[INDEX.index('id="modal-privacy"'):]
        modal = modal[: modal.index("privacy-close")]
        assert "типографии-партнёра" in modal and "не передаются" in modal

    def test_mail_content_matches_what_the_policy_promises(self):
        # в письмо идут номер заказа, размер и текст; ничего про IP, User-Agent, платёж
        src = (WEB / "api" / "services" / "partner_delivery.py").read_text(encoding="utf-8")
        for word in ("accepted_ip", "accepted_ua", "yookassa", "payment", "user-agent"):
            assert word not in src.lower(), word
