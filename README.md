# NuppiTgBot

Telegram-бот и сайт Django для приёма и проверки фотографий упаковок, поиска повторов и формирования отчётов.

## Локальный запуск

1. Создайте виртуальное окружение Python.
2. Установите зависимости: `pip install -r requirements.txt`.
3. Скопируйте `.env.example` в `.env` и заполните ключи доступа.
4. Запустите бота: `python bot.py`.
5. Запустите сайт из папки `django_portal`: `python manage.py runserver 0.0.0.0:8000`.

Файл `.env`, базы данных, фотографии, логи и Excel-отчёты намеренно не сохраняются в Git.

## Render Background Worker

- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Root Directory: оставьте пустым
- обязательная переменная: `TELEGRAM_BOT_TOKEN`
- для проверок ИИ добавьте `ANTHROPIC_API_KEY` и при необходимости данные Sightengine;
  дешёвый предварительный просмотр QR выполняет `CLAUDE_QR_SCREENING_MODEL`, а Sonnet
  подключается только для видимого сложного кода
- бот принимает сообщения ежедневно с 07:00 до 19:00 по времени Ташкента;
  часы можно переопределить переменными `BOT_WORK_START_HOUR` и `BOT_WORK_END_HOUR`
- для тарифа Render с 512 МБ памяти используйте `BACKGROUND_PHOTO_WORKERS=1` и
  `AI_DUPLICATE_BATCH_SIZE=3`; фотографии сначала сохраняются в базе, затем уменьшаются до безопасного
  рабочего размера и проверяются по очереди

Чтобы база и отчёты не исчезали после перезапуска Render, подключите Persistent Disk с Mount Path
`/var/data` и задайте:

```text
BOT_DB_PATH=/var/data/medical_bot.db
EXCEL_REPORTS_DIR=/var/data/excel_files
```

## Render Web Service: бот и сайт с автоматической синхронизацией

Render-диск доступен только одному сервису. Поэтому для сайта и бота с общей SQLite-базой используйте
один платный **Web Service**, а не отдельные Web Service и Background Worker.

- Build Command: `pip install -r requirements.txt`
- Start Command: `python cloud_service.py`
- Persistent Disk: `/var/data`
- `BOT_DB_PATH=/var/data/medical_bot.db`
- `DJANGO_DB_PATH=/var/data/portal.sqlite3`
- `EXCEL_REPORTS_DIR=/var/data/excel_files`
- `DJANGO_DEBUG=0`
- `DJANGO_ALLOWED_HOSTS` можно не дополнять: домен Render добавляется автоматически

`cloud_service.py` запускает один Gunicorn-процесс сайта и Telegram-бота. Оба процесса читают один
`medical_bot.db`, поэтому принятая ботом фотография появляется на сайте при следующем обновлении страницы.
При первом запуске новую учётную запись создателя можно создать переменными
`DJANGO_SUPERUSER_USERNAME` и `DJANGO_SUPERUSER_PASSWORD`; существующая учётная запись не перезаписывается.

Во время переноса со старого Worker сначала задайте новому Web Service `DISABLE_TELEGRAM_BOT=1`.
После копирования базы и остановки старого Worker смените значение на `0`: это не даст двум процессам
одновременно получать обновления Telegram с одним токеном.
