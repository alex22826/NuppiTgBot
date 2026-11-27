import sqlite3
import secrets
import re
import hashlib
import threading
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import BytesIO
from functools import wraps
from urllib.parse import quote, unquote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from PIL import Image, ImageOps, UnidentifiedImageError

from .decision_rules import (
    DECISION_ACCEPTED,
    DECISION_LABELS,
    DECISION_REJECTED,
    DECISION_REVIEW,
    automatic_photo_decision,
)

PHOTO_PAGE_SIZE = 20
PHOTO_THUMBNAIL_SIZE = (480, 480)
PHOTO_THUMBNAIL_QUALITY = 72
CHAT_HISTORY_LIMIT = 300
FULL_REPORT_DAYS = 10
PHOTO_PAYMENT_RATE_SUM = 100_000
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
_DASHBOARD_CATALOG_CACHE = {"key": None, "value": None}
_DASHBOARD_CATALOG_LOCK = threading.Lock()


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


def duplicate_name_key(value):
    """Normalize an operator-entered name for strict equality checks."""
    return re.sub(r"[^a-zа-я0-9]+", "", str(value or "").casefold().replace("ё", "е"))


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


def database_table_exists(connection, table_name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone() is not None


def ensure_photo_checks_table(connection):
    """Create the supervisor decision table on an older database safely."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS photo_checks (
            photo_id INTEGER PRIMARY KEY,
            product_status TEXT NOT NULL DEFAULT 'Не проверено',
            material_status TEXT NOT NULL DEFAULT 'Не проверено',
            location_status TEXT NOT NULL DEFAULT 'Не запрашивалась',
            duplicate_status TEXT NOT NULL DEFAULT 'Не проверено',
            editing_status TEXT NOT NULL DEFAULT 'Не проверено',
            assignment_status TEXT NOT NULL DEFAULT 'Задание не назначено',
            decision TEXT NOT NULL DEFAULT 'review',
            decision_reason TEXT NOT NULL DEFAULT '',
            automated_at TEXT,
            manual_decision TEXT,
            manual_reason TEXT,
            manual_reviewer TEXT,
            manual_at TEXT,
            FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_photo_checks_decision "
        "ON photo_checks(decision, manual_decision)"
    )


def ensure_visit_assignments_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS visit_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medic_telegram_id INTEGER,
            city TEXT NOT NULL,
            maternity_hospital TEXT NOT NULL,
            doctor_name TEXT NOT NULL DEFAULT '',
            required_product TEXT NOT NULL DEFAULT '',
            required_material TEXT NOT NULL DEFAULT '',
            date_from TEXT,
            date_to TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)


def csrf_failure(request, reason=""):
    """Recover from an expired login form without weakening CSRF protection."""
    if request.path == reverse("login"):
        query = request.GET.copy()
        query["csrf"] = "1"
        return redirect(f"{reverse('login')}?{query.urlencode()}")

    from django.views.csrf import csrf_failure as django_csrf_failure

    return django_csrf_failure(request, reason=reason)


def is_creator(user):
    """Создатель определяется только явно заданным логином сайта."""
    return bool(
        user.is_authenticated
        and settings.CREATOR_DJANGO_USERNAME
        and user.username == settings.CREATOR_DJANGO_USERNAME
    )


def creator_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_creator(request.user):
            raise PermissionDenied("Только создатель сайта может изменять данные.")
        return view(request, *args, **kwargs)
    return wrapped


def photo_thumbnail_cache_path(photo_id, image_digest):
    """Хранит восстанавливаемые превью вне постоянного диска с основной БД."""
    cache_directory = settings.PHOTO_THUMBNAIL_CACHE_DIR
    safe_digest = re.sub(r"[^a-zA-Z0-9_-]", "", image_digest or "")[:32] or "unknown"
    return cache_directory / f"{photo_id}-{safe_digest}.jpg"


def make_photo_thumbnail(image_bytes):
    """Создаёт лёгкое превью, не меняя оригинал в базе данных."""
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            # Для JPEG draft уменьшает расход памяти ещё до полного декодирования.
            source.draft("RGB", PHOTO_THUMBNAIL_SIZE)
            image = ImageOps.exif_transpose(source)
            image.thumbnail(PHOTO_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=PHOTO_THUMBNAIL_QUALITY,
                optimize=True,
            )
            return output.getvalue()
    except (OSError, UnidentifiedImageError):
        # Старые/повреждённые записи продолжают открываться как раньше.
        return image_bytes


def cached_photo_thumbnail(photo_id, image_digest, image_bytes):
    if not image_digest:
        image_digest = hashlib.sha256(image_bytes).hexdigest()
    cache_path = photo_thumbnail_cache_path(photo_id, image_digest)
    try:
        if cache_path.exists():
            return cache_path.read_bytes()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        thumbnail = make_photo_thumbnail(image_bytes)
        temporary_path = cache_path.with_suffix(f".{secrets.token_hex(6)}.tmp")
        try:
            temporary_path.write_bytes(thumbnail)
            temporary_path.replace(cache_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return thumbnail
    except OSError:
        # Недоступный кэш не должен мешать просмотру сайта.
        return make_photo_thumbnail(image_bytes)


def bot_database(read_only=True):
    if not settings.BOT_DB_PATH.exists():
        return None
    if read_only:
        connection = sqlite3.connect(f"file:{settings.BOT_DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(settings.BOT_DB_PATH)
        connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    connection.create_function("DOCTOR_SEARCH_KEY", 1, doctor_search_key, deterministic=True)
    return connection


def photo_columns(connection):
    return {row["name"] for row in connection.execute("PRAGMA table_info(photos)")}


def user_columns(connection):
    return {row["name"] for row in connection.execute("PRAGMA table_info(users)")}


def ensure_active_column(connection):
    if "is_active" not in user_columns(connection):
        connection.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")


def ensure_allowed_users_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS allowed_users (
            telegram_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL
        )
    """)


def ensure_employee_hospitals_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS employee_hospitals (
            telegram_id INTEGER NOT NULL,
            city TEXT NOT NULL,
            maternity_hospital TEXT NOT NULL,
            PRIMARY KEY (telegram_id, maternity_hospital)
        )
    """)


def hospital_table_exists(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_hospitals'"
    ).fetchone() is not None


def ensure_hospital_renames_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS hospital_renames (
            city TEXT NOT NULL,
            old_name TEXT NOT NULL,
            new_name TEXT NOT NULL,
            renamed_at TEXT NOT NULL,
            PRIMARY KEY (city, old_name)
        )
    """)


def load_hospital_renames(connection):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hospital_renames'"
    ).fetchone()
    if not exists:
        return {}
    return {
        (doctor_search_key(row["city"]), doctor_search_key(row["old_name"])): row["new_name"]
        for row in connection.execute("SELECT city, old_name, new_name FROM hospital_renames")
    }


def resolve_hospital_name(city, hospital, renames):
    """Возвращает последнее название роддома после цепочки переименований."""
    resolved = hospital
    visited = set()
    for _ in range(20):
        key = (doctor_search_key(city), doctor_search_key(resolved))
        if key in visited or key not in renames:
            break
        visited.add(key)
        resolved = renames[key]
    return resolved


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


def build_doctor_options(database_rows, hospital_renames=None):
    """Возвращает имена врачей, которые уже встречались на фотографиях."""
    options = {}
    hospital_renames = hospital_renames or {}

    def add(name, city, hospital):
        clean_name = re.sub(r"\s+", " ", str(name or "")).strip()
        key = doctor_search_key(clean_name)
        if not key:
            return
        entry = options.setdefault(key, {"name": clean_name, "contexts": set()})
        context = " · ".join(value for value in (city, hospital) if value and value != "Не указан")
        if context:
            entry["contexts"].add(context)

    for row in database_rows:
        add(
            row["doctor_name"], row["city"],
            resolve_hospital_name(row["city"], row["maternity_hospital"], hospital_renames),
        )

    return sorted(
        (
            {"name": item["name"], "context": "; ".join(sorted(item["contexts"]))}
            for item in options.values()
        ),
        key=lambda item: item["name"].casefold(),
    )


def locations_match(selected, candidate, allow_partial=False):
    selected_key = doctor_search_key(selected)
    candidate_key = doctor_search_key(candidate)
    if not selected_key:
        return True
    if not candidate_key:
        return False
    if selected_key == candidate_key:
        return True
    return allow_partial and (selected_key in candidate_key or candidate_key in selected_key)


def build_doctor_groups(database_rows, selected_city="", selected_hospital="", hospital_renames=None):
    """Группирует распознанных на фотографиях врачей по городам и роддомам."""
    groups = {}
    hospital_renames = hospital_renames or {}

    def add(city, hospital, name):
        if not locations_match(selected_city, city):
            return
        if not locations_match(selected_hospital, hospital, allow_partial=True):
            return
        doctor_name = re.sub(r"\s+", " ", str(name or "")).strip()
        if not doctor_search_key(doctor_name):
            return
        group_city = selected_city or city or "Не указан"
        group_hospital = selected_hospital or hospital or "Не указан"
        group_key = (doctor_search_key(group_city), doctor_search_key(group_hospital))
        group = groups.setdefault(group_key, {
            "city": group_city,
            "hospital": group_hospital,
            "doctors": {},
        })
        group["doctors"].setdefault(doctor_search_key(doctor_name), doctor_name)

    for row in database_rows:
        add(
            row["city"],
            resolve_hospital_name(row["city"], row["maternity_hospital"], hospital_renames),
            row["doctor_name"],
        )

    result = []
    for group in groups.values():
        label = group["hospital"] if selected_city else f"{group['city']} · {group['hospital']}"
        result.append({
            "label": label,
            "city": group["city"],
            "hospital": group["hospital"],
            "doctors": sorted(group["doctors"].values(), key=str.casefold),
        })
    return sorted(result, key=lambda item: (item["city"].casefold(), item["hospital"].casefold()))


def database_revision():
    """Версия SQLite с учётом WAL, чтобы безопасно кэшировать справочники."""
    revision = [str(settings.BOT_DB_PATH)]
    for path in (
        settings.BOT_DB_PATH,
        settings.BOT_DB_PATH.with_name(settings.BOT_DB_PATH.name + "-wal"),
    ):
        try:
            stat = path.stat()
            revision.extend((stat.st_mtime_ns, stat.st_size))
        except OSError:
            revision.extend((None, None))
    return tuple(revision)


def load_dashboard_catalog(connection):
    """Кэширует редко меняющиеся списки городов, роддомов и врачей."""
    cache_key = database_revision()
    if _DASHBOARD_CATALOG_CACHE["key"] == cache_key:
        return _DASHBOARD_CATALOG_CACHE["value"]

    with _DASHBOARD_CATALOG_LOCK:
        if _DASHBOARD_CATALOG_CACHE["key"] == cache_key:
            return _DASHBOARD_CATALOG_CACHE["value"]

        hospital_renames = load_hospital_renames(connection)
        columns = photo_columns(connection)
        active_column = "is_active" in user_columns(connection)
        has_hospital_table = hospital_table_exists(connection)
        assigned_rows = list(connection.execute(
            """SELECT DISTINCT city, maternity_hospital FROM users
               WHERE role = 'Медик' AND maternity_hospital IS NOT NULL
                 AND TRIM(maternity_hospital) != ''"""
        ))
        if has_hospital_table:
            assigned_rows.extend(connection.execute(
                """SELECT DISTINCT eh.city, eh.maternity_hospital
                   FROM employee_hospitals AS eh
                   JOIN users AS u ON u.telegram_id = eh.telegram_id
                   WHERE u.role = 'Медик' AND eh.maternity_hospital IS NOT NULL
                     AND TRIM(eh.maternity_hospital) != ''"""
            ))

        assigned_hospitals = {}
        for row in assigned_rows:
            assigned_city = row["city"]
            assigned_hospital = resolve_hospital_name(
                assigned_city, row["maternity_hospital"], hospital_renames,
            )
            assigned_hospitals[
                (doctor_search_key(assigned_city), doctor_search_key(assigned_hospital))
            ] = {"city": assigned_city, "maternity_hospital": assigned_hospital}
        assigned_rows = list(assigned_hospitals.values())

        photo_hospital = (
            "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
            if "maternity_hospital" in columns else "sender.maternity_hospital"
        )
        doctor_rows = connection.execute(
            f"""SELECT DISTINCT TRIM(COALESCE(p.doctor_name, '')) AS doctor_name,
                       COALESCE(sender.city, 'Не указан') AS city,
                       COALESCE({photo_hospital}, 'Не указан') AS maternity_hospital
                FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                WHERE p.doctor_name IS NOT NULL AND TRIM(p.doctor_name) != ''
                ORDER BY doctor_name COLLATE NOCASE"""
        ).fetchall()
        database_doctors = []
        for row in doctor_rows:
            row_city = row["city"]
            row_hospital = resolve_hospital_name(
                row_city, row["maternity_hospital"], hospital_renames,
            )
            assigned = assigned_hospitals.get(
                (doctor_search_key(row_city), doctor_search_key(row_hospital))
            )
            if assigned:
                database_doctors.append({
                    "doctor_name": row["doctor_name"],
                    "city": assigned["city"],
                    "maternity_hospital": assigned["maternity_hospital"],
                })

        value = {
            "hospital_renames": hospital_renames,
            "columns": columns,
            "active_column": active_column,
            "has_hospital_table": has_hospital_table,
            "assigned_hospital_rows": assigned_rows,
            "database_doctors": database_doctors,
        }
        _DASHBOARD_CATALOG_CACHE.update(key=cache_key, value=value)
        return value


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


def return_to_photo_list(request):
    """После действия оставляет пользователя на тех же фильтрах списка."""
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("dashboard")


def replace_employee_hospitals(connection, telegram_id, city, hospitals):
    ensure_employee_hospitals_table(connection)
    connection.execute("DELETE FROM employee_hospitals WHERE telegram_id = ?", (telegram_id,))
    connection.executemany(
        "INSERT INTO employee_hospitals (telegram_id, city, maternity_hospital) VALUES (?, ?, ?)",
        [(telegram_id, city, hospital) for hospital in hospitals],
    )


def new_medic_code(connection):
    for _ in range(10000):
        code = f"{secrets.randbelow(10000):04d}"
        if not connection.execute("SELECT 1 FROM users WHERE medic_code = ?", (code,)).fetchone():
            return code
    raise RuntimeError("Не удалось подобрать свободный код медика.")


def employee_data(request):
    telegram_id = request.POST.get("telegram_id", "").strip()
    full_name = request.POST.get("full_name", "").strip()
    username = request.POST.get("username", "").strip().lstrip("@")
    city = request.POST.get("city", "").strip()
    hospitals = parse_hospitals(request.POST.get("maternity_hospital", ""))
    role = request.POST.get("role", "Медик")
    if not telegram_id.isdigit() or not full_name or not city or not hospitals or role not in {"Медик", "Оператор"}:
        return None
    return int(telegram_id), full_name, username, city, hospitals, role


def photo_review_group(photo):
    """Группирует результаты: красный — ИИ/монтаж, жёлтый — копия или сомнительный код."""
    review = (photo["review_status"] or "").casefold()
    code = (photo["code_status"] or "").casefold()
    if "ии-изображ" in review or "подделк" in review:
        return "red"
    if "коп" in review or "обрезк" in review:
        return "yellow"
    # Отсутствующий код разрешён, а распознанный код из правильного диапазона считается нормальным.
    # Несовпадение региона, неизвестный формат и выход за диапазон требуют внимания оператора.
    if code and not any(marker in code for marker in (
        "правильный код", "код распознан", "нет данных", "отсутствует", "проверка по базе пока выключена",
    )):
        return "yellow"
    if "не проверено" in review:
        return "yellow"
    if is_ai_normal_status(review):
        return "clear"
    return "clear"


def is_copy_or_crop_status(value):
    status = (value or "").casefold()
    return "коп" in status or "обрез" in status


def is_ai_normal_status(value):
    status = (value or "").casefold()
    return "нет явных признаков" in status or "нормаль" in status or "принято" in status


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


def photo_details(connection, photo_id):
    """Одна запись из всей базы, независимо от фильтров и лимита списка."""
    columns = photo_columns(connection)
    fields = ("photo_date", "package_number", "mother_name", "review_status", "review_reason",
              "image_digest", "qr_data", "original_photo_id", "code_status",
              "ai_generated_score")
    optional = ", ".join(f"p.{field}" if field in columns else f"NULL AS {field}" for field in fields)
    hospital = "COALESCE(p.maternity_hospital, sender.maternity_hospital)" if "maternity_hospital" in columns else "sender.maternity_hospital"
    row = connection.execute(
        f"""SELECT p.id, p.sent_at, p.sender_telegram_id, {optional},
                   COALESCE(p.doctor_name, '') AS doctor_name,
                   COALESCE(sender.full_name, 'Пользователь удалён') AS sender_name,
                   COALESCE(sender.city, 'Не указан') AS city,
                   COALESCE({hospital}, 'Не указан') AS maternity_hospital
            FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            WHERE p.id = ?""", (photo_id,),
    ).fetchone()
    if not row:
        return None
    photo = dict(row)
    photo["sent_at_display"] = format_sent_at(photo["sent_at"])
    return photo


def photo_details_for_operator(connection, photo_id):
    """Return one photo with the operator's effective decision, across old schemas too."""
    photo = photo_details(connection, photo_id)
    if not photo:
        return None

    check = None
    if database_table_exists(connection, "photo_checks"):
        check = connection.execute(
            """SELECT decision, decision_reason, manual_decision, manual_reason
               FROM photo_checks WHERE photo_id = ?""",
            (photo_id,),
        ).fetchone()

    automatic_decision = (
        check["decision"] if check and check["decision"] else DECISION_REVIEW
    )
    manual_decision = check["manual_decision"] if check else None
    effective_decision = manual_decision or automatic_decision or DECISION_REVIEW
    photo["decision"] = effective_decision
    photo["decision_label"] = DECISION_LABELS.get(effective_decision, "На проверке")
    photo["manual_decision"] = manual_decision
    photo["manual_reason"] = (check["manual_reason"] or "") if check else ""
    photo["decision_reason"] = (
        (check["manual_reason"] or check["decision_reason"] or "")
        if check else (photo.get("review_reason") or "")
    )
    photo["is_copy"] = is_copy_or_crop_status(photo.get("review_status"))
    score = photo.get("ai_generated_score")
    photo["ai_generated_percent"] = score * 100 if score is not None else None
    return photo


@login_required
def photo_lookup(request):
    """Open a photo card by the numeric ID shown on the operator site."""
    raw_photo_id = request.GET.get("photo_id", "")
    match = re.fullmatch(r"\s*(?:№|#)?\s*(\d+)\s*", raw_photo_id)
    if not match or int(match.group(1)) < 1:
        return render(request, "dashboard/photo_lookup_error.html", {
            "requested_photo_id": raw_photo_id,
            "lookup_error": "Введите положительный номер фотографии, например 8450.",
            "is_creator": is_creator(request.user),
        }, status=400)

    photo_id = int(match.group(1))
    connection = bot_database()
    if not connection:
        return render(request, "dashboard/photo_lookup_error.html", {
            "requested_photo_id": photo_id,
            "lookup_error": "База фотографий временно недоступна.",
            "is_creator": is_creator(request.user),
        }, status=503)
    try:
        exists = connection.execute(
            "SELECT 1 FROM photos WHERE id = ?", (photo_id,),
        ).fetchone()
    finally:
        connection.close()
    if not exists:
        return render(request, "dashboard/photo_lookup_error.html", {
            "requested_photo_id": photo_id,
            "lookup_error": f"Фотография №{photo_id} не найдена или была удалена.",
            "is_creator": is_creator(request.user),
        }, status=404)
    return redirect("photo_detail", photo_id=photo_id)


@login_required
def photo_detail(request, photo_id):
    """Show one complete photo card without dashboard filters or pagination."""
    connection = bot_database()
    if not connection:
        raise Http404
    try:
        photo = photo_details_for_operator(connection, photo_id)
    finally:
        connection.close()
    if not photo:
        raise Http404
    return render(request, "dashboard/photo_detail.html", {
        "photo": photo,
        "is_creator": is_creator(request.user),
    })


def legacy_original_id(connection, photo):
    """Восстанавливает старую связь только из сохранённых свидетельств, не из pHash."""
    if photo["image_digest"]:
        row = connection.execute(
            "SELECT id FROM photos WHERE image_digest = ? AND id < ? ORDER BY id LIMIT 1",
            (photo["image_digest"], photo["id"]),
        ).fetchone()
        if row:
            return row["id"]
    # Старый бот сохранял дату исходного снимка внутри причины проверки.
    match = re.search(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
        photo["review_reason"] or "",
    )
    if match:
        rows = connection.execute(
            "SELECT id FROM photos WHERE sent_at = ? AND id != ? ORDER BY id LIMIT 2",
            (match.group(0), photo["id"]),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    if photo["qr_data"]:
        row = connection.execute(
            "SELECT id FROM photos WHERE qr_data = ? AND id < ? ORDER BY id LIMIT 1",
            (photo["qr_data"], photo["id"]),
        ).fetchone()
        if row:
            return row["id"]
    return None


def resolve_original_photo(connection, photo):
    """Идёт по цепочке копий до оригинала; удалённые и циклические связи не подменяет."""
    seen = {photo["id"]}
    current = photo
    while True:
        original_id = current["original_photo_id"]
        if original_id is None:
            original_id = legacy_original_id(connection, current)
        if original_id is None or original_id in seen:
            return None
        seen.add(original_id)
        current = photo_details(connection, original_id)
        if not current:
            return None
        if not is_copy_or_crop_status(current["review_status"]):
            return current


@login_required
def photo_original(request, photo_id):
    connection = bot_database()
    if not connection:
        raise Http404
    try:
        photo = photo_details(connection, photo_id)
        if not photo or not is_copy_or_crop_status(photo["review_status"]):
            raise Http404
        original = resolve_original_photo(connection, photo)
    finally:
        connection.close()
    return render(request, "dashboard/photo_original.html", {
        "photo": photo, "original": original, "is_creator": is_creator(request.user),
    })


@login_required
def dashboard(request):
    creator_view = is_creator(request.user)
    connection = bot_database()
    if not connection:
        return render(request, "dashboard/dashboard.html", {
            "database_error": "Не найдена база medical_bot.db.",
            "is_creator": creator_view,
        })

    city = request.GET.get("city", "")
    hospital = request.GET.get("hospital", "")
    selected_medic = request.GET.get("medic", "").strip()
    if not selected_medic.isdigit():
        selected_medic = ""
    selected_doctor = request.GET.get("doctor", "").strip()[:200]
    if not doctor_search_key(selected_doctor):
        selected_doctor = ""
    selected_decision = request.GET.get("decision", "").strip().casefold()
    if selected_decision not in DECISION_LABELS:
        selected_decision = ""
    selected_date_from, selected_date_to = requested_period(request)
    _, full_report_date_from, full_report_date_to = full_report_period()
    requested_page = request.GET.get("page", "1").strip()
    photo_page = int(requested_page) if requested_page.isdigit() else 1
    photo_page = max(photo_page, 1)
    with connection:
        catalog = load_dashboard_catalog(connection)
        hospital_renames = catalog["hospital_renames"]
        if city and hospital:
            hospital = resolve_hospital_name(city, hospital, hospital_renames)
        assigned_hospital_rows = catalog["assigned_hospital_rows"]
        cities = sorted(
            {row["city"] for row in assigned_hospital_rows if row["city"]},
            key=str.casefold,
        )
        hospitals = set()
        if city:
            hospitals.update(
                row["maternity_hospital"] for row in assigned_hospital_rows
                if locations_match(city, row["city"])
            )
        hospitals = sorted(hospitals, key=str.casefold)
        report_hospital_options = sorted(
            assigned_hospital_rows,
            key=lambda row: (
                str(row["city"] or "").casefold(),
                str(row["maternity_hospital"] or "").casefold(),
            ),
        )
        columns = catalog["columns"]
        active_column = catalog["active_column"]
        package_number = "p.package_number" if "package_number" in columns else "NULL"
        review_status = "p.review_status" if "review_status" in columns else "'Не проверено'"
        review_reason = "p.review_reason" if "review_reason" in columns else "'Для старой записи нет оценки ИИ.'"
        ai_generated_score = "p.ai_generated_score * 100" if "ai_generated_score" in columns else "NULL"
        code_status = "p.code_status" if "code_status" in columns else "'Нет данных о коде'"
        qr_data = "p.qr_data" if "qr_data" in columns else "NULL"
        original_photo_id = "p.original_photo_id" if "original_photo_id" in columns else "NULL"
        has_photo_checks = database_table_exists(connection, "photo_checks")
        if has_photo_checks:
            checks_join = " LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id"
            effective_decision = f"COALESCE(NULLIF(checks.manual_decision, ''), NULLIF(checks.decision, ''), '{DECISION_REVIEW}')"
            decision_reason = f"COALESCE(NULLIF(checks.manual_reason, ''), checks.decision_reason, {review_reason}, '')"
            manual_decision = "NULLIF(checks.manual_decision, '')"
            manual_reason = "COALESCE(checks.manual_reason, '')"
            product_status = "COALESCE(checks.product_status, 'Не проверено')"
            material_status = "COALESCE(checks.material_status, 'Не проверено')"
            location_status = "COALESCE(checks.location_status, 'Не запрашивалась')"
            assignment_status = "COALESCE(checks.assignment_status, 'Задание не назначено')"
            duplicate_status = "COALESCE(checks.duplicate_status, 'Не проверено')"
            editing_status = "COALESCE(checks.editing_status, 'Не проверено')"
        else:
            checks_join = ""
            effective_decision = f"'{DECISION_REVIEW}'"
            decision_reason = review_reason
            manual_decision = "NULL"
            manual_reason = "''"
            product_status = material_status = duplicate_status = editing_status = "'Не проверено'"
            location_status = "'Не запрашивалась'"
            assignment_status = "'Задание не назначено'"
        photo_hospital = "COALESCE(p.maternity_hospital, sender.maternity_hospital)" if "maternity_hospital" in columns else "sender.maternity_hospital"
        database_doctors = catalog["database_doctors"]
        doctor_options = build_doctor_options(database_doctors, hospital_renames)
        doctor_groups = build_doctor_groups(database_doctors, city, hospital, hospital_renames)
        report_doctor_groups = build_doctor_groups(
            database_doctors, hospital_renames=hospital_renames,
        )
        query = f"""
            SELECT p.id, p.sent_at, p.photo_date, {package_number} AS package_number,
                   p.mother_name, COALESCE(p.doctor_name, '') AS doctor_name,
                   COALESCE(sender.full_name, 'Пользователь удалён') AS sender_name,
                   p.sender_telegram_id,
                   {review_status} AS review_status, {review_reason} AS review_reason,
                   {ai_generated_score} AS ai_generated_percent, {code_status} AS code_status,
                   {qr_data} AS qr_data,
                   {original_photo_id} AS original_photo_id,
                   {effective_decision} AS decision,
                   {decision_reason} AS decision_reason,
                   {manual_decision} AS manual_decision,
                   {manual_reason} AS manual_reason,
                   {product_status} AS product_status,
                   {material_status} AS material_status,
                   {location_status} AS location_status,
                   {assignment_status} AS assignment_status,
                   {duplicate_status} AS duplicate_status,
                   {editing_status} AS editing_status,
                   COALESCE(sender.city, 'Не указан') AS city,
                   COALESCE({photo_hospital}, 'Не указан') AS maternity_hospital
            FROM photos AS p
            LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            {checks_join}
        """
        # Город, роддом, врач и медик ограничивают все счётчики. Период
        # дополнительно применяется к списку, статистике и счётчику периода.
        scope_conditions, scope_parameters = [], []
        if city:
            scope_conditions.append("sender.city = ?")
            scope_parameters.append(city)
        if hospital:
            scope_conditions.append(f"{photo_hospital} = ?")
            scope_parameters.append(hospital)
        if selected_doctor:
            selected_doctor_key = doctor_search_key(selected_doctor)
            scope_conditions.append(doctor_search_condition())
            scope_parameters.extend([f"%{selected_doctor_key}%", selected_doctor_key])
        # Выпадающий список показывает только медиков, у которых уже есть фото
        # в выбранном городе/роддоме. Даты на список не влияют.
        medic_options = connection.execute(
            f"""SELECT DISTINCT p.sender_telegram_id AS telegram_id,
                       COALESCE(sender.full_name, 'Пользователь удалён') AS full_name
                FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                {(' WHERE ' + ' AND '.join(scope_conditions)) if scope_conditions else ''}
                ORDER BY full_name COLLATE NOCASE""",
            scope_parameters,
        ).fetchall()
        if scope_conditions:
            report_medic_options = connection.execute(
                """SELECT DISTINCT p.sender_telegram_id AS telegram_id,
                          COALESCE(sender.full_name, 'Пользователь удалён') AS full_name
                   FROM photos AS p
                   LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                   ORDER BY full_name COLLATE NOCASE"""
            ).fetchall()
        else:
            report_medic_options = medic_options
        if selected_medic:
            scope_conditions.append("p.sender_telegram_id = ?")
            scope_parameters.append(int(selected_medic))
        conditions, parameters = list(scope_conditions), list(scope_parameters)
        # ISO timestamps begin with YYYY-MM-DD, so direct range comparisons
        # preserve the previous DATE() behaviour while allowing SQLite to use
        # the sent_at index. DATE(p.sent_at) forced a full scan on every page.
        if selected_date_from:
            conditions.append("p.sent_at >= ?")
            parameters.append(selected_date_from)
        if selected_date_to:
            exclusive_date_to = (
                datetime.strptime(selected_date_to, "%Y-%m-%d").date()
                + timedelta(days=1)
            ).isoformat()
            conditions.append("p.sent_at < ?")
            parameters.append(exclusive_date_to)

        # Status cards always show the whole selected period.  Clicking one of
        # them adds the decision filter only to the list and period statistics.
        decision_count_conditions = list(conditions)
        decision_count_parameters = list(parameters)
        decision_count_where = (
            " WHERE " + " AND ".join(decision_count_conditions)
            if decision_count_conditions else ""
        )
        decision_rows = connection.execute(
            f"""SELECT {effective_decision} AS decision, COUNT(*) AS photo_count
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                {checks_join}
                {decision_count_where}
                GROUP BY {effective_decision}""",
            decision_count_parameters,
        ).fetchall()
        decision_counts = {key: 0 for key in DECISION_LABELS}
        for row in decision_rows:
            if row["decision"] in decision_counts:
                decision_counts[row["decision"]] = row["photo_count"]
        if selected_decision:
            conditions.append(f"{effective_decision} = ?")
            parameters.append(selected_decision)

        visit_count = (
            "COUNT(DISTINCT COALESCE(p.visit_id, -p.id))"
            if "visit_id" in columns else "COUNT(*)"
        )
        count_from = (
            "FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id"
            + checks_join
        )
        scope_where = (" WHERE " + " AND ".join(scope_conditions)) if scope_conditions else ""
        period_where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        scope_summary = connection.execute(
            f"""SELECT COUNT(*) AS photo_count, {visit_count} AS visit_count,
                       COUNT(DISTINCT p.sender_telegram_id) AS sender_count
                {count_from}{scope_where}""",
            scope_parameters,
        ).fetchone()
        summary = connection.execute(
            f"SELECT COUNT(*) AS photo_count, {visit_count} AS visit_count, "
            f"COUNT(DISTINCT p.sender_telegram_id) AS sender_count {count_from}{period_where}",
            parameters,
        ).fetchone()
        photo_page_count = max(
            1,
            (summary["photo_count"] + PHOTO_PAGE_SIZE - 1) // PHOTO_PAGE_SIZE,
        )
        photo_page = min(photo_page, photo_page_count)
        photo_offset = (photo_page - 1) * PHOTO_PAGE_SIZE
        medic_stats = connection.execute(
            f"""SELECT p.sender_telegram_id, COALESCE(sender.full_name, 'Пользователь удалён') AS medic_name,
                       COUNT(*) AS photo_count, {visit_count} AS visit_count,
                       SUM(CASE WHEN {effective_decision} = '{DECISION_ACCEPTED}' THEN 1 ELSE 0 END) AS accepted_count,
                       SUM(CASE WHEN {effective_decision} = '{DECISION_REVIEW}' THEN 1 ELSE 0 END) AS review_count,
                       SUM(CASE WHEN {effective_decision} = '{DECISION_REJECTED}' THEN 1 ELSE 0 END) AS rejected_count
                {count_from}{decision_count_where}
                GROUP BY p.sender_telegram_id, sender.full_name
                ORDER BY photo_count DESC, medic_name COLLATE NOCASE""",
            decision_count_parameters,
        ).fetchall()
        # Это именно число зарегистрированных активных медиков, а не только тех,
        # кто уже успел отправить фото. При выборе роддома учитываются назначения.
        medic_where, medic_parameters = ["u.role = 'Медик'"], []
        if active_column:
            medic_where.append("u.is_active = 1")
        if city:
            medic_where.append("u.city = ?")
            medic_parameters.append(city)
        medic_from = "FROM users AS u"
        if hospital and catalog["has_hospital_table"]:
            medic_from += " JOIN employee_hospitals AS eh ON eh.telegram_id = u.telegram_id"
            medic_where.append("eh.maternity_hospital = ?")
            medic_parameters.append(hospital)
        elif hospital:
            medic_where.append("u.maternity_hospital = ?")
            medic_parameters.append(hospital)
        medic_total = connection.execute(
            f"SELECT COUNT(DISTINCT u.telegram_id) AS medic_count {medic_from} WHERE " + " AND ".join(medic_where),
            medic_parameters,
        ).fetchone()
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY p.sent_at DESC LIMIT ? OFFSET ?"
        photos = [dict(row) for row in connection.execute(
            query,
            [*parameters, PHOTO_PAGE_SIZE, photo_offset],
        )]
    connection.close()

    photo_groups = {"clear": [], "yellow": [], "red": []}
    for photo in photos:
        photo["sent_at_display"] = format_sent_at(photo["sent_at"])
        photo["is_copy"] = is_copy_or_crop_status(photo["review_status"])
        photo["decision_label"] = DECISION_LABELS.get(photo["decision"], "На проверке")
        photo["manual_decision_label"] = DECISION_LABELS.get(photo["manual_decision"], "")
        photo_groups[photo_review_group(photo)].append(photo)

    page_query = request.GET.copy()
    page_query.pop("page", None)
    page_query = page_query.urlencode()
    decision_query = request.GET.copy()
    decision_query.pop("page", None)
    decision_query.pop("decision", None)
    decision_query = decision_query.urlencode()
    photo_page_start = photo_offset + 1 if summary["photo_count"] else 0
    photo_page_end = min(photo_offset + len(photos), summary["photo_count"])

    return render(request, "dashboard/dashboard.html", {
        "cities": cities, "hospitals": hospitals, "selected_city": city,
        "selected_hospital": hospital, "selected_date_from": selected_date_from,
        "selected_date_to": selected_date_to, "medics": medic_options,
        "selected_medic": selected_medic, "selected_doctor": selected_doctor,
        "selected_decision": selected_decision,
        "decision_labels": DECISION_LABELS,
        "decision_counts": decision_counts,
        "decision_query": decision_query,
        "doctor_options": doctor_options, "doctor_groups": doctor_groups,
        "report_hospital_options": report_hospital_options,
        "report_doctor_groups": report_doctor_groups,
        "report_medics": report_medic_options,
        "selected_medic_id": int(selected_medic) if selected_medic else None,
        "photos": photos, "photo_groups": photo_groups, "summary": summary,
        "scope_summary": scope_summary, "medic_stats": medic_stats, "medic_total": medic_total,
        "selected_period_label": format_selected_period(selected_date_from, selected_date_to),
        "full_report_date_from": full_report_date_from.isoformat(),
        "full_report_date_to": full_report_date_to.isoformat(),
        "photo_page": photo_page,
        "photo_page_count": photo_page_count,
        "photo_page_previous": photo_page - 1 if photo_page > 1 else None,
        "photo_page_next": photo_page + 1 if photo_page < photo_page_count else None,
        "photo_page_start": photo_page_start,
        "photo_page_end": photo_page_end,
        "page_query": page_query,
        "is_creator": creator_view,
        "return_to": request.get_full_path(),
    })


@login_required
def chat_history(request):
    """Show an authenticated operator a read-only Telegram chat history."""
    creator_view = is_creator(request.user)
    connection = bot_database()
    if not connection:
        return render(request, "dashboard/chat_history.html", {
            "database_error": "Не найдена база medical_bot.db.",
            "is_creator": creator_view,
        })
    try:
        if not database_table_exists(connection, "chat_messages"):
            return render(request, "dashboard/chat_history.html", {
                "chat_unavailable": True,
                "is_creator": creator_view,
            })
        conversations = [dict(row) for row in connection.execute(
            """SELECT cm.telegram_id,
                      COALESCE(NULLIF(TRIM(u.full_name), ''),
                               NULLIF(TRIM(MAX(cm.sender_name)), ''),
                               'Пользователь ' || cm.telegram_id) AS full_name,
                      COALESCE(NULLIF(TRIM(u.username), ''),
                               NULLIF(TRIM(MAX(cm.username)), ''), '') AS username,
                      COUNT(*) AS message_count,
                      MAX(cm.created_at) AS last_message_at
               FROM chat_messages AS cm
               LEFT JOIN users AS u ON u.telegram_id = cm.telegram_id
               GROUP BY cm.telegram_id, u.full_name, u.username
               ORDER BY julianday(last_message_at) DESC, cm.telegram_id"""
        )]
        requested_user = request.GET.get("telegram_id", "").strip()
        if requested_user.lstrip("-").isdigit():
            selected_telegram_id = int(requested_user)
        else:
            selected_telegram_id = conversations[0]["telegram_id"] if conversations else None
        selected_date_from = requested_day(request.GET.get("date_from", ""))
        selected_date_to = requested_day(request.GET.get("date_to", ""))
        conditions, parameters = [], []
        if selected_telegram_id is not None:
            conditions.append("cm.telegram_id = ?")
            parameters.append(selected_telegram_id)
        if selected_date_from:
            period_start, _ = local_day_bounds(selected_date_from)
            conditions.append("julianday(cm.created_at) >= julianday(?)")
            parameters.append(period_start)
        if selected_date_to:
            _, period_end = local_day_bounds(selected_date_to)
            conditions.append("julianday(cm.created_at) < julianday(?)")
            parameters.append(period_end)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        chat_messages = [dict(row) for row in connection.execute(
            f"""SELECT cm.*,
                       (SELECT p.id FROM photos AS p
                        WHERE p.telegram_file_id = cm.telegram_file_id
                          AND p.sender_telegram_id = cm.telegram_id
                        ORDER BY p.id DESC LIMIT 1) AS photo_id
                FROM chat_messages AS cm{where}
                ORDER BY julianday(cm.created_at) DESC, cm.id DESC
                LIMIT ?""",
            [*parameters, CHAT_HISTORY_LIMIT],
        )]
    finally:
        connection.close()
    chat_messages.reverse()
    type_labels = {
        "text": "Текст",
        "photo": "Фотография",
        "document": "Файл",
        "video": "Видео",
        "voice": "Голосовое сообщение",
        "audio": "Аудио",
        "sticker": "Стикер",
        "location": "Геолокация",
        "contact": "Контакт",
        "service": "Служебное сообщение",
    }
    for item in chat_messages:
        item["created_at_display"] = format_sent_at(item["created_at"])
        item["type_label"] = type_labels.get(item["message_type"], item["message_type"])
    selected_conversation = next(
        (
            conversation for conversation in conversations
            if conversation["telegram_id"] == selected_telegram_id
        ),
        None,
    )
    return render(request, "dashboard/chat_history.html", {
        "is_creator": creator_view,
        "conversations": conversations,
        "selected_telegram_id": selected_telegram_id,
        "selected_conversation": selected_conversation,
        "selected_date_from": selected_date_from,
        "selected_date_to": selected_date_to,
        "chat_messages": chat_messages,
        "chat_history_limit": CHAT_HISTORY_LIMIT,
    })


@creator_required
def management(request):
    """Отдельная страница управления, чтобы не замедлять просмотр фотографий."""
    connection = bot_database(read_only=False)
    if not connection:
        return render(request, "dashboard/management.html", {
            "database_error": "Не найдена база medical_bot.db.",
            "is_creator": True,
        })

    selected_city = request.GET.get("city", "").strip()[:200]
    selected_hospital = request.GET.get("hospital", "").strip()[:200]
    today = timezone.localtime(timezone.now(), timezone.get_default_timezone()).date()
    try:
        with connection:
            ensure_visit_assignments_table(connection)
            hospital_renames = load_hospital_renames(connection)
            if selected_city and selected_hospital:
                selected_hospital = resolve_hospital_name(
                    selected_city, selected_hospital, hospital_renames,
                )
            columns = photo_columns(connection)
            active_column = "is_active" in user_columns(connection)
            active_expression = "u.is_active" if active_column else "1"
            users = connection.execute(f"""
                SELECT u.telegram_id, u.full_name, u.username, u.city, u.maternity_hospital,
                       u.role, u.medic_code, {active_expression} AS is_active,
                       COALESCE(counts.photo_count, 0) AS photo_count
                FROM users AS u
                LEFT JOIN (
                    SELECT sender_telegram_id, COUNT(*) AS photo_count
                    FROM photos GROUP BY sender_telegram_id
                ) AS counts ON counts.sender_telegram_id = u.telegram_id
                ORDER BY is_active DESC, full_name COLLATE NOCASE
            """).fetchall()

            photo_hospital = (
                "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
                if "maternity_hospital" in columns else "sender.maternity_hospital"
            )
            delete_locations = {}
            for row in connection.execute(f"""
                SELECT DISTINCT COALESCE(sender.city, '') AS city,
                                COALESCE({photo_hospital}, '') AS maternity_hospital
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                WHERE sender.telegram_id IS NOT NULL
                  AND TRIM(COALESCE(sender.city, '')) != ''
                  AND TRIM(COALESCE({photo_hospital}, '')) != ''
            """):
                location_city = row["city"].strip()
                location_hospital = resolve_hospital_name(
                    location_city, row["maternity_hospital"].strip(), hospital_renames,
                )
                location_key = (
                    doctor_search_key(location_city), doctor_search_key(location_hospital),
                )
                delete_locations[location_key] = {
                    "city": location_city,
                    "hospital": location_hospital,
                    "value": encode_photo_location(location_city, location_hospital),
                }
            photo_delete_locations = sorted(
                delete_locations.values(),
                key=lambda item: (item["city"].casefold(), item["hospital"].casefold()),
            )
            assignments = connection.execute(
                """SELECT assignment.*, COALESCE(user.full_name, 'Все медики') AS medic_name
                   FROM visit_assignments AS assignment
                   LEFT JOIN users AS user ON user.telegram_id = assignment.medic_telegram_id
                   ORDER BY assignment.is_active DESC, assignment.id DESC"""
            ).fetchall()
    finally:
        connection.close()

    return render(request, "dashboard/management.html", {
        "selected_city": selected_city,
        "selected_hospital": selected_hospital,
        "today_date": today,
        "registered_users": users,
        "photo_delete_locations": photo_delete_locations,
        "assignments": assignments,
        "is_creator": True,
    })


@creator_required
@require_POST
def add_visit_assignment(request):
    city = re.sub(r"\s+", " ", request.POST.get("city", "")).strip()[:200]
    hospital = re.sub(r"\s+", " ", request.POST.get("maternity_hospital", "")).strip()[:200]
    doctor = re.sub(r"\s+", " ", request.POST.get("doctor_name", "")).strip()[:200]
    product = re.sub(r"\s+", " ", request.POST.get("required_product", "")).strip()[:100]
    material = re.sub(r"\s+", " ", request.POST.get("required_material", "")).strip()[:100]
    date_from = requested_day(request.POST.get("date_from", "").strip())
    date_to = requested_day(request.POST.get("date_to", "").strip())
    medic = request.POST.get("medic_telegram_id", "").strip()
    if not city or not hospital or (not product and not material):
        messages.error(request, "Укажите город, роддом и хотя бы продукт или материал задания.")
        return redirect("management")
    if date_from and date_to and date_from > date_to:
        messages.error(request, "Дата начала задания не может быть позже даты окончания.")
        return redirect("management")
    if medic and not medic.isdigit():
        messages.error(request, "Выберите медика из списка.")
        return redirect("management")
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_visit_assignments_table(connection)
        connection.execute(
            """INSERT INTO visit_assignments
               (medic_telegram_id, city, maternity_hospital, doctor_name,
                required_product, required_material, date_from, date_to, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                int(medic) if medic else None, city, hospital, doctor, product, material,
                date_from or None, date_to or None, timezone.now().isoformat(),
            ),
        )
    messages.success(request, "Задание добавлено. Оно применится к следующему подходящему визиту.")
    return redirect("management")


@creator_required
@require_POST
def delete_visit_assignment(request, assignment_id):
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_visit_assignments_table(connection)
        result = connection.execute(
            "DELETE FROM visit_assignments WHERE id = ?", (assignment_id,),
        )
    if not result.rowcount:
        raise Http404
    messages.success(request, "Задание удалено. Уже сохранённые результаты фотографий не изменены.")
    return redirect("management")


@creator_required
@require_POST
def disable_user(request, telegram_id):
    """Отключает доступ к боту, сохраняя историю отправленных фотографий."""
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_active_column(connection)
        connection.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    return redirect("dashboard")


@creator_required
@require_POST
def delete_employee_photos(request, telegram_id):
    """Удаляет все фотографии выбранного медика, не удаляя его профиль."""
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        employee = connection.execute(
            "SELECT full_name, role FROM users WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
        if not employee or employee["role"] != "Медик":
            raise Http404
        deleted_count = connection.execute(
            "DELETE FROM photos WHERE sender_telegram_id = ?", (telegram_id,),
        ).rowcount
    messages.success(
        request,
        f"У медика {employee['full_name']} удалено фотографий: {deleted_count}. Профиль медика сохранён.",
    )
    return redirect("dashboard")


@creator_required
@require_POST
def delete_employee(request, telegram_id):
    """Полностью удаляет профиль сотрудника и его доступ к боту, сохраняя историю фото."""
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_allowed_users_table(connection)
        ensure_employee_hospitals_table(connection)
        employee = connection.execute("SELECT full_name FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if not employee:
            raise Http404
        connection.execute("DELETE FROM allowed_users WHERE telegram_id = ?", (telegram_id,))
        connection.execute("DELETE FROM employee_hospitals WHERE telegram_id = ?", (telegram_id,))
        connection.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    messages.success(request, f"Сотрудник {employee['full_name']} полностью удалён. История фото сохранена.")
    return redirect("dashboard")


def logout_view(request):
    """Выход с сайта и немедленный переход на страницу входа."""
    logout(request)
    return redirect("login")


@login_required
@require_POST
def edit_photo_metadata(request, photo_id):
    """Save operator metadata without erasing a confirmed visual duplicate."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    raw_date = request.POST.get("photo_date", "").strip()
    mother_name = request.POST.get("mother_name", "").strip()
    raw_qr_data = request.POST.get("qr_data", "").strip()
    qr_data = canonical_qr_data(raw_qr_data)
    photo_date = requested_day(raw_date)
    if raw_date and not photo_date:
        error = "Дата должна быть указана в формате ГГГГ-ММ-ДД."
        if wants_json:
            return JsonResponse({"ok": False, "error": error}, status=400)
        messages.error(request, error)
        return return_to_photo_list(request)
    if qr_data and not QR_REGION_CODE_PATTERN.fullmatch(qr_data):
        error = "QR/Data Matrix должен иметь формат NN-NNNNNNNNNN, например 01-0000000123."
        if wants_json:
            return JsonResponse({"ok": False, "error": error}, status=400)
        messages.error(request, error)
        return return_to_photo_list(request)

    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        columns = photo_columns(connection)
        required = {
            "photo_date", "mother_name", "qr_data", "code_status",
            "review_status", "review_reason", "original_photo_id",
        }
        if not required.issubset(columns):
            error = "В базе бота нет нужных полей для редактирования. Обновите бот."
            if wants_json:
                return JsonResponse({"ok": False, "error": error}, status=409)
            messages.error(request, error)
            return return_to_photo_list(request)
        photo = connection.execute(
            """SELECT p.review_status, p.review_reason, p.original_photo_id,
                      COALESCE(
                          (SELECT eh.city
                           FROM employee_hospitals AS eh
                           WHERE eh.telegram_id = p.sender_telegram_id
                             AND eh.maternity_hospital = p.maternity_hospital
                           ORDER BY eh.city COLLATE NOCASE
                           LIMIT 1),
                          sender.city,
                          ''
                      ) AS city
               FROM photos AS p
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.id = ?""",
            (photo_id,),
        ).fetchone()
        if not photo:
            if wants_json:
                return JsonResponse({"ok": False, "error": "Фотография не найдена."}, status=404)
            raise Http404

        duplicate = None
        duplicate_basis = ""
        if qr_data:
            duplicate = connection.execute(
                """SELECT id, sent_at FROM photos
                   WHERE id < ? AND qr_data = ? ORDER BY id LIMIT 1""",
                (photo_id, qr_data),
            ).fetchone()
            if duplicate:
                duplicate_basis = f"Такой же QR-код есть у фотографии от {duplicate['sent_at']}."
        name_key = duplicate_name_key(mother_name)
        if not duplicate and photo_date and name_key:
            candidates = connection.execute(
                """SELECT id, sent_at, mother_name FROM photos
                   WHERE id < ? AND photo_date = ?
                     AND mother_name IS NOT NULL AND TRIM(mother_name) != ''
                   ORDER BY id""",
                (photo_id, photo_date),
            ).fetchall()
            duplicate = next(
                (row for row in candidates
                 if duplicate_name_key(row["mother_name"]) == name_key),
                None,
            )
            if duplicate:
                duplicate_basis = (
                    "Точно совпали одновременно ФИО роженицы и дата с фотографией от "
                    f"{duplicate['sent_at']}."
                )

        current_status = str(photo["review_status"] or "")
        current_reason = str(photo["review_reason"] or "")
        confirmed_visual_duplicate = bool(photo["original_photo_id"]) and (
            current_reason.startswith("Усиленная проверка обнаружила")
            or current_reason.startswith("Повторно отправлен тот же файл")
        )
        if duplicate:
            original_photo_id = duplicate["id"]
            review_status = "Возможная копия"
            review_reason = duplicate_basis
        elif confirmed_visual_duplicate:
            original_photo_id = photo["original_photo_id"]
            review_status = current_status
            review_reason = current_reason
        else:
            original_photo_id = None
            if (photo["original_photo_id"] or "коп" in current_status.casefold()
                    or "обрез" in current_status.casefold()):
                review_status = "Не проверено"
                review_reason = (
                    "Совпадение QR или ФИО+даты не найдено; усиленная визуальная "
                    "проверка этой фотографии будет выполнена отдельно."
                )
            else:
                review_status = current_status
                review_reason = current_reason

        code_status = qr_code_status(qr_data, photo["city"])
        result = connection.execute(
            """UPDATE photos
               SET photo_date = ?, mother_name = ?, qr_data = ?, code_status = ?,
                   original_photo_id = ?, review_status = ?, review_reason = ?
               WHERE id = ?""",
            (
                photo_date or None, mother_name or None, qr_data, code_status,
                original_photo_id, review_status, review_reason, photo_id,
            ),
        )
        ensure_photo_checks_table(connection)
        automatic = automatic_photo_decision(
            review_status, code_status, original_photo_id=original_photo_id,
        )
        connection.execute(
            """INSERT OR IGNORE INTO photo_checks
               (photo_id, product_status, material_status, location_status,
                duplicate_status, editing_status, assignment_status, decision,
                decision_reason, automated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo_id, automatic["product_status"], automatic["material_status"],
                automatic["location_status"], automatic["duplicate_status"],
                automatic["editing_status"], automatic["assignment_status"],
                automatic["decision"], automatic["decision_reason"],
                timezone.now().isoformat(),
            ),
        )
        connection.execute(
            """UPDATE photo_checks
               SET duplicate_status = ?, automated_at = ? WHERE photo_id = ?""",
            (
                "Возможный повтор" if duplicate else "Повтор не обнаружен",
                timezone.now().isoformat(), photo_id,
            ),
        )
    if not result.rowcount:
        if wants_json:
            return JsonResponse({"ok": False, "error": "Фотография не найдена."}, status=404)
        raise Http404
    message = "Дата, ФИО и QR/Data Matrix сохранены."
    if duplicate:
        message += " Обнаружена копия по разрешённому правилу."
    if wants_json:
        return JsonResponse({
            "ok": True,
            "photo_id": photo_id,
            "photo_date": photo_date,
            "mother_name": mother_name,
            "qr_data": qr_data or "",
            "code_status": code_status,
            "review_status": review_status,
            "review_reason": review_reason,
            "original_photo_id": original_photo_id,
            "message": message,
        })
    messages.success(request, message)
    return return_to_photo_list(request)


@login_required
@require_POST
def set_photo_decision(request, photo_id):
    """Let any authenticated site operator save a human decision."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    decision = request.POST.get("decision", "").strip().casefold()
    reason = request.POST.get("reason", "").strip()[:500]
    if decision not in DECISION_LABELS:
        if wants_json:
            return JsonResponse(
                {"ok": False, "error": "Выберите корректный итог проверки фотографии."},
                status=400,
            )
        messages.error(request, "Выберите корректный итог проверки фотографии.")
        return return_to_photo_list(request)
    if decision == DECISION_REJECTED and not reason:
        if wants_json:
            return JsonResponse(
                {"ok": False, "error": "Для отклонения укажите причину."},
                status=400,
            )
        messages.error(request, "Для отклонения укажите причину.")
        return return_to_photo_list(request)

    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        columns = photo_columns(connection)
        review_status = "review_status" if "review_status" in columns else "NULL AS review_status"
        code_status = "code_status" if "code_status" in columns else "NULL AS code_status"
        original_photo_id = (
            "original_photo_id" if "original_photo_id" in columns else "NULL AS original_photo_id"
        )
        photo = connection.execute(
            f"SELECT id, {review_status}, {code_status}, {original_photo_id} "
            "FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
        if not photo:
            raise Http404
        ensure_photo_checks_table(connection)
        automatic = automatic_photo_decision(
            photo["review_status"],
            photo["code_status"],
            original_photo_id=photo["original_photo_id"],
        )
        connection.execute(
            """INSERT OR IGNORE INTO photo_checks
               (photo_id, product_status, material_status, location_status,
                duplicate_status, editing_status, assignment_status, decision,
                decision_reason, automated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo_id, automatic["product_status"], automatic["material_status"],
                automatic["location_status"], automatic["duplicate_status"],
                automatic["editing_status"], automatic["assignment_status"],
                automatic["decision"], automatic["decision_reason"],
                timezone.now().isoformat(),
            ),
        )
        current_check = connection.execute(
            "SELECT manual_decision FROM photo_checks WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()
        previous_decision = (
            str(current_check["manual_decision"] or "").strip().casefold()
            if current_check else ""
        )
        if previous_decision not in DECISION_LABELS:
            previous_decision = DECISION_REVIEW
        connection.execute(
            """UPDATE photo_checks
               SET manual_decision = ?, manual_reason = ?, manual_reviewer = ?, manual_at = ?
               WHERE photo_id = ?""",
            (decision, reason, request.user.username, timezone.now().isoformat(), photo_id),
        )
    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "photo_id": photo_id,
                "decision": decision,
                "decision_label": DECISION_LABELS[decision],
                "previous_decision": previous_decision,
                "reason": reason,
            }
        )
    messages.success(request, f"Итог фотографии: {DECISION_LABELS[decision]}.")
    return return_to_photo_list(request)


@login_required
@require_POST
def delete_photo(request, photo_id):
    """Полностью удаляет одну фотографию и её данные из базы бота."""
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        if database_table_exists(connection, "photo_checks"):
            connection.execute("DELETE FROM photo_checks WHERE photo_id = ?", (photo_id,))
        result = connection.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    if not result.rowcount:
        raise Http404
    messages.success(request, "Фотография полностью удалена из базы.")
    return return_to_photo_list(request)


@creator_required
@require_POST
def delete_filtered_photos(request):
    """Удаляет фото только при совпадении сотрудника, роддома и локальной даты отправки."""
    telegram_id = request.POST.get("telegram_id", "").strip()
    location = decode_photo_location(request.POST.get("location", ""))
    sent_date = requested_day(request.POST.get("sent_date", "").strip())
    bounds = local_day_bounds(sent_date)
    if not telegram_id.isdigit() or not location or not bounds:
        messages.error(request, "Выберите сотрудника, роддом и корректную дату отправки.")
        return redirect("dashboard")

    city, hospital = location
    start_at, end_at = bounds
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        employee = connection.execute(
            "SELECT full_name FROM users WHERE telegram_id = ?", (int(telegram_id),),
        ).fetchone()
        if not employee:
            raise Http404
        columns = photo_columns(connection)
        photo_hospital = (
            "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
            if "maternity_hospital" in columns else "sender.maternity_hospital"
        )
        deleted_count = connection.execute(
            f"""DELETE FROM photos WHERE id IN (
                    SELECT p.id FROM photos AS p
                    LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                    WHERE p.sender_telegram_id = ?
                      AND DOCTOR_SEARCH_KEY(COALESCE(sender.city, '')) = ?
                      AND DOCTOR_SEARCH_KEY(COALESCE({photo_hospital}, '')) = ?
                      AND julianday(p.sent_at) >= julianday(?)
                      AND julianday(p.sent_at) < julianday(?)
                )""",
            (
                int(telegram_id), doctor_search_key(city), doctor_search_key(hospital),
                start_at, end_at,
            ),
        ).rowcount

    display_date = datetime.strptime(sent_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    messages.success(
        request,
        f"Удалено фотографий: {deleted_count}. Сотрудник: {employee['full_name']}; "
        f"роддом: {hospital}; дата отправки: {display_date}. Профиль сотрудника сохранён.",
    )
    return redirect("dashboard")


@creator_required
@require_POST
def rename_hospital(request):
    """Переименовывает роддом и переносит связанные фото без создания дублей."""
    city = re.sub(r"\s+", " ", request.POST.get("city", "")).strip()[:200]
    old_name = re.sub(r"\s+", " ", request.POST.get("old_name", "")).strip()[:200]
    new_name = re.sub(r"\s+", " ", request.POST.get("new_name", "")).strip()[:200]
    city_key = doctor_search_key(city)
    old_key = doctor_search_key(old_name)
    new_key = doctor_search_key(new_name)
    if not city_key or not old_key or not new_key:
        messages.error(request, "Укажите город, текущее и новое название роддома.")
        return redirect("dashboard")
    if old_key == new_key:
        messages.error(request, "Новое название совпадает с текущим.")
        return redirect("dashboard")

    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_employee_hospitals_table(connection)
        ensure_hospital_renames_table(connection)
        columns = photo_columns(connection)

        user_matches = connection.execute(
            """SELECT COUNT(*) FROM users
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(COALESCE(maternity_hospital, '')) = ?""",
            (city_key, old_key),
        ).fetchone()[0]
        assignment_matches = connection.execute(
            """SELECT COUNT(*) FROM employee_hospitals
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(maternity_hospital) = ?""",
            (city_key, old_key),
        ).fetchone()[0]
        photo_matches = 0
        if "maternity_hospital" in columns:
            photo_matches = connection.execute(
                """SELECT COUNT(*) FROM photos AS p
                   JOIN users AS u ON u.telegram_id = p.sender_telegram_id
                   WHERE DOCTOR_SEARCH_KEY(u.city) = ?
                     AND DOCTOR_SEARCH_KEY(COALESCE(p.maternity_hospital, '')) = ?""",
                (city_key, old_key),
            ).fetchone()[0]
        if not (user_matches or assignment_matches or photo_matches):
            messages.error(request, f"Роддом «{old_name}» в городе «{city}» не найден.")
            return redirect("dashboard")

        # Если такое название не используется в других городах, можно безопасно
        # перенести и фотографии удалённых сотрудников, у которых город уже неизвестен.
        hospital_cities = {
            doctor_search_key(row[0])
            for row in connection.execute(
                """SELECT city FROM users
                   WHERE DOCTOR_SEARCH_KEY(COALESCE(maternity_hospital, '')) = ?
                   UNION
                   SELECT city FROM employee_hospitals
                   WHERE DOCTOR_SEARCH_KEY(maternity_hospital) = ?""",
                (old_key, old_key),
            ) if row[0]
        }
        moved_photos = 0
        if "maternity_hospital" in columns:
            if hospital_cities.issubset({city_key}):
                result = connection.execute(
                    """UPDATE photos SET maternity_hospital = ?
                       WHERE DOCTOR_SEARCH_KEY(COALESCE(maternity_hospital, '')) = ?""",
                    (new_name, old_key),
                )
            else:
                result = connection.execute(
                    """UPDATE photos SET maternity_hospital = ?
                       WHERE DOCTOR_SEARCH_KEY(COALESCE(maternity_hospital, '')) = ?
                         AND sender_telegram_id IN (
                             SELECT telegram_id FROM users WHERE DOCTOR_SEARCH_KEY(city) = ?
                         )""",
                    (new_name, old_key, city_key),
                )
            moved_photos = result.rowcount

        if database_table_exists(connection, "visit_assignments"):
            connection.execute(
                """UPDATE visit_assignments SET maternity_hospital = ?
                   WHERE DOCTOR_SEARCH_KEY(city) = ?
                     AND DOCTOR_SEARCH_KEY(maternity_hospital) = ?""",
                (new_name, city_key, old_key),
            )

        assignments = connection.execute(
            """SELECT telegram_id, city FROM employee_hospitals
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(maternity_hospital) = ?""",
            (city_key, old_key),
        ).fetchall()
        connection.executemany(
            """INSERT OR IGNORE INTO employee_hospitals (telegram_id, city, maternity_hospital)
               VALUES (?, ?, ?)""",
            [(row["telegram_id"], row["city"], new_name) for row in assignments],
        )
        connection.execute(
            """DELETE FROM employee_hospitals
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(maternity_hospital) = ?""",
            (city_key, old_key),
        )
        updated_users = connection.execute(
            """UPDATE users SET maternity_hospital = ?
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(COALESCE(maternity_hospital, '')) = ?""",
            (new_name, city_key, old_key),
        ).rowcount

        # Сохраняем псевдоним, чтобы старые ссылки и цепочки переименований
        # продолжали открывать роддом под его новым названием.
        connection.execute(
            """UPDATE hospital_renames SET new_name = ?
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(new_name) = ?""",
            (new_name, city_key, old_key),
        )
        connection.execute(
            """DELETE FROM hospital_renames
               WHERE DOCTOR_SEARCH_KEY(city) = ?
                 AND DOCTOR_SEARCH_KEY(old_name) = ?""",
            (city_key, old_key),
        )
        connection.execute(
            """INSERT INTO hospital_renames (city, old_name, new_name, renamed_at)
               VALUES (?, ?, ?, ?)""",
            (city, old_name, new_name, datetime.now(datetime_timezone.utc).isoformat()),
        )

    messages.success(
        request,
        f"Роддом «{old_name}» переименован в «{new_name}». "
        f"Перенесено фотографий: {moved_photos}; обновлено сотрудников: {updated_users}.",
    )
    return redirect("dashboard")


@creator_required
@require_POST
def add_employee(request):
    data = employee_data(request)
    if not data:
        messages.error(request, "Заполните Telegram ID, ФИО, город, роддом и роль.")
        return redirect("dashboard")
    telegram_id, full_name, username, city, hospitals, role = data
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_active_column(connection)
        ensure_allowed_users_table(connection)
        ensure_employee_hospitals_table(connection)
        existing = connection.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existing:
            messages.error(request, "Сотрудник с таким Telegram ID уже существует. Используйте «Изменить».")
            return redirect("dashboard")
        medic_code = new_medic_code(connection) if role == "Медик" else None
        connection.execute(
            """INSERT INTO users
               (telegram_id, username, full_name, city, district, maternity_hospital, role, medic_code, is_active, registered_at)
               VALUES (?, ?, ?, ?, '-', ?, ?, ?, 1, datetime('now'))""",
            (telegram_id, username or None, full_name, city, hospitals[0], role, medic_code),
        )
        replace_employee_hospitals(connection, telegram_id, city, hospitals)
        connection.execute(
            "INSERT OR REPLACE INTO allowed_users (telegram_id, added_by, added_at) VALUES (?, ?, datetime('now'))",
            (telegram_id, request.user.id),
        )
    messages.success(request, f"Сотрудник добавлен. Telegram ID {telegram_id} получил доступ к боту.")
    return redirect("dashboard")


@creator_required
def edit_employee(request, telegram_id):
    connection = bot_database(read_only=False)
    if not connection:
        raise Http404
    with connection:
        ensure_active_column(connection)
        ensure_allowed_users_table(connection)
        ensure_employee_hospitals_table(connection)
        employee = connection.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if not employee:
            raise Http404
        if request.method == "POST":
            data = employee_data(request)
            if not data or data[0] != telegram_id:
                messages.error(request, "Проверьте заполнение полей. Telegram ID менять нельзя.")
                return redirect("edit_employee", telegram_id=telegram_id)
            _, full_name, username, city, hospitals, role = data
            active = 1 if request.POST.get("is_active") else 0
            medic_code = employee["medic_code"] if role == "Медик" else None
            if role == "Медик" and not medic_code:
                medic_code = new_medic_code(connection)
            connection.execute(
                """UPDATE users SET username=?, full_name=?, city=?, maternity_hospital=?,
                   role=?, medic_code=?, is_active=? WHERE telegram_id=?""",
                (username or None, full_name, city, hospitals[0], role, medic_code, active, telegram_id),
            )
            replace_employee_hospitals(connection, telegram_id, city, hospitals)
            if active:
                connection.execute(
                    "INSERT OR REPLACE INTO allowed_users (telegram_id, added_by, added_at) VALUES (?, ?, datetime('now'))",
                    (telegram_id, request.user.id),
                )
            else:
                connection.execute("DELETE FROM allowed_users WHERE telegram_id = ?", (telegram_id,))
            messages.success(request, "Данные сотрудника сохранены.")
            return redirect("dashboard")
        assigned_hospitals = [row["maternity_hospital"] for row in connection.execute(
            "SELECT maternity_hospital FROM employee_hospitals WHERE telegram_id = ? ORDER BY maternity_hospital", (telegram_id,)
        )]
    return render(request, "dashboard/employee_form.html", {
        "employee": employee,
        "assigned_hospitals": ", ".join(assigned_hospitals) or employee["maternity_hospital"],
    })


@creator_required
@require_POST
def add_site_operator(request):
    """Создаёт обычный сайт-аккаунт без доступа к Django admin и данным сотрудников."""
    username = request.POST.get("site_username", "").strip()
    password = request.POST.get("site_password", "")
    if len(username) < 3 or len(password) < 8:
        messages.error(request, "Логин должен быть не короче 3 символов, пароль — не короче 8.")
        return redirect("dashboard")
    if username == settings.CREATOR_DJANGO_USERNAME:
        messages.error(request, "Этот логин зарезервирован для создателя.")
        return redirect("dashboard")
    user_model = get_user_model()
    if user_model.objects.filter(username=username).exists():
        messages.error(request, "Такой логин сайта уже существует.")
        return redirect("dashboard")
    user_model.objects.create_user(username=username, password=password, is_staff=False, is_superuser=False)
    messages.success(request, f"Создан доступ оператора к сайту: {username}.")
    return redirect("dashboard")


def full_ten_day_report_data(connection, now=None, date_from=None, date_to=None):
    """Сводные данные за выбранный период; по умолчанию — последние десять дней."""
    local_timezone = timezone.get_default_timezone()
    local_now, date_from, date_to = full_report_period(now, date_from, date_to)
    period_start = timezone.make_aware(
        datetime.combine(date_from, datetime.min.time()), local_timezone,
    )
    period_end = timezone.make_aware(
        datetime.combine(date_to + timedelta(days=1), datetime.min.time()), local_timezone,
    )

    columns = photo_columns(connection)
    photo_hospital = (
        "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
        if "maternity_hospital" in columns else "sender.maternity_hospital"
    )
    review_status = "p.review_status" if "review_status" in columns else "''"
    records = connection.execute(
        f"""SELECT p.sent_at, COALESCE(p.doctor_name, '') AS doctor_name,
                   COALESCE(sender.city, 'Не указан') AS city,
                   COALESCE({photo_hospital}, 'Не указан') AS maternity_hospital,
                   {review_status} AS review_status
            FROM photos AS p
            LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            JOIN photo_checks AS checks ON checks.photo_id = p.id
            WHERE julianday(p.sent_at) >= julianday(?)
              AND julianday(p.sent_at) < julianday(?)
              AND checks.manual_decision = 'accepted'
            ORDER BY p.sent_at, p.id""",
        (period_start.isoformat(), period_end.isoformat()),
    ).fetchall()

    hospital_renames = load_hospital_renames(connection)
    day_counts = {
        (date_from + timedelta(days=offset)): {"photo_count": 0, "copy_count": 0}
        for offset in range((date_to - date_from).days + 1)
    }
    doctors = {}
    for record in records:
        local_date = sent_at_local_date(record["sent_at"])
        if local_date not in day_counts:
            continue
        is_copy = is_copy_or_crop_status(record["review_status"])
        day_counts[local_date]["photo_count"] += 1
        day_counts[local_date]["copy_count"] += int(is_copy)

        doctor_name = str(record["doctor_name"] or "").strip() or "Без указанного врача"
        city = str(record["city"] or "Не указан").strip() or "Не указан"
        hospital = resolve_hospital_name(
            city,
            str(record["maternity_hospital"] or "Не указан").strip() or "Не указан",
            hospital_renames,
        )
        group = doctors.setdefault((doctor_name, city), {
            "doctor_name": doctor_name,
            "city": city,
            "hospitals": set(),
            "photo_count": 0,
            "copy_count": 0,
        })
        group["hospitals"].add(hospital)
        group["photo_count"] += 1
        group["copy_count"] += int(is_copy)

    doctor_rows = []
    for group in doctors.values():
        doctor_rows.append({
            "doctor_name": group["doctor_name"],
            "city": group["city"],
            "maternity_hospital": "; ".join(sorted(group["hospitals"], key=str.casefold)),
            "photo_count": group["photo_count"],
            "copy_count": group["copy_count"],
        })
    doctor_rows.sort(key=lambda item: (item["doctor_name"].casefold(), item["city"].casefold()))

    daily_rows = [
        {"date": day, **counts}
        for day, counts in day_counts.items()
    ]
    total_photos = sum(item["photo_count"] for item in daily_rows)
    total_copies = sum(item["copy_count"] for item in daily_rows)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": local_now,
        "daily_rows": daily_rows,
        "doctor_rows": doctor_rows,
        "total_photos": total_photos,
        "total_copies": total_copies,
        "total_amount": total_photos * PHOTO_PAYMENT_RATE_SUM,
    }


def build_full_ten_day_report(report_data):
    """Создаёт полный сводный Excel в формате предоставленного образца."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сводка"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A4"

    dark_blue = "1F4E78"
    light_blue = "D9EAF7"
    pale_blue = "EAF3F8"
    table_blue = "5B9BD5"
    light_gray = "F2F2F2"
    pale_green = "E2F0D9"
    pale_yellow = "FFF2CC"
    dark_text = "1F1F1F"
    green_text = "375623"
    note_text = "7F6000"
    border_side = Side(style="thin", color="D9E2F3")
    row_border = Border(bottom=border_side)

    for column, width in {
        "A": 16, "B": 24, "C": 22, "D": 18,
        "E": 31, "F": 25, "G": 25, "H": 18,
    }.items():
        sheet.column_dimensions[column].width = width

    def merged_banner(row, value, fill, font, height=None):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        sheet.cell(row, 1, value)
        if height:
            sheet.row_dimensions[row].height = height
        for cell in sheet[row][:8]:
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.font = font
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    generated_at = report_data["generated_at"].strftime("%d.%m.%Y, %H:%M:%S")
    merged_banner(
        1, "Отчёт по фотографиям и начислениям врачам", dark_blue,
        Font(name="Calibri", size=18, bold=True, color="FFFFFF"), 30,
    )
    merged_banner(
        2, f"Период: {date_from}–{date_to} · только фотографии, принятые оператором", light_blue,
        Font(name="Calibri", size=11, bold=True, color=dark_blue), 22,
    )
    merged_banner(
        3, f"Снимок данных сформирован {generated_at} по местному времени", pale_blue,
        Font(name="Calibri", size=9, italic=True, color="595959"),
    )

    sheet["A5"] = "Ставка за 1 фотографию"
    sheet["B5"] = PHOTO_PAYMENT_RATE_SUM
    sheet["A6"] = "Фотографий всего"
    sheet["A7"] = "Начислено всего"
    sheet["A8"] = "Возможные копии"
    sheet["A9"] = "Файлов врачей"
    for row in range(5, 10):
        sheet.cell(row, 1).fill = PatternFill("solid", fgColor=pale_green if row == 7 else light_gray)
        sheet.cell(row, 1).font = Font(
            name="Calibri", size=12 if row == 7 else 11, bold=True,
            color=green_text if row == 7 else dark_text,
        )
        sheet.cell(row, 1).alignment = Alignment(vertical="center", wrap_text=True)
        sheet.cell(row, 2).alignment = Alignment(horizontal="right", vertical="center")
        sheet.cell(row, 2).border = row_border
    sheet["B5"].font = Font(name="Calibri", size=11, bold=True, color="0000FF")
    sheet["B5"].number_format = '#,##0 "сум"'
    sheet["B6"] = report_data["total_photos"]
    sheet["B8"] = report_data["total_copies"]
    sheet["B9"] = len(report_data["doctor_rows"])
    for address in ("B6", "B8", "B9"):
        sheet[address].font = Font(name="Calibri", size=10, color=dark_text)
        sheet[address].number_format = "#,##0"
    sheet["B7"] = report_data["total_amount"]
    for address in ("A7", "B7"):
        sheet[address].fill = PatternFill("solid", fgColor=pale_green)
        sheet[address].font = Font(name="Calibri", size=12, bold=True, color=green_text)
    sheet["B7"].number_format = '#,##0 "сум"'

    merged_banner(
        10,
        "Расчёт включает только фотографии, которым оператор вручную поставил статус «Принято». "
        "Фотографии на проверке и отклонённые не включены в количество и оплату.",
        pale_yellow, Font(name="Calibri", size=10, color=note_text), 36,
    )

    daily_header_row = 12
    daily_start_row = daily_header_row + 1
    daily_headers = ["Дата", "Фотографий", "Возможные копии", "Начислено, сум"]
    for column, value in enumerate(daily_headers, start=1):
        cell = sheet.cell(daily_header_row, column, value)
        cell.fill = PatternFill("solid", fgColor=table_blue)
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[daily_header_row].height = 28

    for offset, record in enumerate(report_data["daily_rows"]):
        row = daily_start_row + offset
        sheet.cell(row, 1, record["date"])
        sheet.cell(row, 2, record["photo_count"])
        sheet.cell(row, 3, record["copy_count"])
        sheet.cell(row, 4, record["photo_count"] * PHOTO_PAYMENT_RATE_SUM)
        for cell in sheet[row][:4]:
            cell.font = Font(name="Calibri", size=10, color=dark_text)
            cell.alignment = Alignment(vertical="center")
            cell.border = row_border
        sheet.cell(row, 1).number_format = "dd/mm/yyyy"
        sheet.cell(row, 2).number_format = "#,##0"
        sheet.cell(row, 3).number_format = "#,##0"
        sheet.cell(row, 4).number_format = '#,##0 "сум"'

    daily_total_row = daily_start_row + len(report_data["daily_rows"])
    sheet.cell(daily_total_row, 1, "Итого")
    sheet.cell(daily_total_row, 2, report_data["total_photos"])
    sheet.cell(daily_total_row, 3, report_data["total_copies"])
    sheet.cell(daily_total_row, 4, report_data["total_amount"])
    for cell in sheet[daily_total_row][:4]:
        cell.fill = PatternFill("solid", fgColor=pale_green)
        cell.font = Font(name="Calibri", size=11, bold=True, color=green_text)
        cell.alignment = Alignment(vertical="center")
    sheet.cell(daily_total_row, 2).number_format = "#,##0"
    sheet.cell(daily_total_row, 3).number_format = "#,##0"
    sheet.cell(daily_total_row, 4).number_format = '#,##0 "сум"'

    doctor_header_row = daily_total_row + 3
    doctor_start_row = doctor_header_row + 1
    doctor_headers = [
        "№", "Врач (как записано в базе)", "Регион", "Роддом",
        "Фотографий", "Возможные копии", "Ставка, сум", "Начислено, сум",
    ]
    for column, value in enumerate(doctor_headers, start=1):
        cell = sheet.cell(doctor_header_row, column, value)
        cell.fill = PatternFill("solid", fgColor=table_blue)
        cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[doctor_header_row].height = 28

    for offset, record in enumerate(report_data["doctor_rows"], start=1):
        row = doctor_start_row + offset - 1
        values = [
            offset, record["doctor_name"], record["city"], record["maternity_hospital"],
            record["photo_count"], record["copy_count"], PHOTO_PAYMENT_RATE_SUM,
            record["photo_count"] * PHOTO_PAYMENT_RATE_SUM,
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row, column, value)
            cell.font = Font(name="Calibri", size=10, color=dark_text)
            cell.alignment = Alignment(
                horizontal="center" if column == 1 else None,
                vertical="center", wrap_text=column in (2, 3, 4),
            )
            cell.border = row_border
        for column in (5, 6):
            sheet.cell(row, column).number_format = "#,##0"
        for column in (7, 8):
            sheet.cell(row, column).number_format = '#,##0 "сум"'

    doctor_total_row = doctor_start_row + len(report_data["doctor_rows"])
    sheet.cell(doctor_total_row, 1, "Итого")
    sheet.cell(doctor_total_row, 5, report_data["total_photos"])
    sheet.cell(doctor_total_row, 6, report_data["total_copies"])
    sheet.cell(doctor_total_row, 8, report_data["total_amount"])
    for cell in sheet[doctor_total_row][:8]:
        cell.fill = PatternFill("solid", fgColor=pale_green)
        cell.font = Font(name="Calibri", size=11, bold=True, color=green_text)
        cell.alignment = Alignment(vertical="center")
    for column in (5, 6):
        sheet.cell(doctor_total_row, column).number_format = "#,##0"
    sheet.cell(doctor_total_row, 8).number_format = '#,##0 "сум"'

    source_row = doctor_total_row + 2
    merged_banner(
        source_row,
        "Источник: облачная база сайта. В отчёт входят только фотографии, принятые оператором "
        "и находившиеся в базе на момент формирования; удалённые записи восстановить нельзя.",
        light_gray, Font(name="Calibri", size=9, italic=True, color="595959"), 34,
    )
    if report_data["doctor_rows"]:
        sheet.auto_filter.ref = f"A{doctor_header_row}:H{doctor_total_row - 1}"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output


@login_required
def export_full_ten_day_report(request):
    """Отдельный полный отчёт по всей базе за выбранные оператором даты."""
    connection = bot_database()
    if not connection:
        raise Http404
    selected_date_from, selected_date_to = requested_period(request)
    with connection:
        report_data = full_ten_day_report_data(
            connection,
            date_from=selected_date_from,
            date_to=selected_date_to,
        )
    output = build_full_ten_day_report(report_data)
    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    filename = f"Полный_отчет_{date_from}-{date_to}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename=full_report.xlsx; filename*=UTF-8''{quote(filename)}"
    )
    return response


def rejected_photo_report_data(
    connection, now=None, date_from=None, date_to=None,
    city="", hospital="", medic="", doctor="",
):
    """Return operator-rejected photos and their comments for a selected period."""
    local_timezone = timezone.get_default_timezone()
    local_now, date_from, date_to = full_report_period(now, date_from, date_to)
    period_start = timezone.make_aware(
        datetime.combine(date_from, datetime.min.time()), local_timezone,
    )
    period_end = timezone.make_aware(
        datetime.combine(date_to + timedelta(days=1), datetime.min.time()), local_timezone,
    )
    city = str(city or "").strip()
    hospital = str(hospital or "").strip()
    medic = str(medic or "").strip()
    doctor = str(doctor or "").strip()[:200]
    if not database_table_exists(connection, "photo_checks"):
        records = []
    else:
        columns = photo_columns(connection)
        photo_hospital = (
            "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
            if "maternity_hospital" in columns else "sender.maternity_hospital"
        )
        optional = {
            "photo_date": "p.photo_date" if "photo_date" in columns else "NULL",
            "mother_name": "p.mother_name" if "mother_name" in columns else "NULL",
            "qr_data": "p.qr_data" if "qr_data" in columns else "NULL",
            "review_status": "p.review_status" if "review_status" in columns else "'Не проверено'",
            "review_reason": "p.review_reason" if "review_reason" in columns else "''",
        }
        conditions = [
            "julianday(p.sent_at) >= julianday(?)",
            "julianday(p.sent_at) < julianday(?)",
            "checks.manual_decision = ?",
        ]
        parameters = [
            period_start.isoformat(), period_end.isoformat(), DECISION_REJECTED,
        ]
        if city:
            conditions.append("sender.city = ?")
            parameters.append(city)
        if hospital:
            conditions.append(f"{photo_hospital} = ?")
            parameters.append(hospital)
        if medic.isdigit():
            conditions.append("p.sender_telegram_id = ?")
            parameters.append(int(medic))
        if doctor_search_key(doctor):
            doctor_key = doctor_search_key(doctor)
            conditions.append(doctor_search_condition())
            parameters.extend([f"%{doctor_key}%", doctor_key])

        rows = connection.execute(
            f"""SELECT p.id, p.image, p.sent_at,
                       COALESCE(p.doctor_name, '') AS doctor_name,
                       COALESCE(sender.full_name, 'Пользователь удалён') AS sender_name,
                       p.sender_telegram_id,
                       COALESCE(sender.city, 'Не указан') AS city,
                       COALESCE({photo_hospital}, 'Не указан') AS maternity_hospital,
                       {optional['photo_date']} AS photo_date,
                       {optional['mother_name']} AS mother_name,
                       {optional['qr_data']} AS qr_data,
                       {optional['review_status']} AS review_status,
                       {optional['review_reason']} AS review_reason,
                       COALESCE(checks.manual_reason, '') AS operator_comment
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE {' AND '.join(conditions)}
                ORDER BY p.sent_at, p.id""",
            parameters,
        ).fetchall()
        hospital_renames = load_hospital_renames(connection)
        records = []
        for row in rows:
            record = dict(row)
            record["maternity_hospital"] = resolve_hospital_name(
                record["city"], record["maternity_hospital"], hospital_renames,
            )
            records.append(record)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "generated_at": local_now,
        "records": records,
        "total_photos": len(records),
        "filters": {
            "city": city,
            "hospital": hospital,
            "medic": medic if medic.isdigit() else "",
            "doctor": doctor if doctor_search_key(doctor) else "",
        },
    }


def build_rejected_photo_report(report_data):
    """Build manual-rejection sheets grouped by city, hospital, doctor and medic."""
    workbook = Workbook()
    headers = [
        "№", "Дата отправки", "Фотография", "Город", "Роддом", "Врач",
        "Медик", "Комментарий оператора", "Автопроверка",
    ]
    local_timezone = timezone.get_default_timezone()
    thin_gray = Side(style="thin", color="D9D9D9")
    widths = {
        "A": 6, "B": 20, "C": 24, "D": 16, "E": 24, "F": 24,
        "G": 24, "H": 38, "I": 24,
    }

    def text_key(value):
        return str(value or "").strip().casefold()

    def record_key(record):
        return (
            text_key(record.get("city")),
            text_key(record.get("maternity_hospital")),
            text_key(record.get("doctor_name") or "Не указан"),
            text_key(record.get("sender_name")),
            str(record.get("sent_at") or ""),
            int(record.get("id") or 0),
        )

    def populate_sheet(sheet, records):
        sheet.sheet_view.showGridLines = False
        for column, value in enumerate(headers, start=1):
            cell = sheet.cell(1, column, value)
            cell.fill = PatternFill("solid", fgColor="C00000")
            cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.row_dimensions[1].height = 32

        for offset, record in enumerate(records, start=1):
            row_number = 1 + offset
            sent_at = record["sent_at"]
            try:
                sent_at = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
                if timezone.is_aware(sent_at):
                    sent_at = timezone.localtime(sent_at, local_timezone).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
            values = [
                offset, sent_at, "", record["city"], record["maternity_hospital"],
                record["doctor_name"] or "Не указан", record["sender_name"],
                record["operator_comment"] or "Комментарий не указан",
                record["review_status"],
            ]
            for column, value in enumerate(values, start=1):
                cell = sheet.cell(row_number, column, value)
                cell.font = Font(name="Calibri", size=9, color="1F1F1F")
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                cell.border = Border(bottom=thin_gray)
            sheet.cell(row_number, 2).number_format = "DD.MM.YYYY HH:MM"
            sheet.row_dimensions[row_number].height = 115
            try:
                image = ExcelImage(BytesIO(record["image"]))
                image.width, image.height = 150, 110
                sheet.add_image(image, f"C{row_number}")
            except Exception:
                sheet.cell(row_number, 3).value = "Не удалось встроить фото"

        for column, width in widths.items():
            sheet.column_dimensions[column].width = width
        sheet.freeze_panes = "A2"
        if records:
            sheet.auto_filter.ref = f"A1:I{1 + len(records)}"
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    def sheet_title(city, hospital):
        base = re.sub(r"[\\/*?:\[\]]+", " ", f"{city} — {hospital}")
        base = re.sub(r"\s+", " ", base).strip() or "Без города — без роддома"
        candidate = base[:31]
        suffix = 2
        while candidate in workbook.sheetnames:
            tail = f" ({suffix})"
            candidate = f"{base[:31 - len(tail)]}{tail}"
            suffix += 1
        return candidate

    records = sorted(report_data["records"], key=record_key)
    overview = workbook.active
    overview.title = "Общий отчёт"
    populate_sheet(overview, records)

    grouped = {}
    for record in records:
        group = (record["city"], record["maternity_hospital"])
        grouped.setdefault(group, []).append(record)
    for (city, hospital), group_records in grouped.items():
        sheet = workbook.create_sheet(sheet_title(city, hospital))
        populate_sheet(sheet, group_records)

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output


@login_required
def export_rejected_photo_report(request):
    """Download rejected photos, operator comments and context for a date range."""
    connection = bot_database()
    if not connection:
        raise Http404
    selected_date_from, selected_date_to = requested_period(request)
    city = request.GET.get("city", "").strip()[:200]
    hospital = request.GET.get("hospital", "").strip()[:200]
    selected_medic = request.GET.get("medic", "").strip()
    if not selected_medic.isdigit():
        selected_medic = ""
    selected_doctor = request.GET.get("doctor", "").strip()[:200]
    if not doctor_search_key(selected_doctor):
        selected_doctor = ""
    with connection:
        report_data = rejected_photo_report_data(
            connection,
            date_from=selected_date_from,
            date_to=selected_date_to,
            city=city,
            hospital=hospital,
            medic=selected_medic,
            doctor=selected_doctor,
        )
    output = build_rejected_photo_report(report_data)
    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    filename = f"Отклоненные_фотографии_{date_from}-{date_to}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename=rejected_photos.xlsx; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_required
def export_photos(request):
    """Скачиваемая копия только фотографий, вручную принятых оператором."""
    connection = bot_database()
    if not connection:
        raise Http404
    city = request.GET.get("city", "")
    hospital = request.GET.get("hospital", "")
    selected_medic = request.GET.get("medic", "").strip()
    selected_doctor = request.GET.get("doctor", "").strip()[:200]
    if not doctor_search_key(selected_doctor):
        selected_doctor = ""
    selected_date_from, selected_date_to = requested_period(request)
    with connection:
        columns = photo_columns(connection)
        has_photo_checks = database_table_exists(connection, "photo_checks")
        if has_photo_checks:
            checks_join = " LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id"
        else:
            checks_join = ""
        photo_hospital = "COALESCE(p.maternity_hospital, sender.maternity_hospital)" if "maternity_hospital" in columns else "sender.maternity_hospital"
        query = f"""
            SELECT p.image, COALESCE(sender.city, 'Не указан') AS city,
                   COALESCE({photo_hospital}, 'Не указан') AS maternity_hospital,
                   COALESCE(p.doctor_name, '') AS doctor_name, p.sent_at, p.photo_date,
                   p.mother_name, p.mother_phone,
                   {"p.review_status" if "review_status" in columns else "'Не проверено'"} AS review_status
            FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            {checks_join}
        """
        conditions, parameters = [], []
        if has_photo_checks:
            conditions.append("checks.manual_decision = ?")
            parameters.append(DECISION_ACCEPTED)
        else:
            # Без таблицы ручных решений ни одна фотография не считается принятой оператором.
            conditions.append("1 = 0")
        if city:
            conditions.append("sender.city = ?")
            parameters.append(city)
        if hospital:
            conditions.append(f"{photo_hospital} = ?")
            parameters.append(hospital)
        if selected_medic.isdigit():
            conditions.append("p.sender_telegram_id = ?")
            parameters.append(int(selected_medic))
        if selected_doctor:
            selected_doctor_key = doctor_search_key(selected_doctor)
            conditions.append(doctor_search_condition())
            parameters.extend([f"%{selected_doctor_key}%", selected_doctor_key])
        if selected_date_from:
            conditions.append("DATE(p.sent_at) >= ?")
            parameters.append(selected_date_from)
        if selected_date_to:
            conditions.append("DATE(p.sent_at) <= ?")
            parameters.append(selected_date_to)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY DATE(p.sent_at), p.sent_at"
        records = connection.execute(query, parameters).fetchall()

    workbook = Workbook()
    doctor_groups = {}
    # Копии и обрезанные фотографии остаются на сайте жёлтыми, но в Excel не выгружаются.
    records = [record for record in records if not is_copy_or_crop_status(record["review_status"])]
    for record in records:
        doctor_groups.setdefault((record["doctor_name"] or "Без указанного врача").strip(), []).append(record)
    if not doctor_groups:
        doctor_groups["Фотографии"] = []
    for index, (doctor, doctor_records) in enumerate(doctor_groups.items()):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = excel_sheet_title(doctor, workbook.sheetnames[:-1] if index else [])
        headers = ["Дата отправки", "Фотография", "Количество фотографий", "Комментарий", "Дата на фотографии", "ФИО роженицы", "Номер телефона роженицы"]
        sheet.append(headers)
        header_fill = PatternFill("solid", fgColor="5B3488")
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row_number, record in enumerate(doctor_records, start=2):
            sent_at = record["sent_at"]
            try:
                sent_at = datetime.fromisoformat(sent_at).replace(tzinfo=None)
            except (TypeError, ValueError):
                pass
            sheet.append([sent_at, "", 1, "", record["photo_date"], record["mother_name"], record["mother_phone"]])
            sheet.row_dimensions[row_number].height = 115
            for cell in sheet[row_number]:
                cell.alignment = Alignment(vertical="center", wrap_text=True)
            try:
                image = ExcelImage(BytesIO(record["image"]))
                image.width, image.height = 150, 110
                sheet.add_image(image, f"B{row_number}")
            except Exception:
                sheet.cell(row_number, 2).value = "Не удалось встроить фото"
            sheet.cell(row_number, 1).number_format = "DD.MM.YYYY"
            sheet.cell(row_number, 5).number_format = "DD.MM.YYYY"
        for column, width in {"A": 20, "B": 24, "C": 24, "D": 28, "E": 22, "F": 28, "G": 24}.items():
            sheet.column_dimensions[column].width = width
        sheet.freeze_panes = "A2"
    output = BytesIO()
    workbook.save(output)
    response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if selected_doctor:
        filename = f"{safe_excel_filename(selected_doctor)}.xlsx"
    elif hospital:
        filename = f"{safe_excel_filename(hospital)}.xlsx"
    else:
        filename = "Фотографии.xlsx"
    # filename* обязателен для корректного имени с кириллицей в Chrome/Telegram Desktop.
    response["Content-Disposition"] = (
        f"attachment; filename=report.xlsx; filename*=UTF-8''{quote(filename)}"
    )
    return response


@login_required
def photo_image(request, photo_id):
    connection = bot_database()
    if not connection:
        raise Http404
    thumbnail_requested = request.GET.get("thumbnail") == "1"
    try:
        with connection:
            try:
                photo_identity = connection.execute(
                    "SELECT image_digest FROM photos WHERE id = ?", (photo_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                photo_identity = connection.execute(
                    "SELECT NULL AS image_digest FROM photos WHERE id = ?", (photo_id,),
                ).fetchone()
            if not photo_identity:
                raise Http404

            image_digest = photo_identity["image_digest"]
            if thumbnail_requested and image_digest:
                cache_path = photo_thumbnail_cache_path(photo_id, image_digest)
                try:
                    if cache_path.exists():
                        image_bytes = cache_path.read_bytes()
                    else:
                        photo = connection.execute(
                            "SELECT image FROM photos WHERE id = ?", (photo_id,),
                        ).fetchone()
                        if not photo:
                            raise Http404
                        image_bytes = cached_photo_thumbnail(photo_id, image_digest, photo["image"])
                except OSError:
                    photo = connection.execute(
                        "SELECT image FROM photos WHERE id = ?", (photo_id,),
                    ).fetchone()
                    if not photo:
                        raise Http404
                    image_bytes = make_photo_thumbnail(photo["image"])
            else:
                photo = connection.execute(
                    "SELECT image FROM photos WHERE id = ?", (photo_id,),
                ).fetchone()
                if not photo:
                    raise Http404
                image_bytes = (
                    cached_photo_thumbnail(photo_id, image_digest, photo["image"])
                    if thumbnail_requested else photo["image"]
                )
    finally:
        connection.close()

    response = HttpResponse(image_bytes, content_type="image/jpeg")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=604800"
    if request.GET.get("download") == "1":
        response["Content-Disposition"] = f'attachment; filename="photo_{photo_id}.jpg"'
    return response
