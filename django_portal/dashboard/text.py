"""Нормализация текста, имён файлов и форматирование дат для сайта."""

import re
from datetime import datetime
from datetime import timezone as datetime_timezone
from urllib.parse import quote, unquote

from django.utils import timezone


def duplicate_name_key(value):
    """Normalize an operator-entered name for strict equality checks."""
    return re.sub(r"[^a-zа-я0-9]+", "", str(value or "").casefold().replace("ё", "е"))


def parse_hospitals(value):
    hospitals, seen = [], set()
    for item in re.split(r"[,;\n]+", value or ""):
        hospital = item.strip()
        if hospital and hospital.casefold() not in seen:
            hospitals.append(hospital)
            seen.add(hospital.casefold())
    return hospitals


def excel_sheet_title(value, used_titles):
    base = re.sub(r'[\\/:*?\[\]]+', " ", value or "Без врача").strip()[:31] or "Без врача"
    title, number = base, 2
    while title in used_titles:
        suffix = f" ({number})"
        title = base[:31 - len(suffix)] + suffix
        number += 1
    return title


def safe_excel_filename(value):
    return re.sub(r'[<>:"/\\|?*]+', "_", value or "Фотографии").strip(". ") or "Фотографии"


def doctor_search_key(value):
    """Нормализует ФИО для поиска без учёта регистра, пробелов и пунктуации."""
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def doctor_search_condition(column="p.doctor_name"):
    normalized = f"DOCTOR_SEARCH_KEY(COALESCE({column}, ''))"
    return f"({normalized} LIKE ? OR ({normalized} != '' AND ? LIKE '%' || {normalized} || '%'))"


def encode_photo_location(city, hospital):
    """Создаёт безопасное значение option для пары город/роддом."""
    return f"{quote(str(city or ''), safe='')}|{quote(str(hospital or ''), safe='')}"


def decode_photo_location(value):
    """Разбирает выбранную в форме пару город/роддом."""
    if not value or "|" not in value:
        return None
    encoded_city, encoded_hospital = value.split("|", 1)
    city = unquote(encoded_city).strip()[:200]
    hospital = unquote(encoded_hospital).strip()[:200]
    if not city or not hospital:
        return None
    return city, hospital


def format_sent_at(value):
    """Формат для сайта; исходное время в базе и связях копий остаётся неизменным."""
    if not value:
        return "—"
    try:
        sent_at = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        # Старые записи без смещения трактуются как UTC, как и время отправки в боте.
        if timezone.is_naive(sent_at):
            sent_at = sent_at.replace(tzinfo=datetime_timezone.utc)
        return timezone.localtime(sent_at, timezone.get_default_timezone()).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "—"


def sent_at_local_date(value):
    """Календарная дата отправки в часовом поясе сайта."""
    if not value:
        return None
    try:
        sent_at = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if timezone.is_naive(sent_at):
            sent_at = sent_at.replace(tzinfo=datetime_timezone.utc)
        return timezone.localtime(sent_at, timezone.get_default_timezone()).date()
    except (TypeError, ValueError, OverflowError):
        return None
