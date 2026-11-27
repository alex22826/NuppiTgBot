from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="dashboard/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", views.logout_view, name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("chats/", views.chat_history, name="chat_history"),
    path("management/", views.management, name="management"),
    path("photos/find/", views.photo_lookup, name="photo_lookup"),
    path("assignments/add/", views.add_visit_assignment, name="add_visit_assignment"),
    path("assignments/<int:assignment_id>/delete/", views.delete_visit_assignment, name="delete_visit_assignment"),
    path("photo/<int:photo_id>/", views.photo_image, name="photo_image"),
    path("photo/<int:photo_id>/view/", views.photo_detail, name="photo_detail"),
    path("photo/<int:photo_id>/original/", views.photo_original, name="photo_original"),
    path("photo/<int:photo_id>/edit/", views.edit_photo_metadata, name="edit_photo_metadata"),
    path("photo/<int:photo_id>/decision/", views.set_photo_decision, name="set_photo_decision"),
    path("photo/<int:photo_id>/delete/", views.delete_photo, name="delete_photo"),
    path("photos/delete-filtered/", views.delete_filtered_photos, name="delete_filtered_photos"),
    path("photos/export.xlsx", views.export_photos, name="export_photos"),
    path(
        "reports/full-last-10-days.xlsx",
        views.export_full_ten_day_report,
        name="export_full_ten_day_report",
    ),
    path(
        "reports/rejected-photos.xlsx",
        views.export_rejected_photo_report,
        name="export_rejected_photo_report",
    ),
    path("hospitals/rename/", views.rename_hospital, name="rename_hospital"),
    path("users/<int:telegram_id>/disable/", views.disable_user, name="disable_user"),
    path("users/<int:telegram_id>/photos/delete/", views.delete_employee_photos, name="delete_employee_photos"),
    path("users/<int:telegram_id>/delete/", views.delete_employee, name="delete_employee"),
    path("employees/add/", views.add_employee, name="add_employee"),
    path("employees/<int:telegram_id>/edit/", views.edit_employee, name="edit_employee"),
    path("site-operators/add/", views.add_site_operator, name="add_site_operator"),
]
