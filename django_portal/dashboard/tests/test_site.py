"""Тесты сайта-панели."""
import gc
import sqlite3
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from .. import db as site_db
from .. import photos as site_photos
from .. import reports as site_reports
from .. import text as site_text
from .. import views
from ..views import photos as photo_views
from ..views import reports as report_views


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

    def accept(self, *photo_ids):
        """Отметить фото принятыми оператором: отчёты учитывают только такие."""
        site_db.ensure_photo_checks_table(self.connection)
        self.connection.executemany(
            "INSERT OR REPLACE INTO photo_checks (photo_id, manual_decision) VALUES (?, 'accepted')",
            [(photo_id,) for photo_id in photo_ids],
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

    def test_dashboard_has_sections_status_tabs_and_filter_reset(self):
        self.add_photo(1)
        response, context = self.dashboard_at()
        self.assertEqual(context['decision_total'], 1)
        self.assertFalse(context['has_filters'])
        self.assertContains(response, reverse('reports'))
        self.assertContains(response, 'class="status-tab review')
        self.assertNotContains(response, 'Сбросить фильтры')

        response, context = self.dashboard_at(city='Ташкент')
        self.assertTrue(context['has_filters'])
        self.assertContains(response, 'Сбросить фильтры')

    def test_reports_page_shows_medic_statistics(self):
        self.add_photo(1, sent_at='2026-09-03T10:00:00+00:00')
        response, context = self.reports_at(date_from='2026-09-01', date_to='2026-09-03')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(context['medic_stats']), 1)
        self.assertContains(response, 'Статистика медиков')
        self.assertContains(response, 'Medic A')
        self.assertContains(response, 'action="/photos/export.xlsx"')
        self.assertContains(response, 'name="date_from" value="2026-09-01"')

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
            Path(__file__).resolve().parents[1] / 'static' / 'dashboard' / 'photo_viewer.js'
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
        self.assertContains(response, 'id="period-photo-counter">Найдено <b>0</b> фото')
        self.assertContains(response, 'Период: <b>Все даты</b>')
        self.assertContains(response, f"Медиков: <b>{context['medic_total']['medic_count']}</b>")
        self.assertContains(response, 'За всё время: <b>0</b>')
        self.assertContains(response, 'Фотографий не найдено')
        self.assertNotContains(response, 'UTC+5')

    def reports_at(self, now=None, **filters):
        request = self.factory.get('/reports/', filters)
        request.user = SimpleNamespace(is_authenticated=True, username='operator')
        with override_settings(TIME_ZONE='Asia/Tashkent', USE_TZ=True), \
             patch('django.utils.timezone.now', return_value=now or datetime(2026, 9, 3, 12, tzinfo=timezone.utc)), \
             patch.object(report_views, 'render', wraps=report_views.render) as rendering:
            response = views.reports_page(request)
        return response, rendering.call_args.args[2]

    def test_full_report_form_defaults_to_last_ten_days(self):
        response, context = self.reports_at()
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
        self.accept(1, 2, 3)
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
        self.assertContains(response, 'Период: <b>03.09.2026</b>')

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
        self.assertContains(response, 'Период: <b>01.08.2026 — 31.08.2026</b>')

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
        self.accept(1, 2)
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
        self.assertContains(response, 'Период: <b>с 02.09.2026</b>')

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
