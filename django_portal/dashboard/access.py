"""Права доступа: создатель сайта и обычные операторы."""

from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def is_creator(user):
    """Создатель — логин из CREATOR_DJANGO_USERNAME.

    Если логин не задан, создателями считаются суперпользователи: так сайт сразу
    готов к работе после ``python manage.py createsuperuser``.
    """
    if not user.is_authenticated:
        return False
    if settings.CREATOR_DJANGO_USERNAME:
        return user.username == settings.CREATOR_DJANGO_USERNAME
    return bool(getattr(user, "is_superuser", False))


def creator_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_creator(request.user):
            raise PermissionDenied("Только создатель сайта может изменять данные.")
        return view(request, *args, **kwargs)
    return wrapped
