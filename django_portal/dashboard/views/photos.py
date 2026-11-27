"""Список фотографий, карточка, решения оператора и удаление."""

import re
import sqlite3
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..access import creator_required, is_creator
from ..catalog import (
    build_doctor_groups,
    build_doctor_options,
    load_dashboard_catalog,
    locations_match,
    resolve_hospital_name,
)
from ..db import (
    bot_database,
    database_table_exists,
    ensure_photo_checks_table,
    photo_columns,
)
from ..decision_rules import (
    DECISION_ACCEPTED,
    DECISION_LABELS,
    DECISION_REJECTED,
    DECISION_REVIEW,
    automatic_photo_decision,
)
from ..periods import (
    format_selected_period,
    full_report_period,
    local_day_bounds,
    requested_day,
    requested_period,
)
from ..photos import (
    cached_photo_thumbnail,
    is_copy_or_crop_status,
    make_photo_thumbnail,
    photo_details,
    photo_details_for_operator,
    photo_review_group,
    photo_thumbnail_cache_path,
    resolve_original_photo,
)
from ..qr_codes import QR_REGION_CODE_PATTERN, canonical_qr_data, qr_code_status
from ..text import (
    decode_photo_location,
    doctor_search_condition,
    doctor_search_key,
    duplicate_name_key,
    format_sent_at,
)
from .common import return_to_photo_list

PHOTO_PAGE_SIZE = 20


@login_required
def dashboard(request):
    """Фотографии с вкладками по итогу проверки и фильтрами."""
    return render(request, "dashboard/dashboard.html", photo_list_context(request))


FILTER_PARAMETERS = ("city", "hospital", "doctor", "medic", "date_from", "date_to")

# Сортировка списка: ключ из адреса → (подпись, ORDER BY). Только из этого списка,
# поэтому значение из запроса никогда не попадает в SQL напрямую.
PHOTO_SORTS = {
    "new": ("Сначала новые", "p.sent_at DESC, p.id DESC"),
    "old": ("Сначала старые", "p.sent_at ASC, p.id ASC"),
    "doctor": ("По врачу (А–Я)", "doctor_name COLLATE NOCASE, p.sent_at DESC"),
    "medic": ("По медику (А–Я)", "sender_name COLLATE NOCASE, p.sent_at DESC"),
    "hospital": ("По роддому (А–Я)", "city COLLATE NOCASE, maternity_hospital COLLATE NOCASE, p.sent_at DESC"),
    "number": ("По номеру фото", "p.id DESC"),
}
DEFAULT_PHOTO_SORT = "new"


def active_filter_chips(request, labels):
    """Метки выбранных фильтров; у каждой — ссылка, которая убирает только её."""
    chips = []
    for name in FILTER_PARAMETERS:
        value = request.GET.get(name, "").strip()
        if not value:
            continue
        query = request.GET.copy()
        query.pop(name, None)
        query.pop("page", None)
        if name == "city":
            query.pop("hospital", None)
            query.pop("doctor", None)
        title, shown = labels.get(name, (name, value))
        if not shown:
            continue
        chips.append({"title": title, "value": shown, "remove_query": query.urlencode()})
    return chips


def photo_list_context(request):
    """Фильтры, счётчики и страница фото — общие для «Фотографий» и «Отчётов»."""
    creator_view = is_creator(request.user)
    connection = bot_database()
    if not connection:
        return {
            "database_error": "Не найдена база medical_bot.db.",
            "is_creator": creator_view,
        }

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
    selected_sort = request.GET.get("sort", DEFAULT_PHOTO_SORT).strip()
    if selected_sort not in PHOTO_SORTS:
        selected_sort = DEFAULT_PHOTO_SORT
    group_by_flag = request.GET.get("group", "flag") != "none"
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
        query += f" ORDER BY {PHOTO_SORTS[selected_sort][1]} LIMIT ? OFFSET ?"
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
        photo["review_group"] = photo_review_group(photo)
        photo_groups[photo["review_group"]].append(photo)

    medic_names = {str(row["telegram_id"]): row["full_name"] for row in medic_options}
    filter_chips = active_filter_chips(request, {
        "city": ("Город", city),
        "hospital": ("Роддом", hospital),
        "doctor": ("Врач", selected_doctor),
        "medic": ("Медик", medic_names.get(selected_medic, selected_medic)),
        "date_from": ("С", format_selected_period(selected_date_from, selected_date_from) if selected_date_from else ""),
        "date_to": ("По", format_selected_period(selected_date_to, selected_date_to) if selected_date_to else ""),
    })
    view_query = request.GET.copy()
    for name in ("page", "sort", "group"):
        view_query.pop(name, None)

    page_query = request.GET.copy()
    page_query.pop("page", None)
    page_query = page_query.urlencode()
    decision_query = request.GET.copy()
    decision_query.pop("page", None)
    decision_query.pop("decision", None)
    decision_query = decision_query.urlencode()
    photo_page_start = photo_offset + 1 if summary["photo_count"] else 0
    photo_page_end = min(photo_offset + len(photos), summary["photo_count"])

    return {
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
        "decision_total": sum(decision_counts.values()),
        "photo_sorts": {key: label for key, (label, _order) in PHOTO_SORTS.items()},
        "selected_sort": selected_sort,
        "group_by_flag": group_by_flag,
        "filter_chips": filter_chips,
        "view_query": view_query.urlencode(),
        "has_filters": any(request.GET.get(name) for name in FILTER_PARAMETERS),
        "is_creator": creator_view,
        "return_to": request.get_full_path(),
    }


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
        "review_group": photo_review_group(photo),
        "return_to": request.get_full_path(),
        "is_creator": is_creator(request.user),
    })


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
