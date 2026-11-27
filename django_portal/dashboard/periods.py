"""Разбор выбранных дат и периодов отчётов."""

from datetime import datetime, timedelta

from django.utils import timezone

FULL_REPORT_DAYS = 10


def requested_day(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def local_day_bounds(value):
    """Границы выбранной календарной даты в часовом поясе сайта."""
    day = requested_day(value)
    if not day:
        return None
    local_start = timezone.make_aware(
        datetime.strptime(day, "%Y-%m-%d"), timezone.get_default_timezone(),
    )
    return local_start.isoformat(), (local_start + timedelta(days=1)).isoformat()


def requested_period(request):
    """Возвращает границы периода в формате SQLite DATE: от и до включительно."""
    # Старый параметр date оставлен для совместимости со старыми закладками.
    legacy_date = requested_day(request.GET.get("date", ""))
    date_from = requested_day(request.GET.get("date_from", "")) or legacy_date
    date_to = requested_day(request.GET.get("date_to", "")) or legacy_date
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def format_selected_period(date_from, date_to):
    """Короткая подпись выбранного пользователем периода."""
    if not date_from and not date_to:
        return "Все даты"

    def display(value):
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")

    if date_from and date_to:
        if date_from == date_to:
            return display(date_from)
        return f"{display(date_from)} — {display(date_to)}"
    if date_from:
        return f"с {display(date_from)}"
    return f"по {display(date_to)}"


def full_report_period(now=None, date_from=None, date_to=None):
    """Return a safe report period; by default it is today plus nine previous days."""
    def as_date(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None
        return value

    date_from = as_date(date_from)
    date_to = as_date(date_to)
    local_timezone = timezone.get_default_timezone()
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, local_timezone)
    local_now = timezone.localtime(current, local_timezone)

    if date_from is None and date_to is None:
        date_to = local_now.date()
        date_from = date_to - timedelta(days=FULL_REPORT_DAYS - 1)
    elif date_from is None:
        date_from = date_to - timedelta(days=FULL_REPORT_DAYS - 1)
    elif date_to is None:
        date_to = local_now.date()
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return local_now, date_from, date_to
