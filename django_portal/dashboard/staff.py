"""Данные сотрудников для форм сайта."""

import secrets

from .db import ensure_employee_hospitals_table
from .text import parse_hospitals


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
