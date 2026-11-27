"""Роли сайта и права по разделам (ТЗ, раздел 3).

Роли и их права заданы здесь, в коде: администратор назначает сотруднику роль,
а не собирает права вручную. Так список «кто что может» всегда один и тот же
и его легко показать на странице «Пользователи и роли».

Роли системы продаж (агент, экспедитор, склад, кассир) добавятся вместе с её
разделами — пока им нечего показывать.
"""

OWNER = "owner"
ADMIN = "admin"
SUPERVISOR = "supervisor"
PHOTO_OPERATOR = "photo_operator"

# Права: код → (раздел, что разрешает).
PERMISSIONS = {
    "photos.view": ("Фотографии", "Смотреть фото и их данные"),
    "photos.decide": ("Фотографии", "Принимать фото и возвращать на проверку"),
    "photos.reject": ("Фотографии", "Отклонять фото с причиной"),
    "photos.edit": ("Фотографии", "Исправлять дату, ФИО и QR на фото"),
    "photos.delete": ("Фотографии", "Удалять фото"),
    "reports.view": ("Отчёты", "Смотреть отчёты и статистику"),
    "reports.export": ("Отчёты", "Скачивать Excel"),
    "chats.view": ("Переписка", "Читать переписку с ботом"),
    "staff.manage": ("Управление", "Сотрудники бота, роддома, задания"),
    "users.manage": ("Пользователи", "Входы на сайт и роли"),
    "audit.view": ("Журнал", "Смотреть журнал действий"),
}

ROLES = {
    OWNER: {
        "label": "Владелец",
        "description": "Видит все разделы и все регионы. Назначает администраторов.",
        "permissions": frozenset(PERMISSIONS),
        "scoped": False,
    },
    ADMIN: {
        "label": "Администратор",
        "description": "Заводит сотрудников и входы на сайт, назначает роли, ведёт справочники.",
        "permissions": frozenset(PERMISSIONS),
        "scoped": False,
    },
    SUPERVISOR: {
        "label": "Руководитель",
        "description": "Проверяет фото и смотрит отчёты своих регионов. Удалять фото не может.",
        "permissions": frozenset({
            "photos.view", "photos.decide", "photos.reject", "photos.edit",
            "reports.view", "reports.export", "chats.view",
        }),
        "scoped": True,
    },
    PHOTO_OPERATOR: {
        "label": "Оператор проверки фото",
        "description": "Проверяет фотоотчёты: принимает или отклоняет с причиной. Удалять фото не может.",
        "permissions": frozenset({
            "photos.view", "photos.decide", "photos.reject", "photos.edit",
            "reports.view", "reports.export", "chats.view",
        }),
        "scoped": True,
    },
}

ROLE_CHOICES = [(code, role["label"]) for code, role in ROLES.items()]
DEFAULT_ROLE = PHOTO_OPERATOR

# Кого какая роль может назначать: администратор не может сделать кого-то
# владельцем или администратором — только владелец.
ASSIGNABLE_ROLES = {
    OWNER: [OWNER, ADMIN, SUPERVISOR, PHOTO_OPERATOR],
    ADMIN: [SUPERVISOR, PHOTO_OPERATOR],
}


def role_label(code):
    return ROLES.get(code, {}).get("label", code or "—")


def permission_matrix():
    """Таблица «право × роль» для страницы ролей."""
    rows = []
    for code, (section, title) in PERMISSIONS.items():
        rows.append({
            "code": code,
            "section": section,
            "title": title,
            "roles": [code in role["permissions"] for role in ROLES.values()],
        })
    return rows
