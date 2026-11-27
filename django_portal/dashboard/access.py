"""Права доступа: создатель сайта и обычные операторы."""

from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def is_creator(user):
    """Создатель определяется только явно заданным логином сайта."""
    return bool(
        user.is_authenticated
        and settings.CREATOR_DJANGO_USERNAME
        and user.username == settings.CREATOR_DJANGO_USERNAME
    )


def creator_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_creator(request.user):
            raise PermissionDenied("Только создатель сайта может изменять данные.")
        return view(request, *args, **kwargs)
    return wrapped
