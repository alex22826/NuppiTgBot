"""Справочник региональных QR/Data Matrix-кодов и проверка номера."""

import re

QR_REGION_RANGES = {
    "01": ("Ташкент", 10_000),
    "02": ("Самарканд", 23_000),
    "03": ("Карши", 100_000),
    "04": ("Бухара", 6_001),
    "05": ("Фергана", 6_000),
    "06": ("Андижан", 6_000),
    "07": ("Ургенч", 7_000),
    "08": ("Наманган", 6_000),
    "09": ("Денау", 4_000),
    "10": ("Термез", 2_000),
    "11": ("Коканд", 5_000),
    "12": ("Джиззах", 3_000),
    "13": ("Навои", 1_000),
}


QR_CITY_REGION_ALIASES = {
    "ташкент": "Ташкент",
    "самарканд": "Самарканд",
    "карши": "Карши",
    "кашкадарья": "Карши",
    "бухара": "Бухара",
    "фергана": "Фергана",
    "андижан": "Андижан",
    "ургенч": "Ургенч",
    "хорезм": "Ургенч",
    "наманган": "Наманган",
    "денау": "Денау",
    "термез": "Термез",
    "коканд": "Коканд",
    "джиззак": "Джиззах",
    "джиззах": "Джиззах",
    "навои": "Навои",
}


QR_REGION_CODE_PATTERN = re.compile(
    r"(?<!\d)(?P<prefix>\d{2})\s*[-‐‑‒–—−]\s*(?P<number>\d{10})(?!\d)"
)


QR_REGION_PLAIN_CODE_PATTERN = re.compile(r"(?<!\d)(?P<prefix>\d{2})(?P<number>\d{10})(?!\d)")


QR_REGION_FLEXIBLE_CODE_PATTERN = re.compile(
    r"(?<!\d)(?P<prefix>\d{2})[\s\-\u2010\u2011\u2012\u2013\u2014\u2212:./\\№#_]*"
    r"(?P<number>\d(?:\s*\d){9})(?!\d)"
)


def canonical_qr_data(value):
    """Normalize an operator-entered regional QR/Data Matrix number."""
    if not value or not str(value).strip():
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    match = QR_REGION_CODE_PATTERN.search(cleaned)
    if not match:
        plain_match = QR_REGION_PLAIN_CODE_PATTERN.search(re.sub(r"\s+", "", cleaned))
        if plain_match and plain_match.group("prefix") in QR_REGION_RANGES:
            match = plain_match
    if not match:
        flexible_match = QR_REGION_FLEXIBLE_CODE_PATTERN.search(cleaned)
        if flexible_match and flexible_match.group("prefix") in QR_REGION_RANGES:
            number = re.sub(r"\s+", "", flexible_match.group("number"))
            return f"{flexible_match.group('prefix')}-{int(number):010d}"
    if not match:
        return cleaned
    return f"{match.group('prefix')}-{int(match.group('number')):010d}"


def qr_code_status(qr_data, city=None):
    """Recalculate the same regional QR status used by the Telegram bot."""
    if not qr_data:
        return "Код отсутствует (допустимо)"
    match = QR_REGION_CODE_PATTERN.fullmatch(qr_data)
    if not match:
        return "Посторонний QR/Data Matrix: формат кода отсутствует в разрешённом справочнике"
    prefix = match.group("prefix")
    number = int(match.group("number"))
    region_range = QR_REGION_RANGES.get(prefix)
    if not region_range:
        return f"Посторонний QR/Data Matrix: префикс региона {prefix} отсутствует в справочнике"
    region, maximum = region_range
    if number < 1 or number > maximum:
        return (
            f"Посторонний QR/Data Matrix: код вне диапазона региона {region}; "
            f"допустимо 1–{maximum}"
        )
    normalized_city = re.sub(r"[^0-9a-zа-яё]+", " ", (city or "").casefold()).strip()
    expected_region = QR_CITY_REGION_ALIASES.get(normalized_city)
    if expected_region and expected_region != region:
        return f"Код другого региона: {region}; выбран регион {city}"
    if city and not expected_region:
        return f"Код распознан: {region}; регион {city} отсутствует в справочнике"
    return f"Код распознан: {region}"
