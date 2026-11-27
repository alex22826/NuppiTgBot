"""Управление сотрудниками, роддомами, заданиями и входами сайта."""

import re
from datetime import datetime
from datetime import timezone as datetime_timezone

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import audit
from ..access import creator_required, permission_required
from ..catalog import load_hospital_renames, resolve_hospital_name
from ..db import (
    bot_database,
    database_table_exists,
    ensure_active_column,
    ensure_allowed_users_table,
    ensure_employee_hospitals_table,
    ensure_hospital_renames_table,
    ensure_visit_assignments_table,
    photo_columns,
    user_columns,
)
from ..periods import requested_day
from ..staff import employee_data, new_medic_code, replace_employee_hospitals
from ..text import doctor_search_key, encode_photo_location


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
    audit.record(
        request, "assignment_add",
        f"Задание: {city} · {hospital}" + (f", врач {doctor}" if doctor else "")
        + (f", продукт {product}" if product else "") + (f", материал {material}" if material else ""),
        object_type="assignment", city=city, hospital=hospital, doctor=doctor, product=product,
        material=material, date_from=date_from or "", date_to=date_to or "", medic=medic,
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
        assignment = connection.execute(
            "SELECT city, maternity_hospital FROM visit_assignments WHERE id = ?", (assignment_id,),
        ).fetchone()
        result = connection.execute(
            "DELETE FROM visit_assignments WHERE id = ?", (assignment_id,),
        )
    if not result.rowcount:
        raise Http404
    audit.record(
        request, "assignment_delete",
        f"Удалено задание №{assignment_id}" + (f": {assignment['city']} · {assignment['maternity_hospital']}" if assignment else ""),
        object_type="assignment", object_id=assignment_id,
    )
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
        employee = connection.execute(
            "SELECT full_name FROM users WHERE telegram_id = ?", (telegram_id,),
        ).fetchone()
        connection.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
    if employee:
        audit.record(request, "employee_disable", f"Отключён доступ к боту: {employee['full_name']}",
                     object_type="employee", object_id=telegram_id)
    return redirect("dashboard")


@creator_required
@permission_required("photos.delete")
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
    audit.record(
        request, "employee_photos_delete",
        f"Удалены все фото сотрудника {employee['full_name']}: {deleted_count}",
        object_type="employee", object_id=telegram_id, deleted=deleted_count,
    )
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
    audit.record(request, "employee_delete", f"Удалён сотрудник бота {employee['full_name']}",
                 object_type="employee", object_id=telegram_id)
    messages.success(request, f"Сотрудник {employee['full_name']} полностью удалён. История фото сохранена.")
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

    audit.record(
        request, "hospital_rename",
        f"Роддом «{old_name}» ({city}) → «{new_name}». Перенесено фото: {moved_photos}",
        object_type="hospital", city=city, old_name=old_name, new_name=new_name,
        moved_photos=moved_photos, updated_users=updated_users,
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
    audit.record(request, "employee_add", f"Добавлен сотрудник бота {full_name} ({role}, {city})",
                 object_type="employee", object_id=telegram_id, role=role, city=city, hospitals=hospitals)
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
            audit.record(
                request, "employee_edit",
                f"Изменён сотрудник бота {full_name}: {role}, {city}, доступ {'открыт' if active else 'закрыт'}",
                object_type="employee", object_id=telegram_id,
                before={"full_name": employee["full_name"], "role": employee["role"], "city": employee["city"],
                        "is_active": employee["is_active"]},
                after={"full_name": full_name, "role": role, "city": city, "is_active": active, "hospitals": hospitals},
            )
            messages.success(request, "Данные сотрудника сохранены.")
            return redirect("dashboard")
        assigned_hospitals = [row["maternity_hospital"] for row in connection.execute(
            "SELECT maternity_hospital FROM employee_hospitals WHERE telegram_id = ? ORDER BY maternity_hospital", (telegram_id,)
        )]
    return render(request, "dashboard/employee_form.html", {
        "employee": employee,
        "assigned_hospitals": ", ".join(assigned_hospitals) or employee["maternity_hospital"],
    })
