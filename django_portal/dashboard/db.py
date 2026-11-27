"""Подключение к базе Telegram-бота и мягкие миграции старых схем."""

import sqlite3

from django.conf import settings

from .text import doctor_search_key


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
