"""Собирает статичную демо-версию сайта-панели для GitHub Pages.

    python tools/build_pages_demo.py [папка_вывода]     # по умолчанию _site

Скрипт создаёт временную базу с выдуманными медиками и фотографиями, открывает
страницы настоящего Django-сайта через тестовый клиент и сохраняет их как HTML.
Ссылки переписываются на относительные, формы отключаются, сверху добавляется
плашка «демо». Реальные базы, .env и ключи не используются.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]

MEDICS = [
    (1001, "Каримова Дилноза", "Ташкент", ["Роддом №1", "Роддом №4"]),
    (1002, "Юсупов Бахтиёр", "Ташкент", ["Роддом №6"]),
    (1003, "Рахимова Малика", "Самарканд", ["Областной перинатальный центр"]),
]
DOCTORS = ["Азизова Нигора", "Ким Елена", "Тураева Шахноза", "Мирзаев Отабек", "Сафарова Гулнора"]
STATUSES = [
    "Нет явных признаков",
    "Нет явных признаков",
    "Нет явных признаков",
    "Возможная копия",
    "Возможное ИИ-изображение",
    "Не проверено",
]
CHATS = [
    (1001, "incoming", "Здравствуйте, отправила 2 фото из Роддома №1."),
    (1001, "outgoing", "Спасибо, приняли. На втором фото не читается код — переснимите, пожалуйста."),
    (1001, "incoming", "Хорошо, пересниму завтра утром."),
    (1003, "incoming", "Добрый день! Можно добавить новый роддом?"),
    (1003, "outgoing", "Да, добавили «Областной перинатальный центр» в ваш список."),
]
DEMO_TEAM = [
    ("admin", "Администратор Демо", "admin", ""),
    ("supervisor", "Руководитель Ташкента", "supervisor", "Ташкент"),
    ("operator", "Оператор Проверки", "photo_operator", ""),
]
PALETTE = [(46, 125, 110), (52, 101, 164), (196, 120, 48), (140, 70, 120), (90, 110, 60), (170, 60, 60)]
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# Демо без сервера: решения и правки фото сохраняются только на странице
# (window.NUPPI_DEMO), выгрузки Excel скачиваются, остальные формы показывают
# короткое уведомление вместо перехода на несуществующий адрес.
DEMO_SCRIPT = (
    "<script>window.NUPPI_DEMO=true;"
    "window.addEventListener('submit',function(e){"
    "var f=e.target;if(f.closest('.decision-form,.photo-edit-form'))return;"
    "var t=(e.submitter&&e.submitter.getAttribute('formaction'))||f.getAttribute('action')||'';"
    "if(/\\.xlsx$/.test(t))return;"
    "e.preventDefault();e.stopImmediatePropagation();"
    "if(window.nuppiToast)window.nuppiToast('В демо-версии эта функция недоступна.','info');},true);</script>"
)


def configure_environment(work: Path) -> None:
    os.environ.update(
        BOT_DB_PATH=str(work / "bot.db"),
        DJANGO_DB_PATH=str(work / "portal.sqlite3"),
        EXCEL_REPORTS_DIR=str(work / "excel"),
        DJANGO_SECRET_KEY=secrets.token_urlsafe(40),
        DJANGO_DEBUG="0",
        DJANGO_ALLOWED_HOSTS="testserver",
        CREATOR_DJANGO_USERNAME="",
        CREATOR_TELEGRAM_ID="",
        TELEGRAM_BOT_TOKEN="000:demo",
        ANTHROPIC_API_KEY="",
        SIGHTENGINE_API_USER="",
        SIGHTENGINE_API_SECRET="",
        DJANGO_SETTINGS_MODULE="medical_portal.settings",
    )
    sys.path[:0] = [str(PROJECT_DIR), str(PROJECT_DIR / "django_portal")]


def load_fonts():
    from PIL import ImageFont

    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, 34), ImageFont.truetype(path, 22)
        except OSError:
            continue
    default = ImageFont.load_default()
    return default, default


def package_image(number: int, code: str | None, fonts) -> bytes:
    """Рисует условную коробку на столе — заведомо не настоящая упаковка."""
    from PIL import Image, ImageDraw

    big, small = fonts
    rnd = random.Random(number)
    color = PALETTE[number % len(PALETTE)]
    img = Image.new("RGB", (900, 1200), (222, 214, 200))
    draw = ImageDraw.Draw(img)
    for y in range(0, 1200, 40):
        draw.line((0, y, 900, y + rnd.randint(-8, 8)), fill=(212, 203, 188), width=2)
    x0, y0 = 170 + rnd.randint(-30, 30), 220 + rnd.randint(-30, 30)
    draw.rectangle((x0 + 18, y0 + 18, x0 + 578, y0 + 778), fill=(170, 160, 145))
    draw.rectangle((x0, y0, x0 + 560, y0 + 760), fill=color)
    draw.rectangle((x0, y0, x0 + 560, y0 + 150), fill=tuple(min(255, c + 60) for c in color))
    draw.text((x0 + 40, y0 + 50), "DEMO PRODUCT", fill="white", font=big)
    draw.text((x0 + 40, y0 + 200), f"Фото №{number} · тестовые данные", fill="white", font=small)
    qx, qy = x0 + 330, y0 + 520
    draw.rectangle((qx - 10, qy - 10, qx + 190, qy + 190), fill="white")
    for i in range(16):
        for j in range(16):
            if i == 0 or j == 15 or rnd.random() < 0.45:
                draw.rectangle((qx + j * 11, qy + i * 11, qx + j * 11 + 10, qy + i * 11 + 10), fill="black")
    if code:
        draw.text((x0 + 40, y0 + 690), code, fill="white", font=small)
    buffer = BytesIO()
    img.save(buffer, "JPEG", quality=82)
    return buffer.getvalue()


def seed_bot_database(rnd: random.Random) -> list[int]:
    from dashboard.decision_rules import automatic_photo_decision

    import bot
    import manage_users

    bot.init_db()
    for telegram_id, name, city, hospitals in MEDICS:
        manage_users.main(["add", str(telegram_id), name, city, ", ".join(hospitals)])

    fonts = load_fonts()
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    photo_ids = []
    for day in range(6):
        for medic_id, _name, city, hospitals in MEDICS:
            for _ in range(rnd.randint(1, 2)):
                status = rnd.choice(STATUSES)
                prefix = "01" if city == "Ташкент" else "02"
                qr = None if rnd.random() < 0.15 else f"{prefix}-{rnd.randint(1, 9000):010d}"
                image = package_image(len(photo_ids) + 1, qr, fonts)
                code_status = bot.qr_code_status(qr, city)
                sent_at = now - timedelta(days=day, hours=rnd.randint(1, 9))
                with bot.connect() as con:
                    cursor = con.execute(
                        """INSERT INTO photos (sender_telegram_id, medic_telegram_id, medic_code,
                           doctor_name, image_digest, qr_data, maternity_hospital, review_status,
                           review_reason, code_status, telegram_file_id, image, sent_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (medic_id, medic_id, str(medic_id), rnd.choice(DOCTORS),
                         hashlib.sha256(image).hexdigest(), qr, rnd.choice(hospitals), status,
                         "демо-данные", code_status, "demo", image, sent_at.isoformat()),
                    )
                    checks = automatic_photo_decision(status, code_status)
                    con.execute(
                        """INSERT INTO photo_checks (photo_id, product_status, material_status,
                           location_status, duplicate_status, editing_status, assignment_status,
                           decision, decision_reason) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (cursor.lastrowid, checks["product_status"], checks["material_status"],
                         checks["location_status"], checks["duplicate_status"], checks["editing_status"],
                         checks["assignment_status"], checks["decision"], checks["decision_reason"]),
                    )
                    photo_ids.append(cursor.lastrowid)

    names = {telegram_id: name for telegram_id, name, _city, _hospitals in MEDICS}
    with bot.connect() as con:
        for index, (telegram_id, direction, text) in enumerate(CHATS):
            con.execute(
                """INSERT INTO chat_messages (telegram_id, direction, message_type, text, sender_name,
                   telegram_message_id, created_at) VALUES (?,?,?,?,?,?,?)""",
                (telegram_id, direction, "text", text,
                 names[telegram_id] if direction == "incoming" else "Оператор", index + 1,
                 (now - timedelta(hours=10 - index)).isoformat()),
            )
    link_demo_duplicates(bot, photo_ids)
    return photo_ids


def link_demo_duplicates(bot, photo_ids):
    """Несколько связанных фото, чтобы в карточке было что показать в «Похожих»."""
    if len(photo_ids) < 6:
        return
    first, second, third, fourth = photo_ids[0], photo_ids[3], photo_ids[5], photo_ids[1]
    with bot.connect() as con:
        # Та же роженица и дата на двух разных снимках.
        for photo_id in (first, second):
            con.execute(
                "UPDATE photos SET mother_name = ?, photo_date = ? WHERE id = ?",
                ("Абдуллаева Сабина", "2026-11-20", photo_id),
            )
        # Тот же код на упаковке у двух фото разных дней.
        code = con.execute("SELECT qr_data FROM photos WHERE id = ?", (third,)).fetchone()[0]
        if code:
            con.execute("UPDATE photos SET qr_data = ? WHERE id = ?", (code, fourth))
        # Повторно отправленный тот же файл.
        source = con.execute(
            "SELECT sender_telegram_id, medic_telegram_id, medic_code, doctor_name, image_digest, qr_data, "
            "maternity_hospital, image, sent_at FROM photos WHERE id = ?", (first,),
        ).fetchone()
        cursor = con.execute(
            """INSERT INTO photos (sender_telegram_id, medic_telegram_id, medic_code, doctor_name,
                   image_digest, qr_data, maternity_hospital, review_status, review_reason, code_status,
                   telegram_file_id, image, sent_at, original_photo_id)
               VALUES (?,?,?,?,?,?,?,'Возможная копия','Повторно отправлен тот же файл',?, 'demo', ?, ?, ?)""",
            (source[0], source[1], source[2], source[3], source[4], source[5], source[6],
             bot.qr_code_status(source[5], "Ташкент"), source[7], source[8], first),
        )
        photo_ids.append(cursor.lastrowid)


class SiteSnapshot:
    """Снимает страницы сайта и переписывает в них ссылки под статичный хостинг."""

    def __init__(self, client, photo_ids: list[int]):
        self.client = client
        self.pages = {
            "/": "index.html",
            "/reports/": "reports.html",
            "/management/": "management.html",
            "/chats/": "chats.html",
            "/users/": "users.html",
            "/journal/": "journal.html",
        }
        self.filters = {value: f"index-{value}.html" for value in ("review", "accepted", "rejected")}
        self.paged = {}
        for number in range(2, 50):
            response = client.get(f"/?page={number}")
            if response.status_code != 200 or f"?page={number - 1}" not in response.content.decode():
                break
            self.paged[str(number)] = f"index-{number}.html"
        # У каждого диалога переписки своя страница.
        self.chats = {}
        for telegram_id in sorted(set(re.findall(r'\?telegram_id=(-?\d+)', client.get("/chats/").content.decode()))):
            self.chats[telegram_id] = f"chats-{telegram_id}.html"
        for photo_id in photo_ids:
            self.pages[f"/photo/{photo_id}/view/"] = f"photo/{photo_id}.html"
            # Сравнения с похожими фото, найденными в карточке.
            detail = client.get(f"/photo/{photo_id}/view/").content.decode()
            for left, right in re.findall(r"/photo/(\d+)/compare/(\d+)/", detail):
                self.pages[f"/photo/{left}/compare/{right}/"] = f"photo/compare-{left}-{right}.html"
            # Страница «оригинала» есть только у фото, помеченных как копия.
            if client.get(f"/photo/{photo_id}/original/").status_code == 200:
                self.pages[f"/photo/{photo_id}/original/"] = f"photo/{photo_id}-original.html"
        self.files = {
            "/photos/export.xlsx": "reports/photos.xlsx",
            "/reports/full-last-10-days.xlsx": "reports/full-last-10-days.xlsx",
            "/reports/rejected-photos.xlsx": "reports/rejected-photos.xlsx",
        }
        for photo_id in photo_ids:
            self.files[f"/photo/{photo_id}/"] = f"img/{photo_id}.jpg"

    def fetch(self, url: str):
        response = self.client.get(url)
        if response.status_code != 200:
            raise SystemExit(f"{url}: HTTP {response.status_code}")
        return response

    @staticmethod
    def relative(target: str, page: str) -> str:
        return "../" * page.count("/") + target

    def static_target(self, url: str, source: str = "/") -> str:
        url, hash_mark, fragment = url.partition("#")
        target = self._static_target(url, source)
        return f"{target}#{fragment}" if hash_mark and target != "#" else target

    def _static_target(self, url: str, source: str) -> str:
        path, _, query = url.partition("?")
        if path == "":
            path = source
        query = query.replace("&amp;", "&")
        if path == "/chats/":
            chat_match = re.match(r"telegram_id=(-?\d+)", query)
            if chat_match and chat_match.group(1) in self.chats:
                return self.chats[chat_match.group(1)]
            return "chats.html"
        if path == "/":
            page_match = re.fullmatch(r"page=(\d+)", query)
            if page_match:
                return self.paged.get(page_match.group(1), "index.html")
            decision_match = re.fullmatch(r"decision=(\w+)", query)
            if decision_match and decision_match.group(1) in self.filters:
                return self.filters[decision_match.group(1)]
            return "index.html"
        if path.startswith("/static/"):
            # Метка версии (?v=…) нужна, чтобы браузер не показывал старые стили.
            return path[1:] + (f"?{query}" if query else "")
        if path in self.files:
            return self.files[path]
        if path in self.pages:
            return self.pages[path]
        if path == "/logout/":
            return "#"
        # Остальные адреса работают только на сервере: ведём на главную.
        return "index.html"

    def rewrite(self, html: str, page: str, source: str = "/") -> str:
        def replace(match):
            target = self.static_target(match.group(2), source)
            if target != "#":
                target = self.relative(target, page)
            return f'{match.group(1)}="{target}"'

        html = re.sub(r'(href|src|action|formaction|data-[a-z-]+)="([/?][^"]*)"', replace, html)
        html = html.replace("</body>", f"{DEMO_SCRIPT}</body>")
        return re.sub(r'<input type="hidden" name="csrfmiddlewaretoken"[^>]*>', "", html)

    def save(self, out: Path) -> None:
        html_pages = dict(self.pages)
        html_pages.update({f"/?decision={value}": page for value, page in self.filters.items()})
        html_pages.update({f"/?page={number}": page for number, page in self.paged.items()})
        html_pages.update({f"/chats/?telegram_id={key}": page for key, page in self.chats.items()})
        for url, page in html_pages.items():
            target = out / page
            target.parent.mkdir(parents=True, exist_ok=True)
            source = url.partition("?")[0]
            target.write_text(self.rewrite(self.fetch(url).content.decode(), page, source), encoding="utf-8")
        for url, name in self.files.items():
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            response = self.fetch(url)
            target.write_bytes(
                b"".join(response.streaming_content) if response.streaming else response.content
            )
        shutil.copytree(PROJECT_DIR / "django_portal/dashboard/static", out / "static")
        (out / ".nojekyll").write_text("")
        print(f"Готово: {len(html_pages)} страниц, {len(self.files)} файлов → {out}")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "_site").resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="nuppi-demo-") as work_dir:
        configure_environment(Path(work_dir))
        rnd = random.Random(7)
        photo_ids = seed_bot_database(rnd)

        import django

        django.setup()
        from dashboard.models import StaffProfile
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.test import Client

        call_command("migrate", verbosity=0)
        users = get_user_model().objects
        admin = users.create_superuser("demo", password=secrets.token_urlsafe(16))
        # Несколько выдуманных пользователей с разными ролями — для страниц
        # «Пользователи и роли» и «Журнал действий».
        team = {}
        for username, full_name, role, regions in DEMO_TEAM:
            user = users.create_user(username, password=secrets.token_urlsafe(16))
            StaffProfile.objects.create(user=user, full_name=full_name, role=role, regions=regions)
            team[username] = user
        StaffProfile.objects.create(user=admin, full_name="Демо Владелец", role="owner")

        def client_for(user):
            client = Client()
            client.force_login(user)
            return client

        reviewers = [client_for(team["operator"]), client_for(team["supervisor"]), client_for(admin)]
        # Решения ставим через настоящую страницу, чтобы сошлись счётчики, отчёты и журнал.
        for index, photo_id in enumerate(photo_ids[: len(photo_ids) * 2 // 3]):
            reviewer = reviewers[index % len(reviewers)]
            if rnd.random() < 0.8:
                reviewer.post(f"/photo/{photo_id}/decision/", {"decision": "accepted"})
            else:
                reviewer.post(
                    f"/photo/{photo_id}/decision/",
                    {"decision": "rejected", "reason": "Не видно код на упаковке"},
                )
        client = client_for(admin)
        client.get("/reports/full-last-10-days.xlsx")
        SiteSnapshot(client, photo_ids).save(out)


if __name__ == "__main__":
    main()
