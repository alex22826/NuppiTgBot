"""Карточка фото по ТЗ: похожие фото, сводка проверок, сравнение, данные с упаковки."""
import gc
import json
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from ..db import bot_database
from ..models import AuditEvent, StaffProfile
from ..roles import SUPERVISOR
from ..similar import find_similar_photos, photo_checks_summary

PASSWORD = "Ochen-Slozhnyi-Parol-42"
BASE = "f0f0f0f0f0f0f0f0"


def flip(hex_value, bits):
    """Тот же 64-битный отпечаток с изменёнными младшими битами."""
    return f"{int(hex_value, 16) ^ ((1 << bits) - 1):016x}"


def jpeg(width, height):
    buffer = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


@override_settings(CREATOR_DJANGO_USERNAME="")
class PhotoCardTests(TestCase):
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
                code_status TEXT, image_digest TEXT, qr_data TEXT, original_photo_id INTEGER,
                image_hash TEXT, visual_hashes TEXT, maternity_hospital TEXT, image BLOB);
            CREATE TABLE employee_hospitals (telegram_id INTEGER, city TEXT, maternity_hospital TEXT);
            INSERT INTO users VALUES (1, 'Medic A', NULL, 'Ташкент', 'Роддом 1', 'Медик', '0001', 1);
            INSERT INTO users VALUES (2, 'Medic B', NULL, 'Самарканд', 'Роддом 2', 'Медик', '0002', 1);
        """)
        rows = [
            # id, sender, sent_at, qr, digest, name, date, image_hash, visual
            (1, 1, "2026-09-10T10:00:00+00:00", "01-0000000001", "d1", "Иванова Анна", "2026-09-01", BASE, None),
            (2, 1, "2026-09-01T10:00:00+00:00", "01-0000000001", "d2", None, None, None, None),
            (3, 2, "2026-09-02T10:00:00+00:00", None, "d1", None, None, None, None),
            (4, 1, "2026-09-03T10:00:00+00:00", None, "d4", "ИВАНОВА  анна", "2026-09-01", None, None),
            (5, 1, "2026-09-04T10:00:00+00:00", None, "d5", None, None, flip(BASE, 3), None),
            (6, 1, "2026-09-05T10:00:00+00:00", None, "d6", None, None, "0" * 16,
             json.dumps({"phash": [flip(BASE, 10)], "edge": ["1" * 16], "dhash": []})),
            (7, 1, "2026-09-06T10:00:00+00:00", None, "d7", None, None, flip(BASE, 12), None),
            (8, 1, "2026-09-07T10:00:00+00:00", None, "d8", None, None, "0" * 16, None),
        ]
        for photo_id, sender, sent_at, qr, digest, name, date, image_hash, visual in rows:
            connection.execute(
                """INSERT INTO photos (id, sender_telegram_id, doctor_name, sent_at, qr_data, image_digest,
                       mother_name, photo_date, image_hash, visual_hashes, review_status, review_reason, image)
                   VALUES (?, ?, 'Doctor', ?, ?, ?, ?, ?, ?, ?, 'Нет явных признаков', '', ?)""",
                (photo_id, sender, sent_at, qr, digest, name, date, image_hash, visual, jpeg(40, 30)),
            )
        # Отпечаток «контуров» у фото 1 совпадает с фото 6 — вместе с pHash это два семейства.
        connection.execute("UPDATE photos SET visual_hashes = ? WHERE id = 1",
                           (json.dumps({"phash": [], "edge": ["1" * 16], "dhash": []}),))
        connection.commit()
        connection.close()
        override = override_settings(BOT_DB_PATH=self.db_path, PHOTO_THUMBNAIL_CACHE_DIR=Path(self.temporary.name) / "cache")
        override.enable()
        self.addCleanup(override.disable)
        self.owner = get_user_model().objects.create_superuser("owner", password=PASSWORD)

    def similar_for(self, photo_id, regions=None):
        connection = bot_database()
        try:
            from ..photos import photo_details_for_operator
            photo = photo_details_for_operator(connection, photo_id)
            return photo, find_similar_photos(connection, photo, regions)
        finally:
            connection.close()

    def test_every_kind_of_match_is_found_with_reasons(self):
        _, similar = self.similar_for(1)
        reasons = {item["id"]: [reason["code"] for reason in item["reasons"]] for item in similar}
        self.assertEqual(reasons[2], ["same_code"])
        self.assertEqual(reasons[3], ["same_file"])
        self.assertEqual(reasons[4], ["same_name_date"])
        self.assertEqual(reasons[5], ["same_shot"])
        self.assertEqual(reasons[6], ["visual"])
        # Совпал только один отпечаток — у одинаковых коробок так бывает всегда.
        self.assertNotIn(7, reasons)
        self.assertNotIn(8, reasons)
        # Самые весомые совпадения идут первыми.
        self.assertEqual(similar[0]["id"], 3)

    def test_checks_summary_flags_earlier_repeat_and_name_date(self):
        photo, similar = self.similar_for(1)
        checks = {check["title"]: check for check in photo_checks_summary(photo, similar)}
        self.assertEqual(checks["Читаемость"]["tone"], "ok")
        self.assertEqual(checks["Повтор"]["tone"], "bad")
        self.assertIn("№3", checks["Повтор"]["text"])
        self.assertEqual(checks["ФИО и дата с прошлыми записями"]["tone"], "warn")
        photo, similar = self.similar_for(8)
        checks = {check["title"]: check for check in photo_checks_summary(photo, similar)}
        self.assertEqual(checks["Читаемость"]["text"], "Данные с упаковки не прочитаны")
        self.assertEqual(checks["ФИО и дата с прошлыми записями"]["tone"], "neutral")

    def test_detail_page_shows_similar_checks_and_reasons(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("photo_detail", args=[1])).content.decode()
        for text in ("Похожие фото", "Тот же код на упаковке", "Совпадают ФИО и дата", "Проверки",
                     "Данные с упаковки", "Не видно код на упаковке", "?preview=1",
                     reverse("photo_compare", args=[1, 3])):
            self.assertIn(text, page)

    def test_compare_only_related_photos(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("photo_compare", args=[1, 4]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Совпадают ФИО и дата")
        self.assertContains(response, 'class="same"')
        self.assertEqual(self.client.get(reverse("photo_compare", args=[1, 8])).status_code, 404)

    def test_supervisor_sees_foreign_match_without_details(self):
        supervisor = get_user_model().objects.create_user("sup", password=PASSWORD)
        StaffProfile.objects.create(user=supervisor, role=SUPERVISOR, regions="Ташкент")
        _, similar = self.similar_for(1, regions=["Ташкент"])
        foreign = next(item for item in similar if item["id"] == 3)
        self.assertFalse(foreign["in_scope"])
        self.client.force_login(supervisor)
        page = self.client.get(reverse("photo_detail", args=[1])).content.decode()
        self.assertIn("другой регион", page)
        self.assertNotIn(reverse("photo_compare", args=[1, 3]), page)
        self.assertEqual(self.client.get(reverse("photo_compare", args=[1, 3])).status_code, 404)

    def test_brand_and_formula_are_saved_and_logged(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("edit_photo_metadata", args=[8]),
            {"photo_date": "", "mother_name": "", "qr_data": "", "package_brand": "nuppi gold", "package_formula": "1"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.json()["package_brand"], "NUPPI GOLD")
        connection = sqlite3.connect(self.db_path)
        row = connection.execute("SELECT package_brand, package_formula FROM photos WHERE id = 8").fetchone()
        connection.close()
        self.assertEqual(row, ("NUPPI GOLD", "1"))
        event = AuditEvent.objects.get(action="photo_edit")
        self.assertIn("марка «—» → «NUPPI GOLD»", event.summary)

    def test_working_copy_is_larger_than_thumbnail_but_smaller_than_original(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE photos SET image = ? WHERE id = 8", (jpeg(3000, 2000),))
        connection.commit()
        connection.close()
        self.client.force_login(self.owner)
        sizes = {}
        for name, query in (("thumbnail", {"thumbnail": "1"}), ("preview", {"preview": "1"}), ("original", {})):
            response = self.client.get(reverse("photo_image", args=[8]), query)
            sizes[name] = Image.open(BytesIO(response.content)).size
        self.assertEqual(sizes["original"], (3000, 2000))
        self.assertEqual(max(sizes["preview"]), 1600)
        self.assertEqual(max(sizes["thumbnail"]), 480)
