"""Данные для бокового меню, общие для всех страниц."""

import logging
import sqlite3

from .access import region_condition, user_permissions, user_regions, user_role
from .db import bot_database, database_table_exists
from .decision_rules import DECISION_REVIEW
from .roles import role_label

logger = logging.getLogger(__name__)


def review_count(regions=None):
    """Сколько фото ждут решения оператора; None, если база недоступна."""
    connection = bot_database()
    if not connection:
        return None
    try:
        if not database_table_exists(connection, "photos"):
            return 0
        region_sql, region_params = region_condition(regions)
        region_where = f" AND {region_sql}" if region_sql else ""
        users_join = "LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id"
        if not database_table_exists(connection, "photo_checks"):
            return connection.execute(
                f"SELECT COUNT(*) FROM photos AS p {users_join} WHERE 1 = 1{region_where}", region_params,
            ).fetchone()[0]
        return connection.execute(
            f"""SELECT COUNT(*) FROM photos AS p
                {users_join}
                LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE COALESCE(NULLIF(checks.manual_decision, ''),
                               NULLIF(checks.decision, ''), '{DECISION_REVIEW}') = ?{region_where}""",
            (DECISION_REVIEW, *region_params),
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
    permissions = user_permissions(user)
    role = user_role(user)
    regions = user_regions(user)
    return {
        "nav": {
            "role": role,
            "role_label": role_label(role),
            "perms": permissions,
            "regions": regions or [],
            "is_creator": "users.manage" in permissions,
            "review_count": review_count(regions) if "photos.view" in permissions else None,
        },
    }
