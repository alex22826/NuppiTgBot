"""Данные самого сайта: профили пользователей и журнал действий.

Фото, сотрудники бота и переписка по-прежнему хранятся в базе бота.
"""

from django.conf import settings
from django.db import models

from .roles import DEFAULT_ROLE, ROLE_CHOICES
from .text import doctor_search_key


class StaffProfile(models.Model):
    """Роль и регионы пользователя сайта."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profile",
    )
    full_name = models.CharField("ФИО", max_length=200, blank=True)
    role = models.CharField("Роль", max_length=32, choices=ROLE_CHOICES, default=DEFAULT_ROLE)
    regions = models.TextField(
        "Регионы", blank=True,
        help_text="Города через запятую. Пусто — все регионы.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "профиль пользователя"
        verbose_name_plural = "профили пользователей"

    def __str__(self):
        return self.full_name or self.user.get_username()

    def region_list(self):
        seen, result = set(), []
        for item in self.regions.split(","):
            name = " ".join(item.split())
            key = doctor_search_key(name)
            if key and key not in seen:
                seen.add(key)
                result.append(name)
        return result


class AuditEvent(models.Model):
    """Одна запись журнала: кто, когда и что сделал."""

    created_at = models.DateTimeField("Когда", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    # Логин и роль сохраняются текстом: запись остаётся понятной, даже если
    # пользователя потом переименуют или удалят.
    username = models.CharField("Логин", max_length=150, blank=True)
    role = models.CharField("Роль", max_length=32, blank=True)
    action = models.CharField("Действие", max_length=40, db_index=True)
    object_type = models.CharField("Объект", max_length=40, blank=True)
    object_id = models.CharField("Номер объекта", max_length=64, blank=True)
    summary = models.TextField("Описание", blank=True)
    details = models.JSONField("Подробности", default=dict, blank=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "событие журнала"
        verbose_name_plural = "журнал действий"
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.username}: {self.summary}"
