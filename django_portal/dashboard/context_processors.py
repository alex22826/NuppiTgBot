"""Данные для бокового меню, общие для всех страниц."""

import logging
import sqlite3

from .access import is_creator
from .db import bot_database, database_table_exists
from .decision_rules import DECISION_REVIEW

logger = logging.getLogger(__name__)


def review_count():
    """Сколько фото ждут решения оператора; None, если база недоступна."""
    connection = bot_database()
    if not connection:
        return None
    try:
        if not database_table_exists(connection, "photos"):
            return 0
        if not database_table_exists(connection, "photo_checks"):
            return connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        return connection.execute(
            f"""SELECT COUNT(*) FROM photos AS p
                LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE COALESCE(NULLIF(checks.manual_decision, ''),
                               NULLIF(checks.decision, ''), '{DECISION_REVIEW}') = ?""",
            (DECISION_REVIEW,),
        ).fetchone()[0]
    except sqlite3.Error:
        logger.exception("Не удалось посчитать фото на проверке для меню")
        return None
    finally:
        connection.close()


def navigation(request):
    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return {}
    return {
        "nav": {
            "is_creator": is_creator(user),
            "review_count": review_count(),
        },
    }
