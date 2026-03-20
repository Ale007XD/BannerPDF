"""
test_hmac.py
~~~~~~~~~~~~
Тесты HMAC-SHA256 верификации X-Tona-Signature.

Покрывает:
  - Корректная подпись → True
  - Неверная подпись → False
  - Пустое тело → работает
  - Другой секрет → False
  - Не задан TONA_WEBHOOK_SECRET → False
  - Timing-safe сравнение (обход через case-insensitive)
"""

import hashlib
import hmac
import importlib

import web.api.services.payment as pay_module_ref


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifyTonaSignature:

    def test_valid_signature(self, set_env):
        """Корректная подпись → True."""
        from web.api.services.payment import verify_tona_signature
        body = b'{"order_id":"test-123","status":"paid"}'
        sig = _sign(body, "test_secret_key")
        assert verify_tona_signature(body, sig) is True

    def test_invalid_signature(self, set_env):
        """Неверная подпись → False."""
        from web.api.services.payment import verify_tona_signature
        body = b'{"order_id":"test-123","status":"paid"}'
        assert verify_tona_signature(body, "deadbeef" * 8) is False

    def test_empty_body(self, set_env):
        """Пустое тело → валидная подпись работает."""
        from web.api.services.payment import verify_tona_signature
        body = b""
        sig = _sign(body, "test_secret_key")
        assert verify_tona_signature(body, sig) is True

    def test_wrong_secret(self, set_env):
        """Подпись другим секретом → False."""
        from web.api.services.payment import verify_tona_signature
        body = b'{"order_id":"x"}'
        sig = _sign(body, "wrong_secret")
        assert verify_tona_signature(body, sig) is False

    def test_missing_secret_returns_false(self, monkeypatch):
        """Не задан TONA_WEBHOOK_SECRET → False (нельзя верифицировать)."""
        monkeypatch.setenv("TONA_WEBHOOK_SECRET", "")
        # Перезагружаем модуль, чтобы подхватить пустой env
        importlib.reload(pay_module_ref)
        assert pay_module_ref.verify_tona_signature(b"body", "anysig") is False

    def test_uppercase_signature_accepted(self, set_env):
        """Подпись в верхнем регистре — должна приниматься (lower() применяется)."""
        from web.api.services.payment import verify_tona_signature
        body = b'{"order_id":"test-456"}'
        sig = _sign(body, "test_secret_key").upper()
        # verify делает .lower() на входе → должно совпасть
        assert verify_tona_signature(body, sig) is True

    def test_modified_body_rejected(self, set_env):
        """Изменённое тело при той же подписи → False."""
        from web.api.services.payment import verify_tona_signature
        original = b'{"order_id":"test-789","status":"paid"}'
        sig = _sign(original, "test_secret_key")
        tampered = b'{"order_id":"test-789","status":"paid","amount":9999}'
        assert verify_tona_signature(tampered, sig) is False
