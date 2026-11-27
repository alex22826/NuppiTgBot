"""Выгрузки Excel."""

from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from django.http import Http404, HttpResponse
from django.shortcuts import render
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font, PatternFill

from .. import audit
from ..access import permission_required, region_condition, user_regions
from ..db import bot_database, database_table_exists, photo_columns
from ..decision_rules import DECISION_ACCEPTED
from ..periods import requested_period
from ..photos import is_copy_or_crop_status
from ..reports import (
    build_full_ten_day_report,
    build_rejected_photo_report,
    full_ten_day_report_data,
    rejected_photo_report_data,
)
from ..text import (
    doctor_search_condition,
    doctor_search_key,
    excel_sheet_title,
    safe_excel_filename,
)


@permission_required("reports.view")
def reports_page(request):
    """Все выгрузки Excel и статистика медиков на одной странице."""
    from ..periods import full_report_period
    from .photos import photo_list_context

    # Без выбранного периода отчёты открываются за последние 10 дней — как и
    # сам полный отчёт, — чтобы цифры на странице совпадали с файлом.
    if not request.GET.get("date_from") and not request.GET.get("date_to"):
        _, default_from, default_to = full_report_period()
        query = request.GET.copy()
        query["date_from"] = default_from.isoformat()
        query["date_to"] = default_to.isoformat()
        request.GET = query
    return render(request, "dashboard/reports.html", photo_list_context(request))


@permission_required("reports.export")
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
            regions=user_regions(request.user),
        )
    output = build_full_ten_day_report(report_data)
    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    audit.record(request, "export", f"Полный отчёт по принятым фото за {date_from} — {date_to}",
                 object_type="report", object_id="full", date_from=date_from, date_to=date_to)
    filename = f"Полный_отчет_{date_from}-{date_to}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename=full_report.xlsx; filename*=UTF-8''{quote(filename)}"
    )
    return response


@permission_required("reports.export")
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
            regions=user_regions(request.user),
        )
    output = build_rejected_photo_report(report_data)
    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    audit.record(request, "export", f"Отчёт по отклонённым фото за {date_from} — {date_to}",
                 object_type="report", object_id="rejected", date_from=date_from, date_to=date_to,
                 filters={key: value for key, value in request.GET.items() if value})
    filename = f"Отклоненные_фотографии_{date_from}-{date_to}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f"attachment; filename=rejected_photos.xlsx; filename*=UTF-8''{quote(filename)}"
    )
    return response


@permission_required("reports.export")
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
        region_sql, region_params = region_condition(user_regions(request.user))
        if region_sql:
            conditions.append(region_sql)
            parameters.extend(region_params)
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
    audit.record(request, "export", f"Список принятых фото в Excel: {len(records)} записей",
                 object_type="report", object_id="photos",
                 filters={key: value for key, value in request.GET.items() if value})

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
