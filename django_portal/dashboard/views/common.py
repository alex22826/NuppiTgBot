"""Общие представления: CSRF, выход, возврат к списку."""

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse


def csrf_failure(request, reason=""):
    """Recover from an expired login form without weakening CSRF protection."""
    if request.path == reverse("login"):
        query = request.GET.copy()
        query["csrf"] = "1"
        return redirect(f"{reverse('login')}?{query.urlencode()}")

    from django.views.csrf import csrf_failure as django_csrf_failure

    return django_csrf_failure(request, reason=reason)


def return_to_photo_list(request):
    """После действия оставляет пользователя на тех же фильтрах списка."""
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("dashboard")


def logout_view(request):
    """Выход с сайта и немедленный переход на страницу входа."""
    logout(request)
    return redirect("login")
