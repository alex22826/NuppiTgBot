from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django использует этот адрес по умолчанию после входа. Оставляем
    # перенаправление как защиту для старых настроек или закладок браузера.
    path("accounts/profile/", RedirectView.as_view(pattern_name="dashboard", permanent=False)),
    path("", include("dashboard.urls")),
]
