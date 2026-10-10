#!/usr/bin/env python3
"""
evidence.py — проверяемые факты о BannerBot для кейса (Wadline и др.).

Делает несколько рендеров через корп. API по вашему ключу и фиксирует:
  • время рендера (серверное X-Render-Time-Ms и полное время запроса);
  • фактический размер страницы PDF в мм против заказанного;
  • есть ли в PDF шрифты (пусто = текст переведён в кривые);
  • сколько операторов цвета каждого типа в страницах (CMYK / RGB / Gray).

Использует только стандартную библиотеку. Для проверок PDF нужен poppler-utils
(pdfinfo, pdffonts); без него вернёт только тайминги.

Запуск:
  python evidence.py --key bp_live_XXXX                  # 3 рендера
  python evidence.py --key bp_live_XXXX --base https://bannerbot.ru --out evidence_out
  python evidence.py --check-dir evidence_out            # проверить уже сохранённые PDF, лимит не тратится

Внимание: Trial-ключ даёт 3 PDF пожизненно — для замеров лучше отдельный ключ
Business (создаётся в админке, 1 000 PDF/мес). Каждый рендер списывается с лимита.
"""
import argparse
import base64
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
import zlib
import urllib.error
import urllib.request
from pathlib import Path

CASES = [
    # (имя, ширина мм, высота мм) — типовой, большой и максимальный размер из README (100–3000 мм)
    ("small_1000x500", 1000, 500),
    ("wide_3000x1000", 3000, 1000),
    ("max_3000x2000", 3000, 2000),
]
BASE_PAYLOAD = {
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [
        {"text": "ЛЕТНЯЯ РАСПРОДАЖА", "scale": 1.0},
        {"text": "Скидки до 70%", "scale": 0.7},
    ],
}
PT_TO_MM = 25.4 / 72

KEY_RE = re.compile(r"^bp_live_[A-Za-z0-9_-]{32}$")


def err_text(body):
    """Достаёт поле detail из JSON-ответа об ошибке, иначе декодирует тело как UTF-8."""
    try:
        d = json.loads(body.decode("utf-8"))
        if isinstance(d, dict) and "detail" in d:
            return str(d["detail"])
    except (ValueError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", errors="replace")[:200]


def call(url, key, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), time.perf_counter() - t0


def run_tool(cmd):
    if not shutil.which(cmd[0]):
        return None
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return out.stdout

NUM = r"-?\d*\.?\d+"
OPS = {
    "CMYK": re.compile(rf"(?:{NUM}\s+){{4}}[kK](?=[\s\r\n]|$)"),
    "RGB": re.compile(rf"(?:{NUM}\s+){{3}}(?:rg|RG)(?=[\s\r\n]|$)"),
    "Gray": re.compile(rf"(?:{NUM}\s+){{1}}[gG](?=[\s\r\n]|$)"),
}


def iter_streams(raw):
    """Отдаёт (заголовок_объекта, декодированный_текст | None) для потоков, кроме растровых."""
    for m in re.finditer(rb"stream\r?\n", raw):
        head = raw[max(0, m.start() - 400):m.start()].split(b"obj")[-1]
        if b"/Image" in head:
            continue  # растровые данные не сканируем
        end = raw.find(b"endstream", m.end())
        chunk = raw[m.end():end]
        try:
            if b"ASCII85Decode" in head:
                chunk = base64.a85decode(chunk.strip(), adobe=True)
            if b"FlateDecode" in head:
                chunk = zlib.decompress(chunk)
        except (ValueError, zlib.error):
            yield head, None
            continue
        yield head, chunk.decode("latin-1")


def pdf_text(raw):
    """Открытый текст PDF + содержимое всех разобранных потоков (в т.ч. сжатых объектов)."""
    return raw.decode("latin-1") + "\n" + "\n".join(t for _, t in iter_streams(raw) if t)


def color_report(raw):
    """Считает операторы цвета в потоках страниц (Flate или открытых). Эвристика."""
    counts = {k: 0 for k in OPS}
    undecoded = 0
    for _, text in iter_streams(raw):
        if text is None:
            undecoded += 1
            continue
        for name, rx in OPS.items():
            counts[name] += len(rx.findall(text))
    names = [n.decode() for n in (b"/DeviceCMYK", b"/DeviceRGB", b"/ICCBased") if n in raw]
    base = f"операторов CMYK: {counts['CMYK']}, RGB: {counts['RGB']}, Gray: {counts['Gray']}"
    extra = f"; объекты: {', '.join(names)}" if names else ""
    warn = f"; НЕ РАЗОБРАНО потоков: {undecoded} — результат неполный" if undecoded else ""
    return base + extra + warn


MEDIABOX_RE = re.compile(r"/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]")
FONT_RE = re.compile(r"/BaseFont\s*/([^\s/\[\]<>()]+)")


def size_text(x0, y0, x1, y1, want_w, want_h, note=""):
    w, h = (x1 - x0) * PT_TO_MM, (y1 - y0) * PT_TO_MM
    ok = abs(w - want_w) <= 1 and abs(h - want_h) <= 1
    return f"{w:.1f}×{h:.1f} мм ({'совпадает' if ok else 'НЕ совпадает'} с {want_w}×{want_h}){note}"


def check_pdf(path, want_w, want_h):
    res = {"size": "н/д", "fonts": "н/д", "colorspaces": "н/д"}
    raw = path.read_bytes()
    text = None

    info = run_tool(["pdfinfo", "-box", str(path)])
    m = re.search(r"MediaBox:\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)", info or "")
    if m:
        res["size"] = size_text(*(float(v) for v in m.groups()), want_w, want_h)
    else:  # poppler нет — разбираем PDF сами
        text = pdf_text(raw)
        m = MEDIABOX_RE.search(text)
        if m:
            res["size"] = size_text(*(float(v) for v in m.groups()), want_w, want_h, " [без poppler]")

    fonts = run_tool(["pdffonts", str(path)])
    if fonts is not None:
        rows = [ln for ln in fonts.splitlines()[2:] if ln.strip()]
        res["fonts"] = "нет (текст в кривых)" if not rows else f"{len(rows)} шт. — текст НЕ в кривых"
    else:
        text = text or pdf_text(raw)
        names = sorted(set(FONT_RE.findall(text)))
        res["fonts"] = ("нет (текст в кривых) [без poppler]" if not names
                        else f"{len(names)} шт. ({', '.join(names[:3])}) — текст НЕ в кривых [без poppler]")

    res["colorspaces"] = color_report(raw)
    return res


def check_dir(d):
    """Проверка уже сохранённых PDF без запросов к API (лимит ключа не тратится)."""
    files = sorted(Path(d).glob("*.pdf"))
    if not files:
        sys.exit(f"В {d} нет PDF.")
    lines = [f"# Проверка сохранённых PDF ({d})", "",
             "| Файл | Размер, КБ | Страница | Шрифты | Цвет (операторы) |", "|---|---|---|---|---|"]
    for f in files:
        m = re.search(r"_(\d+)x(\d+)", f.stem)
        want_w, want_h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        c = check_pdf(f, want_w, want_h)
        lines.append(f"| {f.name} | {f.stat().st_size / 1024:.0f} | {c['size']} | {c['fonts']} | {c['colorspaces']} |")
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="API-ключ bp_live_... (не нужен с --check-dir)")
    ap.add_argument("--check-dir", metavar="DIR", help="только проверить уже сохранённые PDF, без запросов к API")
    ap.add_argument("--base", default="https://bannerbot.ru")
    ap.add_argument("--out", default="evidence_out")
    a = ap.parse_args()
    if a.check_dir:
        return check_dir(a.check_dir)
    if not a.key:
        ap.error("нужен --key (или --check-dir для проверки готовых PDF)")
    if not KEY_RE.match(a.key):
        sys.exit(
            "Ключ выглядит как заглушка или неполный: ожидается bp_live_ + 32 символа "
            f"(всего 40), получено {len(a.key)}.\n"
            "Полный ключ показывается в админке только один раз при создании — "
            "в списке виден лишь префикс. Если не сохранили, создайте новый."
        )
    base = a.base.rstrip("/")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    status, _, body, _ = call(f"{base}/api/v1/usage", a.key)
    if status != 200:
        sys.exit(f"Ключ не принят: HTTP {status} — {err_text(body)}")
    usage = json.loads(body)
    print("Ключ принят:", {k: usage[k] for k in usage if k in ("plan", "pdf_used", "pdf_limit", "is_trial")})
    if usage.get("is_trial"):
        print("! Trial-ключ: всего 3 PDF пожизненно, лучше использовать отдельный Business-ключ.")

    rows, client_s, server_ms = [], [], []
    for name, w, h in CASES:
        payload = {**BASE_PAYLOAD, "width_mm": w, "height_mm": h}
        status, headers, body, dt = call(f"{base}/api/v1/render", a.key, payload)
        if status != 200:
            print(f"{name}: HTTP {status} — {err_text(body)}")
            continue
        path = out / f"{name}.pdf"
        path.write_bytes(body)
        h_ci = {k.lower(): v for k, v in headers.items()}
        srv = int(h_ci.get("x-render-time-ms", 0))
        client_s.append(dt)
        server_ms.append(srv)
        chk = check_pdf(path, w, h)
        rows.append((name, srv, dt, len(body) / 1024, chk))
        print(f"{name}: сервер {srv} мс, полный запрос {dt:.2f} с, {len(body)/1024:.0f} КБ")

    if not rows:
        sys.exit("Ни одного успешного рендера.")

    lines = ["# BannerBot: замеры и проверка PDF", "",
             f"Хост: {base}. Дата: {time.strftime('%Y-%m-%d')}. Рендеров: {len(rows)}.", "",
             "| Файл | Сервер, мс | Запрос целиком, с | Размер, КБ | Страница | Шрифты | Цвет (операторы) |",
             "|---|---|---|---|---|---|---|"]
    for name, srv, dt, kb, c in rows:
        lines.append(f"| {name} | {srv} | {dt:.2f} | {kb:.0f} | {c['size']} | {c['fonts']} | {c['colorspaces']} |")
    lines += ["", f"Медиана серверного времени: {statistics.median(server_ms)} мс; "
                  f"медиана запроса целиком: {statistics.median(client_s):.2f} с.", "",
              "Примечание: цвет определён подсчётом операторов в потоках страниц (эвристика; Gray может быть штатным для чёрного). "
              "Формальная проверка — Acrobat → Output Preview или preflight типографии."]
    (out / "evidence.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nФайлы и отчёт сохранены в {out}/")


if __name__ == "__main__":
    main()
