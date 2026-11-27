"""Справочники городов, роддомов и врачей (с кэшированием)."""

import re
import threading

from .db import database_revision, hospital_table_exists, photo_columns, user_columns
from .text import doctor_search_key

_DASHBOARD_CATALOG_CACHE = {"key": None, "value": None}


_DASHBOARD_CATALOG_LOCK = threading.Lock()


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
