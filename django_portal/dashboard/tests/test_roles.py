"""Роли, права по разделам, регионы руководителей и журнал действий."""
import gc
import sqlite3
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from ..access import has_perm, user_regions, user_role
from ..models import AuditEvent, StaffProfile
from ..roles import ADMIN, OWNER, PHOTO_OPERATOR, SUPERVISOR

PASSWORD = "Ochen-Slozhnyi-Parol-42"


@override_settings(CREATOR_DJANGO_USERNAME="")
class RolesAndAuditTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.db_path = Path(self.temporary.name) / "bot.sqlite3"
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
                city TEXT, maternity_hospital TEXT, role TEXT, medic_code TEXT, is_active INTEGER);
            CREATE TABLE photos (id INTEGER PRIMARY KEY, sender_telegram_id INTEGER, doctor_name TEXT,
                sent_at TEXT, photo_date TEXT, mother_name TEXT, review_status TEXT, review_reason TEXT,
                code_status TEXT, image_digest TEXT, qr_data TEXT, original_photo_id INTEGER, image BLOB);
            INSERT INTO users VALUES (1, 'Medic A', NULL, 'Ташкент', 'Роддом 1', 'Медик', '0001', 1);
            INSERT INTO users VALUES (2, 'Medic B', NULL, 'Самарканд', 'Роддом 2', 'Медик', '0002', 1);
            INSERT INTO photos (id, sender_telegram_id, doctor_name, sent_at, review_status, review_reason, image)
                VALUES (1, 1, 'Doctor A', '2026-09-01T10:00:00+00:00', 'Нет явных признаков', '', X'00');
            INSERT INTO photos (id, sender_telegram_id, doctor_name, sent_at, review_status, review_reason, image)
                VALUES (2, 2, 'Doctor B', '2026-09-01T11:00:00+00:00', 'Нет явных признаков', '', X'00');
            INSERT INTO photos (id, sender_telegram_id, doctor_name, sent_at, review_status, review_reason,
                                original_photo_id, image)
                VALUES (3, 1, 'Doctor A', '2026-09-02T10:00:00+00:00', 'Возможная копия', '', 2, X'00');
        """)
        connection.commit()
        connection.close()
        override = override_settings(BOT_DB_PATH=self.db_path)
        override.enable()
        self.addCleanup(override.disable)

        users = get_user_model().objects
        self.owner = users.create_superuser("owner", password=PASSWORD)
        self.admin = self.make_user("admin_user", ADMIN)
        self.operator = self.make_user("operator", PHOTO_OPERATOR)
        self.supervisor = self.make_user("supervisor", SUPERVISOR, regions="Ташкент")

    def make_user(self, username, role, regions=""):
        user = get_user_model().objects.create_user(username, password=PASSWORD)
        StaffProfile.objects.create(user=user, role=role, regions=regions)
        return user

    def photo_ids(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return [row[0] for row in connection.execute("SELECT id FROM photos ORDER BY id")]
        finally:
            connection.close()

    # ---------- роли и права ----------

    def test_role_resolution(self):
        self.assertEqual(user_role(self.owner), OWNER)
        self.assertEqual(user_role(self.admin), ADMIN)
        self.assertEqual(user_role(self.supervisor), SUPERVISOR)
        no_profile = get_user_model().objects.create_user("plain", password=PASSWORD)
        self.assertEqual(user_role(no_profile), PHOTO_OPERATOR)
        self.assertEqual(user_regions(self.supervisor), ["Ташкент"])
        self.assertIsNone(user_regions(self.admin))
        self.assertFalse(has_perm(self.operator, "photos.delete"))
        self.assertTrue(has_perm(self.admin, "photos.delete"))

    @override_settings(CREATOR_DJANGO_USERNAME="admin_user")
    def test_owner_from_settings_wins_over_profile(self):
        self.assertEqual(user_role(self.admin), OWNER)
        # Когда владелец задан в настройках, суперпользователь без профиля — обычный оператор.
        self.assertEqual(user_role(self.owner), PHOTO_OPERATOR)

    def test_operator_cannot_open_admin_sections_or_delete(self):
        self.client.force_login(self.operator)
        for name in ("users", "journal", "management"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        response = self.client.post(reverse("delete_photo", args=[1]))
        self.assertEqual(response.status_code, 403)
        self.assertIn(1, self.photo_ids())
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn(reverse("users"), page)
        self.assertNotIn(reverse("journal"), page)
        self.assertNotIn("Удалить фото", page)
        self.assertIn("Оператор проверки фото", page)

    def test_admin_deletes_photo_and_it_is_logged(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("delete_photo", args=[1]), {"next": "/"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(1, self.photo_ids())
        event = AuditEvent.objects.get(action="photo_delete")
        self.assertEqual(event.username, "admin_user")
        self.assertEqual(event.role, ADMIN)
        self.assertEqual(event.object_id, "1")
        self.assertIn("Medic A", event.summary)

    def test_decision_is_logged_with_previous_state_and_reason(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("set_photo_decision", args=[1]),
            {"decision": "rejected", "reason": "Не видно код"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(action="photo_decision")
        self.assertEqual(event.summary, "Фото №1: На проверке → Отклонено. Причина: Не видно код")
        self.assertEqual(event.details["previous_decision"], "review")
        detail = self.client.get(reverse("photo_detail", args=[1])).content.decode()
        self.assertIn("Проверил operator", detail)
        self.assertIn("История", detail)

    # ---------- регионы руководителя ----------

    def test_supervisor_sees_only_own_region(self):
        self.client.force_login(self.supervisor)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(sorted(photo["id"] for photo in response.context["photos"]), [1, 3])
        # В списке городов только свой регион.
        self.assertEqual(response.context["cities"], ["Ташкент"])
        self.assertEqual(self.client.get(reverse("photo_detail", args=[2])).status_code, 404)
        self.assertEqual(self.client.get(reverse("photo_detail", args=[1])).status_code, 200)
        # Оригинал из чужого региона открывается только как пара к своей копии.
        self.assertEqual(self.client.get(reverse("photo_image", args=[2])).status_code, 200)
        response = self.client.post(
            reverse("set_photo_decision", args=[2]), {"decision": "accepted"}, HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_supervisor_without_copy_link_cannot_open_foreign_image(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE photos SET original_photo_id = NULL")
        connection.commit()
        connection.close()
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("photo_image", args=[2])).status_code, 404)

    # ---------- пользователи и роли ----------

    def test_admin_creates_supervisor_but_not_admin(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("user_add"), {
            "username": "new_sup", "password": PASSWORD, "full_name": "Новый Руководитель",
            "role": SUPERVISOR, "regions": " Ташкент ,  Самарканд ",
        })
        user = get_user_model().objects.get(username="new_sup")
        self.assertEqual(user_role(user), SUPERVISOR)
        self.assertEqual(user.staff_profile.regions, "Ташкент, Самарканд")
        self.assertFalse(user.is_superuser)
        self.assertTrue(AuditEvent.objects.filter(action="user_add", object_id=str(user.pk)).exists())

        self.client.post(reverse("user_add"), {"username": "new_admin", "password": PASSWORD, "role": ADMIN})
        self.assertFalse(get_user_model().objects.filter(username="new_admin").exists())

    def test_admin_cannot_edit_owner_or_self(self):
        self.client.force_login(self.admin)
        for target in (self.owner, self.admin):
            with self.subTest(target=target.username):
                response = self.client.post(reverse("user_edit", args=[target.pk]), {"role": PHOTO_OPERATOR})
                self.assertRedirects(response, reverse("users"))
        self.assertEqual(user_role(self.owner), OWNER)

    def test_user_edit_changes_role_and_logs_diff(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("user_edit", args=[self.operator.pk]), {
            "full_name": "Оператор Один", "role": SUPERVISOR, "regions": "Самарканд", "is_active": "on",
        })
        self.operator.refresh_from_db()
        self.assertEqual(user_role(self.operator), SUPERVISOR)
        event = AuditEvent.objects.get(action="user_edit")
        self.assertIn("роль: «Оператор проверки фото» → «Руководитель»", event.summary)
        self.assertIn("регионы: «» → «Самарканд»", event.summary)

    def test_users_page_shows_roles_and_matrix(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("users")).content.decode()
        for text in ("Пользователи сайта", "Что может каждая роль", "Руководитель", "supervisor", "Удалять фото"):
            self.assertIn(text, page)

    # ---------- журнал ----------

    def test_login_success_and_failure_are_logged(self):
        self.client.post(reverse("login"), {"username": "operator", "password": "wrong"})
        self.client.post(reverse("login"), {"username": "operator", "password": PASSWORD})
        actions = list(AuditEvent.objects.order_by("id").values_list("action", "username"))
        self.assertEqual(actions, [("login_failed", "operator"), ("login", "operator")])

    def test_journal_filters_by_user_and_group(self):
        self.client.force_login(self.operator)
        self.client.post(reverse("set_photo_decision", args=[1]), {"decision": "accepted"})
        self.client.force_login(self.owner)
        response = self.client.get(reverse("journal"), {"group": "Фото"})
        self.assertEqual([event.action for event in response.context["page"].object_list], ["photo_decision"])
        response = self.client.get(reverse("journal"), {"user": "owner"})
        self.assertEqual({event.action for event in response.context["page"].object_list}, {"login"})
        response = self.client.get(reverse("journal"), {"object_type": "photo", "object_id": "1"})
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertContains(response, "Фото №1: На проверке → Принято")
