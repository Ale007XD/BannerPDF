"""
sanitizer.py
~~~~~~~~~~~~
Очистка и валидация текстовых строк для баннера.
Удаляет управляющие символы, ограничивает длину, нормализует пробелы.
"""

import re
import unicodedata

# Максимальная длина одной строки текста (символов)
MAX_LINE_LENGTH = 120

# Максимальное количество строк в одном баннере
MAX_LINES = 6


def sanitize_line(text: str) -> str:
    """
    Очищает одну строку:
    - Удаляет управляющие символы (кроме пробелов)
    - Нормализует Unicode (NFC)
    - Схлопывает множественные пробелы
    - Обрезает до MAX_LINE_LENGTH
    """
    if not isinstance(text, str):
        return ""

    # Нормализуем Unicode
    text = unicodedata.normalize("NFC", text)

    # Удаляем управляющие символы (категория Cc), кроме обычного пробела
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith("C") or ch == " "
    )

    # Схлопываем пробелы и убираем по краям
    text = re.sub(r" {2,}", " ", text).strip()

    # Обрезаем до максимальной длины
    return text[:MAX_LINE_LENGTH]


def sanitize_text_lines(lines: list[dict]) -> list[dict]:
    """
    Очищает список строк вида [{"text": "...", "scale": 1.0}, ...].
    - Фильтрует пустые строки после очистки
    - Ограничивает количество строк до MAX_LINES
    - Нормирует scale в диапазон [0.3, 1.5]
    """
    result = []
    for item in lines[:MAX_LINES]:
        text = sanitize_line(item.get("text", ""))
        if not text:
            continue
        scale = float(item.get("scale", 1.0))
        scale = max(0.3, min(1.5, scale))
        result.append({"text": text, "scale": scale})
    return result


def validate_banner_config(data: dict) -> list[str]:
    """
    Проверяет конфиг баннера. Возвращает список ошибок (пустой = OK).
    """
    from .config import BANNER_SIZES, COLORS, FONTS

    errors: list[str] = []

    if data.get("size_key") not in BANNER_SIZES:
        errors.append(f"Неизвестный размер: {data.get('size_key')!r}")

    if data.get("bg_color") not in COLORS:
        errors.append(f"Неизвестный цвет фона: {data.get('bg_color')!r}")

    if data.get("text_color") not in COLORS:
        errors.append(f"Неизвестный цвет текста: {data.get('text_color')!r}")

    if data.get("font") not in FONTS:
        errors.append(f"Неизвестный шрифт: {data.get('font')!r}")

    lines = data.get("text_lines", [])
    if not isinstance(lines, list) or len(lines) == 0:
        errors.append("Необходимо указать хотя бы одну строку текста")
    elif len(lines) > MAX_LINES:
        errors.append(f"Максимальное количество строк: {MAX_LINES}")

    return errors
