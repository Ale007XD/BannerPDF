"""
test_hmac.py
~~~~~~~~~~~~
Тесты SHA256 верификации подписи selfwork webhook.

Selfwork использует SHA256(order_id + amount + api_key), а не HMAC.
Подпись передаётся в поле signature тела запроса (не в заголовке).

Покрывает:
  - Корректная подпись → True
  - Неверная подпись → False
  - Нулевой amount → работает
  - Другой секрет → False
  - Не задан SELFWORK_API_KEY → False
  - Регистронезависимость (upper() принимается через lower())
  - Изменённые данные при той же подписи → False
  - compute_init_signature — детерминированность
"""

import hashlib


def _sign_callback(order_id: str, amount_kopecks: int, secret: str) -> str:
    """Эталонная реализация подписи callback selfwork."""
    raw = str(order_id) + str(amount_kopecks) + secret
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TestVerifySelfworkCallback:

    def test_valid_signature(self, set_env):
        """Корректная подпись → True."""
        from web.api.services.payment import verify_selfwork_callback
        order_id = "test-order-123"
        amount   = 29900
        sig = _sign_callback(order_id, amount, "test_secret_key")
        assert verify_selfwork_callback(order_id, amount, sig) is True

    def test_invalid_signature(self, set_env):
        """Неверная подпись → False."""
        from web.api.services.payment import verify_selfwork_callback
        assert verify_selfwork_callback("order-x", 29900, "deadbeef" * 8) is False

    def test_zero_amount(self, set_env):
        """Нулевой amount — подпись всё равно верифицируется."""
        from web.api.services.payment import verify_selfwork_callback
        order_id = "order-zero"
        amount   = 0
        sig = _sign_callback(order_id, amount, "test_secret_key")
        assert verify_selfwork_callback(order_id, amount, sig) is True

    def test_wrong_secret(self, set_env):
        """Подпись другим секретом → False."""
        from web.api.services.payment import verify_selfwork_callback
        order_id = "order-y"
        amount   = 29900
        sig = _sign_callback(order_id, amount, "wrong_secret")
        assert verify_selfwork_callback(order_id, amount, sig) is False

    def test_missing_api_key_returns_false(self, monkeypatch):
        """Не задан SELFWORK_API_KEY → False (нельзя верифицировать)."""
        monkeypatch.setenv("SELFWORK_API_KEY", "")
        from web.api.services.payment import verify_selfwork_callback
        assert verify_selfwork_callback("order-z", 29900, "anysig") is False

    def test_uppercase_signature_accepted(self, set_env):
        """Подпись в верхнем регистре принимается (применяется .lower())."""
        from web.api.services.payment import verify_selfwork_callback
        order_id = "order-upper"
        amount   = 29900
        sig = _sign_callback(order_id, amount, "test_secret_key").upper()
        assert verify_selfwork_callback(order_id, amount, sig) is True

    def test_modified_order_id_rejected(self, set_env):
        """Изменённый order_id при той же подписи → False."""
        from web.api.services.payment import verify_selfwork_callback
        original_id = "order-original"
        amount = 29900
        sig = _sign_callback(original_id, amount, "test_secret_key")
        # Другой order_id — подпись не совпадёт
        assert verify_selfwork_callback("order-tampered", amount, sig) is False

    def test_modified_amount_rejected(self, set_env):
        """Изменённая сумма при той же подписи → False."""
        from web.api.services.payment import verify_selfwork_callback
        order_id = "order-amount"
        sig = _sign_callback(order_id, 29900, "test_secret_key")
        # Изменённая сумма — подпись не совпадёт
        assert verify_selfwork_callback(order_id, 99900, sig) is False


class TestComputeInitSignature:

    def test_deterministic(self, set_env):
        """Одинаковые входные данные → одинаковая подпись."""
        from web.api.services.payment import compute_init_signature
        sig1 = compute_init_signature("order-1", 29900, "Баннер", 1, 29900)
        sig2 = compute_init_signature("order-1", 29900, "Баннер", 1, 29900)
        assert sig1 == sig2

    def test_different_orders_different_signatures(self, set_env):
        """Разные order_id → разные подписи."""
        from web.api.services.payment import compute_init_signature
        sig1 = compute_init_signature("order-aaa", 29900, "Баннер", 1, 29900)
        sig2 = compute_init_signature("order-bbb", 29900, "Баннер", 1, 29900)
        assert sig1 != sig2

    def test_returns_hex_64_chars(self, set_env):
        """Подпись — hex-строка длиной 64 символа (SHA256)."""
        from web.api.services.payment import compute_init_signature
        sig = compute_init_signature("order-len", 29900, "Баннер", 1, 29900)
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)
