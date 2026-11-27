"""Журнал действий (ТЗ, разделы 5.2 и 11): кто, когда и что сделал на сайте."""

import logging

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Действие → (название, группа для фильтра).
ACTIONS = {
    "login": ("Вход на сайт", "Входы"),
    "login_failed": ("Неудачная попытка входа", "Входы"),
    "logout": ("Выход с сайта", "Входы"),
    "photo_decision": ("Решение по фото", "Фото"),
    "photo_edit": ("Правка данных фото", "Фото"),
    "photo_delete": ("Удаление фото", "Фото"),
    "photos_bulk_delete": ("Удаление фото по фильтру", "Фото"),
    "export": ("Выгрузка Excel", "Отчёты"),
    "employee_add": ("Добавлен сотрудник бота", "Сотрудники"),
    "employee_edit": ("Изменён сотрудник бота", "Сотрудники"),
    "employee_disable": ("Отключён доступ к боту", "Сотрудники"),
    "employee_delete": ("Удалён сотрудник бота", "Сотрудники"),
    "employee_photos_delete": ("Удалены все фото сотрудника", "Сотрудники"),
    "assignment_add": ("Добавлено задание", "Сотрудники"),
    "assignment_delete": ("Удалено задание", "Сотрудники"),
    "hospital_rename": ("Переименован роддом", "Сотрудники"),
    "user_add": ("Создан вход на сайт", "Пользователи"),
    "user_edit": ("Изменён пользователь сайта", "Пользователи"),
    "user_password": ("Сменён пароль", "Пользователи"),
}

ACTION_GROUPS = sorted({group for _, group in ACTIONS.values()})

# Действия, которые меняют или удаляют данные, подсвечиваются в журнале.
DANGEROUS_ACTIONS = {"photo_delete", "photos_bulk_delete", "employee_delete", "employee_photos_delete"}


def action_label(action):
    return ACTIONS.get(action, (action, ""))[0]


def client_ip(request):
    if request is None:
        return None
    value = request.META.get("REMOTE_ADDR") or None
    return value


def record(request, action, summary="", *, object_type="", object_id="", user=None, username=None, **details):
    """Записать событие. Ошибка журнала никогда не мешает самому действию."""
    from .access import user_role
    from .models import AuditEvent

    actor = user if user is not None else getattr(request, "user", None)
    authenticated = bool(getattr(actor, "is_authenticated", False))
    if username is None:
        username = getattr(actor, "username", "") if authenticated else ""
    try:
        AuditEvent.objects.create(
            user=actor if authenticated and getattr(actor, "pk", None) else None,
            username=username[:150],
            role=(user_role(actor) or "") if authenticated else "",
            action=action,
            object_type=object_type,
            object_id=str(object_id or ""),
            summary=summary[:2000],
            details=details,
            ip=client_ip(request),
        )
    except Exception:  # noqa: BLE001 — журнал не должен ломать работу сайта
        logger.exception("Не удалось записать событие журнала %s", action)


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    record(request, "login", f"{user.get_username()} вошёл на сайт", user=user)


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user is not None:
        record(request, "logout", f"{user.get_username()} вышел с сайта", user=user)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    username = str((credentials or {}).get("username", ""))[:150]
    record(request, "login_failed", f"Неверный логин или пароль для «{username}»", username=username)
