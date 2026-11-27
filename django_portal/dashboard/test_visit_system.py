import gc
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

import bot

from . import views
from .decision_rules import DECISION_REVIEW, automatic_photo_decision


class DecisionRuleTests(SimpleTestCase):
    def test_all_automatic_results_wait_for_operator(self):
        clean = automatic_photo_decision(
            "Нет явных признаков", "Код распознан: Ташкент",
            is_nuppi_1=True, is_reference_design=True,
        )
        suspicious = automatic_photo_decision(
            "Возможная копия", "Ошибка: код перекрыт", original_photo_id=1,
        )
        foreign_qr = automatic_photo_decision(
            "Нет явных признаков",
            "Посторонний QR/Data Matrix: код вне диапазона региона Ташкент; допустимо 1–10000",
            is_nuppi_1=True,
            is_reference_design=True,
        )
        self.assertEqual(clean["decision"], DECISION_REVIEW)
        self.assertEqual(suspicious["decision"], DECISION_REVIEW)
        self.assertEqual(foreign_qr["decision"], DECISION_REVIEW)
        self.assertIn("Ожидается решение оператора", clean["decision_reason"])
        self.assertIn("разрешённом списке", foreign_qr["decision_reason"])
        self.assertIn("Возможный повтор", suspicious["duplicate_status"])

    def test_automatic_findings_keep_copy_yellow_and_ai_red(self):
        self.assertEqual(
            views.photo_review_group({
                "review_status": "Возможная копия",
                "code_status": "Код распознан: Ташкент",
            }),
            "yellow",
        )
        self.assertEqual(
            views.photo_review_group({
                "review_status": "Обнаружено ИИ-изображение",
                "code_status": "Код распознан: Ташкент",
            }),
            "red",
        )


class VisitSystemTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.db_path = Path(self.temporary.name) / "visit-system.sqlite3"
        self.db_patch = patch.object(bot, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        bot.init_db()
        self.factory = RequestFactory()
        self.settings_override = override_settings(
            BOT_DB_PATH=self.db_path,
            CREATOR_DJANGO_USERNAME="creator",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """INSERT INTO users
                   (telegram_id, username, full_name, city, district, maternity_hospital,
                    role, medic_code, is_active, registered_at)
                   VALUES (1, 'medic', 'Тестовый медик', 'Ташкент', '-', 'Роддом 1',
                           'Медик', '0001', 1, '2026-09-21T00:00:00+00:00')"""
            )

    def test_assignment_is_linked_without_changing_medic_flow(self):
        with sqlite3.connect(self.db_path) as connection:
            assignment_id = connection.execute(
                """INSERT INTO visit_assignments
                   (medic_telegram_id, city, maternity_hospital, doctor_name,
                    required_product, required_material, is_active, created_at)
                   VALUES (1, 'Ташкент', 'Роддом 1', 'Иванова',
                           'NUPPI 1', 'Эталонный дизайн', 1, '2026-09-21T00:00:00+00:00')"""
            ).lastrowid
        visit_id = bot.create_visit(1, "Ташкент", "Роддом 1", "Иванова")
        with sqlite3.connect(self.db_path) as connection:
            linked = connection.execute(
                "SELECT assignment_id FROM visits WHERE id = ?", (visit_id,),
            ).fetchone()[0]
        self.assertEqual(linked, assignment_id)
        self.assertEqual(
            bot.assignment_check_status(
                {"required_product": "NUPPI 1", "required_material": "Эталонный дизайн"},
                True, True, "ok",
            ),
            "Соответствует заданию",
        )

    def test_operator_can_reject_with_reason_while_automatic_result_stays_review(self):
        with sqlite3.connect(self.db_path) as connection:
            photo_id = connection.execute(
                """INSERT INTO photos
                   (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id,
                    image, sent_at, review_status, review_reason, code_status)
                   VALUES (1, 1, '0001', 'file', X'00', '2026-09-21T10:00:00+00:00',
                           'Нет явных признаков', '', 'Код распознан: Ташкент')"""
            ).lastrowid
        request = self.factory.post(
            reverse("set_photo_decision", args=[photo_id]),
            {"decision": "rejected", "reason": "Материал размещён неправильно", "next": "/"},
        )
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")
        with patch.object(views.messages, "success"):
            response = views.set_photo_decision(request, photo_id)
        self.assertEqual(response.status_code, 302)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT decision, manual_decision, manual_reason FROM photo_checks WHERE photo_id = ?",
                (photo_id,),
            ).fetchone()
        self.assertEqual(row, (DECISION_REVIEW, "rejected", "Материал размещён неправильно"))

    def test_operator_decision_can_be_saved_without_page_reload(self):
        with sqlite3.connect(self.db_path) as connection:
            photo_id = connection.execute(
                """INSERT INTO photos
                   (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id,
                    image, sent_at, review_status, review_reason, code_status)
                   VALUES (1, 1, '0001', 'file', X'00', '2026-09-21T10:00:00+00:00',
                           'Нет явных признаков', '', 'Код отсутствует (допустимо)')"""
            ).lastrowid
        request = self.factory.post(
            reverse("set_photo_decision", args=[photo_id]),
            {"decision": "accepted", "reason": "Проверено"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")

        response = views.set_photo_decision(request, photo_id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["decision"], "accepted")
        self.assertEqual(payload["previous_decision"], DECISION_REVIEW)

    def test_ajax_rejection_without_reason_returns_json_error(self):
        request = self.factory.post(
            reverse("set_photo_decision", args=[1]),
            {"decision": "rejected", "reason": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")

        response = views.set_photo_decision(request, 1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("укажите причину", json.loads(response.content)["error"])

    def test_photo_metadata_can_be_saved_without_page_reload(self):
        with sqlite3.connect(self.db_path) as connection:
            photo_id = connection.execute(
                """INSERT INTO photos
                   (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id,
                    image, sent_at, review_status, review_reason, code_status)
                   VALUES (1, 1, '0001', 'file', X'00', '2026-09-21T10:00:00+00:00',
                           'Нет явных признаков', '', 'Код отсутствует (допустимо)')"""
            ).lastrowid
        request = self.factory.post(
            reverse("edit_photo_metadata", args=[photo_id]),
            {
                "photo_date": "2026-10-02",
                "mother_name": "Каримова Малика",
                "qr_data": "01 / 00000 00123",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")

        response = views.edit_photo_metadata(request, photo_id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["photo_date"], "2026-10-02")
        self.assertEqual(payload["mother_name"], "Каримова Малика")
        self.assertEqual(payload["qr_data"], "01-0000000123")
        self.assertEqual(payload["code_status"], "Код распознан: Ташкент")
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT photo_date, mother_name, qr_data, code_status FROM photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
        self.assertEqual(
            row,
            ("2026-10-02", "Каримова Малика", "01-0000000123", "Код распознан: Ташкент"),
        )

    def test_operator_can_clear_qr_and_invalid_qr_is_rejected(self):
        with sqlite3.connect(self.db_path) as connection:
            photo_id = connection.execute(
                """INSERT INTO photos
                   (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id,
                    image, sent_at, review_status, review_reason, qr_data, code_status)
                   VALUES (1, 1, '0001', 'file', X'00', '2026-09-21T10:00:00+00:00',
                           'Нет явных признаков', '', '01-0000000123',
                           'Код распознан: Ташкент')"""
            ).lastrowid

        invalid = self.factory.post(
            reverse("edit_photo_metadata", args=[photo_id]),
            {"photo_date": "", "mother_name": "", "qr_data": "неполный код"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        invalid.user = SimpleNamespace(is_authenticated=True, username="operator-1")
        invalid_response = views.edit_photo_metadata(invalid, photo_id)
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn("NN-NNNNNNNNNN", json.loads(invalid_response.content)["error"])

        clear = self.factory.post(
            reverse("edit_photo_metadata", args=[photo_id]),
            {"photo_date": "", "mother_name": "", "qr_data": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        clear.user = SimpleNamespace(is_authenticated=True, username="operator-1")
        clear_response = views.edit_photo_metadata(clear, photo_id)
        self.assertEqual(clear_response.status_code, 200)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT qr_data, code_status FROM photos WHERE id = ?", (photo_id,),
            ).fetchone()
        self.assertEqual(row, (None, "Код отсутствует (допустимо)"))

    def test_ajax_photo_metadata_rejects_invalid_date_without_redirect(self):
        request = self.factory.post(
            reverse("edit_photo_metadata", args=[1]),
            {"photo_date": "02.10.2026", "mother_name": "Каримова Малика"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")

        response = views.edit_photo_metadata(request, 1)

        self.assertEqual(response.status_code, 400)
        self.assertIn("ГГГГ-ММ-ДД", json.loads(response.content)["error"])

    def test_operator_sees_highlighted_manual_decision_controls(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """INSERT INTO photos
                   (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id,
                    image, sent_at, review_status, review_reason, code_status)
                   VALUES (1, 1, '0001', 'file', X'00', '2026-09-21T10:00:00+00:00',
                           'Нет явных признаков', '', 'Код распознан: Ташкент')"""
            )
        request = self.factory.get(reverse("dashboard"))
        request.user = SimpleNamespace(is_authenticated=True, username="operator-1")

        response = views.dashboard(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("На проверке", content)
        self.assertNotIn('decision-badge accepted', content)
        self.assertIn("Ручное решение оператора", content)
        self.assertIn('name="decision" value="accepted"', content)
        self.assertIn('name="decision" value="review"', content)
        self.assertIn('name="decision" value="rejected"', content)
        self.assertIn('name="qr_data"', content)
        self.assertIn("Сохранить дату, ФИО и QR", content)
        self.assertIn("decision_controls.js", content)
