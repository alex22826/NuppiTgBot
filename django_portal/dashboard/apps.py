from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"
    verbose_name = "Панель Nuppi"

    def ready(self):
        # Подписывает журнал на входы и выходы пользователей.
        from . import audit  # noqa: F401
