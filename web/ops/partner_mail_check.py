"""
Проверка почты партнёров. Запускается ВНУТРИ контейнера api (там есть SMTP_* и база), скрипт передаётся через stdin:

    docker exec -i bannerprint_api python3 - auth       < web/ops/partner_mail_check.py
    docker exec -i bannerprint_api python3 - send КОД   < web/ops/partner_mail_check.py

auth  Вход на SMTP- и IMAP-сервер с SMTP_USER / SMTP_PASSWORD (письма не отправляются). Печатает длину и
      sha256[:6] пароля, который видит контейнер: так видно, что .env не подхвачен (контейнер нужно
      пересоздать: docker compose up -d api) или в пароль попали пробелы. Сам пароль не печатается.
send  Тестовое письмо с PDF на почту партнёра КОД из базы, тем же кодом, что шлёт боевые письма (mailer).
      Копия уходит и на PARTNER_MAIL_BCC. «ОТПРАВЛЕНО» значит, что SMTP-сервер принял письмо; дошло ли оно
      до ящика, видно только в самом ящике (проверьте и «Спам»).
"""
import asyncio
import hashlib
import imaplib
import io
import os
import smtplib
import ssl
import sys


def check_auth() -> None:
    user = os.getenv("SMTP_USER", "")
    pw = os.getenv("SMTP_PASSWORD", "")
    host = os.getenv("SMTP_HOST", "")
    security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    port = int(os.getenv("SMTP_PORT", "465" if security == "ssl" else "587"))
    print("SMTP_HOST=%r PORT=%r SECURITY=%r USER=%r FROM=%r BCC=%r" % (
        host, port, security, user, os.getenv("SMTP_FROM"), os.getenv("PARTNER_MAIL_BCC")))
    print("пароль в контейнере: длина=%d sha256[:6]=%s пробелы по краям=%s" % (
        len(pw), hashlib.sha256(pw.encode()).hexdigest()[:6], pw != pw.strip()))
    if not host or not user:
        print("SMTP_HOST или SMTP_USER пусты: проверять нечего")
        return

    try:
        context = ssl.create_default_context()
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if security == "starttls":
                server.starttls(context=context)
            server.login(user, pw)
        print("SMTP login: OK")
    except Exception as e:
        print("SMTP login:", type(e).__name__, e)

    if not host.startswith("smtp."):
        print("IMAP login: пропущено (адрес IMAP-сервера выводится из smtp.*)")
        return
    try:
        imap = imaplib.IMAP4_SSL(host.replace("smtp.", "imap.", 1), 993, timeout=20)
        imap.login(user, pw)
        imap.logout()
        print("IMAP login: OK")
    except Exception as e:
        print("IMAP login:", type(e).__name__, e)


def send_test(code: str) -> None:
    from reportlab.pdfgen import canvas

    # в контейнере исходники лежат в /app/api, в репозитории (и в тестах) пакет называется web.api
    try:
        from api.services import mailer, partner_store
    except ImportError:
        from web.api.services import mailer, partner_store

    partner = partner_store.get_partner(code)
    if not partner:
        raise SystemExit(f"партнёр {code!r} не найден")
    print("получатель:", partner["email"], "| активен:", bool(partner["active"]),
          "| почта настроена:", mailer.is_configured())
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(72, 750, "BannerBot: test")
    pdf.save()
    msg = mailer.build_message(
        to=partner["email"],
        subject="Тест BannerBot: почта партнёра",
        body="Проверка отправки. Если вы видите вложение test.pdf, письма макетов будут доходить.",
        attachment_name="test.pdf",
        attachment_bytes=buf.getvalue(),
    )
    try:
        asyncio.run(mailer.send_message(msg))
        print("ОТПРАВЛЕНО на", partner["email"])
    except mailer.MailError as e:
        print("ОШИБКА:", e)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "auth":
        check_auth()
    elif mode == "send" and len(sys.argv) > 2:
        send_test(sys.argv[2])
    else:
        raise SystemExit("использование: python3 - auth | python3 - send КОД  (скрипт через stdin, см. docstring)")
