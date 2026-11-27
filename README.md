# NuppiTgBot

Система фотоотчётов для медпредставителей: **Telegram-бот** принимает фотографии упаковок
из роддомов и автоматически их проверяет, а **сайт-панель** (Django) показывает их оператору,
который принимает или отклоняет каждое фото и выгружает отчёты в Excel.

```
Медик ──► Telegram-бот (bot.py) ──► medical_bot.db (SQLite) ◄── Сайт-панель (django_portal)
              │                                                     │
              ├─ QR/Data Matrix (zxing-cpp, OpenCV)                 ├─ фото, фильтры, решения
              ├─ Claude: проверка упаковки и кода                   ├─ сотрудники и роддомы
              └─ Sightengine: признаки ИИ-генерации                 └─ отчёты Excel
```

Автоматика только подсвечивает признаки (копия, посторонний код, возможный монтаж).
Итог «Принято» или «Отклонено» ставит человек на сайте; отчёты и оплата считаются только
по принятым фото.

## Быстрый старт (локально)

Нужен Python 3.12 (подойдёт 3.10+).

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # впишите TELEGRAM_BOT_TOKEN и CREATOR_TELEGRAM_ID
```

**Бот:**

```bash
python bot.py
```

Отправьте боту `/my_id`, впишите число в `CREATOR_TELEGRAM_ID` и добавьте себя сотрудником —
на сайте или из терминала:

```bash
python manage_users.py add ВАШ_ID "Фамилия Имя" Ташкент "Роддом №1"
```

**Сайт** (в отдельном терминале):

```bash
cd django_portal
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Откройте http://127.0.0.1:8000. Бот и сайт читают один и тот же `.env` и одну базу
`medical_bot.db` в корне проекта — дополнительно настраивать пути не нужно.

## Настройки

Все параметры с пояснениями — в [.env.example](.env.example). Главные:

| Переменная | Зачем |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather (обязательно) |
| `CREATOR_TELEGRAM_ID` | ваш Telegram ID: постоянный доступ к боту |
| `ANTHROPIC_API_KEY` | проверка упаковки и QR через Claude (без ключа фото уходят «Не проверено») |
| `SIGHTENGINE_API_USER` / `_SECRET` | оценка вероятности ИИ-генерации (необязательно) |
| `BOT_WORK_START_HOUR` / `_END_HOUR` | часы приёма сообщений по Ташкенту (0 и 24 — круглосуточно) |
| `PHOTO_PAYMENT_RATE_SUM` | оплата за одно принятое фото в отчётах |
| `CREATOR_DJANGO_USERNAME` | логин создателя сайта; если пусто — создатели все суперпользователи |
| `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_HTTPS` | режим и адреса сайта |

Секретный ключ Django задавать не обязательно: сайт создаст постоянный ключ рядом со своей
базой (`.django_secret_key`, в git не попадает).

## Роли

- **Создатель сайта** — управляет сотрудниками, роддомами, заданиями и входами операторов.
- **Оператор сайта** — смотрит фото, ставит решения, правит дату/ФИО/QR, выгружает Excel.
- **Медик (бот)** — отправляет фото: `/send_photo` → роддом → ФИО врача → фотографии → `/done`.
  Видит свои отчёты и комментарии оператора.
- **Оператор (бот)** — отчёт за 10 дней и подсчёт фото по врачам из Excel.

## Структура

```
bot.py                     Telegram-бот: диалоги, проверки фото, отчёты, фоновые задачи
manage_users.py            сотрудники бота из терминала (пока нет сайта)
cloud_service.py           запуск сайта и бота в одном сервисе Render
reference_packaging/       эталонные упаковки для сравнения через Claude
django_portal/
  medical_portal/          настройки Django
  dashboard/
    db.py                  подключение к базе бота, мягкие миграции старых схем
    qr_codes.py            справочник региональных кодов NN-NNNNNNNNNN
    decision_rules.py      правила статусов (общие для бота и сайта)
    photos.py catalog.py   карточки фото, справочники городов/роддомов/врачей
    reports.py             данные и Excel сводных отчётов
    views/                 страницы по разделам: photos, staff, reports, chats
    tests/                 test_site.py, test_bot.py, test_visit_system.py
```

## Тесты

```bash
cd django_portal
PYTHONPATH=.. python manage.py test dashboard
```

## Развёртывание на Render

Один **Web Service** (диск Render подключается только к одному сервису):

- Build Command: `pip install -r requirements.txt`
- Start Command: `python cloud_service.py`
- Persistent Disk: `/var/data`
- Переменные: `TELEGRAM_BOT_TOKEN`, `CREATOR_TELEGRAM_ID`, `BOT_DB_PATH=/var/data/medical_bot.db`,
  `DJANGO_DB_PATH=/var/data/portal.sqlite3`, `EXCEL_REPORTS_DIR=/var/data/excel_files`,
  для первого входа — `DJANGO_SUPERUSER_USERNAME` и `DJANGO_SUPERUSER_PASSWORD`.

`cloud_service.py` запускает Gunicorn с сайтом и бота; если бот падает, перезапускается только он.
Домен Render, HTTPS и защищённые cookie настраиваются автоматически.

Для 512 МБ памяти: `BACKGROUND_PHOTO_WORKERS=1`, `AI_DUPLICATE_BATCH_SIZE=3`.

## Что не попадает в git

`.env`, базы `*.db` / `*.sqlite3`, ключ `.django_secret_key`, фотографии, логи и Excel-отчёты
(см. [.gitignore](.gitignore)).
