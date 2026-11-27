import asyncio
import gc
import importlib.util
import sqlite3
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook, load_workbook
from PIL import Image

from . import views
from . import db as site_db, photos as site_photos, reports as site_reports, text as site_text
from .views import photos as photo_views


class OriginalPhotoTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.db_path = Path(self.temporary.name) / 'photos.sqlite3'
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript('''
            CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
                city TEXT, maternity_hospital TEXT, role TEXT, medic_code TEXT, is_active INTEGER);
            CREATE TABLE photos (id INTEGER PRIMARY KEY, sender_telegram_id INTEGER, doctor_name TEXT,
                sent_at TEXT, photo_date TEXT, mother_name TEXT, review_status TEXT, review_reason TEXT,
                image_digest TEXT, qr_data TEXT, original_photo_id INTEGER, image BLOB);
            INSERT INTO users VALUES (1, 'Medic A', NULL, 'City A', 'Hospital A', 'Медик', '0001', 1);
            INSERT INTO users VALUES (2, 'Medic B', NULL, 'City B', 'Hospital B', 'Медик', '0002', 1);
        ''')
        self.settings_override = override_settings(BOT_DB_PATH=self.db_path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.factory = RequestFactory()

    def add_photo(self, photo_id, **overrides):
        values = dict(id=photo_id, sender_telegram_id=1, doctor_name='Test Doctor',
                      sent_at=f'2026-08-01T10:00:{photo_id % 60:02d}+00:00',
                      review_status='Нет явных признаков', review_reason='', image=b'test')
        values.update(overrides)
        self.connection.execute(
            f"INSERT INTO photos ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()),
        )
        self.connection.commit()

    def resolve(self, photo_id):
        return site_photos.resolve_original_photo(self.connection, site_photos.photo_details(self.connection, photo_id))

    def request(self, photo_id, authenticated=True):
        request = self.factory.get(reverse('photo_original', args=[photo_id]), {'city': 'City B', 'medic': '2'})
        request.user = SimpleNamespace(is_authenticated=True, username='operator') if authenticated else AnonymousUser()
        return request

    def test_chain_opens_root_outside_filters_and_last_300(self):
        self.add_photo(1, image_digest='root')
        self.add_photo(400, sender_telegram_id=2, review_status='Возможная копия', original_photo_id=1)
        self.add_photo(401, sender_telegram_id=2, review_status='Возможная копия', original_photo_id=400)
        self.assertEqual(self.resolve(401)['id'], 1)
        response = views.photo_original(self.request(401), 401)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('photo_image', args=[1]))
        self.assertContains(response, 'Medic A')
        self.assertContains(response, 'Medic B')

    def test_comparison_shows_qr_numbers_for_original_and_copy(self):
        self.add_photo(1, qr_data='01-0000000123')
        self.add_photo(
            2,
            qr_data='01-0000000456',
            review_status='Возможная копия',
            original_photo_id=1,
        )

        response = views.photo_original(self.request(2), 2)

        self.assertContains(response, 'Номер QR/Data Matrix:', count=2)
        self.assertContains(response, '01-0000000123')
        self.assertContains(response, '01-0000000456')

    def test_comparison_does_not_invent_missing_qr_number(self):
        self.add_photo(1)
        self.add_photo(2, review_status='Возможная копия', original_photo_id=1)

        response = views.photo_original(self.request(2), 2)

        self.assertNotContains(response, 'Номер QR/Data Matrix:')

    def test_legacy_exact_digest(self):
        self.add_photo(1, image_digest='same')
        self.add_photo(2, image_digest='same', review_status='Возможная копия')
        self.assertEqual(self.resolve(2)['id'], 1)

    def test_legacy_recorded_timestamp(self):
        self.add_photo(1)
        self.add_photo(2, review_status='Возможная копия',
                       review_reason='ИИ подтвердил совпадение с фото от 2026-08-01T10:00:01+00:00; отправитель не учитывается.')
        self.assertEqual(self.resolve(2)['id'], 1)

    def test_legacy_qr(self):
        self.add_photo(1, qr_data='same-code')
        self.add_photo(2, qr_data='same-code', review_status='Возможная копия')
        self.assertEqual(self.resolve(2)['id'], 1)

    def test_old_schema_without_link_column(self):
        self.add_photo(1, image_digest='same')
        self.add_photo(2, image_digest='same', review_status='Возможная копия')
        self.connection.execute('ALTER TABLE photos DROP COLUMN original_photo_id')
        self.connection.commit()
        self.assertEqual(self.resolve(2)['id'], 1)

    def test_deleted_original_does_not_fallback_to_unrelated_photo(self):
        self.add_photo(2, image_digest='same')
        self.add_photo(3, image_digest='same', original_photo_id=1, review_status='Возможная копия')
        self.assertIsNone(self.resolve(3))
        self.assertContains(views.photo_original(self.request(3), 3), 'Не удалось найти оригинал')

    def test_ambiguous_reason_does_not_guess(self):
        for photo_id in (1, 2):
            self.add_photo(photo_id, sent_at='2026-08-01T10:00:00+00:00')
        self.add_photo(3, review_status='Возможная копия', review_reason='Фото от 2026-08-01T10:00:00+00:00')
        self.assertIsNone(self.resolve(3))

    def test_cycles_and_self_links(self):
        self.add_photo(1, original_photo_id=2, review_status='Возможная копия')
        self.add_photo(2, original_photo_id=1, review_status='Возможная копия')
        self.assertIsNone(self.resolve(1))
        self.connection.execute('UPDATE photos SET original_photo_id = id')
        self.assertIsNone(self.resolve(2))

    def test_deleted_sender_keeps_original_available(self):
        self.add_photo(1, sender_telegram_id=999)
        self.add_photo(2, review_status='Возможная копия', original_photo_id=1)
        self.assertEqual(self.resolve(2)['sender_name'], 'Пользователь удалён')

    def test_login_is_required(self):
        response = views.photo_original(self.request(1, authenticated=False), 1)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_missing_and_non_copy_are_not_found(self):
        from django.http import Http404
        self.add_photo(1)
        for photo_id in (1, 99):
            with self.assertRaises(Http404):
                views.photo_original(self.request(photo_id), photo_id)

    def test_list_only_shows_original_button_for_copies_and_keeps_filters(self):
        self.add_photo(1)
        self.add_photo(2, sender_telegram_id=2, review_status='Возможная копия', original_photo_id=1)
        self.add_photo(3, sender_telegram_id=2, review_status='Не проверено')
        request = self.factory.get('/', {'medic': '2'})
        request.user = SimpleNamespace(is_authenticated=True, username='operator')
        response = views.dashboard(request)
        self.assertContains(response, 'Показать оригинал', count=1)
        self.assertContains(response, reverse('photo_original', args=[2]))
        self.assertNotContains(response, reverse('photo_image', args=[1]) + '"')

    def test_operator_dashboard_links_to_chat_history(self):
        request = self.factory.get('/')
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.dashboard(request)

        self.assertContains(response, reverse('chat_history'))
        self.assertNotContains(response, reverse('management'))

    def test_dashboard_has_photo_number_search(self):
        request = self.factory.get('/')
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.dashboard(request)

        self.assertContains(response, 'Открыть фотографию по номеру')
        self.assertContains(response, reverse('photo_lookup'))
        self.assertContains(response, 'name="photo_id"')

    def test_photo_lookup_redirects_to_complete_card(self):
        self.add_photo(8450, qr_data='12-0000000123', mother_name='Test Mother')
        request = self.factory.get(reverse('photo_lookup'), {'photo_id': '№ 8450'})
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.photo_lookup(request)

        self.assertRedirects(
            response, reverse('photo_detail', args=[8450]),
            fetch_redirect_response=False,
        )

    def test_photo_lookup_explains_missing_and_invalid_numbers(self):
        for value, status in [('abc', 400), ('9999', 404)]:
            request = self.factory.get(reverse('photo_lookup'), {'photo_id': value})
            request.user = SimpleNamespace(is_authenticated=True, username='operator')

            response = views.photo_lookup(request)

            self.assertEqual(response.status_code, status)
            self.assertContains(response, 'Фотография', status_code=status)

    def test_photo_detail_shows_number_image_and_metadata(self):
        self.add_photo(
            8450,
            qr_data='12-0000000123',
            mother_name='Test Mother',
            photo_date='2026-11-03',
        )
        request = self.factory.get(reverse('photo_detail', args=[8450]))
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.photo_detail(request, 8450)

        self.assertContains(response, 'Фотография №8450')
        self.assertContains(response, reverse('photo_image', args=[8450]))
        self.assertContains(response, '12-0000000123')
        self.assertContains(response, 'Test Mother')
        self.assertContains(response, '2026-11-03')

    def test_photo_lookup_and_detail_require_login(self):
        lookup_request = self.factory.get(reverse('photo_lookup'), {'photo_id': '1'})
        lookup_request.user = AnonymousUser()
        detail_request = self.factory.get(reverse('photo_detail', args=[1]))
        detail_request.user = AnonymousUser()

        self.assertEqual(views.photo_lookup(lookup_request).status_code, 302)
        self.assertEqual(views.photo_detail(detail_request, 1).status_code, 302)

    def test_photo_card_always_shows_editable_qr_value(self):
        self.add_photo(1, qr_data='01-0000000134')
        self.add_photo(2)

        response, context = self.dashboard_at()

        self.assertEqual(context['photos'][0]['qr_data'], None)
        self.assertEqual(context['photos'][1]['qr_data'], '01-0000000134')
        self.assertContains(response, 'Номер QR/Data Matrix:', count=2)
        self.assertContains(response, '01-0000000134')
        self.assertContains(response, 'name="qr_data"', count=2)

    def test_operator_can_view_saved_chat_with_linked_photo(self):
        self.connection.execute('ALTER TABLE photos ADD COLUMN telegram_file_id TEXT')
        self.connection.execute('''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            message_type TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            sender_name TEXT,
            username TEXT,
            telegram_file_id TEXT,
            telegram_message_id INTEGER,
            created_at TEXT NOT NULL
        )''')
        self.connection.commit()
        self.add_photo(1)
        self.connection.execute(
            "UPDATE photos SET telegram_file_id='photo-file' WHERE id=1"
        )
        self.connection.executemany(
            '''INSERT INTO chat_messages
               (telegram_id, direction, message_type, text, telegram_file_id,
                telegram_message_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            [
                (1, 'incoming', 'photo', 'Проверьте фотографию', 'photo-file', 10,
                 '2026-10-02T08:00:00+00:00'),
                (1, 'outgoing', 'text', 'Фотография принята', None, 11,
                 '2026-10-02T08:00:01+00:00'),
            ],
        )
        self.connection.commit()
        request = self.factory.get(reverse('chat_history'), {'telegram_id': '1'})
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.chat_history(request)

        self.assertContains(response, 'Переписка с пользователями')
        self.assertContains(response, 'Medic A')
        self.assertContains(response, 'Проверьте фотографию')
        self.assertContains(response, 'Фотография принята')
        self.assertContains(response, 'авторизованным операторам')
        self.assertContains(response, reverse('photo_image', args=[1]) + '?thumbnail=1')

    def test_chat_history_requires_login(self):
        request = self.factory.get(reverse('chat_history'))
        request.user = AnonymousUser()

        response = views.chat_history(request)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_photo_viewer_has_rotation_controls_on_list_and_comparison(self):
        self.add_photo(1)
        self.add_photo(2, review_status='Возможная копия', original_photo_id=1)
        request = self.factory.get('/')
        request.user = SimpleNamespace(is_authenticated=True, username='operator')
        response = views.dashboard(request)
        self.assertContains(response, 'class="photo-viewer-link"', count=2)
        self.assertContains(response, 'id="photo-viewer"')
        self.assertContains(response, 'Повернуть налево')
        self.assertContains(response, 'Повернуть направо')
        self.assertNotContains(response, 'data-photo-zoom')
        self.assertNotContains(response, 'id="photo-viewer-zoom"')
        self.assertContains(response, 'dashboard/photo_viewer.js')

        comparison = views.photo_original(self.request(2), 2)
        self.assertContains(comparison, 'class="photo-viewer-link"', count=2)
        self.assertContains(comparison, 'data-photo-rotate="-90"')

        viewer_script = (
            Path(__file__).resolve().parent / 'static' / 'dashboard' / 'photo_viewer.js'
        ).read_text(encoding='utf-8')
        self.assertIn('image.addEventListener("wheel"', viewer_script)
        self.assertIn('event.preventDefault()', viewer_script)
        self.assertIn('scale(${zoom})', viewer_script)
        self.assertIn('image.addEventListener("pointerdown"', viewer_script)
        self.assertIn('image.addEventListener("pointermove"', viewer_script)
        self.assertIn('translate(${panX}px, ${panY}px)', viewer_script)

    def dashboard_at(self, now=None, **filters):
        request = self.factory.get('/', filters)
        request.user = SimpleNamespace(is_authenticated=True, username='operator')
        with override_settings(TIME_ZONE='Asia/Tashkent', USE_TZ=True), \
             patch('django.utils.timezone.now', return_value=now or datetime(2026, 9, 3, 12, tzinfo=timezone.utc)), \
             patch.object(photo_views, 'render', wraps=photo_views.render) as rendering:
            response = views.dashboard(request)
        return response, rendering.call_args.args[2]

    def test_period_counter_renders_zero_between_total_and_medics(self):
        response, context = self.dashboard_at()
        self.assertEqual(context['summary']['photo_count'], 0)
        self.assertEqual(context['scope_summary']['photo_count'], 0)
        self.assertContains(response, '<div id="period-photo-counter"><b>0</b>')
        self.assertContains(response, 'фотографий за выбранный период')
        self.assertContains(response, '<small>Все даты</small>')
        self.assertNotContains(response, 'UTC+5')
        html = response.content.decode()
        self.assertLess(html.index('фотографий за всё время'), html.index('id="period-photo-counter"'))
        self.assertLess(html.index('id="period-photo-counter"'), html.index('<span>медиков'))

    def test_full_report_form_defaults_to_last_ten_days(self):
        response, context = self.dashboard_at()
        self.assertEqual(context['full_report_date_from'], '2026-08-25')
        self.assertEqual(context['full_report_date_to'], '2026-09-03')
        self.assertContains(response, 'action="/reports/full-last-10-days.xlsx"')
        self.assertContains(response, 'value="2026-08-25"')
        self.assertContains(response, 'value="2026-09-03"')
        self.assertContains(response, 'Скачать полный Excel')
        self.assertContains(response, 'action="/reports/rejected-photos.xlsx"')
        self.assertContains(response, 'Отчёт по отклонённым фотографиям')

    def test_rejected_report_contains_only_manual_rejections_and_comments(self):
        self.add_photo(1, sent_at='2026-08-01T10:00:00+00:00', image=b'not-an-image')
        self.add_photo(2, sent_at='2026-08-01T11:00:00+00:00', image=b'not-an-image')
        self.add_photo(3, sent_at='2026-08-03T10:00:00+00:00', image=b'not-an-image')
        self.add_photo(
            4, sender_telegram_id=2, doctor_name='Doctor B',
            sent_at='2026-08-01T12:00:00+00:00', image=b'not-an-image',
        )
        site_db.ensure_photo_checks_table(self.connection)
        self.connection.executemany(
            '''INSERT INTO photo_checks (photo_id, manual_decision, manual_reason)
               VALUES (?, ?, ?)''',
            [
                (1, 'rejected', 'QR-код использован повторно'),
                (2, 'accepted', 'Проверено оператором'),
                (3, 'rejected', 'За пределами выбранного периода'),
                (4, 'rejected', 'Другая группа'),
            ],
        )
        self.connection.commit()

        with override_settings(TIME_ZONE='Asia/Tashkent', USE_TZ=True):
            report = site_reports.rejected_photo_report_data(
                self.connection,
                now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
                date_from='2026-08-01',
                date_to='2026-08-01',
            )

        self.assertEqual(report['total_photos'], 2)
        self.assertEqual(report['records'][0]['id'], 1)
        self.assertEqual(report['records'][0]['operator_comment'], 'QR-код использован повторно')
        workbook = load_workbook(site_reports.build_rejected_photo_report(report), data_only=True)
        sheet = workbook['Общий отчёт']
        self.assertEqual(sheet['H2'].value, 'QR-код использован повторно')
        self.assertEqual(sheet['D2'].value, 'City A')
        self.assertEqual(sheet['E2'].value, 'Hospital A')
        self.assertEqual(
            [cell.value for cell in sheet[1]],
            [
                '№', 'Дата отправки', 'Фотография', 'Город', 'Роддом',
                'Врач', 'Медик', 'Комментарий оператора', 'Автопроверка',
            ],
        )
        self.assertNotIn('Telegram ID', [cell.value for cell in sheet[1]])
        self.assertNotIn('QR/Data Matrix', [cell.value for cell in sheet[1]])
        self.assertNotIn('Дата на фото', [cell.value for cell in sheet[1]])
        self.assertNotIn('ФИО роженицы', [cell.value for cell in sheet[1]])
        self.assertNotIn('Причина автопроверки', [cell.value for cell in sheet[1]])
        self.assertEqual(
            workbook.sheetnames,
            ['Общий отчёт', 'City A — Hospital A', 'City B — Hospital B'],
        )
        city_b_sheet = workbook['City B — Hospital B']
        self.assertEqual(city_b_sheet['F2'].value, 'Doctor B')
        self.assertEqual(city_b_sheet['G2'].value, 'Medic B')
        self.assertEqual(city_b_sheet['H2'].value, 'Другая группа')
        workbook.close()

        request = self.factory.get(
            reverse('export_rejected_photo_report'),
            {'date_from': '2026-08-01', 'date_to': '2026-08-01'},
        )
        request.user = SimpleNamespace(is_authenticated=True, username='operator')
        response = views.export_rejected_photo_report(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('rejected_photos.xlsx', response['Content-Disposition'])

    def test_rejected_report_filters_city_hospital_doctor_medic_and_period(self):
        self.add_photo(
            1, sender_telegram_id=1, doctor_name='Doctor Alpha',
            sent_at='2026-08-01T10:00:00+00:00', image=b'not-an-image',
        )
        self.add_photo(
            2, sender_telegram_id=1, doctor_name='Doctor Beta',
            sent_at='2026-08-01T11:00:00+00:00', image=b'not-an-image',
        )
        self.add_photo(
            3, sender_telegram_id=2, doctor_name='Doctor Alpha',
            sent_at='2026-08-01T12:00:00+00:00', image=b'not-an-image',
        )
        self.add_photo(
            4, sender_telegram_id=1, doctor_name='Doctor Alpha',
            sent_at='2026-08-02T10:00:00+00:00', image=b'not-an-image',
        )
        site_db.ensure_photo_checks_table(self.connection)
        self.connection.executemany(
            '''INSERT INTO photo_checks (photo_id, manual_decision, manual_reason)
               VALUES (?, ?, ?)''',
            [(photo_id, 'rejected', f'Комментарий {photo_id}') for photo_id in range(1, 5)],
        )
        self.connection.create_function(
            'DOCTOR_SEARCH_KEY', 1, site_text.doctor_search_key, deterministic=True,
        )
        self.connection.commit()

        with override_settings(TIME_ZONE='Asia/Tashkent', USE_TZ=True):
            report = site_reports.rejected_photo_report_data(
                self.connection,
                date_from='2026-08-01',
                date_to='2026-08-01',
                city='City A',
                hospital='Hospital A',
                medic='1',
                doctor='Doctor Alpha',
            )

        self.assertEqual([record['id'] for record in report['records']], [1])
        self.assertEqual(report['total_photos'], 1)
        self.assertEqual(report['filters'], {
            'city': 'City A',
            'hospital': 'Hospital A',
            'medic': '1',
            'doctor': 'Doctor Alpha',
        })

    def test_full_report_data_uses_custom_period(self):
        self.add_photo(1, sent_at='2026-08-01T10:00:00+00:00')
        self.add_photo(2, sent_at='2026-08-02T10:00:00+00:00')
        self.add_photo(3, sent_at='2026-08-03T10:00:00+00:00')
        with override_settings(TIME_ZONE='Asia/Tashkent', USE_TZ=True):
            report = site_reports.full_ten_day_report_data(
                self.connection,
                now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
                date_from='2026-08-01',
                date_to='2026-08-02',
            )
        self.assertEqual(report['date_from'].isoformat(), '2026-08-01')
        self.assertEqual(report['date_to'].isoformat(), '2026-08-02')
        self.assertEqual(len(report['daily_rows']), 2)
        self.assertEqual(report['total_photos'], 2)

    def test_period_counter_uses_selected_dates_including_copies(self):
        timestamps = (
            '2026-09-02T10:00:00+00:00',
            '2026-09-03T09:00:00+00:00',
            '2026-09-03T12:00:00+00:00',
            '2026-09-03T18:00:00+00:00',
            '2026-09-04T10:00:00+00:00',
        )
        for photo_id, sent_at in enumerate(timestamps, 1):
            self.add_photo(photo_id, sent_at=sent_at, photo_date='2026-09-03',
                           review_status='Возможная копия' if photo_id == 2 else 'Проверяется в фоне')
        response, context = self.dashboard_at(date_from='2026-09-03', date_to='2026-09-03')
        self.assertEqual(context['summary']['photo_count'], 3)
        self.assertEqual(context['scope_summary']['photo_count'], 5)
        self.assertContains(response, '<small>03.09.2026</small>')

    def test_period_counter_follows_scope_and_date_filters(self):
        self.connection.executemany(
            'INSERT INTO users VALUES (?, ?, NULL, ?, ?, ?, ?, 1)',
            [(3, 'Medic C', 'City A', 'Hospital C', 'Медик', '0003'),
             (4, 'Medic D', 'City A', 'Hospital A', 'Медик', '0004')],
        )
        for photo_id in range(1, 5):
            self.add_photo(photo_id, sender_telegram_id=photo_id, sent_at='2026-09-03T10:00:00+00:00')
        self.add_photo(5, sent_at='2026-08-15T10:00:00+00:00')
        for filters, total in (
            ({}, 5),
            ({'city': 'City A'}, 4),
            ({'city': 'City A', 'hospital': 'Hospital A'}, 3),
            ({'city': 'City A', 'hospital': 'Hospital A', 'medic': '1'}, 2),
            ({'medic': '999'}, 0),
        ):
            with self.subTest(filters=filters):
                _, context = self.dashboard_at(**filters)
                self.assertEqual(context['summary']['photo_count'], total)
                self.assertEqual(context['scope_summary']['photo_count'], total)
        response, context = self.dashboard_at(medic='1', date_from='2026-08-01', date_to='2026-08-31')
        self.assertEqual(context['scope_summary']['photo_count'], 2)
        self.assertEqual(context['summary']['photo_count'], 1)
        self.assertEqual([photo['id'] for photo in context['photos']], [5])
        self.assertContains(response, '<small>01.08.2026 — 31.08.2026</small>')

    def test_doctor_search_uses_photo_names_and_all_counters(self):
        self.connection.executemany(
            'INSERT INTO users VALUES (?, ?, NULL, ?, ?, ?, ?, 1)',
            [
                (3, 'Medic Bukhara', 'Бухара', 'Алат', 'Медик', '0003'),
                (4, 'Medic Denau', 'Денау', 'Городской роддом', 'Медик', '0004'),
                (5, 'Medic Karshi', 'Карши', 'Касан', 'Медик', '0005'),
                (6, 'Medic Nukus', 'Нукус', 'Городской Роддом Нукус', 'Медик', '0006'),
                (7, 'Medic Samarkand', 'Самарканд', 'Джамбай роддом', 'Медик', '0007'),
            ],
        )
        self.connection.commit()
        self.add_photo(1, doctor_name='Ильясова')
        self.add_photo(2, doctor_name='Мухитдинова Дилноза')
        self.add_photo(3, doctor_name='Ильясова Лола')

        response, context = self.dashboard_at(doctor='  ИЛЬЯСОВА ЛОЛА. ')
        self.assertEqual([photo['id'] for photo in context['photos']], [3, 1])
        self.assertEqual(context['scope_summary']['photo_count'], 2)
        self.assertEqual(context['summary']['photo_count'], 2)
        self.assertEqual(context['selected_doctor'], 'ИЛЬЯСОВА ЛОЛА.')
        self.assertContains(response, 'name="doctor"')
        self.assertContains(response, 'Ильясова Лола')

        known_names = {item['name'] for item in context['doctor_options']}
        self.assertEqual(
            known_names,
            {'Ильясова', 'Ильясова Лола', 'Мухитдинова Дилноза'},
        )
        self.assertNotIn('Раззакова', known_names)

    def test_doctor_filter_is_applied_to_excel_export(self):
        self.connection.execute('ALTER TABLE photos ADD COLUMN mother_phone TEXT')
        self.connection.commit()
        self.add_photo(1, doctor_name='Ильясова')
        self.add_photo(2, doctor_name='Мухитдинова Дилноза')
        request = self.factory.get(reverse('export_photos'), {'doctor': 'Ильясова Лола'})
        request.user = SimpleNamespace(is_authenticated=True, username='operator')

        response = views.export_photos(request)
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), data_only=True)
        self.assertEqual(workbook.sheetnames, ['Ильясова'])
        self.assertEqual(workbook['Ильясова'].max_row, 2)
        workbook.close()

    def test_doctors_are_grouped_by_selected_city_and_hospital(self):
        self.connection.executemany(
            'INSERT INTO users VALUES (?, ?, NULL, ?, ?, ?, ?, 1)',
            [
                (3, 'Medic Samarkand', 'Самарканд', 'Джума роддом', 'Медик', '0003'),
                (4, 'Medic Tashkent', 'Ташкент', 'Роддом(Таш-область)Алмалык', 'Медик', '0004'),
            ],
        )
        self.connection.commit()
        self.add_photo(1, sender_telegram_id=3, doctor_name='Абтлова Махфуза')
        self.add_photo(2, sender_telegram_id=3, doctor_name='Равшанова Ёкиной')
        self.add_photo(3, sender_telegram_id=4, doctor_name='Рано Анорбаева')

        response, context = self.dashboard_at(city='Самарканд', hospital='Джума роддом')
        self.assertEqual(context['hospitals'], ['Джума роддом'])
        self.assertNotIn('Жума', context['hospitals'])
        self.assertEqual(len(context['doctor_groups']), 1)
        self.assertEqual(context['doctor_groups'][0]['label'], 'Джума роддом')
        self.assertEqual(
            context['doctor_groups'][0]['doctors'],
            ['Абтлова Махфуза', 'Равшанова Ёкиной'],
        )
        self.assertContains(response, '<optgroup label="Джума роддом">')
        self.assertContains(response, 'Абтлова Махфуза')

        _, tashkent_context = self.dashboard_at(
            city='Ташкент', hospital='Роддом(Таш-область)Алмалык',
        )
        self.assertEqual(len(tashkent_context['doctor_groups']), 1)
        self.assertIn('Рано Анорбаева', tashkent_context['doctor_groups'][0]['doctors'])

    @override_settings(CREATOR_DJANGO_USERNAME='creator')
    def test_rename_hospital_moves_photos_and_merges_assignments_in_selected_city(self):
        self.connection.execute('ALTER TABLE photos ADD COLUMN maternity_hospital TEXT')
        site_db.ensure_employee_hospitals_table(self.connection)
        self.connection.executemany(
            'INSERT INTO employee_hospitals (telegram_id, city, maternity_hospital) VALUES (?, ?, ?)',
            [
                (1, 'City A', 'Hospital A'),
                (1, 'City A', 'Central Hospital'),
                (2, 'City B', 'Hospital A'),
            ],
        )
        self.add_photo(1, sender_telegram_id=1, maternity_hospital='Hospital A')
        self.add_photo(2, sender_telegram_id=2, maternity_hospital='Hospital A')
        request = self.factory.post(reverse('rename_hospital'), {
            'city': 'City A',
            'old_name': 'Hospital A',
            'new_name': 'Central Hospital',
        })
        request.user = SimpleNamespace(is_authenticated=True, username='creator', id=99)

        with patch('django.contrib.messages.success') as success:
            response = views.rename_hospital(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.connection.execute('SELECT maternity_hospital FROM photos WHERE id=1').fetchone()[0],
            'Central Hospital',
        )
        self.assertEqual(
            self.connection.execute('SELECT maternity_hospital FROM photos WHERE id=2').fetchone()[0],
            'Hospital A',
        )
        self.assertEqual(
            self.connection.execute('SELECT maternity_hospital FROM users WHERE telegram_id=1').fetchone()[0],
            'Central Hospital',
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM employee_hospitals WHERE telegram_id=1 AND maternity_hospital='Central Hospital'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM employee_hospitals WHERE telegram_id=1 AND maternity_hospital='Hospital A'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT new_name FROM hospital_renames WHERE city='City A' AND old_name='Hospital A'"
            ).fetchone()[0],
            'Central Hospital',
        )
        self.assertIn('Перенесено фотографий: 1', success.call_args.args[1])

    @override_settings(CREATOR_DJANGO_USERNAME='creator')
    def test_creator_can_delete_all_photos_of_one_medic_without_deleting_profile(self):
        self.add_photo(1, sender_telegram_id=1)
        self.add_photo(2, sender_telegram_id=1)
        self.add_photo(3, sender_telegram_id=2)
        request = self.factory.post(reverse('delete_employee_photos', args=[1]))
        request.user = SimpleNamespace(is_authenticated=True, username='creator', id=99)

        with patch('django.contrib.messages.success') as success:
            response = views.delete_employee_photos(request, 1)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.connection.execute('SELECT COUNT(*) FROM photos WHERE sender_telegram_id=1').fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute('SELECT COUNT(*) FROM photos WHERE sender_telegram_id=2').fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            self.connection.execute('SELECT 1 FROM users WHERE telegram_id=1').fetchone(),
        )
        self.assertIn('удалено фотографий: 2', success.call_args.args[1])

    @override_settings(CREATOR_DJANGO_USERNAME='creator')
    def test_management_shows_bulk_photo_delete_only_for_medics_with_photos(self):
        self.add_photo(1, sender_telegram_id=1)
        request = self.factory.get('/management/')
        request.user = SimpleNamespace(is_authenticated=True, username='creator')
        response = views.management(request)
        self.assertContains(response, reverse('delete_employee_photos', args=[1]))
        self.assertContains(response, 'Удалить все фото (1)')
        self.assertNotContains(response, reverse('delete_employee_photos', args=[2]))
        self.assertContains(response, reverse('delete_filtered_photos'))
        self.assertContains(response, 'Удалить фотографии по сотруднику, роддому и дате')

    @override_settings(CREATOR_DJANGO_USERNAME='creator', TIME_ZONE='Asia/Tashkent', USE_TZ=True)
    def test_creator_can_delete_photos_matching_employee_hospital_and_local_date(self):
        self.connection.execute('ALTER TABLE photos ADD COLUMN maternity_hospital TEXT')
        self.connection.commit()
        self.add_photo(
            1, sender_telegram_id=1, maternity_hospital='Hospital A',
            sent_at='2026-09-03T18:59:59+00:00',
        )
        self.add_photo(
            2, sender_telegram_id=1, maternity_hospital='Hospital A',
            sent_at='2026-09-03T19:00:00+00:00',
        )
        self.add_photo(
            3, sender_telegram_id=1, maternity_hospital='Hospital B',
            sent_at='2026-09-03T18:00:00+00:00',
        )
        self.add_photo(
            4, sender_telegram_id=2, maternity_hospital='Hospital A',
            sent_at='2026-09-03T18:00:00+00:00',
        )
        request = self.factory.post(reverse('delete_filtered_photos'), {
            'telegram_id': '1',
            'location': site_text.encode_photo_location('City A', 'Hospital A'),
            'sent_date': '2026-09-03',
        })
        request.user = SimpleNamespace(is_authenticated=True, username='creator', id=99)

        with patch('django.contrib.messages.success') as success:
            response = views.delete_filtered_photos(request)

        self.assertEqual(response.status_code, 302)
        remaining_ids = [
            row[0] for row in self.connection.execute('SELECT id FROM photos ORDER BY id')
        ]
        self.assertEqual(remaining_ids, [2, 3, 4])
        self.assertIsNotNone(
            self.connection.execute('SELECT 1 FROM users WHERE telegram_id=1').fetchone(),
        )
        self.assertIn('Удалено фотографий: 1', success.call_args.args[1])

    @override_settings(CREATOR_DJANGO_USERNAME='creator')
    def test_filtered_photo_delete_requires_all_three_filters(self):
        self.add_photo(1, sender_telegram_id=1)
        request = self.factory.post(reverse('delete_filtered_photos'), {
            'telegram_id': '1', 'sent_date': '2026-09-03',
        })
        request.user = SimpleNamespace(is_authenticated=True, username='creator', id=99)

        with patch('django.contrib.messages.error') as error:
            response = views.delete_filtered_photos(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)
        self.assertIn('Выберите сотрудника, роддом', error.call_args.args[1])

    def test_period_counter_is_not_limited_by_photo_pagination(self):
        self.connection.executemany(
            'INSERT INTO photos (id, sender_telegram_id, sent_at) VALUES (?, 1, ?)',
            [(photo_id, '2026-09-03T10:00:00+00:00') for photo_id in range(1, 306)],
        )
        self.connection.commit()
        _, context = self.dashboard_at()
        self.assertEqual(context['summary']['photo_count'], 305)
        self.assertEqual(len(context['photos']), photo_views.PHOTO_PAGE_SIZE)
        self.assertEqual(context['photo_page_count'], 16)

    def test_period_counter_formats_open_date_range(self):
        self.add_photo(1, sent_at='2026-09-01T10:00:00+00:00')
        self.add_photo(2, sent_at='2026-09-03T10:00:00+00:00')
        response, context = self.dashboard_at(date_from='2026-09-02')
        self.assertEqual(context['summary']['photo_count'], 1)
        self.assertContains(response, '<small>с 02.09.2026</small>')

    def test_photo_cards_show_short_local_time_and_keep_raw_timestamp(self):
        from django.template.loader import render_to_string

        original_timestamp = '2026-09-03T11:56:47.536864+00:00'
        self.add_photo(1, sent_at=original_timestamp)
        response, context = self.dashboard_at()
        expected = 'Отправлено: 03.09.2026 16:56'
        self.assertContains(response, expected)
        self.assertNotContains(response, original_timestamp)
        photo = context['photos'][0]
        self.assertEqual(photo['sent_at'], original_timestamp)
        for template in ('photo_card_return.html',):
            with self.subTest(template=template):
                html = render_to_string('dashboard/' + template, {'photo': photo}, request=self.factory.get('/'))
                self.assertIn(expected, html)
                self.assertNotIn(original_timestamp, html)

    @override_settings(TIME_ZONE='Asia/Tashkent')
    def test_comparison_shows_formatted_dates_without_changing_legacy_link(self):
        original_timestamp = '2026-09-02T19:00:00.536864+00:00'
        self.add_photo(1, sent_at=original_timestamp)
        self.add_photo(2, sent_at='2026-09-03T11:56:47.536864+00:00', review_status='Возможная копия',
                       review_reason='Фото от ' + original_timestamp)
        response = views.photo_original(self.request(2), 2)
        self.assertContains(response, 'Отправлено: 03.09.2026 00:00')
        self.assertContains(response, 'Отправлено: 03.09.2026 16:56')
        self.assertEqual(self.resolve(2)['id'], 1)
        self.assertEqual(self.connection.execute('SELECT sent_at FROM photos WHERE id=1').fetchone()[0], original_timestamp)


@override_settings(TIME_ZONE='Asia/Tashkent')
class SentAtFormattingTests(SimpleTestCase):
    def test_utc_and_local_offset_show_the_same_minute_without_seconds(self):
        for value in ('2026-09-03T11:56:47.536864+00:00', '2026-09-03T16:56:47.536864+05:00'):
            with self.subTest(value=value):
                self.assertEqual(site_text.format_sent_at(value), '03.09.2026 16:56')

    def test_legacy_naive_z_and_datetime_values(self):
        for value in ('2026-09-03T11:56:47Z', '2026-09-03 11:56:47',
                      datetime(2026, 9, 3, 11, 56, 47), datetime(2026, 9, 3, 11, 56, 47, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                self.assertEqual(site_text.format_sent_at(value), '03.09.2026 16:56')

    def test_local_midnight_and_new_year_rollover(self):
        self.assertEqual(site_text.format_sent_at('2026-09-03T18:59:59+00:00'), '03.09.2026 23:59')
        self.assertEqual(site_text.format_sent_at('2026-09-03T19:00:00+00:00'), '04.09.2026 00:00')
        self.assertEqual(site_text.format_sent_at('2026-12-31T20:15:01+00:00'), '01.01.2027 01:15')

    def test_empty_or_invalid_values_are_safe(self):
        for value in (None, '', '   ', 'not a date', '2026-13-03T11:56:47+00:00'):
            with self.subTest(value=value):
                self.assertEqual(site_text.format_sent_at(value), '—')


class BotPhotoTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        spec = importlib.util.spec_from_file_location('bot_under_test', Path(__file__).resolve().parents[2] / 'bot.py')
        cls.bot = importlib.util.module_from_spec(spec)
        # Never load real API credentials or start polling during tests.
        with patch('dotenv.load_dotenv'), patch.dict('os.environ', {
            'ANTHROPIC_API_KEY': '', 'TELEGRAM_BOT_TOKEN': 'test-token',
            'SIGHTENGINE_API_USER': '', 'SIGHTENGINE_API_SECRET': '',
        }):
            spec.loader.exec_module(cls.bot)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.db_path = Path(self.temporary.name) / 'bot.sqlite3'
        self.db_patch = patch.object(self.bot, 'DB_PATH', self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.bot.init_db()

    def test_claude_adapter_converts_multimodal_request(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'content': [{'type': 'text', 'text': '{"result":"ok"}'}],
        }
        adapter = self.bot.ClaudeResponses('test-anthropic-key')
        with patch.object(self.bot.requests, 'post', return_value=response) as post:
            result = asyncio.run(adapter.create(
                model='claude-sonnet-4-6',
                max_output_tokens=220,
                system=[{
                    'type': 'input_text',
                    'text': 'Постоянная инструкция',
                    'cache_control': {'type': 'ephemeral'},
                }],
                input=[{'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'Проверь фотографию'},
                    {
                        'type': 'input_image',
                        'image_url': 'data:image/jpeg;base64,YWJj',
                        'detail': 'high',
                        'cache_control': {'type': 'ephemeral'},
                    },
                ]}],
            ))

        self.assertEqual(result.output_text, '{"result":"ok"}')
        request = post.call_args.kwargs
        self.assertEqual(request['headers']['x-api-key'], 'test-anthropic-key')
        self.assertEqual(request['json']['model'], 'claude-sonnet-4-6')
        self.assertEqual(request['json']['max_tokens'], 220)
        self.assertEqual(request['json']['system'], [{
            'type': 'text',
            'text': 'Постоянная инструкция',
            'cache_control': {'type': 'ephemeral'},
        }])
        content = request['json']['messages'][0]['content']
        self.assertEqual([item['type'] for item in content], ['image', 'text'])
        self.assertEqual(content[0]['source'], {
            'type': 'base64', 'media_type': 'image/jpeg', 'data': 'YWJj',
        })
        self.assertEqual(content[0]['cache_control'], {'type': 'ephemeral'})

    def test_claude_adapter_preserves_safe_api_error_details(self):
        response = Mock()
        response.status_code = 400
        response.reason = 'Bad Request'
        response.text = '{"type":"error"}'
        response.headers = {'request-id': 'req_test'}
        response.json.return_value = {
            'type': 'error',
            'error': {
                'type': 'invalid_request_error',
                'message': 'images.0: image exceeds maximum dimensions',
            },
        }
        response.raise_for_status.side_effect = self.bot.requests.HTTPError(
            '400 Client Error'
        )
        adapter = self.bot.ClaudeResponses('secret-key-must-not-appear')

        with patch.object(self.bot.requests, 'post', return_value=response):
            with self.assertRaises(self.bot.ClaudeAPIError) as raised:
                asyncio.run(adapter.create(
                    model='claude-sonnet-4-6',
                    max_output_tokens=220,
                    input=[{'role': 'user', 'content': [
                        {'type': 'input_text', 'text': 'Проверь фотографию'},
                    ]}],
                ))

        message = str(raised.exception)
        self.assertIn('invalid_request_error', message)
        self.assertIn('image exceeds maximum dimensions', message)
        self.assertIn('req_test', message)
        self.assertNotIn('secret-key-must-not-appear', message)

    def test_claude_adapter_rejects_success_without_text(self):
        response = Mock()
        response.headers = {'request-id': 'req_empty'}
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'content': [],
            'stop_reason': 'max_tokens',
        }
        adapter = self.bot.ClaudeResponses('test-key')

        with patch.object(self.bot.requests, 'post', return_value=response):
            with self.assertRaisesRegex(
                self.bot.ClaudeAPIError,
                'no text; stop_reason=max_tokens; request_id=req_empty',
            ):
                asyncio.run(adapter.create(
                    model='claude-sonnet-4-6',
                    max_output_tokens=220,
                    input=[{'role': 'user', 'content': [
                        {'type': 'input_text', 'text': 'Проверь фотографию'},
                    ]}],
                ))

    def add_photo(self, sender=1, sent_at='2026-09-03T08:00:00+00:00', digest='digest', qr=None,
                  status='Нет явных признаков', doctor=None, hospital=None):
        payload = BytesIO()
        Image.new('RGB', (32, 32), 'white').save(payload, format='JPEG')
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute('''INSERT INTO photos
                (sender_telegram_id, medic_telegram_id, medic_code, telegram_file_id, image,
                 sent_at, image_digest, qr_data, review_status, image_hash, doctor_name, maternity_hospital)
                VALUES (?, 1, '0001', 'test-file', ?, ?, ?, ?, ?, '0000000000000000', ?, ?)''',
                (sender, payload.getvalue(), sent_at, digest, qr, status, doctor, hospital)).lastrowid

    def test_urgench_audit_includes_every_hospital_and_excludes_other_cities(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """INSERT INTO users
                   (telegram_id, username, full_name, city, district,
                    maternity_hospital, role, medic_code, is_active, registered_at)
                   VALUES (?, NULL, ?, ?, '', ?, 'Медик', ?, 1, ?)""",
                [
                    (77, 'Ургенч 1', 'Ургенч', 'Хозарсп район', '0777',
                     '2026-10-25T00:00:00+00:00'),
                    (78, 'Ургенч 2', 'Ургенч', 'ВПМ роддом', '0778',
                     '2026-10-25T00:00:00+00:00'),
                    (79, 'Карши', 'Карши', 'Кукдала', '0779',
                     '2026-10-25T00:00:00+00:00'),
                ],
            )
        first = self.add_photo(sender=77, digest='u-1', hospital='Хозарсп район')
        second = self.add_photo(sender=78, digest='u-2', hospital='ВПМ роддом')
        self.add_photo(sender=79, digest='k-1', hospital='Кукдала')

        self.assertEqual(
            self.bot.pending_urgench_duplicate_audit_photo_ids(),
            [first, second],
        )

    def test_missing_qr_is_explicitly_allowed(self):
        self.assertEqual(
            self.bot.qr_code_status(None, 'Ургенч'),
            'Код отсутствует (допустимо)',
        )

    def test_urgench_audit_links_exact_copy_without_requiring_qr(self):
        original = self.add_photo(digest='same-urgench-photo')
        copy = self.add_photo(digest='same-urgench-photo')
        success, duplicate_id = asyncio.run(
            self.bot.audit_urgench_duplicate_photo(copy)
        )
        self.assertTrue(success)
        self.assertEqual(duplicate_id, original)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """SELECT original_photo_id, review_status, duplicate_audit_version
                   FROM photos WHERE id = ?""",
                (copy,),
            ).fetchone()
        self.assertEqual(
            row,
            (original, 'Возможная копия', self.bot.URGENCH_DUPLICATE_AUDIT_VERSION),
        )

    def test_qr_region_ranges_from_sticker_workbook(self):
        expected_ranges = {
            '01': ('Ташкент', 10_000),
            '02': ('Самарканд', 23_000),
            '03': ('Карши', 100_000),
            '04': ('Бухара', 6_001),
            '05': ('Фергана', 6_000),
            '06': ('Андижан', 6_000),
            '07': ('Ургенч', 7_000),
            '08': ('Наманган', 6_000),
            '09': ('Денау', 4_000),
            '10': ('Термез', 2_000),
            '11': ('Коканд', 5_000),
            '12': ('Джиззах', 3_000),
            '13': ('Навои', 1_000),
        }
        self.assertEqual(self.bot.QR_REGION_RANGES, expected_ranges)
        for prefix, (region, maximum) in expected_ranges.items():
            with self.subTest(prefix=prefix, region=region):
                code = f'{prefix}-{maximum:010d}'
                self.assertEqual(self.bot.qr_code_status(code, region), f'Код распознан: {region}')
        self.assertEqual(self.bot.canonical_qr_data(' 01-0000000134 '), '01-0000000134')
        self.assertEqual(
            self.bot.canonical_qr_data('Наклейка: 02-0000001400'),
            '02-0000001400',
        )
        self.assertEqual(self.bot.canonical_qr_data('03–0000000042'), '03-0000000042')
        self.assertEqual(self.bot.canonical_qr_data('040000000017'), '04-0000000017')
        self.assertEqual(self.bot.canonical_qr_data('04 / 00000 00017'), '04-0000000017')
        self.assertEqual(self.bot.qr_code_status('01-0000010000', 'Ташкент'), 'Код распознан: Ташкент')
        self.assertIn('Код другого региона: Самарканд', self.bot.qr_code_status('02-0000000001', 'Ташкент'))
        self.assertIn('вне диапазона региона Ташкент', self.bot.qr_code_status('01-0000010001', 'Ташкент'))
        self.assertEqual(self.bot.qr_code_status('06-0000006000', 'Андижан'), 'Код распознан: Андижан')
        self.assertIn('Посторонний QR/Data Matrix', self.bot.qr_code_status('99-0000000001', 'Ташкент'))
        self.assertIn('Посторонний QR/Data Matrix', self.bot.qr_code_status('https://nuppi.ru/', 'Ташкент'))
        self.assertTrue(self.bot.ai_qr_is_plausible_for_city('03-0000000042', 'Карши'))
        self.assertTrue(self.bot.ai_qr_is_plausible_for_city('03-0000015158', 'Карши'))
        self.assertEqual(
            self.bot.qr_code_status('03-0000015158', 'Карши'),
            'Код распознан: Карши',
        )
        self.assertTrue(self.bot.ai_qr_is_plausible_for_city('11-0000000042', 'Коканд'))
        self.assertFalse(self.bot.ai_qr_is_plausible_for_city('01-0000000042', 'Карши'))
        self.assertFalse(self.bot.ai_qr_is_plausible_for_city('01-6044000001', 'Карши'))

    def test_init_db_reapplies_qr_policy_to_existing_photos(self):
        newly_allowed_id = self.add_photo(qr='02-0000023000', digest='newly-allowed')
        foreign_id = self.add_photo(qr='https://example.test/other-product', digest='foreign')
        product_mismatch_id = self.add_photo(qr='01-0000000001', digest='product-mismatch')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE photos SET code_status = 'Старый статус' WHERE id IN (?, ?, ?)",
                (newly_allowed_id, foreign_id, product_mismatch_id),
            )
            connection.execute(
                """INSERT INTO photo_checks
                   (photo_id, product_status, material_status, decision, decision_reason)
                   VALUES (?, 'NUPPI 1 не подтверждён', 'Эталонный дизайн подтверждён',
                           'review', 'Упаковка NUPPI 1 не подтверждена.')""",
                (product_mismatch_id,),
            )

        self.bot.init_db()

        with sqlite3.connect(self.db_path) as connection:
            rows = dict(connection.execute(
                """SELECT p.id, p.code_status || '|' || checks.decision
                   FROM photos AS p
                   JOIN photo_checks AS checks ON checks.photo_id = p.id
                   WHERE p.id IN (?, ?, ?)""",
                (newly_allowed_id, foreign_id, product_mismatch_id),
            ).fetchall())
        self.assertEqual(rows[newly_allowed_id], 'Код распознан: Самарканд|review')
        self.assertIn('Посторонний QR/Data Matrix', rows[foreign_id])
        self.assertTrue(rows[foreign_id].endswith('|review'))
        self.assertEqual(rows[product_mismatch_id], 'Код распознан: Ташкент|review')

    def test_qr_reader_scans_priority_crops_and_enlarged_tiles(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        source = Image.new('RGB', (2400, 1600), 'white')
        payload = BytesIO()
        source.save(payload, format='JPEG', quality=95)
        scanned_sizes = []

        def fake_read_barcodes(image, **_kwargs):
            scanned_sizes.append(image.size)
            if image.width < 2000:
                return [SimpleNamespace(text='01-0000000134')]
            return []

        with patch.object(self.bot.zxingcpp, 'read_barcodes', side_effect=fake_read_barcodes):
            self.assertEqual(self.bot.read_qr_code(payload.getvalue()), '01-0000000134')

        self.assertTrue(any(width >= 2000 for width, _height in scanned_sizes))
        self.assertTrue(any(width < 2000 for width, _height in scanned_sizes))

    def test_zxing_prefers_regional_number_when_photo_has_several_codes(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        results = [
            SimpleNamespace(text='https://example.test/product'),
            SimpleNamespace(text='Наклейка: 02-0000001400'),
        ]
        with patch.object(self.bot.zxingcpp, 'read_barcodes', return_value=results):
            self.assertEqual(
                self.bot._zxing_decode(Image.new('RGB', (200, 200), 'white')),
                '02-0000001400',
            )

    def test_zxing_reads_raw_gs1_bytes_when_rendered_text_is_incomplete(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        results = [SimpleNamespace(text='', bytes=b'\x1d04-0000000017')]
        with patch.object(self.bot.zxingcpp, 'read_barcodes', return_value=results):
            self.assertEqual(
                self.bot._zxing_decode(Image.new('RGB', (200, 200), 'white')),
                '04-0000000017',
            )

    def test_zxing_reports_damaged_code_presence_without_trusting_its_payload(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        point = lambda x, y: SimpleNamespace(x=x, y=y)
        damaged = SimpleNamespace(
            valid=False,
            error='ChecksumError @ QRDecoder.cpp:337',
            text='01-9999999999',
            bytes=b'01-9999999999',
            position=SimpleNamespace(
                top_left=point(20, 20), top_right=point(80, 20),
                bottom_right=point(80, 80), bottom_left=point(20, 80),
            ),
        )
        with patch.object(self.bot.zxingcpp, 'read_barcodes', return_value=[damaged]):
            self.assertEqual(
                self.bot._zxing_decode(
                    Image.new('RGB', (100, 100), 'white'), return_presence=True,
                ),
                (None, True),
            )

    def test_zxing_rejects_crop_border_as_false_damaged_code_presence(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        point = lambda x, y: SimpleNamespace(x=x, y=y)
        crop_border = SimpleNamespace(
            valid=False,
            error='ChecksumError @ DMDecoder.cpp:410',
            text='', bytes=b'',
            position=SimpleNamespace(
                top_left=point(1, 1), top_right=point(99, 1),
                bottom_right=point(99, 99), bottom_left=point(1, 99),
            ),
        )
        with patch.object(self.bot.zxingcpp, 'read_barcodes', return_value=[crop_border]):
            self.assertEqual(
                self.bot._zxing_decode(
                    Image.new('RGB', (100, 100), 'white'), return_presence=True,
                ),
                (None, False),
            )

    def test_qr_reader_keeps_scanning_after_unrelated_code(self):
        if self.bot.zxingcpp is None:
            self.skipTest('zxing-cpp is not installed')
        source = Image.new('RGB', (800, 800), 'white')
        payload = BytesIO()
        source.save(payload, format='JPEG', quality=95)
        calls = iter((
            [SimpleNamespace(text='https://example.test/product')],
            [SimpleNamespace(text='04-0000000017')],
        ))

        def fake_read_barcodes(_image, **_kwargs):
            return next(calls, [])

        with patch.object(self.bot.zxingcpp, 'read_barcodes', side_effect=fake_read_barcodes):
            self.assertEqual(self.bot.read_qr_code(payload.getvalue()), '04-0000000017')

    def test_qr_scan_uses_smaller_overlapping_tiles(self):
        source = Image.new('RGB', (2400, 1600), 'white')
        sizes = {tile.size for tile in self.bot._qr_scan_tiles(source)}
        self.assertIn((1440, 960), sizes)
        self.assertIn((840, 560), sizes)
        self.assertIn((528, 352), sizes)

    def test_small_qr_crop_is_enlarged_independently_from_ai_savings_size(self):
        crop = Image.new('RGB', (400, 300), 'white')
        with patch.object(self.bot, 'IMAGE_PROCESSING_MAX_DIMENSION', 1280), \
             patch.object(self.bot, 'QR_SCAN_TARGET_DIMENSION', 2400):
            enlarged = self.bot._enlarge_qr_crop(crop)
        self.assertEqual(enlarged.size, (1600, 1200))

    def test_missing_qr_backfill_updates_existing_photo(self):
        photo_id = self.add_photo(qr=None)
        with patch.object(self.bot, 'read_qr_code', return_value='02–0000000042'):
            processed, found, updated = self.bot.backfill_missing_qr_batch()
        self.assertEqual((processed, found, updated), (1, 1, 1))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, code_status, qr_scan_version FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], '02-0000000042')
        self.assertIn('Самарканд', row[1])
        self.assertEqual(row[2], self.bot.QR_SCAN_VERSION)

    def test_qr_backfill_rescans_photos_with_existing_codes(self):
        photo_id = self.add_photo(qr='https://example.test/old-code')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET qr_scan_version = ? WHERE id = ?',
                (self.bot.QR_SCAN_VERSION - 1, photo_id),
            )
        with patch.object(self.bot, 'read_qr_code', return_value='04-0000000017'):
            processed, found, updated = self.bot.backfill_missing_qr_batch()
        self.assertEqual((processed, found, updated), (1, 1, 1))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, code_status, qr_scan_version FROM photos WHERE id = ?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], '04-0000000017')
        self.assertIn('Бухара', row[1])
        self.assertEqual(row[2], self.bot.QR_SCAN_VERSION)

    def test_qr_backfill_skips_existing_valid_regional_code(self):
        photo_id = self.add_photo(qr='01-0000000134')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET qr_scan_version = ? WHERE id = ?',
                (self.bot.QR_SCAN_VERSION - 1, photo_id),
            )
        with patch.object(self.bot, 'read_qr_code', return_value=None):
            processed, found, updated = self.bot.backfill_missing_qr_batch()
        self.assertEqual((processed, found, updated), (0, 0, 0))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, qr_scan_version FROM photos WHERE id = ?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row, ('01-0000000134', self.bot.QR_SCAN_VERSION - 1))

    def test_targeted_recheck_selects_only_missing_qr_on_tashkent_dates(self):
        before = self.add_photo(
            sent_at='2026-09-26T18:59:59+00:00', digest='before-range',
        )
        first = self.add_photo(
            sent_at='2026-09-26T19:00:00+00:00', digest='first-in-range',
        )
        last = self.add_photo(
            sent_at='2026-09-28T18:59:59+00:00', digest='last-in-range',
        )
        after = self.add_photo(
            sent_at='2026-09-28T19:00:00+00:00', digest='after-range',
        )
        with_qr = self.add_photo(
            sent_at='2026-09-27T08:00:00+00:00', digest='already-has-qr',
            qr='01-0000000042',
        )

        jobs = self.bot.pending_targeted_no_qr_recheck_jobs()
        self.assertEqual([job[0] for job in jobs], [first, last])
        self.assertNotIn(before, [job[0] for job in jobs])
        self.assertNotIn(after, [job[0] for job in jobs])
        self.assertNotIn(with_qr, [job[0] for job in jobs])

        self.bot.mark_targeted_no_qr_recheck_complete(first)
        self.assertEqual(
            [job[0] for job in self.bot.pending_targeted_no_qr_recheck_jobs()],
            [last],
        )

    def test_targeted_recheck_uses_archive_fallback_and_marks_completion(self):
        photo_id = self.add_photo(
            sent_at='2026-09-27T08:00:00+00:00', digest='targeted-audit',
        )
        process = AsyncMock(return_value=True)
        with patch.object(
            self.bot, 'pending_targeted_no_qr_recheck_jobs',
            return_value=[(photo_id, 'Ташкент', 'Роддом 1')],
        ), patch.object(
            self.bot, 'process_photo_in_background', process,
        ), patch.object(self.bot, 'TARGETED_NO_QR_RECHECK_DELAY_SECONDS', 0):
            asyncio.run(self.bot.run_targeted_no_qr_recheck())

        process.assert_awaited_once_with(
            photo_id,
            'Ташкент',
            'Роддом 1',
            duplicate_fallback_limit=self.bot.TARGETED_NO_QR_DUPLICATE_FALLBACK,
        )
        with sqlite3.connect(self.db_path) as connection:
            version = connection.execute(
                'SELECT targeted_recheck_version FROM photos WHERE id = ?',
                (photo_id,),
            ).fetchone()[0]
        self.assertEqual(version, self.bot.TARGETED_NO_QR_RECHECK_VERSION)

    def test_full_qr_recheck_selects_all_unread_photos_but_not_valid_or_pending(self):
        missing = self.add_photo(digest='full-missing')
        unrelated = self.add_photo(
            digest='full-unrelated', qr='https://example.test/product',
        )
        valid = self.add_photo(
            digest='full-valid', qr='01-0000000134',
        )
        pending = self.add_photo(
            digest='full-pending', status='Проверяется в фоне',
        )
        old_failed = self.add_photo(digest='full-old-provider-failure')
        impossible_ai_number = self.add_photo(
            digest='full-impossible-ai-number', qr='01-6044000001',
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                '''UPDATE photos
                   SET full_qr_recheck_failures = 3,
                       full_qr_recheck_failure_version = 1
                   WHERE id = ?''',
                (old_failed,),
            )
            connection.execute(
                "UPDATE photos SET code_status = ? WHERE id = ?",
                (
                    'Посторонний QR/Data Matrix: код вне диапазона региона Ташкент',
                    impossible_ai_number,
                ),
            )

        jobs = self.bot.pending_full_qr_recheck_photo_ids(limit=20)

        self.assertIn(missing, jobs)
        self.assertIn(unrelated, jobs)
        self.assertIn(old_failed, jobs)
        self.assertIn(impossible_ai_number, jobs)
        self.assertNotIn(valid, jobs)
        self.assertNotIn(pending, jobs)

    def test_full_qr_recheck_saves_exact_ai_number_and_keeps_decision_on_review(self):
        photo_id = self.add_photo(digest='full-ai-number')
        recovery = {
            'qr_data': '02-0000000042',
            'qr_data_confidence': 0.98,
            'code_visible': True,
            'code_obstructed': False,
            'unreadable_reason': 'none',
        }
        with patch.object(self.bot, 'read_qr_code', return_value=None), \
             patch.object(
                 self.bot, 'recover_missing_qr_data',
                 new=AsyncMock(return_value=recovery),
             ):
            result = asyncio.run(self.bot.recheck_historical_photo_qr(photo_id))

        self.assertEqual(result, (True, 'read'))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                '''SELECT p.qr_data, p.code_status, p.full_qr_recheck_version,
                          checks.decision
                   FROM photos AS p
                   JOIN photo_checks AS checks ON checks.photo_id = p.id
                   WHERE p.id = ?''',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], '02-0000000042')
        self.assertIn('Самарканд', row[1])
        self.assertEqual(row[2], self.bot.FULL_QR_RECHECK_VERSION)
        self.assertEqual(row[3], 'review')

    def test_full_qr_recheck_moves_visible_unreadable_code_to_review(self):
        photo_id = self.add_photo(digest='full-unreadable')
        recovery = {
            'qr_data': None,
            'qr_data_confidence': 0.0,
            'code_visible': True,
            'code_obstructed': False,
            'unreadable_reason': 'blurred',
        }
        with patch.object(self.bot, 'read_qr_code', return_value=None), \
             patch.object(
                 self.bot, 'recover_missing_qr_data',
                 new=AsyncMock(return_value=recovery),
             ):
            result = asyncio.run(self.bot.recheck_historical_photo_qr(photo_id))

        self.assertEqual(result, (True, 'unreadable'))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                '''SELECT p.qr_data, p.code_status, p.review_status,
                          p.full_qr_recheck_version, checks.decision
                   FROM photos AS p
                   JOIN photo_checks AS checks ON checks.photo_id = p.id
                   WHERE p.id = ?''',
                (photo_id,),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertIn('полный номер не прочитан', row[1])
        self.assertEqual(row[2], 'Ошибка: QR-код не прочитан')
        self.assertEqual(row[3], self.bot.FULL_QR_RECHECK_VERSION)
        self.assertEqual(row[4], 'review')

    def test_full_qr_recheck_clears_impossible_legacy_ai_number(self):
        photo_id = self.add_photo(
            digest='legacy-false-ai-qr', qr='01-6044000001',
        )
        recovery = {
            'qr_data': None,
            'qr_data_confidence': 0.0,
            'code_visible': False,
            'code_obstructed': False,
            'unreadable_reason': 'not_visible',
        }
        with patch.object(self.bot, 'read_qr_code', return_value=None), \
             patch.object(
                 self.bot, 'recover_missing_qr_data',
                 new=AsyncMock(return_value=recovery),
             ):
            result = asyncio.run(self.bot.recheck_historical_photo_qr(photo_id))

        self.assertEqual(result, (True, 'absent'))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, code_status FROM photos WHERE id = ?', (photo_id,),
            ).fetchone()
        self.assertEqual(row, (None, 'Код отсутствует (допустимо)'))

    def test_full_qr_recheck_does_not_mark_photo_when_ai_pass_failed(self):
        photo_id = self.add_photo(digest='full-ai-failed')
        with patch.object(self.bot, 'read_qr_code', return_value=None), \
             patch.object(
                 self.bot, 'recover_missing_qr_data',
                 new=AsyncMock(return_value=None),
             ):
            result = asyncio.run(self.bot.recheck_historical_photo_qr(photo_id))

        self.assertEqual(result, (False, 'retry'))
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                '''SELECT full_qr_recheck_version, full_qr_recheck_failures,
                          full_qr_recheck_failure_version
                   FROM photos WHERE id = ?''',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], self.bot.FULL_QR_RECHECK_VERSION)

    def test_forced_duplicate_fallback_compares_nearest_archive_photo(self):
        original = self.add_photo(digest='archive-original')
        current = self.add_photo(digest='archive-current')
        with patch.object(self.bot, 'AI_FALLBACK_CANDIDATE_LIMIT', 0):
            self.assertEqual(
                self.bot.get_ai_candidates(
                    'ffffffffffffffff', exclude_photo_id=current,
                ),
                [],
            )
            forced = self.bot.get_ai_candidates(
                'ffffffffffffffff', exclude_photo_id=current,
                fallback_candidate_limit=1,
            )
        self.assertEqual(forced[0][0]['id'], original)

    def test_default_duplicate_fallback_never_skips_vision_comparison(self):
        original = self.add_photo(digest='different-background-original')
        current = self.add_photo(digest='different-background-current')
        with patch.object(self.bot, 'AI_FALLBACK_CANDIDATE_LIMIT', 3), \
             patch.object(self.bot, 'crop_hash_match_score', return_value=None), \
             patch.object(self.bot, 'visual_hash_match_score', return_value=None):
            candidates = self.bot.get_ai_candidates(
                'ffffffffffffffff', exclude_photo_id=current,
            )

        self.assertEqual(candidates[0][0]['id'], original)
        self.assertLessEqual(len(candidates), 3)

    def test_counts_are_personal_and_use_tashkent_boundaries(self):
        for date in ('2026-08-31T18:59:59+00:00', '2026-08-31T19:00:00+00:00',
                     '2026-09-02T18:59:59+00:00', '2026-09-02T19:00:00+00:00',
                     '2026-09-03T08:00:00+00:00'):
            self.add_photo(sent_at=date, status='Возможная копия')
        self.add_photo(sender=2)  # Same medic code, different actual sender.
        now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
        self.assertEqual(self.bot.medic_photo_counts(1, now), {'total': 5, 'today': 2, 'month': 4})
        self.assertEqual(self.bot.medic_photo_counts(999, now), {'total': 0, 'today': 0, 'month': 0})

    def test_report_hospitals_include_all_assignments_and_historical_photos(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute('''INSERT INTO users
                (telegram_id, username, full_name, city, district, maternity_hospital,
                 role, medic_code, is_active, registered_at)
                VALUES (1, 'medic', 'Медик Один', 'Бухара', '', 'Алат',
                        'Медик', '0001', 1, '2026-09-01T00:00:00+00:00')''')
            connection.execute(
                '''INSERT INTO employee_hospitals (telegram_id, city, maternity_hospital)
                   VALUES (1, 'Самарканд', 'Городской роддом')'''
            )
        self.add_photo(sender=1, digest='historical-hospital', hospital='Старый роддом')
        self.add_photo(sender=2, digest='other-medic-hospital', hospital='Чужой роддом')

        hospitals = self.bot.get_medic_report_hospitals(1)

        self.assertEqual(hospitals, ['Алат', 'Городской роддом', 'Старый роддом'])
        self.assertNotIn('Чужой роддом', hospitals)
        self.assertEqual(
            self.bot.medic_report_hospital_city(1, 'Городской роддом', 'Бухара'),
            'Самарканд',
        )

    def test_status_counts_use_period_and_manual_operator_decision(self):
        unreviewed = self.add_photo(sent_at='2026-09-01T10:00:00+00:00', digest='unreviewed')
        accepted = self.add_photo(sent_at='2026-09-02T10:00:00+00:00', digest='accepted')
        rejected = self.add_photo(sent_at='2026-09-03T10:00:00+00:00', digest='rejected')
        other_sender = self.add_photo(
            sender=2, sent_at='2026-09-02T11:00:00+00:00', digest='other-sender'
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """INSERT INTO photo_checks (photo_id, decision, manual_decision)
                   VALUES (?, ?, ?)""",
                [
                    (unreviewed, 'accepted', None),
                    (accepted, 'rejected', 'accepted'),
                    (rejected, 'accepted', 'rejected'),
                    (other_sender, 'rejected', None),
                ],
            )

        counts = self.bot.medic_photo_status_counts(
            1,
            datetime(2026, 9, 1).date(),
            datetime(2026, 9, 3).date(),
        )

        self.assertEqual(counts, {
            'total': 3,
            'accepted': 1,
            'review': 1,
            'rejected': 1,
            'not_accepted': 2,
        })

    def test_photo_report_accepts_custom_period(self):
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(args=['01.09.2026', '10.09.2026'], user_data={})
        custom_counts = {
            'total': 12, 'accepted': 8, 'review': 3, 'rejected': 1, 'not_accepted': 4,
        }
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value={'role': 'Медик'}), \
             patch.object(self.bot, 'medic_photo_status_counts', return_value=custom_counts) as counts:
            asyncio.run(self.bot.photo_report(update, context))

        counts.assert_called_once_with(
            1, datetime(2026, 9, 1).date(), datetime(2026, 9, 10).date()
        )
        text = update.message.reply_text.call_args.args[0]
        self.assertIn('Период: 01.09.2026–10.09.2026', text)
        self.assertIn('✅ Принято: 8', text)
        self.assertIn('🚫 Не принято: 4', text)

    def test_photo_report_period_accepts_spaces_inside_first_date(self):
        date_from, date_to = self.bot.parse_photo_report_period(
            ['01.09. 2026 - 30.09.2026']
        )

        self.assertEqual(date_from, datetime(2026, 9, 1).date())
        self.assertEqual(date_to, datetime(2026, 9, 30).date())

    def test_photo_report_period_accepts_single_digit_day_and_month(self):
        date_from, date_to = self.bot.parse_photo_report_period(
            ['1.9.2026 — 30.09.2026']
        )

        self.assertEqual(date_from, datetime(2026, 9, 1).date())
        self.assertEqual(date_to, datetime(2026, 9, 30).date())

    def test_report_sends_own_photos_with_city_hospital_and_pagination(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute('''INSERT INTO users
                (telegram_id, username, full_name, city, district, maternity_hospital,
                 role, medic_code, is_active, registered_at)
                VALUES (1, 'medic', 'Медик Один', 'Самарканд', '', 'Роддом 2',
                        'Медик', '0001', 1, '2026-09-01T00:00:00+00:00')''')
        own_photo = self.add_photo(
            sender=1,
            sent_at='2026-09-26T19:00:00+00:00',
            digest='report-own',
            doctor='Иванова Анна',
            hospital='Роддом 2',
        )
        self.add_photo(
            sender=2,
            sent_at='2026-09-27T08:00:00+00:00',
            digest='report-other',
            hospital='Чужой роддом',
        )

        total, records = self.bot.medic_report_photos(
            1, datetime(2026, 9, 27).date(), datetime(2026, 9, 27).date(),
        )

        self.assertEqual(total, 1)
        self.assertEqual(records[0]['id'], own_photo)
        self.assertEqual(records[0]['city'], 'Самарканд')
        self.assertEqual(records[0]['maternity_hospital'], 'Роддом 2')

        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        context = SimpleNamespace(user_data={})
        asyncio.run(self.bot.send_medic_report_photo_page(
            update,
            context,
            1,
            datetime(2026, 9, 27).date(),
            datetime(2026, 9, 27).date(),
        ))

        message.reply_photo.assert_awaited_once()
        caption = message.reply_photo.await_args.kwargs['caption']
        self.assertIn('Город: Самарканд', caption)
        self.assertIn('Роддом: Роддом 2', caption)
        self.assertIn('Врач: Иванова Анна', caption)
        self.assertEqual(context.user_data['report_photo_offset'], 0)
        final_markup = message.reply_text.await_args.kwargs['reply_markup']
        labels = [button.text for row in final_markup.keyboard for button in row]
        self.assertIn(self.bot.MENU_REPORT_PHOTOS_DONE, labels)

    def test_bot_working_hours_use_tashkent_time(self):
        self.assertFalse(self.bot.bot_is_open(datetime(2026, 9, 14, 1, 59, tzinfo=timezone.utc)))
        self.assertTrue(self.bot.bot_is_open(datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)))
        self.assertTrue(self.bot.bot_is_open(datetime(2026, 9, 14, 13, 59, tzinfo=timezone.utc)))
        self.assertFalse(self.bot.bot_is_open(datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)))

    def test_outside_working_hours_stops_other_handlers(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        with patch.object(self.bot, 'bot_is_open', return_value=False):
            with self.assertRaises(self.bot.ApplicationHandlerStop):
                asyncio.run(self.bot.enforce_working_hours(update, SimpleNamespace()))
        self.assertIn('07:00 до 19:00', message.reply_text.call_args.args[0])

    def test_report_does_not_reset_upload_and_checks_role(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(user_data={'doctor_name': 'Unfinished upload'}, args=[])
        status_counts = {'total': 0, 'accepted': 0, 'review': 0, 'rejected': 0, 'not_accepted': 0}
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value={'role': 'Медик'}), \
             patch.object(self.bot, 'medic_photo_counts', return_value={'total': 5, 'today': 2, 'month': 4}) as counts, \
             patch.object(self.bot, 'medic_photo_status_counts', return_value=dict(status_counts)):
            asyncio.run(self.bot.photo_report(update, context))
            counts.assert_called_once_with(1)
        self.assertIn('За всё время\nВсего: 5', update.message.reply_text.call_args.args[0])
        self.assertIn('🚫 Не принято: 0', update.message.reply_text.call_args.args[0])
        self.assertEqual(context.user_data, {'doctor_name': 'Unfinished upload'})
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value=None), \
             patch.object(self.bot, 'medic_photo_counts') as counts:
            asyncio.run(self.bot.photo_report(update, context))
            counts.assert_not_called()
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=True)), \
             patch.object(self.bot, 'get_user') as get_user:
            asyncio.run(self.bot.photo_report(update, context))
            get_user.assert_not_called()

    def test_original_search_ignores_later_parallel_uploads(self):
        first = self.add_photo(qr='same')
        second = self.add_photo(qr='same')
        self.assertIsNone(self.bot.find_exact_photo_by_digest('digest', first))
        self.assertIsNone(self.bot.find_photo_by_qr('same', first))
        self.assertEqual(self.bot.find_exact_photo_by_digest('digest', second)['id'], first)
        self.assertEqual(self.bot.find_photo_by_qr('same', second)['id'], first)
        self.assertEqual(self.bot.get_ai_candidates('0000000000000000', exclude_photo_id=first), [])
        self.assertEqual(self.bot.get_ai_candidates('0000000000000000', exclude_photo_id=second)[0][0]['id'], first)

    def test_metadata_duplicate_candidates_do_not_use_same_date_alone(self):
        first = self.add_photo()
        second = self.add_photo(digest='new-digest')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET photo_date=?, mother_name=? WHERE id=?',
                ('2026-09-27', 'Каримова Дилноза', first),
            )
            connection.execute(
                'UPDATE photos SET photo_date=?, mother_name=? WHERE id=?',
                ('2026-09-27', 'Саидова Малика', second),
            )
        self.assertEqual(
            self.bot.get_metadata_duplicate_candidates(
                '2026-09-27', 'Саидова Малика', exclude_photo_id=second,
            ),
            [],
        )

    def test_exact_name_and_date_pair_is_the_only_metadata_duplicate(self):
        first = self.add_photo()
        second = self.add_photo(digest='second')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET photo_date=?, mother_name=? WHERE id=?',
                ('2026-10-24', 'Каримова М. Д.', first),
            )
        exact = self.bot.find_photo_by_mother_and_date(
            '2026-10-24', 'Каримова М.Д.', exclude_photo_id=second,
        )
        similar = self.bot.find_photo_by_mother_and_date(
            '2026-10-24', 'Каримова Малика', exclude_photo_id=second,
        )
        wrong_date = self.bot.find_photo_by_mother_and_date(
            '2026-10-23', 'Каримова М.Д.', exclude_photo_id=second,
        )
        self.assertEqual(exact['id'], first)
        self.assertIsNone(similar)
        self.assertIsNone(wrong_date)

    def test_duplicate_policy_migration_removes_visual_only_links(self):
        original = self.add_photo(digest='original')
        visual_only = self.add_photo(digest='visual-only')
        qr_original = self.add_photo(digest='qr-original', qr='03-0000000042')
        qr_copy = self.add_photo(digest='qr-copy', qr='03-0000000042')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                '''UPDATE photos SET original_photo_id=?, review_status='Возможная копия',
                   review_reason='Похожая коробка' WHERE id=?''',
                (original, visual_only),
            )
            connection.execute(
                'DELETE FROM app_migrations WHERE name=?',
                (self.bot.STRICT_DUPLICATE_POLICY_MIGRATION,),
            )
            connection.row_factory = sqlite3.Row
            self.bot.reconcile_strict_duplicate_policy(connection)
        with sqlite3.connect(self.db_path) as connection:
            visual_row = connection.execute(
                'SELECT original_photo_id, review_status FROM photos WHERE id=?',
                (visual_only,),
            ).fetchone()
            qr_row = connection.execute(
                'SELECT original_photo_id, review_status FROM photos WHERE id=?',
                (qr_copy,),
            ).fetchone()
        self.assertEqual(visual_row, (None, 'Не проверено'))
        self.assertEqual(qr_row, (qr_original, 'Возможная копия'))

    def test_visual_hashes_find_candidate_after_background_or_crop_changes(self):
        original = self.add_photo(digest='visual-original')
        current = self.add_photo(digest='visual-current')
        shared_detail_hash = '1234567890abcdef'
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET visual_hashes=? WHERE id=?',
                (f'["{shared_detail_hash}"]', original),
            )

        candidates = self.bot.get_ai_candidates(
            'ffffffffffffffff',
            exclude_photo_id=current,
            visual_hashes=f'["{shared_detail_hash}"]',
        )

        self.assertEqual(candidates[0][0]['id'], original)

    def test_candidate_ranking_prefers_two_local_signals_without_more_candidates(self):
        crop_only = self.add_photo(digest='crop-only')
        crop_and_visual = self.add_photo(digest='crop-and-visual')
        current = self.add_photo(digest='current')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'UPDATE photos SET image_hash=?, crop_hash=?, visual_hashes=? WHERE id=?',
                ('ffffffffffffffff', 'crop-only', None, crop_only),
            )
            connection.execute(
                'UPDATE photos SET image_hash=?, crop_hash=?, visual_hashes=? WHERE id=?',
                ('eeeeeeeeeeeeeeee', 'crop-and-visual', '["1234567890abcdef"]', crop_and_visual),
            )

        def crop_score(_new, stored):
            return (1, 2) if stored == 'crop-only' else (1, 5)

        def visual_score(_new_hash, _stored_hash, _new_visual, stored_visual):
            return 4 if stored_visual else None

        with patch.object(self.bot, 'crop_hash_match_score', side_effect=crop_score), \
             patch.object(self.bot, 'visual_hash_match_score', side_effect=visual_score):
            candidates = self.bot.get_ai_candidates(
                '0000000000000000', crop_hash='new-crop',
                exclude_photo_id=current, visual_hashes='["1234567890abcdef"]',
            )

        self.assertEqual(candidates[0][0]['id'], crop_and_visual)
        self.assertLessEqual(len(candidates), self.bot.AI_DUPLICATE_MAX_CANDIDATES)

    def test_visual_fingerprint_survives_rotation_and_replaced_background(self):
        source_bytes = self.bot.REFERENCE_PACKAGING_FILES[0].read_bytes()
        source = Image.open(BytesIO(source_bytes)).convert('RGB')
        rotated = source.rotate(15, expand=True, fillcolor='white')
        canvas = Image.new(
            'RGB',
            (round(rotated.width * 1.5), round(rotated.height * 1.5)),
            'white',
        )
        canvas.paste(
            rotated,
            ((canvas.width - rotated.width) // 2, (canvas.height - rotated.height) // 2),
        )
        changed = BytesIO()
        canvas.save(changed, format='JPEG', quality=92)

        source_hashes = self.bot.calculate_visual_hashes(source_bytes)
        changed_hashes = self.bot.calculate_visual_hashes(changed.getvalue())
        score = self.bot.visual_hash_match_score(
            str(self.bot.imagehash.phash(source)),
            str(self.bot.imagehash.phash(canvas)),
            source_hashes,
            changed_hashes,
        )

        self.assertIsNotNone(score)

    def test_duplicate_comparison_image_is_one_bounded_image(self):
        source = self.bot.REFERENCE_PACKAGING_FILES[0].read_bytes()
        prepared = self.bot.prepare_duplicate_comparison_image(source)

        with Image.open(BytesIO(prepared)) as image:
            self.assertLessEqual(image.width, 1280)
            self.assertLessEqual(image.height, 768)

    def test_ai_duplicate_accepts_exact_pixels_but_rejects_weak_visual_similarity(self):
        candidate = {'id': 1, 'image': b'candidate'}
        responses = [
            SimpleNamespace(output_text=(
                '{"duplicate_index":1,"confidence":0.99,"same_source_pixels":true,'
                '"same_physical_package":true,"same_qr_code":false,'
                '"same_handwriting":true,"unique_matching_marks":["тот же надрыв"],'
                '"identity_conflicts":[]}'
            )),
            SimpleNamespace(output_text=(
                '{"duplicate_index":1,"confidence":0.99,"same_source_pixels":false,'
                '"same_physical_package":true,"same_qr_code":false,'
                '"same_handwriting":false,"unique_matching_marks":["та же складка"],'
                '"scene_differences":["другой фон"],"identity_conflicts":[]}'
            )),
            SimpleNamespace(output_text=(
                '{"duplicate_index":1,"confidence":0.99,"same_source_pixels":false,'
                '"same_physical_package":true,"same_qr_code":false,'
                '"same_handwriting":true,"unique_matching_marks":["тот же надрыв"],'
                '"scene_differences":["другой фон и ракурс"],"identity_conflicts":[]}'
            )),
        ]
        client = SimpleNamespace(responses=SimpleNamespace(
            create=AsyncMock(side_effect=responses),
        ))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'prepare_image_for_analysis', return_value=b'jpg'):
            pixel_copy = asyncio.run(
                self.bot.ai_confirms_any_duplicate(b'new', [(candidate, (0, 0))])
            )
            visual_copy = asyncio.run(
                self.bot.ai_confirms_any_duplicate(b'new', [(candidate, (0, 0))])
            )
            physical_copy = asyncio.run(
                self.bot.ai_confirms_any_duplicate(b'new', [(candidate, (0, 0))])
            )

        self.assertEqual(pixel_copy, candidate)
        self.assertIsNone(visual_copy)
        self.assertEqual(physical_copy, candidate)

    def test_ai_duplicate_accepts_same_full_qr_on_different_box(self):
        candidate = {'id': 1, 'image': b'candidate'}
        response = SimpleNamespace(output_text=(
            '{"duplicate_index":1,"confidence":0.99,"same_source_pixels":false,'
            '"same_physical_package":false,"same_qr_code":true,'
            '"same_handwriting":false,"unique_matching_marks":[],'
            '"scene_differences":["другая коробка и фон"],"identity_conflicts":[]}'
        ))
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'prepare_image_for_analysis', return_value=b'jpg'):
            duplicate = asyncio.run(
                self.bot.ai_confirms_any_duplicate(b'new', [(candidate, (0, 0))])
            )

        self.assertEqual(duplicate, candidate)

    def test_ai_duplicate_accepts_supported_physical_match_below_old_point_97_cutoff(self):
        candidate = {'id': 1, 'image': b'candidate'}
        response = SimpleNamespace(output_text=(
            '{"duplicate_index":1,"confidence":0.94,"same_source_pixels":false,'
            '"same_physical_package":true,"same_qr_code":false,'
            '"same_handwriting":true,"unique_matching_marks":["тот же надрыв"],'
            '"scene_differences":["другой фон и ракурс"],"identity_conflicts":[]}'
        ))
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'prepare_image_for_analysis', return_value=b'jpg'):
            duplicate = asyncio.run(
                self.bot.ai_confirms_any_duplicate(b'new', [(candidate, (0, 0))])
            )

        self.assertEqual(duplicate, candidate)

    def test_completed_duplicate_batch_records_candidates_and_failed_batch_does_not(self):
        candidate = {'id': 17, 'image': b'candidate'}
        clean = SimpleNamespace(output_text=(
            '{"duplicate_index":null,"confidence":0.99,"same_source_pixels":false,'
            '"same_physical_package":false,"same_handwriting":false,'
            '"unique_matching_marks":[],"identity_conflicts":[]}'
        ))
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=clean)))
        checked = set()
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'load_ai_candidate_images', return_value=[(candidate, (0, 0))]), \
             patch.object(self.bot, 'prepare_image_for_analysis', return_value=b'jpg'):
            duplicate = asyncio.run(self.bot.ai_confirms_duplicate_across_all(
                b'new', [(candidate, (0, 0))], checked,
            ))
        self.assertIsNone(duplicate)
        self.assertEqual(checked, {17})

        failed_client = SimpleNamespace(responses=SimpleNamespace(
            create=AsyncMock(side_effect=RuntimeError('temporary failure')),
        ))
        checked = set()
        with patch.object(self.bot, 'ai_client', failed_client), \
             patch.object(self.bot, 'load_ai_candidate_images', return_value=[(candidate, (0, 0))]), \
             patch.object(self.bot, 'prepare_image_for_analysis', return_value=b'jpg'):
            asyncio.run(self.bot.ai_confirms_duplicate_across_all(
                b'new', [(candidate, (0, 0))], checked,
            ))
        self.assertEqual(checked, set())

    def test_completed_duplicate_batch_records_sqlite_row_candidate(self):
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            candidate = connection.execute(
                "SELECT 17 AS id, X'00' AS image"
            ).fetchone()

        checked = set()
        with patch.object(
                self.bot, 'load_ai_candidate_images',
                return_value=[(candidate, (0, 0))],
             ), patch.object(
                self.bot, 'ai_confirms_any_duplicate',
                AsyncMock(return_value=(None, True)),
             ):
            duplicate = asyncio.run(self.bot.ai_confirms_duplicate_across_all(
                b'new', [(candidate, (0, 0))], checked,
            ))

        self.assertIsNone(duplicate)
        self.assertEqual(checked, {17})

    def test_qr_recovery_uses_haiku_screen_for_photo_without_visible_code(self):
        no_code = SimpleNamespace(output_text=(
            '{"code_visible":false,"code_obstructed":false,"qr_data":null,'
            '"qr_data_confidence":0,"unreadable_reason":"not_visible"}'
        ))
        create = AsyncMock(return_value=no_code)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'metadata_focus_images', return_value=[b'a', b'b', b'c', b'd']):
            result = asyncio.run(self.bot.recover_missing_qr_data(
                b'photo', code_visible_hint=False,
            ))
        self.assertFalse(result['code_visible'])
        self.assertEqual(create.await_count, 1)
        self.assertEqual(
            create.await_args.kwargs['model'], self.bot.CLAUDE_QR_SCREENING_MODEL,
        )
        self.assertEqual(
            create.await_args.kwargs['system'][0]['cache_control'],
            {'type': 'ephemeral', 'ttl': '1h'},
        )

    def test_qr_recovery_does_not_store_number_seen_by_only_one_model(self):
        visible = SimpleNamespace(output_text=(
            '{"code_visible":true,"code_obstructed":false,"qr_data":null,'
            '"qr_data_confidence":0,"unreadable_reason":"too_small"}'
        ))
        verified = SimpleNamespace(output_text=(
            '{"code_visible":true,"code_obstructed":false,'
            '"qr_data":"01-0000000042","qr_data_confidence":0.99,'
            '"unreadable_reason":"none"}'
        ))
        create = AsyncMock(side_effect=[visible, verified])
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'metadata_focus_images', return_value=[b'a', b'b', b'c', b'd']):
            result = asyncio.run(self.bot.recover_missing_qr_data(
                b'photo', code_visible_hint=False,
            ))
        self.assertIsNone(result['qr_data'])
        self.assertEqual(
            [call.kwargs['model'] for call in create.await_args_list],
            [self.bot.CLAUDE_QR_SCREENING_MODEL, self.bot.CLAUDE_VISION_MODEL],
        )

    def test_qr_recovery_stores_only_exact_number_confirmed_by_both_models(self):
        agreed = SimpleNamespace(output_text=(
            '{"code_visible":true,"code_obstructed":false,'
            '"qr_data":"01-0000000042","qr_data_confidence":0.99,'
            '"unreadable_reason":"none"}'
        ))
        create = AsyncMock(side_effect=[agreed, agreed])
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'metadata_focus_images', return_value=[b'a', b'b']):
            result = asyncio.run(self.bot.recover_missing_qr_data(
                b'photo', code_visible_hint=True,
            ))

        self.assertEqual(result['qr_data'], '01-0000000042')
        self.assertEqual(create.await_count, 2)

    def test_qr_recovery_rejects_number_when_model_says_code_is_not_visible(self):
        contradictory = SimpleNamespace(output_text=(
            '{"code_visible":false,"code_obstructed":false,'
            '"qr_data":"01-0000000042","qr_data_confidence":0.99,'
            '"unreadable_reason":"not_visible"}'
        ))
        create = AsyncMock(return_value=contradictory)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'metadata_focus_images', return_value=[b'a', b'b']):
            result = asyncio.run(self.bot.recover_missing_qr_data(
                b'photo', code_visible_hint=True,
            ))

        self.assertFalse(result['code_visible'])
        self.assertIsNone(result['qr_data'])

    def test_background_discards_hallucinated_qr_without_visible_code(self):
        photo_id = self.add_photo(digest='false-qr', status='Проверяется в фоне')
        recovery = {
            'qr_data': None,
            'qr_data_confidence': 0.0,
            'code_visible': False,
            'code_obstructed': False,
            'unreadable_reason': 'not_visible',
        }
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'find_photo_by_qr', return_value=None), \
             patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'get_ai_candidates', return_value=[]), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'AI_BROAD_DUPLICATE_CHECKS_ENABLED', True), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     '2026-10-02', 'Тестова', '1', '01-6044000001',
                     True, True, False, False, True,
                     'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
                 )),
             ), \
             patch.object(
                 self.bot, 'recover_missing_qr_data', AsyncMock(return_value=recovery),
             ), \
             patch.object(
                 self.bot, 'sightengine_ai_assessment',
                 AsyncMock(return_value=(None, None, None)),
             ), \
             patch.object(self.bot, 'update_hospital_excel_file'):
            asyncio.run(
                self.bot.process_photo_in_background(photo_id, 'Карши', 'Кукдала')
            )

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, code_status FROM photos WHERE id = ?', (photo_id,),
            ).fetchone()
        self.assertEqual(row, (None, 'Код отсутствует (допустимо)'))

    def test_background_marks_duplicate_only_for_equal_qr(self):
        qr = '03-0000000001'
        original_id = self.add_photo(digest='original', qr=qr)
        photo_id = self.add_photo(digest='different', status='Проверяется в фоне')
        metadata = AsyncMock(return_value=(
            None, None, '1', qr, True, True, False, False, True,
            'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
        ))
        with patch.object(self.bot, 'read_qr_code_details', return_value=(qr, True)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'ai_confirms_duplicate_across_all', side_effect=AssertionError('visual AI must not decide')), \
             patch.object(self.bot, 'extract_photo_metadata', metadata), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False):
            asyncio.run(self.bot.process_photo_in_background(photo_id, 'Карши', 'Кукдала'))

        metadata.assert_awaited_once()
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT original_photo_id, review_status FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row, (original_id, 'Возможная копия'))

    def test_identical_digest_is_duplicate_even_without_qr(self):
        original_id = self.add_photo(digest='same-bytes', qr=None)
        photo_id = self.add_photo(digest='same-bytes', status='Проверяется в фоне')
        metadata = AsyncMock(return_value=(
            '2026-10-13', 'Распознано ИИ', '1', None,
            True, True, False, False, False,
            'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
        ))
        with patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'ai_confirms_duplicate_across_all', side_effect=AssertionError('exact bytes must decide first')), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', False), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False), \
             patch.object(self.bot, 'extract_photo_metadata', metadata):
            asyncio.run(self.bot.process_photo_in_background(photo_id, 'Карши', 'Кукдала'))

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                '''SELECT photo_date, mother_name, original_photo_id, review_status
                   FROM photos WHERE id=?''',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[:3], (None, None, original_id))
        self.assertEqual(row[3], 'Возможная копия')

    def test_exact_completed_file_reuses_claude_and_sightengine_results(self):
        original_id = self.add_photo(digest='paid-once', qr=None)
        photo_id = self.add_photo(digest='paid-once', status='Проверяется в фоне')
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                '''UPDATE photos
                   SET package_number = ?, ai_generated_score = ?, code_status = ?
                   WHERE id = ?''',
                ('1', 0.02, 'Код отсутствует (допустимо)', original_id),
            )
            connection.execute(
                '''INSERT OR REPLACE INTO photo_checks
                   (photo_id, product_status, material_status, decision,
                    decision_reason, automated_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (
                    original_id, 'NUPPI 1 обнаружен',
                    'Эталонный дизайн подтверждён', 'review',
                    'Проверено', '2026-10-01T00:00:00+00:00',
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', True), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', True), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(side_effect=AssertionError('Claude must be reused')),
             ), \
             patch.object(
                 self.bot, 'sightengine_ai_assessment',
                 AsyncMock(side_effect=AssertionError('Sightengine must be reused')),
             ):
            asyncio.run(
                self.bot.process_photo_in_background(photo_id, 'Карши', 'Кукдала')
            )

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                '''SELECT original_photo_id, review_status, ai_generated_score,
                          package_number, code_status
                   FROM photos WHERE id = ?''',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[:4], (original_id, 'Возможная копия', 0.02, '1'))
        self.assertEqual(row[4], 'Код отсутствует (допустимо)')

    def test_qr_duplicate_does_not_repeat_completed_metadata_retry(self):
        original_id = self.add_photo(digest='original', qr='03-0000000001')
        photo_id = self.add_photo(digest='new-photo', status='Проверяется в фоне')
        candidate = {'id': original_id, 'sent_at': '2026-10-12T08:00:00+00:00'}
        pipeline_retry = AsyncMock(return_value=None)
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'find_photo_by_qr', return_value=candidate), \
             patch.object(
                 self.bot, 'read_qr_code_details',
                 return_value=('03-0000000001', True),
             ), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', False), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     None, None, '1', None, True, True, False, False, True,
                     'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
                 )),
             ), \
             patch.object(self.bot, 'retry_uncertain_photo_metadata', pipeline_retry), \
             patch.object(self.bot, 'update_hospital_excel_file'):
            asyncio.run(
                self.bot.process_photo_in_background(
                    photo_id, 'Карши', 'Частный роддом',
                )
            )

        pipeline_retry.assert_not_awaited()

    def test_reused_foreign_qr_is_duplicate_even_when_boxes_differ(self):
        foreign_payload = 'https://example.test/foreign-product'
        original_id = self.add_photo(digest='foreign-original', qr=foreign_payload)
        photo_id = self.add_photo(digest='foreign-new', status='Проверяется в фоне')
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(
                 self.bot, 'read_qr_code_details',
                 return_value=(foreign_payload, True),
             ), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'AI_BROAD_DUPLICATE_CHECKS_ENABLED', False), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', False), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     None, None, '1', None, True, True, False, False, True,
                     'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
                 )),
             ), \
             patch.object(self.bot, 'update_hospital_excel_file'):
            asyncio.run(
                self.bot.process_photo_in_background(photo_id, 'Карши', 'Частный роддом')
            )

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT review_status, original_photo_id, code_status FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], 'Возможная копия')
        self.assertEqual(row[1], original_id)
        self.assertIn('Посторонний QR/Data Matrix', row[2])

    def test_suspicious_generated_qr_is_highlighted_for_operator(self):
        photo_id = self.add_photo(digest='synthetic-qr', status='Проверяется в фоне')
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'find_photo_by_qr', return_value=None), \
             patch.object(
                 self.bot, 'read_qr_code_details',
                 return_value=('03-0000000042', True),
             ), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'AI_BROAD_DUPLICATE_CHECKS_ENABLED', False), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', False), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     None, None, '1', '03-0000000042', True, True, True, False, True,
                     'Подозрительный QR/Data Matrix',
                     'Код выглядит цифровой вклейкой.', 'ok',
                 )),
             ), \
             patch.object(self.bot, 'update_hospital_excel_file'):
            asyncio.run(
                self.bot.process_photo_in_background(photo_id, 'Карши', 'Частный роддом')
            )

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT review_status, review_reason, code_status FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], 'Возможная подделка QR-кода')
        self.assertIn('цифровой вклейкой', row[1])
        self.assertIn('нарисованным, сгенерированным или заменённым', row[2])

    def test_exact_bytes_without_qr_or_manual_metadata_are_a_duplicate(self):
        original_id = self.add_photo(
            digest='same-bytes', qr=None, status='Нет явных признаков',
        )
        photo_id = self.add_photo(digest='same-bytes', status='Проверяется в фоне')
        metadata = AsyncMock(return_value=(
            None, None, '1', None, True, True, False, False, False,
            'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
        ))
        with patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'QR_STRICT_RECOVERY_FOR_ALL_MISSING', False), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', False), \
             patch.object(self.bot, 'extract_photo_metadata', metadata):
            asyncio.run(self.bot.process_photo_in_background(photo_id, 'Карши', 'Кукдала'))

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT qr_data, original_photo_id, review_status FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[:2], (None, original_id))
        self.assertEqual(row[2], 'Возможная копия')

    def test_background_marks_writing_over_qr_as_error_even_after_clean_ai_result(self):
        photo_id = self.add_photo(digest='new-photo', status='Проверяется в фоне')
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'find_photo_by_qr', return_value=None), \
             patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'get_ai_candidates', return_value=[]), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     None, None, '1', None, True, True, False, True,
                      True,
                     'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
                 )),
             ), \
             patch.object(self.bot, 'sightengine_ai_assessment', AsyncMock(return_value=(None, None, None))), \
             patch.object(self.bot, 'update_hospital_excel_file'):
            asyncio.run(self.bot.process_photo_in_background(photo_id, 'Ташкент', 'Роддом 1'))

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                'SELECT review_status, review_reason, code_status FROM photos WHERE id=?',
                (photo_id,),
            ).fetchone()
        self.assertEqual(row[0], 'Ошибка: QR-код перекрыт')
        self.assertIn('перекрытие', row[1])
        self.assertEqual(
            row[2], 'Ошибка: на QR/Data Matrix нанесена надпись или код перекрыт',
        )

    def test_sightengine_remains_enabled_during_openai_savings_mode(self):
        photo_id = self.add_photo(digest='sightengine-photo', status='Проверяется в фоне')
        sightengine = AsyncMock(return_value=(
            'Нет явных признаков', 'Sightengine: вероятность ИИ-генерации 2.0%.', 0.02,
        ))
        with patch.object(self.bot, 'find_exact_photo_by_digest', return_value=None), \
             patch.object(self.bot, 'find_photo_by_qr', return_value=None), \
             patch.object(self.bot, 'read_qr_code_details', return_value=(None, False)), \
             patch.object(self.bot.imagehash, 'phash', return_value='0000000000000000'), \
             patch.object(self.bot, 'calculate_crop_hash', return_value='crop-hash'), \
             patch.object(self.bot, 'calculate_visual_hashes', return_value='["0000000000000000"]'), \
             patch.object(self.bot, 'get_ai_candidates', return_value=[]), \
             patch.object(self.bot, 'ai_client', object()), \
             patch.object(self.bot, 'AI_MAX_SAVINGS_MODE', True), \
             patch.object(self.bot, 'SIGHTENGINE_ENABLED', True), \
             patch.object(
                 self.bot, 'extract_photo_metadata',
                 AsyncMock(return_value=(
                     None, None, '1', None, True, True, False, False,
                      False,
                     'Нет явных признаков', 'Фотография выглядит нормально.', 'ok',
                 )),
             ), \
             patch.object(self.bot, 'sightengine_ai_assessment', sightengine):
            result = asyncio.run(
                self.bot.process_photo_in_background(photo_id, 'Ташкент', 'Роддом 1')
            )

        self.assertTrue(result)
        sightengine.assert_awaited_once()
        with sqlite3.connect(self.db_path) as connection:
            score = connection.execute(
                'SELECT ai_generated_score FROM photos WHERE id = ?',
                (photo_id,),
            ).fetchone()[0]
        self.assertEqual(score, 0.02)

    def test_metadata_ignores_date_and_name_and_uses_three_qr_views(self):
        response = SimpleNamespace(output_text=(
            '{"package_date":"2026-09-20","mother_name":"Тестова Малика",'
            '"package_number":"1","qr_data":"01-0000000042",'
            '"qr_data_confidence":0.99,"is_code_visible":true,'
            '"is_nuppi_1":true,"is_reference_design":true,"is_edited":false,'
            '"is_code_obstructed":false,"visual_status":"clean",'
            '"visual_reason":"Явные признаки не обнаружены."}'
        ))
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'load_reference_packaging', return_value=[b'ref-1', b'ref-2']), \
             patch.object(self.bot, 'metadata_image_parts', return_value=(b'original', b'enhanced', b'contrast')):
            metadata = asyncio.run(self.bot.extract_photo_metadata(b'photo'))

        self.assertEqual(create.await_count, 1)
        request = create.await_args.kwargs
        self.assertEqual(request['model'], self.bot.CLAUDE_METADATA_MODEL)
        self.assertIn(
            'НЕ распознавай и НЕ возвращай рукописную дату или ФИО',
            request['system'][0]['text'],
        )
        self.assertEqual(
            request['system'][0]['cache_control'],
            {'type': 'ephemeral', 'ttl': '1h'},
        )
        image_items = [
            item for item in request['input'][0]['content'] if item['type'] == 'input_image'
        ]
        self.assertEqual(
            [item['detail'] for item in image_items],
            ['low', 'low', 'high', 'high', 'high'],
        )
        self.assertEqual(
            image_items[1]['cache_control'], {'type': 'ephemeral', 'ttl': '1h'},
        )
        self.assertEqual(metadata[:4], (None, None, '1', '01-0000000042'))

    def test_missing_qr_does_not_trigger_date_or_name_retry(self):
        response = SimpleNamespace(output_text=(
            '{"package_date":"2026-09-26","mother_name":"Каримова М.Д.",'
            '"package_number":"1","qr_data":null,"qr_data_confidence":0.0,'
            '"is_code_visible":true,"is_nuppi_1":true,"is_reference_design":true,'
            '"is_edited":false,"is_code_obstructed":false,"visual_status":"clean",'
            '"visual_reason":"Явные признаки не обнаружены."}'
        ))
        create = AsyncMock(return_value=response)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'load_reference_packaging', return_value=[b'ref-1', b'ref-2']), \
             patch.object(self.bot, 'metadata_image_parts', return_value=(b'original', b'enhanced', b'contrast')):
            metadata = asyncio.run(self.bot.extract_photo_metadata(b'photo'))

        self.assertEqual(create.await_count, 1)
        self.assertEqual(metadata[:2], (None, None))
        self.assertIsNone(metadata[3])

    def test_malformed_product_response_never_falls_back_to_handwriting(self):
        create = AsyncMock(return_value=SimpleNamespace(output_text='Не JSON'))
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'load_reference_packaging', return_value=[b'ref-1', b'ref-2']), \
             patch.object(self.bot, 'metadata_image_parts', return_value=(b'original', b'enhanced', b'contrast')):
            metadata = asyncio.run(self.bot.extract_photo_metadata(b'photo'))

        self.assertEqual(create.await_count, 1)
        self.assertEqual(metadata[:2], (None, None))
        self.assertEqual(metadata[-1], 'unknown_error')

    def test_low_credit_error_is_not_immediately_retried(self):
        create = AsyncMock(side_effect=self.bot.ClaudeAPIError(
            'Claude API 400 invalid_request_error: Your credit balance is too low'
        ))
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(self.bot, 'ai_client', client), \
             patch.object(self.bot, 'AI_MAX_SAVINGS_MODE', True), \
             patch.object(self.bot, 'load_reference_packaging', return_value=[b'ref-1', b'ref-2']), \
             patch.object(self.bot, 'metadata_image_parts', return_value=(b'original', b'enhanced', b'contrast')):
            metadata = asyncio.run(self.bot.extract_photo_metadata(b'photo'))

        self.assertEqual(create.await_count, 1)
        self.assertEqual(metadata[:2], (None, None))

    def test_main_wires_report_before_conversation_and_keyboard_shows_it(self):
        application = Mock()
        builder = Mock()
        builder.bot.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = application
        with patch.object(self.bot.Application, 'builder', return_value=builder), patch.object(self.bot, 'init_db'):
            self.bot.main()
        builder.bot.assert_called_once()
        self.assertIsInstance(builder.bot.call_args.args[0], self.bot.ChatLoggingBot)
        builder.bot.return_value.concurrent_updates.assert_called_once_with(False)
        handlers = [call.args[0] for call in application.add_handler.call_args_list]
        report_positions = [i for i, handler in enumerate(handlers) if getattr(handler, 'callback', None) == self.bot.photo_report]
        conversation_position = next(i for i, handler in enumerate(handlers) if isinstance(handler, self.bot.ConversationHandler))
        self.assertEqual(len(report_positions), 2)
        self.assertTrue(all(i < conversation_position for i in report_positions))
        self.assertIn(self.bot.MENU_PHOTO_REPORT, [button.text for row in self.bot.medic_keyboard().keyboard for button in row])
        gate_calls = [
            call for call in application.add_handler.call_args_list
            if getattr(call.args[0], 'callback', None) == self.bot.enforce_working_hours
        ]
        self.assertEqual(len(gate_calls), 1)
        self.assertEqual(gate_calls[0].kwargs['group'], -1)
        incoming_log_calls = [
            call for call in application.add_handler.call_args_list
            if getattr(call.args[0], 'callback', None) == self.bot.log_incoming_chat
        ]
        self.assertEqual(len(incoming_log_calls), 1)
        self.assertEqual(incoming_log_calls[0].kwargs['group'], -2)

    def test_chat_log_saves_incoming_and_outgoing_messages_without_duplicates(self):
        message_date = datetime(2026, 10, 2, 8, 15, tzinfo=timezone.utc)
        message = SimpleNamespace(
            text='Здравствуйте', caption=None, photo=[], document=None, video=None,
            voice=None, audio=None, sticker=None, location=None, contact=None,
            message_id=41, date=message_date,
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=12345),
        )
        asyncio.run(self.bot.log_incoming_chat(update, SimpleNamespace()))
        asyncio.run(self.bot.log_incoming_chat(update, SimpleNamespace()))
        self.bot.save_chat_message(
            12345, 'outgoing', 'text', 'Добрый день',
            telegram_message_id=42, created_at=message_date,
        )

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                'SELECT * FROM chat_messages ORDER BY id'
            ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['direction'], 'incoming')
        self.assertEqual(rows[0]['text'], 'Здравствуйте')
        self.assertEqual(rows[1]['direction'], 'outgoing')
        self.assertEqual(rows[1]['text'], 'Добрый день')

    def test_photo_batch_keyboard_offers_more_photos_and_finish(self):
        labels = [
            button.text
            for row in self.bot.photo_batch_keyboard().keyboard
            for button in row
        ]
        self.assertEqual(labels, [self.bot.MENU_SEND_MORE_PHOTO, self.bot.MENU_FINISH_PHOTOS])

    def test_telegram_confirmation_does_not_retry_an_ambiguous_timeout(self):
        message = SimpleNamespace(
            reply_text=AsyncMock(side_effect=[self.bot.TimedOut(), 'delivered']),
        )
        with patch.object(self.bot.asyncio, 'sleep', AsyncMock()) as sleep:
            result = asyncio.run(self.bot.reply_text_with_retry(message, 'Фотография принята'))
        self.assertIsNone(result)
        self.assertEqual(message.reply_text.await_count, 1)
        sleep.assert_not_awaited()

    def test_telegram_confirmation_retries_a_definite_network_error(self):
        message = SimpleNamespace(
            reply_text=AsyncMock(side_effect=[self.bot.NetworkError('offline'), 'delivered']),
        )
        with patch.object(self.bot.asyncio, 'sleep', AsyncMock()) as sleep:
            result = asyncio.run(self.bot.reply_text_with_retry(message, 'Фотография принята'))
        self.assertEqual(result, 'delivered')
        self.assertEqual(message.reply_text.await_count, 2)
        sleep.assert_awaited_once_with(1)

    def test_locked_chat_log_is_queued_and_saved_later_once(self):
        real_connect = self.bot.connect
        locked = sqlite3.OperationalError('database is locked')
        spool_path = Path(self.temporary.name) / 'chat-spool.jsonl'
        with patch.object(self.bot, 'CHAT_LOG_SPOOL_PATH', spool_path), patch.object(
            self.bot, 'connect', side_effect=[locked] * self.bot.CHAT_LOG_RETRY_ATTEMPTS,
        ):
            saved = self.bot.save_chat_message(
                12345, 'incoming', 'text', 'Не терять меня', telegram_message_id=77,
            )
        self.assertFalse(saved)
        self.assertTrue(spool_path.exists())
        self.assertIn('Не терять меня', spool_path.read_text(encoding='utf-8'))

        with patch.object(self.bot, 'CHAT_LOG_SPOOL_PATH', spool_path), patch.object(
            self.bot, 'connect', side_effect=real_connect,
        ):
            self.assertEqual(self.bot.flush_pending_chat_messages(), 1)
            self.assertEqual(self.bot.flush_pending_chat_messages(), 0)
        self.assertFalse(spool_path.exists())
        with sqlite3.connect(self.db_path) as connection:
            count = connection.execute(
                'SELECT COUNT(*) FROM chat_messages WHERE telegram_message_id = 77'
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_photo_insert_retries_when_database_is_temporarily_locked(self):
        with patch.object(
            self.bot,
            'insert_incoming_photo',
            side_effect=[sqlite3.OperationalError('database is locked'), 321],
        ) as insert, patch.object(self.bot.time, 'sleep') as sleep:
            result = self.bot.insert_incoming_photo_with_retry('args', attempts=2)
        self.assertEqual(result, 321)
        self.assertEqual(insert.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_photo_processing_is_scheduled_before_confirmation_is_sent(self):
        source = Path(self.bot.__file__).read_text(encoding='utf-8')
        receive_start = source.index('async def receive_photo')
        receive_end = source.index('async def continue_photo_batch', receive_start)
        receive_source = source[receive_start:receive_end]
        self.assertLess(
            receive_source.index('context.application.create_task'),
            receive_source.index(
                'await reply_text_with_retry',
                receive_source.index('context.application.create_task'),
            ),
        )

    def test_photo_batch_buttons_are_wired_and_finish_returns_main_keyboard(self):
        application = Mock()
        builder = Mock()
        builder.bot.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = application
        with patch.object(self.bot.Application, 'builder', return_value=builder), patch.object(self.bot, 'init_db'):
            self.bot.main()
        conversation = next(
            call.args[0] for call in application.add_handler.call_args_list
            if isinstance(call.args[0], self.bot.ConversationHandler)
        )
        callbacks = [handler.callback for handler in conversation.states[self.bot.PHOTO]]
        self.assertIn(self.bot.continue_photo_batch, callbacks)
        self.assertEqual(callbacks.count(self.bot.finish_photo_batch), 2)

        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(user_data={'doctor_name': 'Doctor', 'batch_photos': [1, 2]})
        result = asyncio.run(self.bot.finish_photo_batch(update, context))
        self.assertEqual(result, self.bot.ConversationHandler.END)
        self.assertEqual(context.user_data, {})
        finish_markup = update.message.reply_text.call_args.kwargs['reply_markup']
        finish_labels = [button.text for row in finish_markup.keyboard for button in row]
        self.assertIn(self.bot.MENU_SEND_PHOTO, finish_labels)
        self.assertIn(self.bot.MENU_OPERATOR_COMMENTS, finish_labels)
        for label in ('📊 Отчёт по фотографиям', 'отчет по фотографиям', 'Отчёт по фотографиям'):
            self.assertRegex(label, self.bot.PHOTO_REPORT_PATTERN)

    def test_medic_sees_only_operator_comments_for_own_photos(self):
        own_photo = self.add_photo(
            sender=1, doctor='Врач А', hospital='Роддом А',
            sent_at='2026-09-27T08:15:00+00:00',
        )
        other_photo = self.add_photo(sender=2, digest='other-photo')
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """INSERT INTO photo_checks
                   (photo_id, manual_decision, manual_reason, manual_reviewer, manual_at)
                   VALUES (?, 'review', 'Переснимите код крупнее', 'operator',
                           '2026-09-27T09:00:00+00:00')""",
                (own_photo,),
            )
            connection.execute(
                """INSERT INTO photo_checks
                   (photo_id, manual_decision, manual_reason, manual_reviewer, manual_at)
                   VALUES (?, 'rejected', 'Чужой комментарий', 'operator',
                           '2026-09-27T09:01:00+00:00')""",
                (other_photo,),
            )

        comments = self.bot.medic_operator_comments(1)
        self.assertEqual([row['id'] for row in comments], [own_photo])

        message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value={'role': 'Медик'}):
            asyncio.run(self.bot.operator_comments(update, SimpleNamespace()))

        message.reply_photo.assert_awaited_once()
        photo_call = message.reply_photo.await_args
        self.assertEqual(photo_call.kwargs['photo'], 'test-file')
        caption = photo_call.kwargs['caption']
        self.assertIn(f'Фото №{own_photo}', caption)
        self.assertIn('Переснимите код крупнее', caption)
        self.assertIn('На проверке', caption)
        self.assertNotIn('Чужой комментарий', caption)
        self.assertIsNotNone(photo_call.kwargs['reply_markup'])

    def test_operator_comment_uses_database_image_when_file_id_is_invalid(self):
        photo_id = self.add_photo(sender=1)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """INSERT INTO photo_checks
                   (photo_id, manual_decision, manual_reason, manual_reviewer, manual_at)
                   VALUES (?, 'rejected', 'Нужно переснять фотографию', 'operator',
                           '2026-09-27T09:00:00+00:00')""",
                (photo_id,),
            )

        successful_send = SimpleNamespace(message_id=1)
        message = SimpleNamespace(
            reply_text=AsyncMock(),
            reply_photo=AsyncMock(side_effect=[self.bot.BadRequest('invalid file id'), successful_send]),
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=message)
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value={'role': 'Медик'}):
            asyncio.run(self.bot.operator_comments(update, SimpleNamespace()))

        self.assertEqual(message.reply_photo.await_count, 2)
        fallback_photo = message.reply_photo.await_args_list[1].kwargs['photo']
        self.assertIsInstance(fallback_photo, BytesIO)
        self.assertGreater(len(fallback_photo.getvalue()), 0)

    def test_excel_upload_counts_rows_and_existing_summary_values(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Подробно'
        sheet.append(['Служебная строка'])
        sheet.append([])
        sheet.append(['Дата', 'ФИО врача', 'Количество фотографий'])
        sheet.append(['2026-09-01', 'Иванова Анна', 1])
        sheet.append(['2026-09-01', 'Петрова Мария', 3])
        sheet.append(['2026-09-02', '  иванова   анна  ', None])
        sheet.append(['', 'Итого', 5])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()

        counts = self.bot.extract_doctor_photo_counts(stream.getvalue())
        self.assertEqual(counts, [('Петрова Мария', 3), ('Иванова Анна', 2)])

        report = self.bot.build_doctor_photo_count_report(counts, 'исходник.xlsx')
        result = load_workbook(report, data_only=True)
        result_sheet = result['Итог по врачам']
        self.assertEqual(result_sheet['A1'].value, 'Количество фотографий по врачам')
        self.assertEqual(result_sheet['A5'].value, 'Петрова Мария')
        self.assertEqual(result_sheet['B5'].value, 3)
        self.assertEqual(result_sheet['A7'].value, 'Всего')
        self.assertEqual(result_sheet['B7'].value, 5)
        result.close()

    def test_excel_upload_supports_bot_sheet_summary_and_rejects_unknown_layout(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Сидорова Елена'
        sheet['C1'] = 'Количество фото'
        sheet['D1'] = 7
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        self.assertEqual(
            self.bot.extract_doctor_photo_counts(stream.getvalue()),
            [('Сидорова Елена', 7)],
        )

        workbook = Workbook()
        workbook.active.append(['Неизвестный столбец', 'Значение'])
        invalid = BytesIO()
        workbook.save(invalid)
        workbook.close()
        with self.assertRaisesRegex(ValueError, 'не найден столбец'):
            self.bot.extract_doctor_photo_counts(invalid.getvalue())

    def test_operator_ten_day_report_groups_rows_and_uses_tashkent_boundaries(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute('''INSERT INTO users
                (telegram_id, username, full_name, city, district, maternity_hospital,
                 role, medic_code, is_active, registered_at)
                VALUES (1, 'medic', 'Медик Один', 'Ташкент', '', 'Роддом 1',
                        'Медик', '0001', 1, '2026-09-01T00:00:00+00:00')''')

        self.add_photo(sent_at='2026-09-01T18:59:59+00:00', digest='before',
                       doctor='Не входит', hospital='Роддом 1')
        self.add_photo(sent_at='2026-09-01T19:00:00+00:00', digest='first',
                       doctor='Иванова Анна', hospital='Роддом 1')
        self.add_photo(sent_at='2026-09-08T10:00:00+00:00', digest='middle',
                       doctor='Иванова Анна', hospital='Роддом 1')
        self.add_photo(sent_at='2026-09-11T18:59:58+00:00', digest='last',
                       doctor='Иванова Анна', hospital='Роддом 1')
        self.add_photo(sent_at='2026-09-05T08:00:00+00:00', digest='other',
                       doctor='Петрова Мария', hospital='Роддом 2')
        self.add_photo(sent_at='2026-09-11T19:00:00+00:00', digest='after',
                       doctor='Не входит', hospital='Роддом 1')

        report_data = self.bot.operator_ten_day_photo_report(
            datetime(2026, 9, 11, 18, 59, 59, tzinfo=timezone.utc)
        )
        self.assertEqual(report_data['date_from'].isoformat(), '2026-09-02')
        self.assertEqual(report_data['date_to'].isoformat(), '2026-09-11')
        self.assertEqual(report_data['total_photos'], 4)
        self.assertEqual(report_data['total_amount'], 400_000)
        self.assertEqual(report_data['rows'], [
            {
                'city': 'Ташкент', 'maternity_hospital': 'Роддом 1',
                'doctor_name': 'Иванова Анна', 'photo_count': 3,
            },
            {
                'city': 'Ташкент', 'maternity_hospital': 'Роддом 2',
                'doctor_name': 'Петрова Мария', 'photo_count': 1,
            },
        ])

        report = self.bot.build_operator_ten_day_report(report_data)
        workbook = load_workbook(report, data_only=True)
        sheet = workbook['Отчёт за 10 дней']
        self.assertEqual(sheet.max_column, 5)
        self.assertEqual(
            [cell.value for cell in sheet[1]],
            ['Город', 'Роддом', 'Имя врача', 'Количество фото', 'Сумма, сум'],
        )
        self.assertEqual([sheet.cell(2, column).value for column in range(1, 6)],
                         ['Ташкент', 'Роддом 1', 'Иванова Анна', 3, 300_000])
        self.assertEqual([sheet.cell(4, column).value for column in range(1, 6)],
                         [None, None, 'Итого', 4, 400_000])
        workbook.close()

    def test_operator_can_request_ten_day_report_from_bot(self):
        operator = {'role': 'Оператор', 'is_active': 1}
        report_data = {
            'date_from': datetime(2026, 9, 2).date(),
            'date_to': datetime(2026, 9, 11).date(),
            'rows': [],
            'total_photos': 12,
            'total_amount': 1_200_000,
        }
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=10),
            message=SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock()),
        )
        context = SimpleNamespace(user_data={})
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value=operator), \
             patch.object(self.bot, 'operator_ten_day_photo_report', return_value=report_data), \
             patch.object(self.bot, 'build_operator_ten_day_report', return_value=BytesIO(b'xlsx')):
            asyncio.run(self.bot.operator_ten_day_report(update, context))

        call = update.message.reply_document.call_args
        self.assertEqual(call.kwargs['filename'], 'Отчет_за_10_дней_02.09.2026-11.09.2026.xlsx')
        self.assertIn('12 фотографий', call.kwargs['caption'])
        self.assertIn('1 200 000 сум', call.kwargs['caption'])

    def test_operator_menu_and_handlers_include_excel_report(self):
        operator = {'role': 'Оператор', 'is_active': 1}
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()), effective_user=SimpleNamespace(id=10))
        context = SimpleNamespace(user_data={})
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value=operator):
            asyncio.run(self.bot.excel_doctor_report_prompt(update, context))
        self.assertTrue(context.user_data['awaiting_excel_doctor_report'])
        self.assertIn('.xlsx', update.message.reply_text.call_args.args[0])

        update.message.reply_text.reset_mock()
        with patch.object(self.bot, 'deny_unlisted_user', AsyncMock(return_value=False)), \
             patch.object(self.bot, 'get_user', return_value=operator):
            asyncio.run(self.bot.start(update, context))
        start_keyboard = update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertIn(
            self.bot.MENU_EXCEL_DOCTOR_REPORT,
            [button.text for row in start_keyboard.keyboard for button in row],
        )
        self.assertIn(
            self.bot.MENU_OPERATOR_TEN_DAY_REPORT,
            [button.text for row in start_keyboard.keyboard for button in row],
        )

        application = Mock()
        builder = Mock()
        builder.bot.return_value = builder
        builder.concurrent_updates.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = application
        with patch.object(self.bot.Application, 'builder', return_value=builder), patch.object(self.bot, 'init_db'):
            self.bot.main()
        callbacks = [getattr(call.args[0], 'callback', None) for call in application.add_handler.call_args_list]
        self.assertIn(self.bot.excel_doctor_report_prompt, callbacks)
        self.assertIn(self.bot.operator_excel_doctor_report, callbacks)
        self.assertIn(self.bot.operator_ten_day_report, callbacks)

    def test_schema_migration_is_idempotent(self):
        self.add_photo()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute('ALTER TABLE photos DROP COLUMN original_photo_id')
        connection.close()
        self.bot.init_db()
        self.bot.init_db()
        with sqlite3.connect(self.db_path) as connection:
            self.assertIn('original_photo_id', {row[1] for row in connection.execute('PRAGMA table_info(photos)')})
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM photos').fetchone()[0], 1)
