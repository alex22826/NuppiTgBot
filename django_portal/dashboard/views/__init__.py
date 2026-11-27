"""Представления сайта. Реализация разделена по разделам в модулях пакета."""

from .chats import (  # noqa: F401
    chat_history,
)
from .common import (  # noqa: F401
    csrf_failure,
    logout_view,
    return_to_photo_list,
)
from .photos import (  # noqa: F401
    dashboard,
    delete_filtered_photos,
    delete_photo,
    edit_photo_metadata,
    photo_detail,
    photo_image,
    photo_lookup,
    photo_original,
    set_photo_decision,
)
from .reports import (  # noqa: F401
    export_full_ten_day_report,
    export_photos,
    export_rejected_photo_report,
)
from .staff import (  # noqa: F401
    add_employee,
    add_site_operator,
    add_visit_assignment,
    delete_employee,
    delete_employee_photos,
    delete_visit_assignment,
    disable_user,
    edit_employee,
    management,
    rename_hospital,
)

__all__ = [
    "csrf_failure",
    "return_to_photo_list",
    "logout_view",
    "dashboard",
    "photo_lookup",
    "photo_detail",
    "photo_original",
    "photo_image",
    "edit_photo_metadata",
    "set_photo_decision",
    "delete_photo",
    "delete_filtered_photos",
    "chat_history",
    "management",
    "add_visit_assignment",
    "delete_visit_assignment",
    "disable_user",
    "delete_employee_photos",
    "delete_employee",
    "rename_hospital",
    "add_employee",
    "edit_employee",
    "add_site_operator",
    "export_full_ten_day_report",
    "export_rejected_photo_report",
    "export_photos",
]
