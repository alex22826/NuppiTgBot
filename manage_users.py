"""Управление сотрудниками бота из терминала — пока сайт не запущен.

Делает то же самое, что кнопки «Добавить сотрудника» / «Изменить» на сайте:
пишет в таблицы users, employee_hospitals и allowed_users базы бота.

Примеры:
    python manage_users.py add 123456789 "Иванова Анна" Ташкент "Роддом №1, Роддом №2"
    python manage_users.py add 987654321 "Петров Олег" Самарканд "Роддом №3" --role Оператор
    python manage_users.py list
    python manage_users.py disable 123456789
    python manage_users.py enable 123456789
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys

import bot  # тот же путь к базе (BOT_DB_PATH) и та же схема, что у бота

ROLES = ("Медик", "Оператор")


def parse_hospitals(value: str) -> list[str]:
    hospitals, seen = [], set()
    for item in re.split(r"[,;\n]+", value or ""):
        hospital = item.strip()
        if hospital and hospital.casefold() not in seen:
            hospitals.append(hospital)
            seen.add(hospital.casefold())
    return hospitals


def new_medic_code(con) -> str:
    for _ in range(10000):
        code = f"{secrets.randbelow(10000):04d}"
        if not con.execute("SELECT 1 FROM users WHERE medic_code = ?", (code,)).fetchone():
            return code
    raise RuntimeError("Не удалось подобрать свободный код медика.")


def add_or_update(telegram_id: int, full_name: str, city: str, hospitals: list[str],
                  role: str, username: str | None) -> str:
    with bot.connect() as con:
        existing = con.execute(
            "SELECT medic_code FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        medic_code = existing["medic_code"] if existing else None
        if role == "Медик" and not medic_code:
            medic_code = new_medic_code(con)
        if role != "Медик":
            medic_code = None
        if existing:
            con.execute(
                """UPDATE users SET username=?, full_name=?, city=?, maternity_hospital=?,
                   role=?, medic_code=?, is_active=1 WHERE telegram_id=?""",
                (username, full_name, city, hospitals[0], role, medic_code, telegram_id),
            )
        else:
            con.execute(
                """INSERT INTO users (telegram_id, username, full_name, city, district,
                   maternity_hospital, role, medic_code, is_active, registered_at)
                   VALUES (?, ?, ?, ?, '-', ?, ?, ?, 1, datetime('now'))""",
                (telegram_id, username, full_name, city, hospitals[0], role, medic_code),
            )
        con.execute("DELETE FROM employee_hospitals WHERE telegram_id = ?", (telegram_id,))
        con.executemany(
            "INSERT INTO employee_hospitals (telegram_id, city, maternity_hospital) VALUES (?, ?, ?)",
            [(telegram_id, city, hospital) for hospital in hospitals],
        )
        con.execute(
            "INSERT OR REPLACE INTO allowed_users (telegram_id, added_by, added_at) "
            "VALUES (?, 0, datetime('now'))",
            (telegram_id,),
        )
    return "обновлён" if existing else "добавлен"


def set_active(telegram_id: int, active: bool) -> bool:
    with bot.connect() as con:
        cursor = con.execute(
            "UPDATE users SET is_active = ? WHERE telegram_id = ?", (1 if active else 0, telegram_id)
        )
        if active:
            con.execute(
                "INSERT OR REPLACE INTO allowed_users (telegram_id, added_by, added_at) "
                "VALUES (?, 0, datetime('now'))",
                (telegram_id,),
            )
        else:
            con.execute("DELETE FROM allowed_users WHERE telegram_id = ?", (telegram_id,))
        return cursor.rowcount > 0


def list_users() -> None:
    with bot.connect() as con:
        rows = con.execute(
            """SELECT u.telegram_id, u.full_name, u.role, u.city, u.medic_code, u.is_active,
                      GROUP_CONCAT(h.maternity_hospital, ', ') AS hospitals
               FROM users AS u
               LEFT JOIN employee_hospitals AS h ON h.telegram_id = u.telegram_id
               GROUP BY u.telegram_id ORDER BY u.full_name COLLATE NOCASE"""
        ).fetchall()
    if not rows:
        print("Сотрудников пока нет.")
        return
    for row in rows:
        status = "активен" if row["is_active"] else "отключён"
        code = f", код {row['medic_code']}" if row["medic_code"] else ""
        print(f"{row['telegram_id']}: {row['full_name']} — {row['role']}{code}, "
              f"{row['city']}: {row['hospitals'] or '—'} ({status})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Сотрудники Telegram-бота")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="добавить или изменить сотрудника")
    add.add_argument("telegram_id", type=int)
    add.add_argument("full_name")
    add.add_argument("city")
    add.add_argument("hospitals", help="роддомы через запятую")
    add.add_argument("--role", choices=ROLES, default="Медик")
    add.add_argument("--username", default=None)
    sub.add_parser("list", help="показать сотрудников")
    for name in ("disable", "enable"):
        command = sub.add_parser(name)
        command.add_argument("telegram_id", type=int)
    args = parser.parse_args(argv)

    bot.init_db()
    if args.command == "add":
        hospitals = parse_hospitals(args.hospitals)
        if not args.full_name.strip() or not args.city.strip() or not hospitals:
            parser.error("укажите ФИО, город и хотя бы один роддом")
        result = add_or_update(
            args.telegram_id, args.full_name.strip(), args.city.strip(), hospitals,
            args.role, (args.username or "").lstrip("@") or None,
        )
        print(f"Сотрудник {result}: {args.telegram_id} ({args.role}). Доступ к боту открыт.")
    elif args.command == "list":
        list_users()
    else:
        if not set_active(args.telegram_id, args.command == "enable"):
            print("Сотрудник с таким Telegram ID не найден.")
            return 1
        print("Доступ включён." if args.command == "enable" else "Доступ отключён.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
