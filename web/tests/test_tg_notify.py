"""
test_tg_notify.py
~~~~~~~~~~~~~~~~~
Исходящие запросы к Telegram: адрес Bot API и прокси настраиваются, а недоступный Telegram
не держит запрос дольше нескольких секунд и не молчит в логе.

С некоторых хостингов api.telegram.org недоступен: connect уходит в таймаут, и раньше
такой сбой в логе выглядел пустой строкой.
"""

import logging

import httpx
import pytest

from web.api.services import tg_notify


class _FakeClient:
    """Подмена httpx.AsyncClient: запоминает аргументы и адрес, сети не касается."""

    instances: list["_FakeClient"] = []
    error: Exception | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.urls: list[str] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.urls.append(url)
        if _FakeClient.error:
            raise _FakeClient.error
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


@pytest.fixture
def tg(monkeypatch):
    _FakeClient.instances = []
    _FakeClient.error = None
    monkeypatch.setattr(tg_notify.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(tg_notify, "TG_NOTIFY_TOKEN", "123:abc")
    monkeypatch.setattr(tg_notify, "TG_ADMIN_CHAT_ID", "42")
    monkeypatch.setattr(tg_notify, "TG_API_BASE", "https://api.telegram.org")
    monkeypatch.setattr(tg_notify, "TG_PROXY_URL", None)
    return _FakeClient


class TestEndpoint:

    @pytest.mark.asyncio
    async def test_default_api_base(self, tg):
        await tg_notify._tg_post("sendMessage", {})
        assert tg.instances[0].urls == ["https://api.telegram.org/bot123:abc/sendMessage"]

    @pytest.mark.asyncio
    async def test_custom_relay_base(self, tg, monkeypatch):
        monkeypatch.setattr(tg_notify, "TG_API_BASE", "https://relay.example/tg")
        await tg_notify._tg_post("sendMessage", {})
        assert tg.instances[0].urls == ["https://relay.example/tg/bot123:abc/sendMessage"]

    @pytest.mark.asyncio
    async def test_proxy_is_passed_only_when_configured(self, tg, monkeypatch):
        await tg_notify._tg_post("sendMessage", {})
        assert tg.instances[0].kwargs["proxy"] is None

        monkeypatch.setattr(tg_notify, "TG_PROXY_URL", "http://proxy.example:3128")
        await tg_notify._tg_post("sendMessage", {})
        assert tg.instances[1].kwargs["proxy"] == "http://proxy.example:3128"

    def test_trailing_slash_in_base_is_ignored(self, monkeypatch):
        monkeypatch.setenv("TG_API_BASE", "https://relay.example/tg/")
        import importlib
        reloaded = importlib.reload(tg_notify)
        try:
            assert reloaded.TG_API_BASE == "https://relay.example/tg"
        finally:
            monkeypatch.delenv("TG_API_BASE")
            importlib.reload(tg_notify)

    @pytest.mark.asyncio
    async def test_disabled_without_token_makes_no_request(self, tg, monkeypatch):
        monkeypatch.setattr(tg_notify, "TG_NOTIFY_TOKEN", "")
        assert await tg_notify._tg_post("sendMessage", {}) is None
        assert tg.instances == []


class TestUnreachableTelegram:

    @pytest.mark.asyncio
    async def test_connect_timeout_is_short(self, tg):
        await tg_notify._tg_post("sendMessage", {})
        timeout = tg.instances[0].kwargs["timeout"]
        assert timeout.connect <= 5
        assert timeout.read >= 5  # ответ уже полученного запроса ждём спокойно

    @pytest.mark.asyncio
    async def test_failure_returns_none_and_names_the_error(self, tg, caplog):
        tg.error = httpx.ConnectTimeout("")  # у таких исключений текст пустой
        with caplog.at_level(logging.ERROR, logger=tg_notify.logger.name):
            assert await tg_notify._tg_post("sendMessage", {}) is None
        assert "ConnectTimeout" in caplog.text
        assert "123:abc" not in caplog.text  # токен в лог не попадает
