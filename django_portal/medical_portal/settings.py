import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "change-this-secret-key-before-public-deployment")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
configured_hosts = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]
render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if render_hostname:
    configured_hosts.append(render_hostname)
# Локальные адреса сохраняются всегда; адреса в сети задаются в DJANGO_ALLOWED_HOSTS.
ALLOWED_HOSTS = list(dict.fromkeys([*configured_hosts, "127.0.0.1", "localhost"]))

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "dashboard",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "django.middleware.gzip.GZipMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware", "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware", "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "medical_portal.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "medical_portal.wsgi.application"
portal_db_path = Path(os.getenv("DJANGO_DB_PATH", BASE_DIR / "portal.sqlite3"))
portal_db_path.parent.mkdir(parents=True, exist_ok=True)
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": portal_db_path}}
AUTH_PASSWORD_VALIDATORS = []
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
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"

# Путь к базе бота. Укажите абсолютный путь в .env сайта при необходимости.
BOT_DB_PATH = Path(os.getenv("BOT_DB_PATH", BASE_DIR / "medical_bot.db"))

# Превью можно восстановить из оригиналов, поэтому на Render они не должны
# занимать ограниченный постоянный диск рядом с основной SQLite-базой.
PHOTO_THUMBNAIL_CACHE_DIR = Path(
    os.getenv(
        "PHOTO_THUMBNAIL_CACHE_DIR",
        str(Path(tempfile.gettempdir()) / "nuppi-photo-thumbnails"),
    )
)

configured_csrf_origins = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if render_hostname:
    configured_csrf_origins.append(f"https://{render_hostname}")
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(configured_csrf_origins))
CSRF_FAILURE_VIEW = "dashboard.views.csrf_failure"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# Управление данными бота доступно только этому логину сайта. Укажите его в .env.
# Telegram ID используется ботом отдельно и не является логином сайта.
CREATOR_DJANGO_USERNAME = os.getenv("CREATOR_DJANGO_USERNAME", "").strip()
CREATOR_TELEGRAM_ID = os.getenv("CREATOR_TELEGRAM_ID", "").strip()
