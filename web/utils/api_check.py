#!/usr/bin/env python3
"""
api_check.py — сквозная проверка корп. API BannerBot по ключу.

Что делает:
  1. GET  /api/v1/usage         — план, лимиты, RPM; от RPM считается интервал опроса.
  2. POST /api/v1/render        — единичный рендер, сохраняет PDF, проверяет его.
  3. POST /api/v1/batch/submit  — пакет из CSV (свой файл или сгенерированный).
  4. GET  /api/v1/batch/{id}    — опрос статуса с учётом RPM и 429.
  5. GET  /api/v1/batch/{id}/download — ZIP скачивается ОДИН раз; проверяется состав.
  6. Сверка счётчика pdf_used: на сколько вырос после единичного и после пакета.
  --recount: скачать ZIP ещё раз и посмотреть, не списывается ли лимит повторно.

Только стандартная библиотека. Если рядом лежит evidence.py, PDF проверяются
дополнительно (размер страницы, шрифты, цвет).

Запуск:
  python3 api_check.py --key bp_live_...                       # 5 строк CSV
  python3 api_check.py --key bp_live_... --csv banners.csv --size-key 3x2
  python3 api_check.py --key bp_live_... --rows 20 --recount

Расход лимита: 1 PDF на единичный рендер + число строк CSV (+ столько же с --recount,
если повторное скачивание действительно списывается). Trial (3 PDF) для пакета мал.
"""
import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

KEY_RE = re.compile(r"^bp_live_[A-Za-z0-9_-]{32}$")
FONT, BG, FG = "Golos Text", "Белый", "Черный"

try:  # необязательно: проверки PDF из evidence.py
    import evidence
except ImportError:
    evidence = None

results = []  # (название проверки, ok, комментарий)


def check(name, ok, note=""):
    results.append((name, ok, note))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return ok


def err_text(body):
    try:
        d = json.loads(body.decode("utf-8"))
        if isinstance(d, dict) and "detail" in d:
            return str(d["detail"])
    except (ValueError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", errors="replace")[:200]


def http(method, url, key, json_body=None, multipart=None, timeout=180):
    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif multipart is not None:
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()
        for name, value in multipart["fields"].items():
            buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        fname, content = multipart["file"]
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                  f"Content-Type: text/csv\r\n\r\n".encode("utf-8"))
        buf.write(content + b"\r\n" + f"--{boundary}--\r\n".encode())
        data = buf.getvalue()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(), time.perf_counter() - t0


def get_usage(base, key):
    st, _, body, _ = http("GET", f"{base}/api/v1/usage", key)
    if st != 200:
        sys.exit(f"Ключ не принят: HTTP {st} — {err_text(body)}")
    return json.loads(body)


def size_mm(size_key):
    m = re.fullmatch(r"([\d.]+)x([\d.]+)", size_key)
    return (round(float(m.group(1)) * 1000), round(float(m.group(2)) * 1000)) if m else (0, 0)


def pdf_report(path, size_key):
    if evidence is None:
        return
    w, h = size_mm(size_key)
    c = evidence.check_pdf(Path(path), w, h)
    check("PDF: размер страницы", "совпадает" in c["size"] and "НЕ" not in c["size"], c["size"])
    check("PDF: шрифты в кривых", c["fonts"].startswith("нет"), c["fonts"])
    check("PDF: нет RGB/Gray", "RGB: 0, Gray: 0" in c["colorspaces"], c["colorspaces"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--base", default="https://bannerbot.ru")
    ap.add_argument("--size-key", default="3x2", help="типовой размер (3x2 = 3000×2000 мм)")
    ap.add_argument("--csv", help="свой CSV без заголовка; иначе будет сгенерирован")
    ap.add_argument("--rows", type=int, default=5, help="строк в сгенерированном CSV")
    ap.add_argument("--out", default="api_check_out")
    ap.add_argument("--recount", action="store_true", help="скачать ZIP повторно и сверить счётчик")
    a = ap.parse_args()
    if not KEY_RE.match(a.key):
        sys.exit("Ключ выглядит как заглушка или неполный (нужно bp_live_ + 32 символа). "
                 "Полный ключ показывается в админке один раз при создании.")
    base, out = a.base.rstrip("/"), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    print("1. Ключ и лимиты")
    u0 = get_usage(base, a.key)
    rpm = u0["rpm_limit"]
    interval = min(15.0, max(2.0, 60.0 / rpm * 1.25))
    print(f"  план {u0['plan']}, использовано {u0['pdf_used']}/{u0['pdf_limit']}, RPM {rpm}, интервал опроса {interval:.1f} с")

    # --- единичный рендер ---
    print("2. Единичный рендер")
    body = {"size_key": a.size_key, "bg_color": BG, "text_color": FG, "font": FONT,
            "text_lines": [{"text": "ЛЕТНЯЯ РАСПРОДАЖА", "scale": 1.0}, {"text": "Скидки до 70%", "scale": 0.7}]}
    st, h, pdf, dt = http("POST", f"{base}/api/v1/render", a.key, json_body=body)
    if not check("POST /render → 200", st == 200, f"HTTP {st}" + ("" if st == 200 else f": {err_text(pdf)}")):
        sys.exit("Единичный рендер не прошёл, дальше смысла нет.")
    (out / "single.pdf").write_bytes(pdf)
    check("ответ — PDF", pdf[:5] == b"%PDF-", f"{len(pdf)/1024:.0f} КБ, сервер {h.get('x-render-time-ms','?')} мс, запрос {dt:.2f} с")
    pdf_report(out / "single.pdf", a.size_key)
    u1 = get_usage(base, a.key)
    check("счётчик после единичного +1", u1["pdf_used"] == u0["pdf_used"] + 1, f"{u0['pdf_used']} → {u1['pdf_used']}")

    # --- пакет ---
    print("3. Пакет из CSV")
    if a.csv:
        raw = Path(a.csv).read_bytes()
    else:
        sio = io.StringIO()
        w = csv.writer(sio)
        for i in range(1, a.rows + 1):
            w.writerow([f"АКЦИЯ {i}", "Скидки до 70%"])
        raw = sio.getvalue().encode("utf-8")
    (out / "banners.csv").write_bytes(raw)
    expected = sum(1 for row in csv.reader(io.StringIO(raw.decode("utf-8-sig"))) if any(c.strip() for c in row))
    print(f"  строк с данными в CSV: {expected}")
    st, _, body, _ = http("POST", f"{base}/api/v1/batch/submit", a.key, multipart={
        "fields": {"size_key": a.size_key, "bg_color": BG, "text_color": FG, "font": FONT},
        "file": ("banners.csv", raw)})
    if not check("POST /batch/submit → 200", st == 200, f"HTTP {st}" + ("" if st == 200 else f": {err_text(body)}")):
        return finish(out)
    job = json.loads(body)
    check("total совпадает с CSV", job["total"] == expected, f"сервер {job['total']}, ожидалось {expected}")

    deadline, status = time.time() + 600, {}
    while time.time() < deadline:
        time.sleep(interval)
        st, _, body, _ = http("GET", f"{base}/api/v1/batch/{job['job_id']}", a.key)
        if st == 429:
            print(f"  429 на опросе ({err_text(body)}), жду {interval*2:.0f} с")
            time.sleep(interval * 2)
            continue
        if st != 200:
            check("GET /batch/{id}", False, f"HTTP {st}: {err_text(body)}")
            return finish(out)
        status = json.loads(body)
        print(f"  {status['status']}: {status['done']}/{status['total']}, ошибок {len(status['errors'])}")
        if status["status"] in ("ready", "failed"):
            break
    check("задача завершилась (ready)", status.get("status") == "ready", f"статус {status.get('status')}")
    if status.get("status") != "ready":
        return finish(out)
    check("ошибок в задаче нет", not status["errors"], str(status["errors"])[:200])

    u2 = get_usage(base, a.key)
    check("до скачивания лимит не списан", u2["pdf_used"] == u1["pdf_used"],
          f"{u1['pdf_used']} → {u2['pdf_used']} (списание происходит при скачивании)")

    st, h, zbytes, _ = http("GET", f"{base}/api/v1/batch/{job['job_id']}/download", a.key)
    if not check("GET /download → 200", st == 200, f"HTTP {st}" + ("" if st == 200 else f": {err_text(zbytes)}")):
        return finish(out)
    (out / "batch.zip").write_bytes(zbytes)
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = zf.namelist()
    check("файлов в ZIP = строкам CSV", len(names) == expected, f"{len(names)} из {expected}")
    check("X-Files-Count совпадает", h.get("x-files-count") == str(len(names)), h.get("x-files-count", "нет заголовка"))
    check("все файлы — PDF", all(zf.read(n)[:5] == b"%PDF-" for n in names))
    if names:
        (out / "batch_first.pdf").write_bytes(zf.read(names[0]))
        pdf_report(out / "batch_first.pdf", a.size_key)
    u3 = get_usage(base, a.key)
    check("счётчик после пакета +число файлов", u3["pdf_used"] == u2["pdf_used"] + len(names),
          f"{u2['pdf_used']} → {u3['pdf_used']} (ожидалось +{len(names)})")

    if a.recount:
        print("4. Повторное скачивание того же ZIP")
        st, _, _, _ = http("GET", f"{base}/api/v1/batch/{job['job_id']}/download", a.key)
        u4 = get_usage(base, a.key)
        check("повторное скачивание не списывает лимит", st != 200 or u4["pdf_used"] == u3["pdf_used"],
              f"HTTP {st}, счётчик {u3['pdf_used']} → {u4['pdf_used']}")
    finish(out)


def finish(out):
    bad = [r for r in results if not r[1]]
    lines = ["# Проверка API BannerBot", "", "| Проверка | Итог | Комментарий |", "|---|---|---|"]
    lines += [f"| {n} | {'OK' if ok else 'FAIL'} | {note} |" for n, ok, note in results]
    (out / "api_check.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nИтог: {len(results) - len(bad)} OK, {len(bad)} FAIL. Отчёт и файлы: {out}/")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
