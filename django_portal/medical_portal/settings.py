"""Настройки сайта-панели.

Все значения берутся из переменных окружения. Сайт читает тот же файл .env, что и
Telegram-бот (в корне проекта), и при наличии — django_portal/.env, значения из
которого имеют приоритет. Относительные пути считаются от корня проекта, поэтому
по умолчанию сайт открывает ту же базу medical_bot.db, что и бот.
"""

import os
import secrets
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent  # django_portal/
PROJECT_DIR = BASE_DIR.parent  # корень проекта рядом с bot.py

# load_dotenv не перезаписывает уже заданные переменные: сначала грузим более
# конкретный файл сайта, затем общий файл проекта.
load_dotenv(BASE_DIR / ".env")
load_dotenv(PROJECT_DIR / ".env")


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def env_path(name, default):
    """Путь из окружения; относительный путь считается от корня проекта."""
    path = Path(os.getenv(name) or default).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def load_secret_key(storage_dir):
    """DJANGO_SECRET_KEY из окружения или постоянный ключ, созданный один раз.

    Ключ хранится рядом с базой сайта (на Render — на постоянном диске), поэтому
    входы пользователей не сбрасываются при перезапуске, а в репозиторий ничего
    секретного не попадает.
    """
    configured = os.getenv("DJANGO_SECRET_KEY", "").strip()
    if configured:
        return configured
    key_path = storage_dir / ".django_secret_key"
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    key = secrets.token_urlsafe(50)
    storage_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(key)
    return key


# --- Базы данных и файлы -------------------------------------------------------

# База Telegram-бота: фотографии, сотрудники, переписка. Сайт читает и правит её.
BOT_DB_PATH = env_path("BOT_DB_PATH", "medical_bot.db")
# Собственная база сайта: только логины и сессии.
PORTAL_DB_PATH = env_path("DJANGO_DB_PATH", "django_portal/portal.sqlite3")
PORTAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": PORTAL_DB_PATH}}

# Превью восстанавливаются из оригиналов, поэтому лежат во временной папке, а не
# на ограниченном постоянном диске рядом с основной базой.
PHOTO_THUMBNAIL_CACHE_DIR = env_path(
    "PHOTO_THUMBNAIL_CACHE_DIR", Path(tempfile.gettempdir()) / "nuppi-photo-thumbnails",
)

# --- Безопасность --------------------------------------------------------------

SECRET_KEY = load_secret_key(PORTAL_DB_PATH.parent)
DEBUG = env_flag("DJANGO_DEBUG", default=False)

render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
ALLOWED_HOSTS = list(dict.fromkeys([
    *env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost"),
    *([render_hostname] if render_hostname else []),
    "127.0.0.1",
    "localhost",
]))
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([
    *env_list("DJANGO_CSRF_TRUSTED_ORIGINS"),
    *([f"https://{render_hostname}"] if render_hostname else []),
]))
CSRF_FAILURE_VIEW = "dashboard.views.csrf_failure"

# Защищённые cookie включаются, когда сайт открыт по HTTPS (на Render — всегда).
# Локально по http://127.0.0.1 они выключены, иначе вход на сайт не сработает.
HTTPS_ENABLED = env_flag("DJANGO_HTTPS", default=bool(render_hostname or os.getenv("RENDER")))
if HTTPS_ENABLED:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14  # две недели

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Управлять сотрудниками может только этот логин сайта. Если он не задан,
# создателями считаются суперпользователи (python manage.py createsuperuser).
CREATOR_DJANGO_USERNAME = os.getenv("CREATOR_DJANGO_USERNAME", "").strip()
CREATOR_TELEGRAM_ID = os.getenv("CREATOR_TELEGRAM_ID", "").strip()

# --- Django --------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "medical_portal.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "dashboard.context_processors.navigation",
    ]},
}]
WSGI_APPLICATION = "medical_portal.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
# Статика отдаётся и без collectstatic — удобно для локального запуска с DEBUG=0.
WHITENOISE_USE_FINDERS = True

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.getenv("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {"django.request": {"level": "WARNING"}},
}
