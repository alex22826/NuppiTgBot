"""Права доступа: роли пользователей сайта и регионы руководителей."""

import sqlite3
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied

from .roles import DEFAULT_ROLE, OWNER, ROLES
from .text import doctor_search_key


def is_creator(user):
    """Владелец сайта — логин из CREATOR_DJANGO_USERNAME.

    Если логин не задан, владельцами считаются суперпользователи: так сайт сразу
    готов к работе после ``python manage.py createsuperuser``.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if settings.CREATOR_DJANGO_USERNAME:
        return user.username == settings.CREATOR_DJANGO_USERNAME
    return bool(getattr(user, "is_superuser", False))


def staff_profile(user):
    try:
        return user.staff_profile
    except (AttributeError, ObjectDoesNotExist):
        return None


def user_role(user):
    """Роль пользователя. Владелец из настроек — всегда владелец."""
    if not getattr(user, "is_authenticated", False):
        return None
    if is_creator(user):
        return OWNER
    profile = staff_profile(user)
    if profile and profile.role in ROLES:
        return profile.role
    return DEFAULT_ROLE


def user_permissions(user):
    role = user_role(user)
    return ROLES[role]["permissions"] if role else frozenset()


def has_perm(user, permission):
    return permission in user_permissions(user)


def user_regions(user):
    """Регионы, которыми ограничен пользователь; None — все регионы."""
    role = user_role(user)
    if not role or not ROLES[role]["scoped"]:
        return None
    profile = staff_profile(user)
    regions = profile.region_list() if profile else []
    return regions or None


def region_keys(regions):
    return [doctor_search_key(region) for region in regions or [] if doctor_search_key(region)]


def region_condition(regions, column="sender.city"):
    """SQL-условие «город отправителя входит в регионы пользователя»."""
    keys = region_keys(regions)
    if not keys:
        return None, []
    placeholders = ", ".join("?" for _ in keys)
    return f"DOCTOR_SEARCH_KEY(COALESCE({column}, '')) IN ({placeholders})", keys


def region_allowed(regions, city):
    if not regions:
        return True
    return doctor_search_key(city) in set(region_keys(regions))


def photo_in_scope(connection, user, photo_id, allow_originals=False):
    """Фото другого региона для руководителя как будто не существует.

    ``allow_originals`` открывает снимок, если он — оригинал для копии из
    своего региона: оператору нужно сравнить их рядом.
    """
    regions = user_regions(user)
    if not regions:
        return True
    condition, keys = region_condition(regions)
    row = connection.execute(
        f"""SELECT 1 FROM photos AS p
            LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            WHERE p.id = ? AND {condition}""",
        (photo_id, *keys),
    ).fetchone()
    if row or not allow_originals:
        return bool(row)
    try:
        copy = connection.execute(
            f"""SELECT 1 FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                WHERE p.original_photo_id = ? AND {condition} LIMIT 1""",
            (photo_id, *keys),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(copy)


def permission_required(permission):
    """Пускает только роли с нужным правом; остальным — страница «Нет доступа»."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if not has_perm(request.user, permission):
                raise PermissionDenied("Для этого действия у вашей роли нет прав.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def creator_required(view):
    """Старое имя: управление сотрудниками бота, роддомами и заданиями."""
    return permission_required("staff.manage")(view)
