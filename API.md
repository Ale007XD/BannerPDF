# BannerPrint API 

Программный доступ к рендереру печатных баннеров.  
Получите готовый PDF для типографии за один HTTP-запрос.

**Base URL:** `https://bannerbot.ru:8444/api/v1`  
**Формат:** JSON (запрос и ответ) или multipart (batch)  
**Авторизация:** `Authorization: Bearer bp_live_<ключ>`

Получить ключ: [@ale007xd](https://t.me/ale007xd)

---

## Быстрый старт

### 1. Получить API-ключ

Напишите в Telegram [@ale007xd](https://t.me/ale007xd) — укажите план и email.  
Ключ выдаётся один раз, сохраните его сразу.

### 2. Сгенерировать PDF

```bash
curl -X POST https://bannerbot.ru:8444/api/v1/render \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ" \
  -H "Content-Type: application/json" \
  -d '{
    "size_key": "3x2",
    "bg_color": "Белый",
    "text_color": "Черный",
    "font": "Golos Text",
    "text_lines": [
      {"text": "ЛЕТНЯЯ РАСПРОДАЖА", "scale": 1.0},
      {"text": "Скидки до 70%",     "scale": 0.7}
    ]
  }' \
  --output banner.pdf
```

### 3. Готово

`banner.pdf` — PDF/X-1a, CMYK, ICC ISOcoated_v2_300, шрифты в кривых.  
Передавайте напрямую в типографию.

---

## Тарифы

| План       | PDF/месяц | RPM  | Цена        |
|------------|-----------|------|-------------|
| Starter    | 100       | 10   | 1 900 ₽/мес |
| Business   | 1 000     | 60   | 9 900 ₽/мес |
| Enterprise | ∞         | 300  | договор     |

- **PDF/месяц** — лимит рендеров, сбрасывается раз в 30 дней
- **RPM** — максимум запросов в минуту на ключ
- Счётчик растёт только при успешном рендере (HTTP 200)

---

## Эндпоинты

### POST /render — одиночный рендер

Рендерит один баннер, возвращает PDF-файл.

**Тело запроса:**

```json
{
  "size_key":   "3x2",
  "bg_color":   "Белый",
  "text_color": "Черный",
  "font":       "Golos Text",
  "text_lines": [
    {"text": "Текст строки 1", "scale": 1.0},
    {"text": "Текст строки 2", "scale": 0.7}
  ]
}
```

| Поле         | Тип            | Описание |
|--------------|----------------|----------|
| `size_key`   | string         | Типовой размер (см. справочник). Взаимоисключающий с `width_mm`/`height_mm` |
| `width_mm`   | integer        | Ширина в мм, 100–3000. Используется вместо `size_key` |
| `height_mm`  | integer        | Высота в мм, 100–3000. Используется вместо `size_key` |
| `bg_color`   | string         | Цвет фона (см. справочник) |
| `text_color` | string         | Цвет текста (см. справочник) |
| `font`       | string         | Шрифт (см. справочник) |
| `text_lines` | array, 1–6 эл. | Строки текста |
| `text_lines[].text`  | string | Текст строки, до 120 символов |
| `text_lines[].scale` | float  | Масштаб строки: 0.3–2.0, по умолчанию 1.0 |

**Ответ:** `application/pdf`

Заголовки ответа:

| Заголовок          | Описание |
|--------------------|----------|
| `X-Render-Time-Ms` | Время рендера в миллисекундах |
| `X-PDF-Used`       | Использовано PDF после этого запроса |
| `X-PDF-Limit`      | Лимит PDF на период (-1 = безлимит) |

**Пример — кастомный размер:**

```bash
curl -X POST https://bannerbot.ru:8444/api/v1/render \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ" \
  -H "Content-Type: application/json" \
  -d '{
    "width_mm":   1200,
    "height_mm":  400,
    "bg_color":   "Красный",
    "text_color": "Белый",
    "font":       "Fira Sans Cond",
    "text_lines": [{"text": "АКЦИЯ", "scale": 1.0}]
  }' \
  --output banner.pdf
```

---

### GET /usage — статистика использования

Текущий период, лимиты, счётчики.

```bash
curl https://bannerbot.ru:8444/api/v1/usage \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ"
```

**Ответ:**

```json
{
  "plan":          "starter",
  "pdf_used":      23,
  "pdf_limit":     100,
  "pdf_remaining": 77,
  "rpm_limit":     10,
  "period_start":  "2026-03-01",
  "period_end":    "2026-04-01",
  "key_prefix":    "bp_live_AbCd",
  "label":         "Мой проект"
}
```

`pdf_remaining: null` — если план Enterprise (безлимит).

---

### POST /batch/submit — batch-рендер

Рендерит несколько баннеров из CSV-файла, возвращает ZIP с PDF.

**Запрос** — multipart/form-data:

| Поле        | Тип    | Описание |
|-------------|--------|----------|
| `file`      | file   | CSV-файл (UTF-8 или UTF-8 BOM) |
| `bg_color`  | string | Цвет фона для всех баннеров |
| `text_color`| string | Цвет текста |
| `font`      | string | Шрифт |
| `size_key`  | string | Типовой размер — или `width_mm`+`height_mm` |
| `width_mm`  | int    | Ширина мм (если нет `size_key`) |
| `height_mm` | int    | Высота мм (если нет `size_key`) |

**Формат CSV** — без заголовка, каждая строка = один баннер, столбцы = строки текста:

```csv
ЛЕТНЯЯ РАСПРОДАЖА,Скидки до 70%
НОВАЯ КОЛЛЕКЦИЯ,Осень 2026
АКЦИЯ,,
```

Пустые ячейки и полностью пустые строки пропускаются. Максимум 500 строк.

```bash
curl -X POST https://bannerbot.ru:8444/api/v1/batch/submit \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ" \
  -F "file=@banners.csv" \
  -F "size_key=3x2" \
  -F "bg_color=Белый" \
  -F "text_color=Черный" \
  -F "font=Golos Text"
```

**Ответ:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "total":  47
}
```

---

### GET /batch/{job_id} — статус задачи

```bash
curl https://bannerbot.ru:8444/api/v1/batch/550e8400... \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ"
```

**Ответ:**

```json
{
  "job_id":     "550e8400-...",
  "status":     "processing",
  "total":      47,
  "done":       12,
  "errors":     [],
  "created_at": "2026-03-22T10:00:00Z",
  "ready_at":   null,
  "expires_at": "2026-03-22T11:00:00Z"
}
```

| Статус       | Описание |
|--------------|----------|
| `queued`     | В очереди |
| `processing` | Рендерится |
| `ready`      | Готово, можно скачивать |
| `failed`     | Все файлы завершились ошибкой |

---

### GET /batch/{job_id}/download — скачать ZIP

Доступен когда `status = ready`. TTL архива — 1 час после завершения.

```bash
curl https://bannerbot.ru:8444/api/v1/batch/550e8400.../download \
  -H "Authorization: Bearer bp_live_ВАШ_КЛЮЧ" \
  --output banners.zip
```

Заголовки ответа: `X-Files-Count`, `X-Errors-Count`.

**Рекомендуемый polling-интервал:** 3–5 секунд.

---

## Коды ошибок

| Код | Причина | Действие |
|-----|---------|----------|
| 401 | Неверный или истёкший ключ | Проверьте ключ |
| 422 | Ошибка валидации | Проверьте параметры запроса, `detail` содержит описание |
| 429 | Превышен RPM или PDF-лимит | Снизьте частоту запросов или обновите план |
| 500 | Ошибка рендера | Повторите запрос; если повторяется — напишите нам |

Формат ошибки:

```json
{
  "detail": "Описание ошибки на русском"
}
```

---

## Справочник значений

### Типовые размеры (`size_key`)

| Ключ     | Размер (мм)   |
|----------|---------------|
| `3x2`    | 3000 × 2000   |
| `2x2`    | 2000 × 2000   |
| `2x1`    | 2000 × 1000   |
| `1x0.5`  | 1000 × 500    |
| `1.5x1`  | 1500 × 1000   |
| `1.5x0.5`| 1500 × 500    |

Кастомный размер: `width_mm` + `height_mm`, диапазон 100–3000 мм.

### Цвета

`Белый`, `Черный`, `Красный`, `Синий`, `Желтый`, `Зеленый`, `Оранжевый`

### Шрифты

| Значение        | Начертание |
|-----------------|------------|
| `Golos Text`    | Современный гротеск |
| `Tenor Sans`    | Элегантный с засечками |
| `Fira Sans Cond`| Жирный конденсированный |
| `PT Sans Narrow`| Узкий, читаемый |

---

## Пример интеграции (Python)

```python
import requests

API_KEY  = "bp_live_ВАШ_КЛЮЧ"
BASE_URL = "https://bannerbot.ru:8444/api/v1"

def render_banner(text_lines: list[str], size_key: str = "3x2") -> bytes:
    resp = requests.post(
        f"{BASE_URL}/render",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "size_key":   size_key,
            "bg_color":   "Белый",
            "text_color": "Черный",
            "font":       "Golos Text",
            "text_lines": [{"text": t, "scale": 1.0} for t in text_lines],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content  # PDF-байты

pdf = render_banner(["ЛЕТНЯЯ РАСПРОДАЖА", "Скидки до 70%"])
with open("banner.pdf", "wb") as f:
    f.write(pdf)
```

---

## Поддержка

Telegram: [@ale007xd](https://t.me/ale007xd)  
Интерактивная документация (Swagger): `https://bannerbot.ru:8444/api/docs`
