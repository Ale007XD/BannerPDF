"""
web/ops/partner_mail_check.py: диагностика почты партнёров, которую запускают в контейнере (см. README).

Сеть не нужна: SMTP подменяется, отправка перехватывается на уровне mailer.send_message.
"""

import importlib.util
import smtplib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from web.api.services import partner_store

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "partner_mail_check.py"
SECRET = "supersecretpassword"


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("partner_mail_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.setenv("SMTP_USER", "sender@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", SECRET)
    monkeypatch.setenv("SMTP_FROM", "sender@example.test")
    monkeypatch.setenv("PARTNER_MAIL_BCC", "owner@example.test")


class TestAuth:
    def test_password_is_never_printed(self, tool, smtp_env, capsys):
        err = smtplib.SMTPAuthenticationError(535, b"5.7.8 no access")
        with patch.object(tool.smtplib, "SMTP_SSL", side_effect=err), \
                patch.object(tool.imaplib, "IMAP4_SSL", side_effect=OSError("imap down")):
            tool.check_auth()
        out = capsys.readouterr().out
        assert SECRET not in out
        assert f"длина={len(SECRET)}" in out
        assert "SMTP login: SMTPAuthenticationError" in out and "IMAP login: OSError" in out

    def test_login_ok(self, tool, smtp_env, capsys):
        server = patch.object(tool.smtplib, "SMTP_SSL").start()
        imap = patch.object(tool.imaplib, "IMAP4_SSL").start()
        try:
            tool.check_auth()
        finally:
            patch.stopall()
        out = capsys.readouterr().out
        assert "SMTP login: OK" in out and "IMAP login: OK" in out
        server.return_value.login.assert_called_once_with("sender@example.test", SECRET)
        imap.return_value.login.assert_called_once_with("sender@example.test", SECRET)

    def test_not_configured_does_not_try_to_connect(self, tool, monkeypatch, capsys):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("SMTP_USER", raising=False)
        with patch.object(tool.smtplib, "SMTP_SSL", side_effect=AssertionError("не должен подключаться")):
            tool.check_auth()
        assert "пусты" in capsys.readouterr().out

    def test_whitespace_around_password_is_reported(self, tool, smtp_env, monkeypatch, capsys):
        monkeypatch.setenv("SMTP_PASSWORD", SECRET + " ")
        with patch.object(tool.smtplib, "SMTP_SSL"), patch.object(tool.imaplib, "IMAP4_SSL"):
            tool.check_auth()
        assert "пробелы по краям=True" in capsys.readouterr().out

    def test_imap_is_skipped_for_hosts_not_named_smtp(self, tool, smtp_env, monkeypatch, capsys):
        monkeypatch.setenv("SMTP_HOST", "mail.example.test")
        with patch.object(tool.smtplib, "SMTP_SSL"), \
                patch.object(tool.imaplib, "IMAP4_SSL", side_effect=AssertionError("IMAP не нужен")):
            tool.check_auth()
        assert "IMAP login: пропущено" in capsys.readouterr().out


class TestSend:
    def test_sends_test_pdf_to_the_partner_through_the_real_mailer(self, tool, smtp_env, init_test_db, capsys):
        partner_store.create_partner("reklamacity", "Реклама-Сити", "print@example.com")
        with patch("web.api.services.mailer.send_message", new=AsyncMock()) as send:
            tool.send_test("reklamacity")
        msg = send.call_args.args[0]
        assert msg["To"] == "print@example.com"
        assert msg["Bcc"] == "owner@example.test"
        assert [a.get_filename() for a in msg.iter_attachments()] == ["test.pdf"]
        assert msg.get_payload()[1].get_payload(decode=True).startswith(b"%PDF")
        assert "ОТПРАВЛЕНО на print@example.com" in capsys.readouterr().out

    def test_mail_error_is_reported_not_raised(self, tool, smtp_env, init_test_db, capsys):
        from web.api.services import mailer
        partner_store.create_partner("reklamacity", "Реклама-Сити", "print@example.com")
        with patch("web.api.services.mailer.send_message", new=AsyncMock(side_effect=mailer.MailError("SMTPAuthenticationError: 535"))):
            tool.send_test("reklamacity")
        assert "ОШИБКА: SMTPAuthenticationError: 535" in capsys.readouterr().out

    def test_unknown_partner_exits_with_a_message(self, tool, smtp_env, init_test_db):
        with pytest.raises(SystemExit) as e:
            tool.send_test("nope")
        assert "nope" in str(e.value)


class TestCli:
    @pytest.mark.parametrize("argv", [[], ["wat"], ["send"]])
    def test_usage(self, tool, monkeypatch, argv):
        monkeypatch.setattr(tool.sys, "argv", ["-", *argv])
        with pytest.raises(SystemExit) as e:
            exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), {"__name__": "__main__", "sys": tool.sys})
        assert "использование" in str(e.value)
