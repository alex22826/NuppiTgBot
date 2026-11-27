"""Данные и Excel-файлы сводных отчётов."""

import re
from datetime import datetime, timedelta
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .catalog import load_hospital_renames, resolve_hospital_name
from .db import database_table_exists, photo_columns
from .decision_rules import DECISION_REJECTED
from .periods import full_report_period
from .photos import is_copy_or_crop_status
from .text import doctor_search_condition, doctor_search_key, sent_at_local_date

PHOTO_PAYMENT_RATE_SUM = 100_000


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
