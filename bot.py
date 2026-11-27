# Аннотации вида ``str | None`` вычисляются лениво, поэтому бот запускается и на Python 3.9.
from __future__ import annotations

import logging
import os
import sqlite3
import base64
import json
import re
import zipfile
import asyncio
import hashlib
import gc
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from io import BytesIO
from itertools import chain
from pathlib import Path

from dotenv import load_dotenv
import imagehash
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import requests
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import BadRequest, Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    ExtBot,
    MessageHandler,
    filters,
)

from django_portal.dashboard.decision_rules import (
    DECISION_LABELS,
    DECISION_REVIEW,
    automatic_photo_decision,
    legacy_automatic_photo_decision,
)

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    import zxingcpp
except ImportError:
    zxingcpp = None

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def configured_path(variable_name: str, default_name: str) -> Path:
    path = Path(os.getenv(variable_name, default_name)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


DB_PATH = configured_path("BOT_DB_PATH", "medical_bot.db")
EXCEL_REPORTS_DIR = configured_path("EXCEL_REPORTS_DIR", "excel_files")
CHAT_LOG_RETRY_ATTEMPTS = max(1, int(os.getenv("CHAT_LOG_RETRY_ATTEMPTS", "4")))
CHAT_LOG_PENDING_LIMIT = max(100, int(os.getenv("CHAT_LOG_PENDING_LIMIT", "5000")))
PHOTO_SAVE_RETRY_ATTEMPTS = max(1, int(os.getenv("PHOTO_SAVE_RETRY_ATTEMPTS", "3")))
_pending_chat_messages = deque(maxlen=CHAT_LOG_PENDING_LIMIT)
_pending_chat_messages_lock = threading.Lock()
REFERENCE_PACKAGING_FILES = (
    BASE_DIR / "reference_packaging" / "nuppi_gold_1_300.png",
    BASE_DIR / "reference_packaging" / "nuppi_f_1_300.png",
)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AI_MAX_SAVINGS_MODE = os.getenv("AI_MAX_SAVINGS_MODE", "1") == "1"
CLAUDE_VISION_MODEL = (
    os.getenv("CLAUDE_VISION_MODEL", "claude-sonnet-4-6").strip()
    or "claude-sonnet-4-6"
)
CLAUDE_METADATA_MODEL = (
    os.getenv("CLAUDE_METADATA_MODEL", "claude-haiku-4-5-20251001").strip()
    or "claude-haiku-4-5-20251001"
)
CLAUDE_DUPLICATE_MODEL = (
    os.getenv("CLAUDE_DUPLICATE_MODEL", "claude-sonnet-4-6").strip()
    or "claude-sonnet-4-6"
)
CLAUDE_QR_SCREENING_MODEL = (
    os.getenv("CLAUDE_QR_SCREENING_MODEL", CLAUDE_METADATA_MODEL).strip()
    or CLAUDE_METADATA_MODEL
)
CLAUDE_MAX_OUTPUT_TOKENS = max(
    128, int(os.getenv("CLAUDE_MAX_OUTPUT_TOKENS", "350")),
)
CLAUDE_API_URL = os.getenv(
    "CLAUDE_API_URL", "https://api.anthropic.com/v1/messages",
).strip()
CLAUDE_API_VERSION = os.getenv("CLAUDE_API_VERSION", "2023-06-01").strip()
CLAUDE_PROMPT_CACHE_ENABLED = (
    os.getenv("CLAUDE_PROMPT_CACHE_ENABLED", "1") == "1"
)
CLAUDE_PROMPT_CACHE_TTL = os.getenv("CLAUDE_PROMPT_CACHE_TTL", "1h").strip()
if CLAUDE_PROMPT_CACHE_TTL not in {"5m", "1h"}:
    CLAUDE_PROMPT_CACHE_TTL = "1h"
TASHKENT_TIMEZONE = timezone(timedelta(hours=5))
BOT_WORK_START_HOUR = int(os.getenv("BOT_WORK_START_HOUR", "7"))
BOT_WORK_END_HOUR = int(os.getenv("BOT_WORK_END_HOUR", "19"))
AI_DUPLICATE_BATCH_SIZE = max(
    1,
    int(os.getenv("AI_DUPLICATE_BATCH_SIZE", os.getenv("AI_DUPLICATE_CANDIDATE_LIMIT", "3"))),
)
AI_PHASH_MAX_DISTANCE = max(0, int(os.getenv("AI_PHASH_MAX_DISTANCE", "14")))
CROP_HASH_HAMMING_CUTOFF = max(0, int(os.getenv("CROP_HASH_HAMMING_CUTOFF", "8")))
VISUAL_HASH_MAX_DISTANCE = max(8, int(os.getenv("VISUAL_HASH_MAX_DISTANCE", "18")))
VISUAL_EDGE_HASH_MAX_DISTANCE = max(
    8, int(os.getenv("VISUAL_EDGE_HASH_MAX_DISTANCE", "18")),
)
VISUAL_DHASH_MAX_DISTANCE = max(
    8, int(os.getenv("VISUAL_DHASH_MAX_DISTANCE", "16")),
)
AI_FALLBACK_CANDIDATE_LIMIT = max(0, int(os.getenv("AI_FALLBACK_CANDIDATE_LIMIT", "3")))
AI_DUPLICATE_MAX_CANDIDATES = max(1, int(os.getenv("AI_DUPLICATE_MAX_CANDIDATES", "3")))
AI_METADATA_CANDIDATE_LIMIT = max(1, int(os.getenv("AI_METADATA_CANDIDATE_LIMIT", "3")))
DUPLICATE_LOOKBACK_PHOTOS = max(
    1, int(os.getenv("DUPLICATE_LOOKBACK_PHOTOS", "1000")),
)
AI_DUPLICATE_MIN_CONFIDENCE = min(
    1.0, max(0.5, float(os.getenv("AI_DUPLICATE_MIN_CONFIDENCE", "0.92")))
)
if AI_MAX_SAVINGS_MODE:
    AI_PHASH_MAX_DISTANCE = min(AI_PHASH_MAX_DISTANCE, 14)
    CROP_HASH_HAMMING_CUTOFF = min(CROP_HASH_HAMMING_CUTOFF, 8)
    VISUAL_HASH_MAX_DISTANCE = min(VISUAL_HASH_MAX_DISTANCE, 18)
    VISUAL_EDGE_HASH_MAX_DISTANCE = min(VISUAL_EDGE_HASH_MAX_DISTANCE, 18)
    VISUAL_DHASH_MAX_DISTANCE = min(VISUAL_DHASH_MAX_DISTANCE, 16)
    # Always give the vision model a bounded fallback set. Previously a large
    # change of background/angle could make every local hash miss, so Claude was
    # never asked to compare that photo at all. Three candidates still fit in a
    # single paid batch.
    AI_FALLBACK_CANDIDATE_LIMIT = min(
        max(AI_FALLBACK_CANDIDATE_LIMIT, 3), 3,
    )
    # One three-image batch is still inexpensive, while limiting the comparison
    # to two candidates caused the real original to be skipped when many boxes
    # shared the same printed design.
    AI_DUPLICATE_MAX_CANDIDATES = min(AI_DUPLICATE_MAX_CANDIDATES, 3)
    AI_METADATA_CANDIDATE_LIMIT = min(AI_METADATA_CANDIDATE_LIMIT, 3)
# Perceptual hashes and a vision model may consider two standard NUPPI boxes
# almost identical even though they are different physical packages.  They are
# useful for a human-facing similarity search, but must never create a duplicate
# link automatically.  Duplicate decisions are intentionally limited below to
# an identical SHA-256 digest or an exactly equal decoded QR/Data Matrix value.
AI_BROAD_DUPLICATE_CHECKS_ENABLED = False
SIGHTENGINE_API_USER = os.getenv("SIGHTENGINE_API_USER")
SIGHTENGINE_API_SECRET = os.getenv("SIGHTENGINE_API_SECRET")
SIGHTENGINE_ENABLED = os.getenv("SIGHTENGINE_ENABLED", "1") == "1"
SIGHTENGINE_AI_THRESHOLD = float(os.getenv("SIGHTENGINE_AI_THRESHOLD", "0.75"))
BACKGROUND_PHOTO_WORKERS = max(1, int(os.getenv("BACKGROUND_PHOTO_WORKERS", "1")))
MAX_METADATA_VERIFICATION = (
    not AI_MAX_SAVINGS_MODE and os.getenv("MAX_METADATA_VERIFICATION", "0") == "1"
)
AI_ADAPTIVE_METADATA_RETRY = os.getenv("AI_ADAPTIVE_METADATA_RETRY", "1") == "1"
AI_METADATA_CONFIDENCE_THRESHOLD = min(
    0.95, max(0.5, float(os.getenv("AI_METADATA_CONFIDENCE_THRESHOLD", "0.78")))
)
PACKAGE_DATE_MIN_YEAR = int(os.getenv("PACKAGE_DATE_MIN_YEAR", "2020"))
PACKAGE_DATE_MAX_YEAR = int(os.getenv("PACKAGE_DATE_MAX_YEAR", "2035"))
IMAGE_PROCESSING_MAX_DIMENSION = max(640, int(os.getenv("IMAGE_PROCESSING_MAX_DIMENSION", "1280")))
if AI_MAX_SAVINGS_MODE:
    IMAGE_PROCESSING_MAX_DIMENSION = min(IMAGE_PROCESSING_MAX_DIMENSION, 1280)
AI_READING_MAX_DIMENSION = max(1280, int(os.getenv("AI_READING_MAX_DIMENSION", "1920")))
QR_SCAN_MAX_DIMENSION = max(1600, int(os.getenv("QR_SCAN_MAX_DIMENSION", "4096")))
QR_SCAN_TARGET_DIMENSION = max(1600, int(os.getenv("QR_SCAN_TARGET_DIMENSION", "2400")))
QR_SCAN_VERSION = 8
QR_DEDICATED_RECOVERY_ENABLED = os.getenv("QR_DEDICATED_RECOVERY_ENABLED", "1") == "1"
QR_STRICT_RECOVERY_FOR_ALL_MISSING = os.getenv("QR_STRICT_RECOVERY_FOR_ALL_MISSING", "1") == "1"
QR_RECOVERY_MIN_CONFIDENCE = min(
    0.99, max(0.5, float(os.getenv("QR_RECOVERY_MIN_CONFIDENCE", "0.82")))
)
QR_BACKFILL_ENABLED = os.getenv("QR_BACKFILL_ENABLED", "0") == "1"
QR_BACKFILL_BATCH_SIZE = min(2, max(1, int(os.getenv("QR_BACKFILL_BATCH_SIZE", "2"))))
# One resumable QR-only pass over every historical photo that still lacks a
# complete regional number.  It is deliberately much lighter than rerunning
# the complete photo pipeline and yields between rows so the shared Render
# web service remains responsive.
FULL_QR_RECHECK_ENABLED = os.getenv("FULL_QR_RECHECK_ENABLED", "0") == "1"
FULL_QR_RECHECK_VERSION = max(
    2, int(os.getenv("FULL_QR_RECHECK_VERSION", "2")),
)
FULL_QR_RECHECK_BATCH_SIZE = min(
    3, max(1, int(os.getenv("FULL_QR_RECHECK_BATCH_SIZE", "1"))),
)
FULL_QR_RECHECK_DELAY_SECONDS = max(
    2.0, float(os.getenv("FULL_QR_RECHECK_DELAY_SECONDS", "8")),
)
FULL_QR_RECHECK_MAX_FAILURES = max(
    1, int(os.getenv("FULL_QR_RECHECK_MAX_FAILURES", "3")),
)
# One-time, resumable audit requested for photos sent on 27-28 September 2026.
# Versioning in the database prevents another paid check after a restart/deploy.
TARGETED_NO_QR_RECHECK_ENABLED = os.getenv("TARGETED_NO_QR_RECHECK_ENABLED", "0") == "1"
TARGETED_NO_QR_RECHECK_DATE_FROM = os.getenv(
    "TARGETED_NO_QR_RECHECK_DATE_FROM", "2026-09-27",
).strip()
TARGETED_NO_QR_RECHECK_DATE_TO = os.getenv(
    "TARGETED_NO_QR_RECHECK_DATE_TO", "2026-09-28",
).strip()
TARGETED_NO_QR_RECHECK_VERSION = max(
    1, int(os.getenv("TARGETED_NO_QR_RECHECK_VERSION", "1")),
)
TARGETED_NO_QR_RECHECK_DELAY_SECONDS = max(
    1.0, float(os.getenv("TARGETED_NO_QR_RECHECK_DELAY_SECONDS", "3")),
)
TARGETED_NO_QR_DUPLICATE_FALLBACK = min(
    3, max(1, int(os.getenv("TARGETED_NO_QR_DUPLICATE_FALLBACK", "2"))),
)
DUPLICATE_VISUAL_BACKFILL_ENABLED = (
    os.getenv("DUPLICATE_VISUAL_BACKFILL_ENABLED", "0") == "1"
)
DUPLICATE_VISUAL_BACKFILL_DELAY_SECONDS = max(
    5.0, float(os.getenv("DUPLICATE_VISUAL_BACKFILL_DELAY_SECONDS", "15")),
)

# One-time, resumable duplicate audit for every photo assigned to Urgench.
# The pass only compares locally shortlisted candidates and sleeps between rows,
# so it does not repeat the expensive full QR/product pipeline or monopolise the
# shared Render service. Increasing the version intentionally schedules a fresh
# audit after a future duplicate-detector upgrade.
# The historical audit used perceptual similarity and therefore could produce
# false positives.  The strict migration in ``init_db`` now reconciles every
# stored photo using deterministic exact-match rules, so the visual audit must
# stay disabled even if an old Render environment variable is still present.
URGENCH_DUPLICATE_AUDIT_ENABLED = False
URGENCH_DUPLICATE_AUDIT_VERSION = max(
    1, int(os.getenv("URGENCH_DUPLICATE_AUDIT_VERSION", "1")),
)
URGENCH_DUPLICATE_AUDIT_DELAY_SECONDS = max(
    1.0, float(os.getenv("URGENCH_DUPLICATE_AUDIT_DELAY_SECONDS", "2")),
)
URGENCH_DUPLICATE_AUDIT_LOOKBACK = max(
    DUPLICATE_LOOKBACK_PHOTOS,
    int(os.getenv("URGENCH_DUPLICATE_AUDIT_LOOKBACK", "20000")),
)


class ClaudeResponse:
    def __init__(self, output_text: str):
        self.output_text = output_text


class ClaudeAPIError(RuntimeError):
    """Safe Anthropic error that never exposes the API key or image payload."""


class ClaudeResponses:
    """Small compatibility layer for the existing vision request call sites."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    @staticmethod
    def _image_source(image_url: str):
        match = re.fullmatch(
            r"data:(?P<media_type>image/(?:jpeg|png|gif|webp));base64,(?P<data>.+)",
            image_url,
            re.DOTALL,
        )
        if match:
            return {
                "type": "base64",
                "media_type": match.group("media_type"),
                "data": match.group("data"),
            }
        return {"type": "url", "url": image_url}

    @classmethod
    def _content(cls, items):
        images = []
        text_items = []
        for item in items:
            item_type = item.get("type")
            if item_type == "input_image":
                block = {
                    "type": "image",
                    "source": cls._image_source(item["image_url"]),
                }
                if isinstance(item.get("cache_control"), dict):
                    block["cache_control"] = item["cache_control"]
                images.append(block)
            elif item_type == "input_text":
                block = {"type": "text", "text": item.get("text", "")}
                if isinstance(item.get("cache_control"), dict):
                    block["cache_control"] = item["cache_control"]
                text_items.append(block)
        # Anthropic recommends placing images before the question.
        return images + text_items

    @classmethod
    def _system(cls, system):
        if not system or isinstance(system, str):
            return system
        return cls._content(system)

    @staticmethod
    def _send(api_key: str, model: str, max_tokens: int, messages, system=None):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        response = requests.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": CLAUDE_API_VERSION,
                "content-type": "application/json",
            },
            json=payload,
            timeout=180,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            request_id = response.headers.get("request-id") or response.headers.get(
                "x-request-id"
            )
            try:
                error_data = response.json()
            except (TypeError, ValueError):
                error_data = None
            api_error = error_data.get("error", {}) if isinstance(error_data, dict) else {}
            error_type = str(api_error.get("type") or "http_error").strip()
            error_message = str(api_error.get("message") or "").strip()
            if not error_message:
                # Keep the diagnostic short and avoid logging an HTML error page.
                error_message = (response.text or response.reason or "unknown error").strip()[:500]
            request_suffix = f"; request_id={request_id}" if request_id else ""
            raise ClaudeAPIError(
                f"Claude API {response.status_code} {error_type}: "
                f"{error_message}{request_suffix}"
            ) from error
        data = response.json()
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        if isinstance(usage, dict):
            logging.info(
                "Claude usage model=%s input=%s cache_write=%s cache_read=%s output=%s",
                model,
                usage.get("input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                usage.get("output_tokens", 0),
            )
        output_text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        if not output_text.strip():
            request_id = response.headers.get("request-id") or response.headers.get(
                "x-request-id"
            )
            stop_reason = data.get("stop_reason") if isinstance(data, dict) else None
            request_suffix = f"; request_id={request_id}" if request_id else ""
            raise ClaudeAPIError(
                "Claude API returned no text"
                f"; stop_reason={stop_reason or 'unknown'}{request_suffix}"
            )
        return ClaudeResponse(output_text)

    async def create(self, *, model, max_output_tokens, input, system=None, **_ignored):
        messages = [
            {
                "role": message.get("role", "user"),
                "content": self._content(message.get("content", [])),
            }
            for message in input
        ]
        return await asyncio.to_thread(
            self._send, self.api_key, model, max_output_tokens, messages,
            self._system(system),
        )


class ClaudeClient:
    def __init__(self, api_key: str):
        self.responses = ClaudeResponses(api_key)


ai_client = ClaudeClient(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
photo_processing_semaphore = asyncio.Semaphore(BACKGROUND_PHOTO_WORKERS)


def ai_response_options(max_output_tokens: int | None = None, model: str | None = None):
    """Shared Claude Messages API options for every paid vision request."""
    return {
        "model": model or CLAUDE_VISION_MODEL,
        "max_output_tokens": max_output_tokens or CLAUDE_MAX_OUTPUT_TOKENS,
    }


def cached_system_prompt(prompt: str):
    """Cache an identical instruction prefix without changing model behavior."""
    if not CLAUDE_PROMPT_CACHE_ENABLED:
        return prompt
    return [{
        "type": "input_text",
        "text": prompt,
        "cache_control": {
            "type": "ephemeral",
            "ttl": CLAUDE_PROMPT_CACHE_TTL,
        },
    }]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
if ai_client:
    logging.info(
        "Сканер Claude подключён: текст=%s; сложная проверка=%s",
        CLAUDE_METADATA_MODEL, CLAUDE_VISION_MODEL,
    )
else:
    logging.warning("Сканер Claude отключён: ANTHROPIC_API_KEY не задан.")
# httpx includes the complete request URL in INFO messages. Telegram request
# URLs contain the bot token, so keep those transport logs out of Render logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

(
    PHOTO,
    DOCTOR_NAME,
    PHOTO_HOSPITAL,
    VIEW_PHOTO_HOSPITAL,
    REPORT_CITY,
    REPORT_HOSPITAL,
    REPORT_DOCTOR,
    REPORT_PERIOD,
) = range(8)
MENU_SEND_PHOTO = "📷 Отправить фото"
MENU_SEND_MORE_PHOTO = "📷 Отправить ещё фотографию"
MENU_FINISH_PHOTOS = "✅ Завершить отправку"
MENU_PROFILE = "👤 Мой профиль"
MENU_PHOTO_REPORT = "📊 Отчёт по фотографиям"
MENU_VIEW_HOSPITAL_PHOTOS = "🖼 Мои фото по роддому"
MENU_VIEW_NEXT_PHOTOS = "➡️ Следующие 10"
MENU_VIEW_PREVIOUS_PHOTOS = "⬅️ Предыдущие 10"
MENU_VIEW_ANOTHER_HOSPITAL = "🏥 Выбрать другой роддом"
MENU_VIEW_PHOTOS_DONE = "✅ Закончить просмотр"
MENU_REPORT_NEXT_PHOTOS = "➡️ Следующие фото отчёта"
MENU_REPORT_PREVIOUS_PHOTOS = "⬅️ Предыдущие фото отчёта"
MENU_REPORT_PHOTOS_DONE = "✅ Закончить просмотр отчёта"
MENU_REPORT_ALL_DOCTORS = "👩‍⚕️ Все врачи"
MENU_OPERATOR_COMMENTS = "💬 Комментарии оператора"
MENU_EXCEL_DOCTOR_REPORT = "📋 Посчитать фото в Excel"
MENU_OPERATOR_TEN_DAY_REPORT = "📊 Отчёт за 10 дней"
PHOTO_REPORT_PATTERN = re.compile(r"^(?:📊\s*)?отч[её]т по фотографиям$", re.IGNORECASE)
VIEW_HOSPITAL_PHOTOS_PATTERN = re.compile(
    r"^(?:🖼\s*)?(?:мои\s+)?фото(?:графии)?\s+по\s+роддому$",
    re.IGNORECASE,
)
OPERATOR_COMMENTS_PATTERN = re.compile(
    r"^(?:💬\s*)?комментарии оператора$", re.IGNORECASE,
)
REPORT_PHOTO_NAVIGATION_PATTERN = re.compile(
    "^(?:" + "|".join(map(re.escape, (
        MENU_REPORT_NEXT_PHOTOS,
        MENU_REPORT_PREVIOUS_PHOTOS,
        MENU_REPORT_PHOTOS_DONE,
    ))) + ")$"
)
EXCEL_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
EXCEL_UPLOAD_MAX_UNPACKED_BYTES = 100 * 1024 * 1024
EXCEL_UPLOAD_MAX_ROWS = 200_000
OPERATOR_REPORT_DAYS = 10
PHOTO_PAYMENT_RATE_SUM = 100_000
MEDIC_PHOTO_PAGE_SIZE = 10
PHOTO_UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # Bot API не отдаёт ботам файлы больше 20 МБ.


def _telegram_id_setting(name: str) -> int | None:
    raw_value = (os.getenv(name) or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        logging.warning("%s=%r не является числовым Telegram ID и будет проигнорирован", name, raw_value)
        return None


# Создатель имеет постоянный доступ к боту. Раньше здесь был зашит чужой ID;
# теперь он задаётся только в .env (узнать свой ID можно командой /my_id).
CREATOR_TELEGRAM_ID = _telegram_id_setting("CREATOR_TELEGRAM_ID")
QR_REGION_RANGES = {
    "01": ("Ташкент", 10_000),
    "02": ("Самарканд", 23_000),
    "03": ("Карши", 100_000),
    "04": ("Бухара", 6_001),
    "05": ("Фергана", 6_000),
    "06": ("Андижан", 6_000),
    "07": ("Ургенч", 7_000),
    "08": ("Наманган", 6_000),
    "09": ("Денау", 4_000),
    "10": ("Термез", 2_000),
    "11": ("Коканд", 5_000),
    "12": ("Джиззах", 3_000),
    "13": ("Навои", 1_000),
}
QR_CITY_REGION_ALIASES = {
    "ташкент": "Ташкент",
    "самарканд": "Самарканд",
    "карши": "Карши",
    "кашкадарья": "Карши",
    "бухара": "Бухара",
    "фергана": "Фергана",
    "андижан": "Андижан",
    "ургенч": "Ургенч",
    "хорезм": "Ургенч",
    "наманган": "Наманган",
    "денау": "Денау",
    "термез": "Термез",
    "коканд": "Коканд",
    "джиззак": "Джиззах",
    "джиззах": "Джиззах",
    "навои": "Навои",
}
QR_REGION_CODE_PATTERN = re.compile(
    r"(?<!\d)(?P<prefix>\d{2})\s*[-‐‑‒–—−]\s*(?P<number>\d{10})(?!\d)"
)
QR_REGION_PLAIN_CODE_PATTERN = re.compile(r"(?<!\d)(?P<prefix>\d{2})(?P<number>\d{10})(?!\d)")
QR_REGION_FLEXIBLE_CODE_PATTERN = re.compile(
    r"(?<!\d)(?P<prefix>\d{2})[\s\-\u2010\u2011\u2012\u2013\u2014\u2212:./\\№#_]*"
    r"(?P<number>\d(?:\s*\d){9})(?!\d)"
)


def canonical_qr_data(qr_data: str | None) -> str | None:
    if not qr_data or not qr_data.strip():
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", qr_data).strip()
    match = QR_REGION_CODE_PATTERN.search(cleaned)
    if not match:
        plain_match = QR_REGION_PLAIN_CODE_PATTERN.search(re.sub(r"\s+", "", cleaned))
        if plain_match and plain_match.group("prefix") in QR_REGION_RANGES:
            match = plain_match
    if not match:
        flexible_match = QR_REGION_FLEXIBLE_CODE_PATTERN.search(cleaned)
        if flexible_match and flexible_match.group("prefix") in QR_REGION_RANGES:
            number = re.sub(r"\s+", "", flexible_match.group("number"))
            return f"{flexible_match.group('prefix')}-{int(number):010d}"
    if not match:
        return cleaned
    return f"{match.group('prefix')}-{int(match.group('number')):010d}"


def qr_code_status(qr_data: str | None, city: str | None = None) -> str:
    if not qr_data:
        return "Код отсутствует (допустимо)"
    match = QR_REGION_CODE_PATTERN.search(qr_data)
    if not match:
        return "Посторонний QR/Data Matrix: формат кода отсутствует в разрешённом справочнике"

    prefix = match.group("prefix")
    number = int(match.group("number"))
    region_range = QR_REGION_RANGES.get(prefix)
    if not region_range:
        return f"Посторонний QR/Data Matrix: префикс региона {prefix} отсутствует в справочнике"

    region, maximum = region_range
    if number < 1 or number > maximum:
        return (
            f"Посторонний QR/Data Matrix: код вне диапазона региона {region}; "
            f"допустимо 1–{maximum}"
        )

    normalized_city = re.sub(r"[^0-9a-zа-яё]+", " ", (city or "").casefold()).strip()
    expected_region = QR_CITY_REGION_ALIASES.get(normalized_city)
    if expected_region and expected_region != region:
        return f"Код другого региона: {region}; выбран регион {city}"
    if city and not expected_region:
        return f"Код распознан: {region}; регион {city} отсутствует в справочнике"
    return f"Код распознан: {region}"


def ai_qr_is_plausible_for_city(qr_data: str | None, city: str | None) -> bool:
    """Accept an AI-read serial only inside the issued range for the photo city.

    A real symbol decoded by ZXing/OpenCV is still handled by ``qr_code_status``
    and may legitimately be foreign.  This stricter rule is only for text that
    a vision model claims to see, which prevents handwritten numbers from being
    stored as a QR/Data Matrix payload.
    """
    normalized = canonical_qr_data(qr_data)
    match = QR_REGION_CODE_PATTERN.fullmatch(normalized or "")
    if not match:
        return False
    region_range = QR_REGION_RANGES.get(match.group("prefix"))
    if not region_range:
        return False
    region, maximum = region_range
    number = int(match.group("number"))
    if number < 1 or number > maximum:
        return False
    normalized_city = re.sub(r"[^0-9a-zа-яё]+", " ", (city or "").casefold()).strip()
    expected_region = QR_CITY_REGION_ALIASES.get(normalized_city)
    return expected_region is None or expected_region == region


def connect(timeout_seconds: float = 30):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    timeout_seconds = max(0.1, float(timeout_seconds))
    con = sqlite3.connect(DB_PATH, timeout=timeout_seconds)
    con.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT NOT NULL,
          city TEXT NOT NULL, district TEXT NOT NULL, maternity_hospital TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('Медик','Оператор')), medic_code TEXT UNIQUE,
          is_active INTEGER NOT NULL DEFAULT 1,
          registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
          telegram_id INTEGER PRIMARY KEY,
          added_by INTEGER NOT NULL,
          added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          telegram_id INTEGER NOT NULL,
          direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
          message_type TEXT NOT NULL,
          text TEXT NOT NULL DEFAULT '',
          sender_name TEXT,
          username TEXT,
          telegram_file_id TEXT,
          telegram_message_id INTEGER,
          created_at TEXT NOT NULL,
          UNIQUE(telegram_id, direction, telegram_message_id)
        );
        CREATE TABLE IF NOT EXISTS approved_datamatrix_codes (
          code TEXT PRIMARY KEY,
          added_at TEXT NOT NULL,
          source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS employee_hospitals (
          telegram_id INTEGER NOT NULL,
          city TEXT NOT NULL,
          maternity_hospital TEXT NOT NULL,
          PRIMARY KEY (telegram_id, maternity_hospital)
        );
        CREATE TABLE IF NOT EXISTS visit_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          medic_telegram_id INTEGER,
          city TEXT NOT NULL,
          maternity_hospital TEXT NOT NULL,
          doctor_name TEXT NOT NULL DEFAULT '',
          required_product TEXT NOT NULL DEFAULT '',
          required_material TEXT NOT NULL DEFAULT '',
          date_from TEXT,
          date_to TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visits (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sender_telegram_id INTEGER NOT NULL,
          city TEXT NOT NULL,
          maternity_hospital TEXT NOT NULL,
          doctor_name TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT,
          latitude REAL,
          longitude REAL,
          location_accuracy REAL,
          assignment_id INTEGER,
          status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS photo_checks (
          photo_id INTEGER PRIMARY KEY,
          product_status TEXT NOT NULL DEFAULT 'Не проверено',
          material_status TEXT NOT NULL DEFAULT 'Не проверено',
          location_status TEXT NOT NULL DEFAULT 'Не запрашивалась',
          duplicate_status TEXT NOT NULL DEFAULT 'Не проверено',
          editing_status TEXT NOT NULL DEFAULT 'Не проверено',
          assignment_status TEXT NOT NULL DEFAULT 'Задание не назначено',
          decision TEXT NOT NULL DEFAULT 'review',
          decision_reason TEXT NOT NULL DEFAULT '',
          automated_at TEXT,
          manual_decision TEXT,
          manual_reason TEXT,
          manual_reviewer TEXT,
          manual_at TEXT,
          FOREIGN KEY(photo_id) REFERENCES photos(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS photos (
          id INTEGER PRIMARY KEY AUTOINCREMENT, sender_telegram_id INTEGER NOT NULL,
          medic_telegram_id INTEGER NOT NULL, medic_code TEXT NOT NULL,
          doctor_name TEXT, image_hash TEXT, crop_hash TEXT, image_digest TEXT, qr_data TEXT, photo_date TEXT, package_number TEXT, mother_name TEXT, mother_phone TEXT,
          review_status TEXT, review_reason TEXT, ai_generated_score REAL, code_status TEXT,
          telegram_file_id TEXT NOT NULL, image BLOB NOT NULL, sent_at TEXT NOT NULL
        );""")
        user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "is_active" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(photos)")}
        if "doctor_name" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN doctor_name TEXT")
        if "image_hash" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN image_hash TEXT")
        if "crop_hash" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN crop_hash TEXT")
        if "visual_hashes" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN visual_hashes TEXT")
        if "image_digest" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN image_digest TEXT")
        if "qr_data" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN qr_data TEXT")
        if "qr_scan_version" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN qr_scan_version INTEGER NOT NULL DEFAULT 0")
        if "targeted_recheck_version" not in columns:
            con.execute(
                "ALTER TABLE photos ADD COLUMN targeted_recheck_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "full_qr_recheck_version" not in columns:
            con.execute(
                "ALTER TABLE photos ADD COLUMN full_qr_recheck_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "full_qr_recheck_failures" not in columns:
            con.execute(
                "ALTER TABLE photos ADD COLUMN full_qr_recheck_failures "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "full_qr_recheck_failure_version" not in columns:
            con.execute(
                "ALTER TABLE photos ADD COLUMN full_qr_recheck_failure_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "duplicate_audit_version" not in columns:
            con.execute(
                "ALTER TABLE photos ADD COLUMN duplicate_audit_version "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "photo_date" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN photo_date TEXT")
        if "package_number" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN package_number TEXT")
        if "mother_name" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN mother_name TEXT")
        if "mother_phone" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN mother_phone TEXT")
        if "review_status" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN review_status TEXT")
        if "review_reason" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN review_reason TEXT")
        if "ai_generated_score" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN ai_generated_score REAL")
        if "code_status" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN code_status TEXT")
        if "maternity_hospital" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN maternity_hospital TEXT")
        if "original_photo_id" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN original_photo_id INTEGER")
        if "visit_id" not in columns:
            con.execute("ALTER TABLE photos ADD COLUMN visit_id INTEGER")
        visit_columns = {row["name"] for row in con.execute("PRAGMA table_info(visits)")}
        if "assignment_id" not in visit_columns:
            con.execute("ALTER TABLE visits ADD COLUMN assignment_id INTEGER")
        chat_columns = {row["name"] for row in con.execute("PRAGMA table_info(chat_messages)")}
        if "sender_name" not in chat_columns:
            con.execute("ALTER TABLE chat_messages ADD COLUMN sender_name TEXT")
        if "username" not in chat_columns:
            con.execute("ALTER TABLE chat_messages ADD COLUMN username TEXT")
        con.execute(
            """INSERT OR IGNORE INTO employee_hospitals (telegram_id, city, maternity_hospital)
               SELECT telegram_id, city, maternity_hospital FROM users
               WHERE maternity_hospital IS NOT NULL AND TRIM(maternity_hospital) != ''"""
        )
        con.execute("DELETE FROM approved_datamatrix_codes WHERE source = 'Эталоны от администратора 2026-08-28'")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_qr_data ON photos(qr_data)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_qr_scan_version ON photos(qr_scan_version)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_targeted_recheck "
            "ON photos(targeted_recheck_version, sent_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_full_qr_recheck "
            "ON photos(full_qr_recheck_version, full_qr_recheck_failures, id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_duplicate_audit "
            "ON photos(duplicate_audit_version, id)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_image_hash ON photos(image_hash)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_image_digest ON photos(image_digest)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_sent_at ON photos(sent_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_sender_sent_at ON photos(sender_telegram_id, sent_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_user_time ON chat_messages(telegram_id, created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photos_visit_id ON photos(visit_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_visits_sender_started ON visits(sender_telegram_id, started_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_visit_assignments_location ON visit_assignments(city, maternity_hospital, is_active)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_photo_checks_decision ON photo_checks(decision, manual_decision)")
        qr_rows = con.execute(
            """SELECT p.id, p.qr_data, p.code_status,
                      COALESCE(medic.city, sender.city, '') AS city
               FROM photos AS p
               LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.qr_data IS NOT NULL AND TRIM(p.qr_data) != ''"""
        ).fetchall()
        for photo in qr_rows:
            normalized_qr = canonical_qr_data(photo["qr_data"])
            current_status = qr_code_status(normalized_qr, photo["city"])
            if normalized_qr != photo["qr_data"] or current_status != photo["code_status"]:
                con.execute(
                    "UPDATE photos SET qr_data = ?, code_status = ? WHERE id = ?",
                    (normalized_qr, current_status, photo["id"]),
                )
        con.execute("PRAGMA optimize")
        old_photos = con.execute(
            "SELECT id, image FROM photos WHERE image_hash IS NULL"
        ).fetchall()
        for photo in old_photos:
            try:
                photo_hash = str(imagehash.phash(Image.open(BytesIO(photo["image"]))))
                con.execute("UPDATE photos SET image_hash = ? WHERE id = ?", (photo_hash, photo["id"]))
            except Exception:
                logging.warning("Не удалось создать отпечаток для фото id=%s", photo["id"])
        old_digests = con.execute(
            "SELECT id, image FROM photos WHERE image_digest IS NULL"
        ).fetchall()
        for photo in old_digests:
            try:
                con.execute(
                    "UPDATE photos SET image_digest = ? WHERE id = ?",
                    (hashlib.sha256(photo["image"]).hexdigest(), photo["id"]),
                )
            except Exception:
                logging.warning("Не удалось создать точный отпечаток для фото id=%s", photo["id"])
        if os.getenv("BACKFILL_CROP_HASHES", "0") == "1":
            old_crop_hashes = con.execute(
                "SELECT id, image FROM photos WHERE crop_hash IS NULL"
            ).fetchall()
            for photo in old_crop_hashes:
                try:
                    con.execute(
                        "UPDATE photos SET crop_hash = ? WHERE id = ?",
                        (calculate_crop_hash(photo["image"]), photo["id"]),
                    )
                except Exception:
                    logging.warning("Не удалось создать отпечаток обрезки для фото id=%s", photo["id"])
        backfill_photo_checks(con)
        restore_september_legacy_decisions(con)
        reconcile_strict_duplicate_policy(con)


def _chat_timestamp(value=None) -> str:
    timestamp = value if isinstance(value, datetime) else datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat()


def save_chat_message(
    telegram_id,
    direction: str,
    message_type: str,
    text: str = "",
    telegram_file_id: str | None = None,
    telegram_message_id=None,
    created_at=None,
    sender_name: str | None = None,
    username: str | None = None,
    enqueue_on_failure: bool = True,
) -> bool:
    """Persist one chat item and defer it instead of losing it on SQLite locks."""
    try:
        telegram_id = int(telegram_id)
        try:
            telegram_message_id = int(telegram_message_id)
        except (TypeError, ValueError):
            telegram_message_id = None
        record = (
            telegram_id,
            direction,
            str(message_type or "text")[:40],
            str(text or "")[:12_000],
            str(sender_name or "")[:300] or None,
            str(username or "")[:100] or None,
            str(telegram_file_id) if telegram_file_id else None,
            telegram_message_id,
            _chat_timestamp(created_at),
        )
    except Exception as error:
        logging.warning("Некорректные данные сообщения чата: %s", error)
        return False

    for attempt in range(CHAT_LOG_RETRY_ATTEMPTS):
        try:
            with connect(timeout_seconds=1) as con:
                con.execute(
                    """INSERT OR IGNORE INTO chat_messages
                       (telegram_id, direction, message_type, text, sender_name, username,
                        telegram_file_id, telegram_message_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    record,
                )
            return True
        except sqlite3.OperationalError as error:
            if not _is_transient_sqlite_error(error) or attempt + 1 >= CHAT_LOG_RETRY_ATTEMPTS:
                logging.warning("Не удалось сохранить сообщение чата: %s", error)
                break
            time.sleep(0.15 * (2 ** attempt))
        except Exception as error:
            logging.warning("Не удалось сохранить сообщение чата: %s", error)
            break

    if enqueue_on_failure:
        with _pending_chat_messages_lock:
            _pending_chat_messages.append(record)
        logging.warning(
            "Сообщение чата отложено для повторного сохранения; очередь=%s",
            len(_pending_chat_messages),
        )
    return False


def _is_transient_sqlite_error(error: Exception) -> bool:
    message = str(error).casefold()
    return "locked" in message or "busy" in message


def flush_pending_chat_messages(limit: int = 100) -> int:
    """Retry deferred chat records without blocking normal bot processing."""
    saved = 0
    for _ in range(max(1, limit)):
        with _pending_chat_messages_lock:
            if not _pending_chat_messages:
                break
            record = _pending_chat_messages.popleft()
        if save_chat_message(
            record[0], record[1], record[2], record[3], record[6], record[7],
            record[8], record[4], record[5], enqueue_on_failure=False,
        ):
            saved += 1
            continue
        with _pending_chat_messages_lock:
            _pending_chat_messages.appendleft(record)
        break
    return saved


async def flush_chat_log_loop():
    while True:
        await asyncio.sleep(3)
        try:
            await asyncio.to_thread(flush_pending_chat_messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Не удалось обработать очередь истории чата")


class ChatLoggingBot(ExtBot):
    """ExtBot that records successful bot replies in the shared database."""

    async def send_message(self, chat_id, text, *args, **kwargs):
        sent = await super().send_message(chat_id, text, *args, **kwargs)
        await asyncio.to_thread(
            save_chat_message,
            chat_id,
            "outgoing",
            "text",
            text,
            telegram_message_id=getattr(sent, "message_id", None),
            created_at=getattr(sent, "date", None),
        )
        return sent

    async def send_photo(self, chat_id, photo, *args, **kwargs):
        sent = await super().send_photo(chat_id, photo, *args, **kwargs)
        sizes = getattr(sent, "photo", None) or []
        file_id = getattr(sizes[-1], "file_id", None) if sizes else None
        await asyncio.to_thread(
            save_chat_message,
            chat_id,
            "outgoing",
            "photo",
            kwargs.get("caption") or getattr(sent, "caption", None) or "",
            telegram_file_id=file_id,
            telegram_message_id=getattr(sent, "message_id", None),
            created_at=getattr(sent, "date", None),
        )
        return sent

    async def send_document(self, chat_id, document, *args, **kwargs):
        sent = await super().send_document(chat_id, document, *args, **kwargs)
        sent_document = getattr(sent, "document", None)
        text = kwargs.get("caption") or getattr(sent_document, "file_name", None) or "Файл"
        await asyncio.to_thread(
            save_chat_message,
            chat_id,
            "outgoing",
            "document",
            text,
            telegram_file_id=getattr(sent_document, "file_id", None),
            telegram_message_id=getattr(sent, "message_id", None),
            created_at=getattr(sent, "date", None),
        )
        return sent


async def log_incoming_chat(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    message_type = "text"
    text = message.text or message.caption or ""
    file_id = None
    if message.photo:
        message_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.document:
        message_type = "document"
        file_id = message.document.file_id
        text = text or message.document.file_name or "Файл"
    elif message.video:
        message_type = "video"
        file_id = message.video.file_id
    elif message.voice:
        message_type = "voice"
        file_id = message.voice.file_id
    elif message.audio:
        message_type = "audio"
        file_id = message.audio.file_id
    elif message.sticker:
        message_type = "sticker"
        file_id = message.sticker.file_id
        text = message.sticker.emoji or "Стикер"
    elif message.location:
        message_type = "location"
        text = f"{message.location.latitude:.6f}, {message.location.longitude:.6f}"
    elif message.contact:
        message_type = "contact"
        contact_name = " ".join(filter(None, (
            message.contact.first_name, message.contact.last_name,
        )))
        text = f"{contact_name}: {message.contact.phone_number}".strip()
    elif not text:
        message_type = "service"
        text = "Служебное сообщение Telegram"
    await asyncio.to_thread(
        save_chat_message,
        user.id,
        "incoming",
        message_type,
        text,
        file_id,
        message.message_id,
        message.date,
        getattr(user, "full_name", None),
        getattr(user, "username", None),
    )


def create_visit(sender_telegram_id: int, city: str, hospital: str, doctor_name: str) -> int:
    """Create an internal visit without changing the medic-facing conversation."""
    with connect() as con:
        today = datetime.now(timezone.utc).astimezone(TASHKENT_TIMEZONE).date().isoformat()
        assignment = con.execute(
            """SELECT id FROM visit_assignments
               WHERE is_active = 1
                 AND (medic_telegram_id IS NULL OR medic_telegram_id = ?)
                 AND LOWER(TRIM(city)) = LOWER(TRIM(?))
                 AND LOWER(TRIM(maternity_hospital)) = LOWER(TRIM(?))
                 AND (TRIM(doctor_name) = '' OR LOWER(TRIM(doctor_name)) = LOWER(TRIM(?)))
                 AND (date_from IS NULL OR date_from = '' OR date_from <= ?)
                 AND (date_to IS NULL OR date_to = '' OR date_to >= ?)
               ORDER BY CASE WHEN medic_telegram_id IS NULL THEN 1 ELSE 0 END, id DESC
               LIMIT 1""",
            (sender_telegram_id, city, hospital, doctor_name, today, today),
        ).fetchone()
        cursor = con.execute(
            """INSERT INTO visits
               (sender_telegram_id, city, maternity_hospital, doctor_name, started_at, assignment_id, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (
                sender_telegram_id, city, hospital, doctor_name,
                datetime.now(timezone.utc).isoformat(), assignment["id"] if assignment else None,
            ),
        )
        return cursor.lastrowid


def photo_assignment(photo_id: int):
    """Return the assignment linked to a photo's visit, if one was active."""
    with connect() as con:
        return con.execute(
            """SELECT assignment.required_product, assignment.required_material
               FROM photos AS photo
               JOIN visits AS visit ON visit.id = photo.visit_id
               JOIN visit_assignments AS assignment ON assignment.id = visit.assignment_id
               WHERE photo.id = ?""",
            (photo_id,),
        ).fetchone()


def assignment_check_status(assignment, is_nuppi_1, is_reference_design, ai_status):
    if not assignment:
        return "Задание не назначено"
    if ai_status != "ok":
        return "Нужна ручная проверка задания"
    product = (assignment["required_product"] or "").strip().casefold()
    material = (assignment["required_material"] or "").strip().casefold()
    checks = []
    if product:
        checks.append(product in {"nuppi 1", "nuppi f1", "nuppi gold 1"} and is_nuppi_1 is True)
    if material:
        checks.append(material in {"эталонный дизайн", "упаковка эталонного дизайна"} and is_reference_design is True)
    if checks and all(checks):
        return "Соответствует заданию"
    if not checks:
        return "Соответствует заданию"
    return "Не соответствует заданию"


def finish_visit(visit_id: int | None, status: str = "completed"):
    if not visit_id:
        return
    with connect() as con:
        con.execute(
            """UPDATE visits SET completed_at = ?, status = ?
               WHERE id = ? AND completed_at IS NULL""",
            (datetime.now(timezone.utc).isoformat(), status, visit_id),
        )


def save_photo_check(photo_id: int, check_result: dict):
    with connect() as con:
        con.execute(
            """INSERT INTO photo_checks
               (photo_id, product_status, material_status, location_status,
                duplicate_status, editing_status, assignment_status, decision,
                decision_reason, automated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(photo_id) DO UPDATE SET
                 product_status = excluded.product_status,
                 material_status = excluded.material_status,
                 location_status = excluded.location_status,
                 duplicate_status = excluded.duplicate_status,
                 editing_status = excluded.editing_status,
                 assignment_status = excluded.assignment_status,
                 decision = excluded.decision,
                 decision_reason = excluded.decision_reason,
                 automated_at = excluded.automated_at""",
            (
                photo_id,
                check_result["product_status"],
                check_result["material_status"],
                check_result["location_status"],
                check_result["duplicate_status"],
                check_result["editing_status"],
                check_result["assignment_status"],
                check_result["decision"],
                check_result["decision_reason"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def backfill_photo_checks(connection):
    """Apply current QR policy to old photos without re-running paid AI."""
    rows = connection.execute(
        """SELECT p.id, p.review_status, p.code_status, p.original_photo_id,
                  checks.photo_id AS existing_check_id,
                  checks.product_status, checks.material_status,
                  checks.location_status, checks.assignment_status
           FROM photos AS p
           LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
           WHERE checks.photo_id IS NULL
              OR (p.qr_data IS NOT NULL AND TRIM(p.qr_data) != '')
              OR COALESCE(p.qr_scan_version, 0) = ?""",
        (QR_SCAN_VERSION,),
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for photo in rows:
        if photo["existing_check_id"] is not None:
            continue
        product_status = str(photo["product_status"] or "").strip()
        material_status = str(photo["material_status"] or "").strip()
        is_nuppi_1 = (
            True if product_status == "NUPPI 1 обнаружен"
            else False if product_status == "NUPPI 1 не подтверждён"
            else None
        )
        is_reference_design = (
            True if material_status == "Эталонный дизайн подтверждён"
            else False if material_status == "Эталонный дизайн не подтверждён"
            else None
        )
        result = automatic_photo_decision(
            photo["review_status"],
            photo["code_status"],
            is_nuppi_1=is_nuppi_1,
            is_reference_design=is_reference_design,
            original_photo_id=photo["original_photo_id"],
            assignment_status=photo["assignment_status"],
            location_required=(str(photo["location_status"] or "").strip() == "Требуется"),
        )
        connection.execute(
            """INSERT INTO photo_checks
               (photo_id, product_status, material_status, location_status,
                duplicate_status, editing_status, assignment_status, decision,
                decision_reason, automated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                photo["id"], result["product_status"], result["material_status"],
                result["location_status"], result["duplicate_status"],
                result["editing_status"], result["assignment_status"],
                result["decision"], result["decision_reason"], now,
            ),
        )


SEPTEMBER_DECISION_RESTORE_MIGRATION = "restore_september_2026_legacy_decisions_v1"


def restore_september_legacy_decisions(connection):
    """Restore pre-manual-only decisions for September without touching manual overrides."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS app_migrations (
               name TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL,
               details TEXT NOT NULL DEFAULT ''
           )"""
    )
    if connection.execute(
        "SELECT 1 FROM app_migrations WHERE name = ?",
        (SEPTEMBER_DECISION_RESTORE_MIGRATION,),
    ).fetchone():
        return 0

    connection.execute(
        """CREATE TABLE IF NOT EXISTS photo_checks_september_2026_backup AS
           SELECT checks.*
           FROM photo_checks AS checks
           JOIN photos AS photo ON photo.id = checks.photo_id
           WHERE DATE(photo.sent_at, '+5 hours') >= '2026-09-01'
             AND DATE(photo.sent_at, '+5 hours') < '2026-10-01'"""
    )
    rows = connection.execute(
        """SELECT photo.id, photo.review_status, photo.code_status,
                  photo.original_photo_id, checks.product_status,
                  checks.material_status, checks.location_status,
                  checks.assignment_status
           FROM photos AS photo
           JOIN photo_checks AS checks ON checks.photo_id = photo.id
           WHERE DATE(photo.sent_at, '+5 hours') >= '2026-09-01'
             AND DATE(photo.sent_at, '+5 hours') < '2026-10-01'"""
    ).fetchall()
    restored_at = datetime.now(timezone.utc).isoformat()
    for photo in rows:
        product_status = str(photo["product_status"] or "").strip()
        material_status = str(photo["material_status"] or "").strip()
        is_nuppi_1 = (
            True if product_status == "NUPPI 1 обнаружен"
            else False if product_status == "NUPPI 1 не подтверждён"
            else None
        )
        is_reference_design = (
            True if material_status == "Эталонный дизайн подтверждён"
            else False if material_status == "Эталонный дизайн не подтверждён"
            else None
        )
        result = legacy_automatic_photo_decision(
            photo["review_status"],
            photo["code_status"],
            is_nuppi_1=is_nuppi_1,
            is_reference_design=is_reference_design,
            original_photo_id=photo["original_photo_id"],
            assignment_status=photo["assignment_status"],
            location_required=(
                str(photo["location_status"] or "").strip() == "Требуется"
            ),
        )
        connection.execute(
            """UPDATE photo_checks
               SET decision = ?, decision_reason = ?, automated_at = ?
               WHERE photo_id = ?""",
            (
                result["decision"], result["decision_reason"],
                restored_at, photo["id"],
            ),
        )

    connection.execute(
        "INSERT INTO app_migrations (name, applied_at, details) VALUES (?, ?, ?)",
        (
            SEPTEMBER_DECISION_RESTORE_MIGRATION, restored_at,
            f"restored_rows={len(rows)}",
        ),
    )
    logging.info("Восстановлены решения для %s сентябрьских фотографий.", len(rows))
    return len(rows)


STRICT_DUPLICATE_POLICY_MIGRATION = "duplicates_only_equal_qr_or_identical_bytes_v2"


def reconcile_strict_duplicate_policy(connection):
    """Keep only deterministic duplicates: equal QR or identical file bytes.

    Similar packaging, perceptual hashes, matching handwriting and even an exact
    manually entered name/date pair are not proof that two photos are copies.
    Running this versioned migration also clears links created by those older,
    broader rules without touching an operator's manual decision.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS app_migrations (
               name TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL,
               details TEXT NOT NULL DEFAULT ''
           )"""
    )
    if connection.execute(
        "SELECT 1 FROM app_migrations WHERE name = ?",
        (STRICT_DUPLICATE_POLICY_MIGRATION,),
    ).fetchone():
        return 0

    rows = connection.execute(
        """SELECT id, sent_at, qr_data, image_digest,
                  review_status, review_reason, original_photo_id
           FROM photos ORDER BY id"""
    ).fetchall()
    first_by_qr = {}
    first_by_digest = {}
    changed = 0
    migrated_at = datetime.now(timezone.utc).isoformat()

    for photo in rows:
        qr_key = canonical_qr_data(photo["qr_data"])
        qr_key = str(qr_key or "").strip()
        digest_key = str(photo["image_digest"] or "").strip().casefold()
        original = first_by_qr.get(qr_key) if qr_key else None
        duplicate_reason = ""
        if original:
            duplicate_reason = (
                f"Такой же QR-код есть у фотографии от {original['sent_at']}."
            )
        elif digest_key and digest_key in first_by_digest:
            original = first_by_digest[digest_key]
            duplicate_reason = (
                "Повторно отправлен тот же файл фотографии от "
                f"{original['sent_at']}."
            )

        current_status = str(photo["review_status"] or "")
        current_reason = str(photo["review_reason"] or "")
        current_original = photo["original_photo_id"]
        if original:
            desired_original = original["id"]
            desired_status = "Возможная копия"
            desired_reason = duplicate_reason
        else:
            desired_original = None
            if current_original or "коп" in current_status.casefold() or "обрез" in current_status.casefold():
                desired_status = "Не проверено"
                desired_reason = (
                    "Старый признак визуального сходства снят. Копией считается только "
                    "точно такой же файл либо совпадение полного QR/Data Matrix."
                )
            else:
                desired_status = current_status
                desired_reason = current_reason

        if (current_original != desired_original or current_status != desired_status
                or current_reason != desired_reason):
            connection.execute(
                """UPDATE photos
                   SET original_photo_id = ?, review_status = ?, review_reason = ?
                   WHERE id = ?""",
                (desired_original, desired_status, desired_reason, photo["id"]),
            )
            changed += 1

        duplicate_status = "Возможный повтор" if original else "Повтор не обнаружен"
        connection.execute(
            """UPDATE photo_checks
               SET duplicate_status = ?, automated_at = ?
               WHERE photo_id = ?""",
            (duplicate_status, migrated_at, photo["id"]),
        )
        if qr_key and qr_key not in first_by_qr:
            first_by_qr[qr_key] = photo
        if digest_key and digest_key not in first_by_digest:
            first_by_digest[digest_key] = photo

    connection.execute(
        "INSERT INTO app_migrations (name, applied_at, details) VALUES (?, ?, ?)",
        (STRICT_DUPLICATE_POLICY_MIGRATION, migrated_at, f"changed_rows={changed}"),
    )
    logging.info(
        "Строгая политика копий применена: обновлено фотографий %s.", changed,
    )
    return changed


def get_user(user_id, include_inactive=False):
    with connect() as con:
        query = "SELECT * FROM users WHERE telegram_id=?"
        if not include_inactive:
            query += " AND is_active = 1"
        return con.execute(query, (user_id,)).fetchone()


def get_employee_hospitals(user_id: int, city: str):
    with connect() as con:
        rows = con.execute(
            """SELECT maternity_hospital FROM employee_hospitals
               WHERE telegram_id = ? AND city = ? ORDER BY maternity_hospital COLLATE NOCASE""",
            (user_id, city),
        ).fetchall()
        if rows:
            return [row["maternity_hospital"] for row in rows]
        user = con.execute("SELECT maternity_hospital FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        return [user["maternity_hospital"]] if user and user["maternity_hospital"] else []


def medic_report_scope(user_id: int):
    """Return the medic's city/hospital pairs, including historical visits."""
    hospital_expr = """COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                                NULLIF(TRIM(visit.maternity_hospital), ''),
                                NULLIF(TRIM(sender.maternity_hospital), ''),
                                'Роддом не указан')"""
    city_expr = f"""COALESCE(NULLIF(TRIM(visit.city), ''),
                             (SELECT NULLIF(TRIM(eh.city), '')
                                FROM employee_hospitals AS eh
                               WHERE eh.telegram_id = p.sender_telegram_id
                                 AND TRIM(eh.maternity_hospital) = {hospital_expr}
                               LIMIT 1),
                             NULLIF(TRIM(sender.city), ''),
                             'Город не указан')"""
    with connect() as con:
        rows = con.execute(
            f"""SELECT DISTINCT city, maternity_hospital
               FROM (
                   SELECT COALESCE(NULLIF(TRIM(city), ''), 'Город не указан') AS city,
                          TRIM(maternity_hospital) AS maternity_hospital
                   FROM employee_hospitals
                   WHERE telegram_id = ? AND TRIM(maternity_hospital) != ''
                   UNION ALL
                   SELECT COALESCE(NULLIF(TRIM(city), ''), 'Город не указан'),
                          TRIM(maternity_hospital)
                   FROM users
                   WHERE telegram_id = ? AND TRIM(COALESCE(maternity_hospital, '')) != ''
                   UNION ALL
                   SELECT COALESCE(NULLIF(TRIM(city), ''), 'Город не указан'),
                          TRIM(maternity_hospital)
                   FROM visits
                   WHERE sender_telegram_id = ? AND TRIM(COALESCE(maternity_hospital, '')) != ''
                   UNION ALL
                   SELECT {city_expr}, {hospital_expr}
                   FROM photos AS p
                   LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                   LEFT JOIN visits AS visit ON visit.id = p.visit_id
                   WHERE p.sender_telegram_id = ?
               )
               WHERE TRIM(maternity_hospital) != ''
               ORDER BY city COLLATE NOCASE, maternity_hospital COLLATE NOCASE""",
            (user_id, user_id, user_id, user_id),
        ).fetchall()
    return [
        {"city": row["city"], "maternity_hospital": row["maternity_hospital"]}
        for row in rows
    ]


def get_medic_report_cities(user_id: int):
    """Return cities in which this medic has assigned or historical photos."""
    return sorted(
        {item["city"] for item in medic_report_scope(user_id)},
        key=str.casefold,
    )


def get_medic_report_hospitals(user_id: int, city: str | None = None):
    """Return this medic's hospitals, optionally limited to one selected city."""
    hospitals = {
        item["maternity_hospital"]
        for item in medic_report_scope(user_id)
        if city is None or item["city"] == city
    }
    return sorted(hospitals, key=str.casefold)


def get_medic_report_doctors(user_id: int, city: str, hospital: str):
    """Return named doctors found in this medic's photos for the selected place."""
    hospital_expr = """COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                                NULLIF(TRIM(visit.maternity_hospital), ''),
                                NULLIF(TRIM(sender.maternity_hospital), ''),
                                'Роддом не указан')"""
    city_expr = f"""COALESCE(NULLIF(TRIM(visit.city), ''),
                             (SELECT NULLIF(TRIM(eh.city), '')
                                FROM employee_hospitals AS eh
                               WHERE eh.telegram_id = p.sender_telegram_id
                                 AND TRIM(eh.maternity_hospital) = {hospital_expr}
                               LIMIT 1),
                             NULLIF(TRIM(sender.city), ''),
                             'Город не указан')"""
    with connect() as con:
        rows = con.execute(
            f"""SELECT DISTINCT TRIM(p.doctor_name) AS doctor_name
                  FROM photos AS p
                  LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                  LEFT JOIN visits AS visit ON visit.id = p.visit_id
                 WHERE p.sender_telegram_id = ?
                   AND {city_expr} = ?
                   AND {hospital_expr} = ?
                   AND TRIM(COALESCE(p.doctor_name, '')) != ''
                 ORDER BY doctor_name COLLATE NOCASE""",
            (user_id, city, hospital),
        ).fetchall()
    return [row["doctor_name"] for row in rows]


def medic_report_hospital_city(user_id: int, hospital: str, fallback_city: str = "") -> str:
    """Resolve the city stored with a medic's selected hospital."""
    with connect() as con:
        row = con.execute(
            """SELECT city FROM employee_hospitals
               WHERE telegram_id = ? AND maternity_hospital = ?
               ORDER BY CASE WHEN city = ? THEN 0 ELSE 1 END, city COLLATE NOCASE
               LIMIT 1""",
            (user_id, hospital, fallback_city),
        ).fetchone()
    return ((row["city"] if row else fallback_city) or "Город не указан").strip()


def is_allowed_user(user_id: int) -> bool:
    if CREATOR_TELEGRAM_ID is not None and user_id == CREATOR_TELEGRAM_ID:
        return True
    with connect() as con:
        return con.execute(
            "SELECT 1 FROM allowed_users WHERE telegram_id = ?", (user_id,)
        ).fetchone() is not None


async def deny_unlisted_user(update: Update) -> bool:
    if is_allowed_user(update.effective_user.id):
        return False
    if update.effective_message is None:
        return True
    await update.effective_message.reply_text(
        "Бот закрытый. Ваш Telegram ID: "
        f"{update.effective_user.id}\nПередайте его администратору для добавления в список доступа."
    )
    return True


def profile_message(user):
    text = f"ФИО: {user['full_name']}\nДолжность: {user['role']}"
    if user["role"] == "Медик" and user["medic_code"]:
        text += f"\nКод медика: {user['medic_code']}"
    return text


def prepare_image_for_analysis(image_bytes: bytes, max_dimension: int | None = None, quality: int = 90) -> bytes:
    limit = max_dimension or IMAGE_PROCESSING_MAX_DIMENSION
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()


def _qr_scan_image(image_bytes: bytes) -> Image.Image:
    """Keep substantially more detail than the general AI image preparation."""
    with Image.open(BytesIO(image_bytes)) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    if max(source.size) > QR_SCAN_MAX_DIMENSION:
        source.thumbnail((QR_SCAN_MAX_DIMENSION, QR_SCAN_MAX_DIMENSION), Image.Resampling.LANCZOS)
    return source


def _qr_axis_positions(total: int, crop_size: int):
    """Cover an axis without blind gaps while keeping the number of crops bounded."""
    remaining = max(0, total - crop_size)
    if not remaining:
        return [0]
    # Ten percent overlap is enough to keep a complete quiet zone in at least
    # one neighbouring crop while avoiding an excessive number of scans.
    steps = max(1, (remaining + max(1, round(crop_size * 0.90)) - 1) // max(1, round(crop_size * 0.90)))
    return sorted({round(index * remaining / steps) for index in range(steps + 1)})


def _qr_scan_tiles(source: Image.Image, crop_ratios=(0.60, 0.35, 0.22)):
    """Return overlapping crops at several scales for small codes and cluttered photos."""
    width, height = source.size
    if min(width, height) < 500:
        return
    seen_boxes = set()
    for crop_ratio in crop_ratios:
        crop_width = max(1, round(width * crop_ratio))
        crop_height = max(1, round(height * crop_ratio))
        x_positions = _qr_axis_positions(width, crop_width)
        y_positions = _qr_axis_positions(height, crop_height)
        for y in y_positions:
            for x in x_positions:
                box = (x, y, x + crop_width, y + crop_height)
                if box not in seen_boxes:
                    seen_boxes.add(box)
                    yield source.crop(box)


def _qr_priority_crops(source: Image.Image):
    """Yield focused upper-package crops where tiny serial symbols are printed."""
    width, height = source.size
    if min(width, height) < 160:
        return
    # NUPPI F and GOLD layouts place the serial symbol in the upper half of
    # the front panel. These overlapping crops preserve far more module detail
    # than the generic tiling pass, while still allowing off-centre packages.
    fractional_boxes = (
        (0.20, 0.04, 0.80, 0.56),
        (0.40, 0.04, 0.98, 0.58),
        (0.02, 0.04, 0.62, 0.58),
        (0.30, 0.14, 0.72, 0.50),
    )
    seen = set()
    for left, top, right, bottom in fractional_boxes:
        box = (
            max(0, round(width * left)),
            max(0, round(height * top)),
            min(width, round(width * right)),
            min(height, round(height * bottom)),
        )
        if box in seen or box[2] - box[0] < 32 or box[3] - box[1] < 32:
            continue
        seen.add(box)
        yield source.crop(box)


def _enlarge_qr_crop(
    image: Image.Image,
    max_scale: float = 4.0,
    resample=Image.Resampling.LANCZOS,
) -> Image.Image:
    longest_side = max(image.size)
    if longest_side >= QR_SCAN_TARGET_DIMENSION:
        return image
    scale = min(max_scale, QR_SCAN_TARGET_DIMENSION / max(1, longest_side))
    if scale <= 1.05:
        return image
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        resample,
    )


def _decoded_barcode_values(result):
    """Return both rendered and raw ZXing payloads without losing GS1 separators."""
    values = []
    text_value = (getattr(result, "text", "") or "").strip()
    if text_value:
        values.append(text_value)
    raw_value = getattr(result, "bytes", b"") or b""
    if isinstance(raw_value, (bytes, bytearray)):
        for encoding in ("utf-8", "ascii", "latin-1"):
            try:
                decoded = bytes(raw_value).decode(encoding).strip()
            except (UnicodeDecodeError, LookupError):
                continue
            if decoded and decoded not in values:
                values.append(decoded)
    return values


def _plausible_failed_barcode_detection(result, image_size) -> bool:
    """Keep a strong "code is visible" signal without trusting corrupt data.

    ``return_errors=True`` lets ZXing report a symbol whose locator pattern was
    found but whose checksum could not be restored.  This is especially useful
    for a tiny, blurred or partly written-over code.  Very large detections at a
    crop border are usually packaging edges, so they are deliberately ignored.
    """
    error_text = str(getattr(result, "error", "") or "")
    if "ChecksumError" not in error_text:
        return False
    position = getattr(result, "position", None)
    if position is None:
        return False
    try:
        points = (
            position.top_left, position.top_right,
            position.bottom_right, position.bottom_left,
        )
        xs = [float(point.x) for point in points]
        ys = [float(point.y) for point in points]
    except (AttributeError, TypeError, ValueError):
        return False
    detected_width = max(xs) - min(xs)
    detected_height = max(ys) - min(ys)
    if min(detected_width, detected_height) < 8:
        return False
    image_width, image_height = image_size
    if (detected_width >= image_width * 0.9
            or detected_height >= image_height * 0.9):
        return False
    aspect_ratio = detected_width / max(1.0, detected_height)
    return 1 / 3 <= aspect_ratio <= 3


def _zxing_decode(variant: Image.Image, exhaustive: bool = False,
                  return_presence: bool = False):
    formats = None
    for format_name in ("DataMatrix", "QRCode", "MicroQRCode", "Aztec"):
        barcode_format = getattr(zxingcpp.BarcodeFormat, format_name, None)
        if barcode_format is not None:
            # Объединение через «|» даёт BarcodeFormats во всех версиях zxing-cpp;
            # кортеж форматов версия 2.x не принимает (TypeError), и раньше из-за
            # этого Data Matrix молча не распознавался.
            formats = barcode_format if formats is None else formats | barcode_format
    options = {
        "formats": formats,
        "try_rotate": True,
        "try_downscale": True,
        "try_invert": True,
    }
    fallback = None
    code_detected = False
    binarizers = [None]
    binarizer_type = getattr(zxingcpp, "Binarizer", None)
    if exhaustive and binarizer_type is not None:
        # LocalAverage is ZXing's default.  The two alternatives recover hard
        # shadows and already-near-binary screenshots without another AI call.
        for name in ("GlobalHistogram", "FixedThreshold"):
            candidate = getattr(binarizer_type, name, None)
            if candidate is not None and candidate not in binarizers:
                binarizers.append(candidate)
    for binarizer in binarizers:
        scan_options = dict(options)
        if binarizer is not None:
            scan_options["binarizer"] = binarizer
        if return_presence:
            scan_options["return_errors"] = True
        try:
            results = zxingcpp.read_barcodes(variant, **scan_options)
        except TypeError:
            # Compatibility with older zxing-cpp builds that predate one of
            # the optional parameters used by newer wheels.  Drop the newest
            # option first so binarizer/return_errors keep working on 2.x.
            scan_options.pop("try_invert", None)
            try:
                results = zxingcpp.read_barcodes(variant, **scan_options)
            except TypeError:
                scan_options.pop("binarizer", None)
                scan_options.pop("return_errors", None)
                results = zxingcpp.read_barcodes(variant, **scan_options)
        for result in results:
            valid = getattr(result, "valid", True)
            if valid:
                code_detected = True
            elif return_presence and _plausible_failed_barcode_detection(
                    result, variant.size):
                code_detected = True
            if not valid:
                # Never store bytes from a checksum/format error.  The failed
                # detection is used only to send the photo to operator review.
                continue
            for value in _decoded_barcode_values(result):
                # A package can contain several symbols. Prefer the regional
                # serial instead of returning an unrelated product URL.
                normalized = canonical_qr_data(value)
                if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                    return (normalized, True) if return_presence else normalized
                if fallback is None:
                    fallback = value
    if return_presence:
        return fallback, code_detected
    return fallback


def _qr_contrast_variants(image: Image.Image, exhaustive: bool = True):
    """Yield a small, high-value set of variants for glare and poor contrast."""
    yield image
    gray = image.convert("L")
    yield gray
    contrasted = ImageOps.autocontrast(gray, cutoff=1).filter(
        ImageFilter.UnsharpMask(radius=2, percent=220, threshold=1)
    )
    yield contrasted
    # A gentler enhancement helps colored Data Matrix modules survive glare
    # without destroying their quiet zone.
    yield ImageOps.autocontrast(gray, cutoff=3).filter(
        ImageFilter.UnsharpMask(radius=1, percent=140, threshold=2)
    )
    if not exhaustive:
        return
    # Fixed thresholds complement ZXing's own binarizer when the package is
    # photographed under a hard shadow or a bright reflection.
    yield contrasted.point(lambda value: 255 if value > 115 else 0)
    yield contrasted.point(lambda value: 255 if value > 165 else 0)


def read_qr_code_details(image_bytes: bytes):
    """Return the decoded value and whether a damaged code was still detected."""
    try:
        source = _qr_scan_image(image_bytes)
    except Exception as error:
        logging.warning("Не удалось подготовить изображение для QR-проверки: %s", error)
        return None, False

    fallback_value = None
    code_detected = False
    priority_crops = list(_qr_priority_crops(source))
    if zxingcpp is not None:
        try:
            # Small package codes can occupy only 25-40 pixels in the original
            # photo. Scan likely label areas first with two interpolation modes:
            # LANCZOS restores soft photos, while NEAREST preserves crisp modules.
            for crop in priority_crops:
                for resample in (Image.Resampling.LANCZOS, Image.Resampling.NEAREST):
                    enlarged = _enlarge_qr_crop(crop, max_scale=8.0, resample=resample)
                    border = max(12, round(min(enlarged.size) * 0.04))
                    scan_image = ImageOps.expand(enlarged, border=border, fill="white")
                    for variant in _qr_contrast_variants(scan_image, exhaustive=False):
                        value, detected = _zxing_decode(
                            variant, exhaustive=True, return_presence=True,
                        )
                        code_detected = code_detected or detected
                        if value:
                            normalized = canonical_qr_data(value)
                            if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                                return normalized, True
                            fallback_value = fallback_value or value

            for variant in _qr_contrast_variants(source):
                value, detected = _zxing_decode(
                    variant, exhaustive=True, return_presence=True,
                )
                code_detected = code_detected or detected
                if value:
                    normalized = canonical_qr_data(value)
                    if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                        return normalized, True
                    fallback_value = fallback_value or value

            for tile in _qr_scan_tiles(source):
                enlarged = _enlarge_qr_crop(tile)
                border = max(8, round(min(enlarged.size) * 0.02))
                enlarged = ImageOps.expand(enlarged, border=border, fill="white")
                # Native crops sometimes preserve already-sharp modules better;
                # enlarged crops recover symbols that occupy only a few pixels.
                for scan_image in (tile, enlarged):
                    for variant in _qr_contrast_variants(scan_image, exhaustive=False):
                        value, detected = _zxing_decode(
                            variant, return_presence=True,
                        )
                        code_detected = code_detected or detected
                        if value:
                            normalized = canonical_qr_data(value)
                            if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                                return normalized, True
                            fallback_value = fallback_value or value
        except Exception as error:
            logging.warning("Не удалось прочитать Data Matrix: %s", error)

    if cv2 is None or np is None:
        return fallback_value, code_detected
    try:
        detectors = [cv2.QRCodeDetector()]
        aruco_detector = getattr(cv2, "QRCodeDetectorAruco", None)
        if aruco_detector is not None:
            try:
                detectors.append(aruco_detector())
            except cv2.error:
                pass
        priority_cv_crops = (
            _enlarge_qr_crop(crop, max_scale=8.0, resample=Image.Resampling.NEAREST)
            for crop in priority_crops
        )
        cv_scan_images = chain(
            ((source, True),),
            ((crop, False) for crop in priority_cv_crops),
            ((tile, False) for tile in _qr_scan_tiles(source, (0.60, 0.35))),
        )
        for pil_variant, is_full_image in cv_scan_images:
            pil_variant = _enlarge_qr_crop(pil_variant)
            image = cv2.cvtColor(np.asarray(pil_variant), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            _, threshold = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            adaptive = cv2.adaptiveThreshold(
                enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 5,
            )
            # Grayscale is already represented by ``enhanced``.  Four distinct
            # variants keep the difficult-code pass useful without doubling
            # CPU time on the single shared Render service.
            for variant in (image, enhanced, threshold, adaptive):
                for detector in detectors:
                    data, points, _ = detector.detectAndDecode(variant)
                    code_detected = code_detected or points is not None
                    if data and data.strip():
                        value = data.strip()
                        normalized = canonical_qr_data(value)
                        if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                            return normalized, True
                        fallback_value = fallback_value or value
                    # Multi-symbol detection is substantially slower and only
                    # needs to run on the complete image. Tight crops normally
                    # contain one package code.
                    if not is_full_image:
                        continue
                    try:
                        result = detector.detectAndDecodeMulti(variant)
                        if len(result) == 4:
                            ok, decoded_values, points, _ = result
                            code_detected = code_detected or points is not None
                            if ok:
                                for value in decoded_values:
                                    if value and value.strip():
                                        value = value.strip()
                                        normalized = canonical_qr_data(value)
                                        if normalized and QR_REGION_CODE_PATTERN.fullmatch(normalized):
                                            return normalized, True
                                        fallback_value = fallback_value or value
                    except (AttributeError, cv2.error):
                        pass
        return fallback_value, code_detected
    except Exception as error:
        logging.warning("Не удалось прочитать QR-код: %s", error)
        return fallback_value, code_detected


def read_qr_code(image_bytes: bytes):
    """Backward-compatible value-only wrapper used by historical backfills."""
    return read_qr_code_details(image_bytes)[0]


def find_photo_by_qr(qr_data: str, exclude_photo_id: int | None = None):
    if not qr_data:
        return None
    with connect() as con:
        query = "SELECT id, sent_at FROM photos WHERE qr_data = ?"
        parameters = [qr_data]
        if exclude_photo_id is not None:
            query += " AND id < ?"
            parameters.append(exclude_photo_id)
        return con.execute(query + " ORDER BY id LIMIT 1", parameters).fetchone()


def find_photo_by_mother_and_date(photo_date: str | None, mother_name: str | None,
                                  exclude_photo_id: int | None = None):
    """Return an earlier photo only for an exact normalized date+name pair.

    A date by itself, a similar spelling, a similar package or an identical image
    is deliberately insufficient. The operator may enter these fields manually;
    automatic image analysis no longer reads them from the package.
    """
    name_key = _duplicate_name_key(mother_name)
    if not photo_date or not name_key:
        return None
    with connect() as con:
        query = (
            "SELECT id, sent_at, mother_name FROM photos "
            "WHERE photo_date = ? AND mother_name IS NOT NULL "
            "AND TRIM(mother_name) != ''"
        )
        parameters = [photo_date]
        if exclude_photo_id is not None:
            query += " AND id < ?"
            parameters.append(exclude_photo_id)
        rows = con.execute(query + " ORDER BY id", parameters).fetchall()
    return next(
        (row for row in rows if _duplicate_name_key(row["mother_name"]) == name_key),
        None,
    )


def find_exact_photo_by_digest(image_digest: str, exclude_photo_id: int | None = None):
    if not image_digest:
        return None
    with connect() as con:
        query = "SELECT id, sent_at FROM photos WHERE image_digest = ?"
        parameters = [image_digest]
        if exclude_photo_id is not None:
            query += " AND id < ?"
            parameters.append(exclude_photo_id)
        return con.execute(query + " ORDER BY id LIMIT 1", parameters).fetchone()


def calculate_crop_hash(image_bytes: bytes) -> str:
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        return str(imagehash.crop_resistant_hash(image))


def crop_hash_match_score(first_hash: str | None, second_hash: str | None):
    """Return (matching regions, distance) for two crop-resistant hashes."""
    if not first_hash or not second_hash:
        return None
    try:
        matches, distance = imagehash.hex_to_multihash(first_hash).hash_diff(
            imagehash.hex_to_multihash(second_hash),
            hamming_cutoff=CROP_HASH_HAMMING_CUTOFF,
        )
        return (matches, distance) if matches else None
    except Exception as error:
        logging.warning("Не удалось сравнить отпечатки обрезки: %s", error)
        return None


def crop_hashes_match(first_hash: str | None, second_hash: str | None) -> bool:
    return crop_hash_match_score(first_hash, second_hash) is not None


def _package_candidate_crops(image: Image.Image):
    """Locate likely purple/gold NUPPI boxes without depending on the background."""
    if cv2 is None or np is None or not hasattr(cv2, "findContours"):
        return []
    working = image.copy()
    working.thumbnail((512, 512), Image.Resampling.LANCZOS)
    pixels = np.asarray(working)
    hsv = cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV)
    purple = cv2.inRange(hsv, np.array([112, 35, 30]), np.array([165, 255, 255]))
    gold = cv2.inRange(hsv, np.array([8, 45, 50]), np.array([45, 255, 255]))
    mask = cv2.bitwise_or(purple, gold)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    image_area = max(1, working.width * working.height)
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area_ratio = (width * height) / image_area
        aspect_ratio = width / max(1, height)
        if area_ratio < 0.015 or not 0.30 <= aspect_ratio <= 1.60:
            continue
        padding = max(3, round(max(width, height) * 0.05))
        box = (
            max(0, x - padding),
            max(0, y - padding),
            min(working.width, x + width + padding),
            min(working.height, y + height + padding),
        )
        candidates.append((area_ratio, working.crop(box)))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in candidates[:3]]


def calculate_visual_hashes(image_bytes: bytes) -> str:
    """Build typed fingerprints robust to crop, background, tilt and rotation.

    Perceptual hashes describe the overall light/dark layout, edge hashes retain
    tears/handwriting, and difference hashes retain local geometry. Keeping the
    groups typed prevents an edge hash from being compared with an ordinary
    photo hash. Old list-only fingerprints remain readable below.
    """
    with Image.open(BytesIO(image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        crops = [image]
        crops.extend(_package_candidate_crops(image))
        # Centre crops help when colour detection is confused by bedding or
        # clothing but the package still occupies the main part of the frame.
        for ratio in (0.80, 0.60):
            width = round(image.width * ratio)
            height = round(image.height * ratio)
            left = (image.width - width) // 2
            top = (image.height - height) // 2
            crops.append(image.crop((left, top, left + width, top + height)))

        hashes = {"phash": [], "edge": [], "dhash": []}

        def add_hash(group, value):
            encoded = str(value)
            if encoded not in hashes[group]:
                hashes[group].append(encoded)

        for crop_index, crop in enumerate(crops):
            angles = (
                (0, 90, 180, 270)
                if crop_index == 0
                else (0, -30, -15, 15, 30, 90, 180, 270)
            )
            for angle in angles:
                variant = crop if angle == 0 else crop.rotate(
                    angle,
                    resample=Image.Resampling.BICUBIC,
                    expand=True,
                    fillcolor="white",
                )
                normalized = ImageOps.contain(
                    variant, (384, 384), Image.Resampling.LANCZOS,
                )
                add_hash("phash", imagehash.phash(normalized))
                add_hash("dhash", imagehash.dhash(normalized))
                edges = ImageOps.autocontrast(
                    normalized.convert("L"), cutoff=1,
                ).filter(ImageFilter.FIND_EDGES)
                add_hash("edge", imagehash.phash(edges))

        # Bound storage and comparison time even when several coloured objects
        # around the package produce additional candidate crops.
        payload = {
            "version": 2,
            "phash": hashes["phash"][:48],
            "edge": hashes["edge"][:40],
            "dhash": hashes["dhash"][:40],
        }
        return json.dumps(payload, separators=(",", ":"))


def _visual_hash_groups(single_hash: str | None, visual_hashes: str | None):
    groups = {"phash": [single_hash] if single_hash else [], "edge": [], "dhash": []}
    if visual_hashes:
        try:
            decoded = json.loads(visual_hashes)
            if isinstance(decoded, list):
                # Fingerprints written before version 2 were all pHashes.
                groups["phash"].extend(
                    value for value in decoded if isinstance(value, str) and value
                )
            elif isinstance(decoded, dict):
                for group in groups:
                    values = decoded.get(group)
                    if isinstance(values, list):
                        groups[group].extend(
                            value for value in values if isinstance(value, str) and value
                        )
        except (TypeError, ValueError):
            logging.warning("Некорректный набор визуальных отпечатков пропущен")
    return {
        group: list(dict.fromkeys(values))
        for group, values in groups.items()
    }


def _visual_hash_values(single_hash: str | None, visual_hashes: str | None):
    """Backward-compatible flattened view used by older callers/tests."""
    groups = _visual_hash_groups(single_hash, visual_hashes)
    return list(dict.fromkeys(chain.from_iterable(groups.values())))


def _hex_hash_distance(first_value: str, second_value: str):
    try:
        if len(first_value) != len(second_value):
            return None
        return (int(first_value, 16) ^ int(second_value, 16)).bit_count()
    except (TypeError, ValueError):
        return None


def visual_hash_match_details(first_hash: str | None, second_hash: str | None,
                              first_visual_hashes: str | None = None,
                              second_visual_hashes: str | None = None):
    """Return every independent fingerprint family that matched.

    Requiring more than one family for the paid comparison prevents the common
    printed NUPPI design from flooding the candidate list, while still catching
    the same box photographed at another angle or on a different background.
    """
    first_groups = _visual_hash_groups(first_hash, first_visual_hashes)
    second_groups = _visual_hash_groups(second_hash, second_visual_hashes)
    thresholds = {
        "phash": VISUAL_HASH_MAX_DISTANCE,
        "edge": VISUAL_EDGE_HASH_MAX_DISTANCE,
        "dhash": VISUAL_DHASH_MAX_DISTANCE,
    }
    accepted_distances = {}
    if not any(first_groups.values()) or not any(second_groups.values()):
        return accepted_distances
    try:
        for group, maximum in thresholds.items():
            distances = [
                distance
                for left in first_groups[group]
                for right in second_groups[group]
                if (distance := _hex_hash_distance(left, right)) is not None
            ]
            if distances:
                best = min(distances)
                if best <= maximum:
                    accepted_distances[group] = best
    except Exception as error:
        logging.warning("Не удалось сравнить визуальные отпечатки: %s", error)
        return {}
    return accepted_distances


def visual_hash_match_score(first_hash: str | None, second_hash: str | None,
                            first_visual_hashes: str | None = None,
                            second_visual_hashes: str | None = None):
    details = visual_hash_match_details(
        first_hash, second_hash, first_visual_hashes, second_visual_hashes,
    )
    return min(details.values()) if details else None


def _duplicate_candidate_rank(difference: int, crop_score, visual_difference):
    """Rank locally selected candidates without adding paid AI comparisons.

    A match supported by both the crop-resistant and rotation/background
    fingerprints is more trustworthy than a match produced by only one generic
    box feature.  The returned tuple keeps the existing three-candidate cap, but
    puts the most informative photos into that same paid request.
    """
    phash_matches = difference <= AI_PHASH_MAX_DISTANCE
    if crop_score:
        matching_regions, crop_distance = crop_score
        tier = 0 if visual_difference is not None else 1
        return (
            tier,
            -matching_regions,
            crop_distance,
            visual_difference if visual_difference is not None else 10_000,
            difference,
        )
    if visual_difference is not None:
        return (2 if phash_matches else 3, 0, 0, visual_difference, difference)
    if phash_matches:
        return (4, 0, 0, 10_000, difference)
    return None


def get_ai_candidates(photo_hash: str, crop_hash: str | None = None,
                      exclude_photo_id: int | None = None,
                      fallback_candidate_limit: int | None = None,
                      visual_hashes: str | None = None,
                      lookback_photos: int | None = None,
                      require_multi_signal: bool = False):
    new_hash = imagehash.hex_to_hash(photo_hash)
    lookback = max(1, int(lookback_photos or DUPLICATE_LOOKBACK_PHOTOS))
    with connect() as con:
        if exclude_photo_id is not None:
            photos = con.execute(
                "SELECT id, sent_at, image_hash, crop_hash, visual_hashes FROM photos "
                "WHERE id < ? AND image_hash IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (exclude_photo_id, lookback),
            ).fetchall()
        else:
            photos = con.execute(
                "SELECT id, sent_at, image_hash, crop_hash, visual_hashes FROM photos "
                "WHERE image_hash IS NOT NULL ORDER BY id DESC LIMIT ?",
                (lookback,),
            ).fetchall()
    candidates = []
    fallback_candidates = []
    for photo in photos:
        if exclude_photo_id is not None and photo["id"] == exclude_photo_id:
            continue
        difference = new_hash - imagehash.hex_to_hash(photo["image_hash"])
        crop_score = crop_hash_match_score(crop_hash, photo["crop_hash"])
        visual_details = visual_hash_match_details(
            photo_hash, photo["image_hash"], visual_hashes, photo["visual_hashes"],
        )
        visual_difference = min(visual_details.values()) if visual_details else None
        rank = _duplicate_candidate_rank(difference, crop_score, visual_difference)
        if require_multi_signal and rank is not None:
            # A strong crop match is already made from several regions. Without
            # it, demand two independent hash families, or whole-image pHash plus
            # another family. This rejects generic box-design similarities.
            strong_local_evidence = bool(crop_score) or len(visual_details) >= 2 or (
                difference <= AI_PHASH_MAX_DISTANCE and bool(visual_details)
            )
            if not strong_local_evidence:
                rank = None
        if rank is not None:
            candidates.append((photo, rank))
        else:
            # Removing/replacing the background can change the whole-image pHash
            # considerably. Keep a small number of the nearest photos from the
            # recent lookback window so the vision model still gets a chance to compare
            # the packaging itself. This remains bounded to one small AI batch.
            fallback_candidates.append((photo, difference))

    fallback_limit = (
        AI_FALLBACK_CANDIDATE_LIMIT
        if fallback_candidate_limit is None
        else max(0, int(fallback_candidate_limit))
    )
    if fallback_limit and len(candidates) < fallback_limit:
        missing = fallback_limit - len(candidates)
        for photo, difference in sorted(fallback_candidates, key=lambda item: item[1])[:missing]:
            rank = (3, 0, 0, difference)
            candidates.append((photo, rank))
    candidates.sort(key=lambda item: item[1])
    return [
        (dict(photo), rank)
        for photo, rank in candidates[:AI_DUPLICATE_MAX_CANDIDATES]
    ]


def _duplicate_name_key(value: str | None) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").casefold().replace("ё", "е"))


def get_metadata_duplicate_candidates(photo_date: str | None, mother_name: str | None,
                                      exclude_photo_id: int | None = None,
                                      photo_hash: str | None = None):
    """Compatibility helper that returns only exact name+date matches."""
    del photo_hash
    name_key = _duplicate_name_key(mother_name)
    if not photo_date or not name_key:
        return []
    with connect() as con:
        query = (
            "SELECT id, sent_at, photo_date, mother_name, image_hash FROM photos "
            "WHERE photo_date = ? AND mother_name IS NOT NULL "
            "AND TRIM(mother_name) != ''"
        )
        parameters = [photo_date]
        if exclude_photo_id is not None:
            query += " AND id < ?"
            parameters.append(exclude_photo_id)
        rows = con.execute(query + " ORDER BY id", parameters).fetchall()
    return [
        (dict(photo), (0, photo["id"]))
        for photo in rows
        if _duplicate_name_key(photo["mother_name"]) == name_key
    ][:AI_METADATA_CANDIDATE_LIMIT]


def load_ai_candidate_images(candidates):
    ids = [candidate["id"] for candidate, _ in candidates]
    if not ids:
        return []
    images = {}
    with connect() as con:
        placeholders = ",".join("?" for _ in ids)
        images.update({
            row["id"]: row
            for row in con.execute(
                f"SELECT id, sent_at, image, image_hash, crop_hash "
                f"FROM photos WHERE id IN ({placeholders})",
                ids,
            )
        })
    return [(images[candidate["id"]], rank) for candidate, rank in candidates if candidate["id"] in images]


def prepare_duplicate_comparison_image(image_bytes: bytes) -> bytes:
    """Put the full scene and an enlarged package crop into one AI image.

    This remains one image per photo and is capped below the normal reading
    resolution, so it improves visibility of small scratches and handwriting
    without increasing the number of Claude images or requests.
    """
    prepared = prepare_image_for_analysis(
        image_bytes, min(AI_READING_MAX_DIMENSION, 1280), quality=88,
    )
    try:
        with Image.open(BytesIO(prepared)) as opened:
            source = opened.convert("RGB")
        crops = _package_candidate_crops(source)
        if not crops:
            return prepared

        canvas = Image.new("RGB", (1280, 768), "white")
        full_panel = ImageOps.contain(source, (620, 748), Image.Resampling.LANCZOS)
        crop_panel = ImageOps.contain(crops[0], (620, 748), Image.Resampling.LANCZOS)
        canvas.paste(full_panel, ((630 - full_panel.width) // 2, (768 - full_panel.height) // 2))
        canvas.paste(
            crop_panel,
            (650 + (630 - crop_panel.width) // 2, (768 - crop_panel.height) // 2),
        )
        output = BytesIO()
        canvas.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()
    except Exception as error:
        logging.debug("Не удалось подготовить усиленный кадр для поиска копии: %s", error)
        return prepared


async def ai_confirms_any_duplicate(new_image: bytes, candidates, *, return_status=False) -> object:
    if not ai_client or not candidates:
        return (None, False) if return_status else None
    prompt = (
        "Первое изображение — новое фото. Остальные изображения — кандидаты №1, №2 и далее. "
        "Назови duplicate_index ТОЛЬКО если на новом фото находится та же самая физическая упаковка, "
        "снятая повторно, либо тот же исходный кадр/его обрезанная, сжатая или повёрнутая копия. "
        "Также это повтор, если на двух фотографиях точно совпадает полный QR/Data Matrix номер, даже когда "
        "сами коробки выглядят по-разному: один серийный QR нельзя использовать второй раз. Не угадывай QR "
        "по нескольким видимым цифрам — нужен полный одинаковый номер. "
        "Ракурс, масштаб, освещение, кадрирование и фон могут полностью различаться. "
        "Игнорируй фон за пределами упаковки: его могли удалить, размыть, перекрасить или заменить. "
        "Сравнивай саму упаковку: ТОЧНУЮ форму штрихов рукописных даты/ФИО, складки, надрывы, пятна, скотч, "
        "царапины и взаимное расположение уникальных деталей. Напечатанные логотип NUPPI, пингвин, цвета и "
        "типовой дизайн НЕ являются доказательством копии. Одинаковая дата сама по себе тоже не доказательство. "
        "Если совпадают уникальные следы упаковки, а отличается только фон, обрезка или надпись, это копия. "
        "Другой ракурс, фон, масштаб, поворот, свет, другая анкета, кровать, стол, ребёнок, руки или любые "
        "добавленные/убранные предметы относятся только к различиям сцены и НЕ являются конфликтами "
        "идентичности упаковки. Различие даты или ФИО может означать повторное "
        "использование той же коробки и не отменяет совпадение, если одинаковы QR, надрыв, складка, пятно, скотч "
        "или другие уникальные следы самой упаковки. Конфликтом считай только несовместимые физические детали коробки. "
        "При любом сомнении верни null. Ответь только JSON: "
        "{\"duplicate_index\": номер или null, \"confidence\": 0..1, "
        "\"same_source_pixels\": true/false, \"same_physical_package\": true/false, "
        "\"same_qr_code\": true/false, \"same_handwriting\": true/false, "
        "\"unique_matching_marks\": [\"конкретный уникальный признак\"], "
        "\"scene_differences\": [\"фон/ракурс/масштаб\"], "
        "\"identity_conflicts\": [\"только несовместимая физическая деталь упаковки\"]}."
    )
    prepared_new_image = await asyncio.to_thread(
        prepare_duplicate_comparison_image, new_image,
    )
    # Candidate selection is local and cheap, so only genuinely similar photos
    # reach this paid high-detail comparison.
    duplicate_detail = "high"
    content = [
        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(prepared_new_image).decode('ascii')}", "detail": duplicate_detail},
    ]
    for candidate, _ in candidates:
        prepared_candidate = await asyncio.to_thread(
            prepare_duplicate_comparison_image, candidate["image"],
        )
        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{base64.b64encode(prepared_candidate).decode('ascii')}",
            "detail": duplicate_detail,
        })
    content.append({
        "type": "input_text",
        "text": "Сравни изображения по инструкции и верни только требуемый JSON.",
    })
    try:
        response = await ai_client.responses.create(
            **ai_response_options(260, model=CLAUDE_DUPLICATE_MODEL),
            system=cached_system_prompt(prompt),
            input=[{"role": "user", "content": content}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        index = data.get("duplicate_index")
        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        same_qr_code = data.get("same_qr_code") is True
        conflicts = data.get("identity_conflicts", data.get("conflicts"))
        has_conflict = isinstance(conflicts, list) and any(
            isinstance(conflict, str) and conflict.strip() for conflict in conflicts
        )
        same_source_pixels = data.get("same_source_pixels") is True
        same_physical_package = data.get("same_physical_package") is True
        same_handwriting = data.get("same_handwriting") is True
        unique_marks = data.get("unique_matching_marks")
        unique_mark_count = len([
            mark for mark in unique_marks
            if isinstance(mark, str) and mark.strip()
        ]) if isinstance(unique_marks, list) else 0
        # Exact source pixels are decisive. A re-photographed physical box needs
        # handwriting plus a unique physical mark, or at least two concrete
        # physical marks. The printed NUPPI design alone is never evidence.
        evidence_is_strong = (
            same_qr_code
            or same_source_pixels
            or (
                same_physical_package
                and (
                    (same_handwriting and unique_mark_count >= 1)
                    or unique_mark_count >= 2
                )
            )
        )
        minimum_confidence = (
            AI_DUPLICATE_MIN_CONFIDENCE
            if same_qr_code or same_source_pixels
            else max(AI_DUPLICATE_MIN_CONFIDENCE, 0.97)
        )
        if (type(index) is int and 1 <= index <= len(candidates)
                and confidence >= minimum_confidence
                and evidence_is_strong
                and (same_qr_code or same_source_pixels or not has_conflict)):
            result = candidates[index - 1][0]
        else:
            result = None
        return (result, True) if return_status else result
    except Exception as error:
        logging.warning("Групповая ИИ-проверка фотографий не выполнена: %s", error)
        return (None, False) if return_status else None


async def ai_confirms_duplicate_across_all(
        new_image: bytes, candidates, checked_candidate_ids: set[int] | None = None) -> object:
    for start in range(0, len(candidates), AI_DUPLICATE_BATCH_SIZE):
        batch_metadata = candidates[start:start + AI_DUPLICATE_BATCH_SIZE]
        batch = await asyncio.to_thread(load_ai_candidate_images, batch_metadata)
        duplicate, completed = await ai_confirms_any_duplicate(
            new_image, batch, return_status=True,
        )
        if completed and checked_candidate_ids is not None:
            checked_candidate_ids.update(
                int(candidate["id"])
                for candidate, _ in batch
                if candidate["id"] is not None
            )
        if duplicate:
            return duplicate
    return None


def normalize_photo_date(text: str):
    candidate = (text or "").upper().replace(" ", "")

    def valid_date(year, month, day):
        if year < 100:
            year += 2000
        try:
            parsed = datetime(year, month, day).date()
            return parsed.isoformat() if PACKAGE_DATE_MIN_YEAR <= year <= PACKAGE_DATE_MAX_YEAR else None
        except ValueError:
            return None
    iso_match = re.search(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", candidate)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        return valid_date(year, month, day)
    match = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?!\d)", candidate)
    if match:
        day, month, year = map(int, match.groups())
        return valid_date(year, month, day)
    roman_months = {
        "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6 ,
        "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12,
    }
    roman_match = re.search(r"(\d{1,2})[./-]([IVX]{1,4})[./-](\d{2,4})", candidate)
    if roman_match:
        day = int(roman_match.group(1))
        month = roman_months.get(roman_match.group(2))
        year = int(roman_match.group(3))
        if year < 100:
            year += 2000
        if month:
            return valid_date(year, month, day)
    compact_match = re.fullmatch(r"(\d{2})(\d{2})(\d{2}|\d{4})", candidate)
    if compact_match:
        day, month, year = map(int, compact_match.groups())
        return valid_date(year, month, day)
    for date_format in (
        "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y",
        "%d.%m.%y", "%d/%m/%y", "%d-%m-%y",
    ):
        try:
            parsed = datetime.strptime(candidate, date_format).date()
            return valid_date(parsed.year, parsed.month, parsed.day)
        except ValueError:
            continue
    return None


def normalize_mother_name(value: str | None):
    name = re.sub(r"\s+", " ", (value or "").strip())
    # Keep a final dot because it may belong to a one-letter initial (М.Д.).
    name = re.sub(r"^[,.;:№#\-]+|[,;:]+$", "", name).strip()
    if (not name or any(character.isdigit() for character in name)
            or not re.fullmatch(r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʼ'’.\- ]+", name)):
        return None
    words = re.findall(
        r"[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]+(?:[ʼ'’-][A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]+)*\.?",
        name,
    )
    if not words or not any(
        len(re.sub(r"[^A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ]", "", word)) >= 2
        for word in words
    ):
        return None
    return " ".join(words[:5])


def normalize_ai_confidence(value, fallback: float = 0.0) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def normalize_package_number(value: str):
    text = (value or "").strip().upper()
    if re.fullmatch(r"\d{1,3}", text):
        return text
    if not re.fullmatch(r"[IVXLCDM]+", text):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(text):
        current = values[character]
        total = total - current if current < previous else total + current
        previous = max(previous, current)
    return str(total) if 1 <= total <= 999 else None


def metadata_image_parts(image_bytes: bytes):
    prepared_bytes = prepare_image_for_analysis(
        image_bytes, max_dimension=AI_READING_MAX_DIMENSION, quality=92,
    )
    with Image.open(BytesIO(prepared_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        note = image.copy()
        scale = min(3, max(1, IMAGE_PROCESSING_MAX_DIMENSION // max(width, height)))
        note = note.resize((note.width * scale, note.height * scale), Image.Resampling.LANCZOS)
        color_note = ImageEnhance.Contrast(note).enhance(1.8)
        color_note = ImageEnhance.Sharpness(color_note).enhance(2.4).filter(
            ImageFilter.UnsharpMask(radius=2, percent=180, threshold=1)
        )
        gray_note = ImageOps.autocontrast(note.convert("L"), cutoff=1)
        gray_note = ImageEnhance.Contrast(gray_note).enhance(2.1).filter(
            ImageFilter.UnsharpMask(radius=2, percent=200, threshold=1)
        )
        color_result, gray_result = BytesIO(), BytesIO()
        color_note.save(color_result, format="JPEG", quality=92)
        gray_note.save(gray_result, format="JPEG", quality=92)
        original_result = BytesIO()
        image.save(original_result, format="JPEG", quality=92)
        return original_result.getvalue(), color_result.getvalue(), gray_result.getvalue()


def metadata_focus_images(image_bytes: bytes):
    """Enlarge overlapping quadrants so tiny handwriting is not lost in a full photo."""
    with Image.open(BytesIO(image_bytes)) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    source.thumbnail(
        (AI_READING_MAX_DIMENSION * 2, AI_READING_MAX_DIMENSION * 2),
        Image.Resampling.LANCZOS,
    )
    width, height = source.size
    crop_width = max(1, round(width * 0.65))
    crop_height = max(1, round(height * 0.65))
    images = []
    for y in sorted({0, max(0, height - crop_height)}):
        for x in sorted({0, max(0, width - crop_width)}):
            crop = source.crop((x, y, x + crop_width, y + crop_height))
            scale = min(2.5, AI_READING_MAX_DIMENSION / max(1, max(crop.size)))
            if scale > 1.05:
                crop = crop.resize(
                    (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            crop = ImageEnhance.Contrast(crop).enhance(1.35)
            crop = ImageEnhance.Sharpness(crop).enhance(1.8).filter(
                ImageFilter.UnsharpMask(radius=1, percent=130, threshold=2)
            )
            output = BytesIO()
            crop.save(output, format="JPEG", quality=92, optimize=True)
            images.append(output.getvalue())
    return images


async def retry_uncertain_photo_metadata(image_bytes: bytes, first_date: str | None,
                                         first_name: str | None, first_qr: str | None):
    """Read handwritten fields from focused crops with the economical text model."""
    prompt = (
        "На четырёх изображениях показаны перекрывающиеся увеличенные части ОДНОЙ фотографии упаковки. "
        "Найди рукописную дату и ФИО/фамилию роженицы, даже если надпись повёрнута или находится у края. "
        "Читай каждый символ, не используй напечатанный срок годности. Дата идёт как день.месяц.год; "
        "римский месяц IV означает 04, VIII означает 08. Также найди QR/Data Matrix и перепиши только "
        "полный напечатанный рядом региональный номер: две цифры, дефис, десять цифр. Не угадывай цифры. "
        f"Первый проход дал дату={first_date or 'null'}, ФИО={first_name or 'null'}, QR={first_qr or 'null'}; "
        "он может ошибаться, поэтому проверь самостоятельно. Ответь только JSON без Markdown: "
        "{\"package_date\":\"YYYY-MM-DD или null\",\"package_date_confidence\":0..1,"
        "\"mother_name\":\"видимое ФИО/фамилия или null\",\"mother_name_confidence\":0..1,"
        "\"qr_data\":\"NN-NNNNNNNNNN или null\",\"qr_data_confidence\":0..1,"
        "\"code_visible\":true или false}."
    )
    try:
        focus_images = await asyncio.to_thread(metadata_focus_images, image_bytes)
        response = await ai_client.responses.create(
            **ai_response_options(300, model=CLAUDE_METADATA_MODEL),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                *[
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}",
                        "detail": "high",
                    }
                    for image in focus_images
                ],
            ]}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        qr_data = canonical_qr_data(str(data.get("qr_data") or ""))
        if qr_data and not QR_REGION_CODE_PATTERN.fullmatch(qr_data):
            qr_data = None
        return {
            "package_date": normalize_photo_date(str(data.get("package_date") or "")),
            "package_date_confidence": normalize_ai_confidence(data.get("package_date_confidence")),
            "mother_name": normalize_mother_name(data.get("mother_name")),
            "mother_name_confidence": normalize_ai_confidence(data.get("mother_name_confidence")),
            "qr_data": qr_data,
            "qr_data_confidence": normalize_ai_confidence(data.get("qr_data_confidence")),
            "code_visible": (
                True if data.get("code_visible") is True
                else False if data.get("code_visible") is False
                else None
            ),
        }
    except Exception as error:
        logging.warning("Точная повторная проверка даты, ФИО и QR не выполнена: %s", error)
        return None


async def _request_qr_recovery(focus_images, model: str):
    """Run one strict QR pass with the selected Claude model."""
    prompt = (
        "На всех изображениях показаны увеличенные перекрывающиеся части ОДНОЙ фотографии упаковки. "
        "Найди каждый QR-код или Data Matrix. Сначала реши, виден ли хотя бы один код. Затем попытайся "
        "прочитать полный региональный номер ИЗ СОДЕРЖИМОГО КОДА; если символ не декодируется, разрешено "
        "переписать только полный номер, напечатанный непосредственно рядом с этим же кодом. Формат строго "
        "NN-NNNNNNNNNN: две цифры, дефис и ровно десять цифр. Сверь каждую цифру на нескольких кадрах. "
        "Ничего не угадывай и не дополняй по диапазону региона. code_obstructed=true только если ручка, "
        "маркер, царапина, наклейка или предмет заходит прямо на чёрно-белые элементы кода. Отдельно поставь "
        "code_suspicious=true, если QR/Data Matrix выглядит нарисованным вручную, сгенерированным ИИ, "
        "вклеенным/заменённым цифровым монтажом, имеет невозможную сетку модулей или неестественные края. "
        "Размытие, блик, "
        "наклон и мелкий размер — это не перекрытие. Если код виден, но хотя бы одна цифра ненадёжна, "
        "qr_data должен быть null, а code_visible=true. Ответь только JSON без Markdown: "
        "{\"code_visible\":true или false,\"code_obstructed\":true или false,"
        "\"code_suspicious\":true или false,"
        "\"qr_data\":\"NN-NNNNNNNNNN или null\",\"qr_data_confidence\":0..1,"
        "\"unreadable_reason\":\"obstructed|suspected_fake|blurred|glare|cropped|too_small|not_visible|none\"}."
    )
    try:
        response = await ai_client.responses.create(
            **ai_response_options(220, model=model),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                *[
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}",
                        "detail": "high",
                    }
                    for image in focus_images
                ],
            ]}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        qr_data = canonical_qr_data(str(data.get("qr_data") or ""))
        if qr_data and not QR_REGION_CODE_PATTERN.fullmatch(qr_data):
            qr_data = None
        confidence = normalize_ai_confidence(data.get("qr_data_confidence"))
        code_obstructed = data.get("code_obstructed") is True
        code_suspicious = data.get("code_suspicious") is True
        reason = str(data.get("unreadable_reason") or "none").strip().lower()
        if reason not in {
            "obstructed", "suspected_fake", "blurred", "glare", "cropped",
            "too_small", "not_visible", "none",
        }:
            reason = "none"
        if reason == "not_visible":
            code_visible = False
            code_obstructed = False
            code_suspicious = False
        else:
            code_visible = data.get("code_visible") is True or code_obstructed
        if not code_visible or confidence < QR_RECOVERY_MIN_CONFIDENCE:
            qr_data = None
        return {
            "qr_data": qr_data,
            "qr_data_confidence": confidence,
            "code_visible": code_visible,
            "code_obstructed": code_obstructed,
            "code_suspicious": code_suspicious,
            "unreadable_reason": reason,
        }
    except Exception as error:
        logging.warning("Проверка QR/Data Matrix моделью %s не выполнена: %s", model, error)
        return None


async def recover_missing_qr_data(image_bytes: bytes, code_visible_hint: bool | None = None):
    """Read a difficult QR without trusting a single model's guessed number.

    A small or blurred symbol may look visible while its payload is impossible
    to decode.  In that situation both models must independently return the
    exact same full value before it is stored or used as duplicate evidence.
    Missing QR remains allowed and is never turned into an error by this pass.
    """
    if not ai_client or not QR_DEDICATED_RECOVERY_ENABLED:
        return None
    focus_images = await asyncio.to_thread(metadata_focus_images, image_bytes)
    screening = await _request_qr_recovery(focus_images, CLAUDE_QR_SCREENING_MODEL)
    if not screening:
        return None
    if not screening["code_visible"] and code_visible_hint is not True:
        return screening

    verified = await _request_qr_recovery(focus_images, CLAUDE_VISION_MODEL)
    if not verified:
        return {
            **screening,
            "qr_data": None,
            "qr_data_confidence": 0.0,
        }

    code_visible = verified["code_visible"] and (
        screening["code_visible"] or code_visible_hint is True
    )
    screening_qr = screening["qr_data"]
    verified_qr = verified["qr_data"]
    qr_data = None
    confidence = 0.0
    if (
        code_visible
        and screening_qr
        and screening_qr == verified_qr
        and screening["qr_data_confidence"] >= QR_RECOVERY_MIN_CONFIDENCE
        and verified["qr_data_confidence"] >= QR_RECOVERY_MIN_CONFIDENCE
    ):
        qr_data = verified_qr
        confidence = min(
            screening["qr_data_confidence"], verified["qr_data_confidence"],
        )

    return {
        "qr_data": qr_data,
        "qr_data_confidence": confidence,
        "code_visible": code_visible,
        "code_obstructed": (
            code_visible
            and screening["code_obstructed"]
            and verified["code_obstructed"]
        ),
        "code_suspicious": (
            screening.get("code_suspicious", False)
            or verified.get("code_suspicious", False)
        ),
        "unreadable_reason": (
            verified["unreadable_reason"]
            if verified["unreadable_reason"] != "none"
            else screening["unreadable_reason"]
        ),
    }


@lru_cache(maxsize=1)
def load_reference_packaging():
    try:
        return tuple(
            prepare_image_for_analysis(path.read_bytes(), max_dimension=1024, quality=85)
            for path in REFERENCE_PACKAGING_FILES
        )
    except OSError as error:
        logging.error("Не удалось прочитать эталонные упаковки: %s", error)
        return []


async def extract_handwritten_fields_strict(original: bytes, handwritten_note: bytes, contrast_note: bytes):
    prompt = (
        "Нужно распознать ТОЛЬКО рукописную дату и ФИО на коробке. Второе изображение — усиленный "
        "цветной кадр, третье — тот же кадр с усиленным контрастом. Надпись может быть на любой стороне "
        "коробки, поэтому сначала найди её, затем прочитай КАЖДУЮ цифру даты слева направо: день, месяц, год. "
        "Сверь цифры и буквы между обоими вариантами. Не используй напечатанный срок годности. "
        "Дата может быть написана как 27.IV.2026: римский IV означает месяц 04. "
        "Верни строго JSON без Markdown: {\"package_date\": \"YYYY-MM-DD или null\", "
        "\"mother_name\": \"видимое ФИО/фамилия или null\"}. Не угадывай неразборчивые символы."
    )
    try:
        response = await ai_client.responses.create(
            **ai_response_options(220),
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(original).decode('ascii')}", "detail": "high"},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(handwritten_note).decode('ascii')}", "detail": "high"},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(contrast_note).decode('ascii')}", "detail": "high"},
                ],
            }],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        return normalize_photo_date(str(data.get("package_date") or "")), normalize_mother_name(data.get("mother_name"))
    except Exception as error:
        logging.warning("Повторное распознавание даты не выполнено: %s", error)
        return None, None


async def verify_package_date_digits(original: bytes, handwritten_note: bytes, contrast_note: bytes):
    prompt = (
        "На фото есть РУКОПИСНАЯ дата на упаковке. Найди только её и перепроверь цифры. "
        "Дата всегда записана в порядке ДЕНЬ.МЕСЯЦ.ГОД, например 02.09.2026. "
        "Никогда не меняй местами день и год. Последняя группа — год из 4 цифр; "
        "02.09.2026 нельзя превращать в 26.09.2002. Месяц может быть римским: "
        "IV = 04. Не считывай напечатанный срок годности. Если хотя бы одна группа "
        "цифр не читается уверенно, верни null. Ответь строго JSON без Markdown: "
        "{\"day\": число или null, \"month\": число 1-12 или null, "
        "\"year\": число из 4 цифр или null, \"package_date\": \"YYYY-MM-DD или null\"}."
    )
    try:
        response = await ai_client.responses.create(
            **ai_response_options(220),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(original).decode('ascii')}", "detail": "high"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(handwritten_note).decode('ascii')}", "detail": "high"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(contrast_note).decode('ascii')}", "detail": "high"},
            ]}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        day, month, year = data.get("day"), data.get("month"), data.get("year")
        if all(isinstance(value, int) for value in (day, month, year)):
            return normalize_photo_date(f"{day:02d}.{month:02d}.{year:04d}")
        return normalize_photo_date(str(data.get("package_date") or ""))
    except Exception as error:
        logging.warning("Проверка цифр даты не выполнена: %s", error)
        return None


async def resolve_handwritten_fields(original: bytes, handwritten_note: bytes, contrast_note: bytes,
                                     first_date: str | None, first_name: str | None,
                                     second_date: str | None, second_name: str | None):
    prompt = (
        "Это финальная проверка рукописной даты и ФИО на коробке. Первое фото — вся коробка, второе — "
        "усиленный цветной кадр, третье — контрастный кадр. Прочитай надпись самостоятельно, "
        "посимвольно. Сначала проверь порядок: день.месяц.год, затем каждую букву ФИО. Не используй срок "
        "годности. Ни один вариант ниже не является правильным автоматически: "
        f"первое чтение: дата={first_date or 'null'}, ФИО={first_name or 'null'}; "
        f"второе чтение: дата={second_date or 'null'}, ФИО={second_name or 'null'}. "
        "Дата с римским месяцем 27.IV.2026 означает 2026-04-27. Верни только строго JSON без Markdown: "
        "{\"package_date\": \"YYYY-MM-DD или null\", \"mother_name\": \"видимое ФИО/фамилия или null\"}. "
        "Не угадывай неразборчивые символы."
    )
    try:
        response = await ai_client.responses.create(
            **ai_response_options(220),
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(original).decode('ascii')}", "detail": "high"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(handwritten_note).decode('ascii')}", "detail": "high"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(contrast_note).decode('ascii')}", "detail": "high"},
            ]}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        return normalize_photo_date(str(data.get("package_date") or "")), normalize_mother_name(data.get("mother_name"))
    except Exception as error:
        logging.warning("Финальная сверка даты и ФИО не выполнена: %s", error)
        return None, None


async def extract_photo_metadata(image_bytes: bytes):
    """Inspect product/QR signals without reading handwritten date or mother name."""
    if not ai_client:
        return (None, None, None, None, False, False, False, False,
                None, None, None, "no_api_key")
    reference_images = load_reference_packaging()
    if len(reference_images) != 2:
        return (None, None, None, None, False, False, False, False,
                None, None, None, "reference_missing")
    prompt = (
        "Первые два изображения — эталоны разрешённых упаковок. Следующие три изображения — "
        "одно проверяемое фото: полный кадр, усиленный цветной вариант и контрастный вариант. "
        "НЕ распознавай и НЕ возвращай рукописную дату или ФИО роженицы: эти поля теперь заполняет "
        "только оператор. Сосредоточься на QR/Data Matrix и проверке упаковки.\n"
        "Сравни дизайн коробки с эталонами. is_reference_design=true только для жёлто-фиолетовой "
        "упаковки NUPPI с соответствующим эталону расположением логотипа и пингвина. "
        "is_nuppi_1=true только если явно видны NUPPI/NUPPI GOLD и номер 1 либо I. "
        "Римский или арабский номер упаковки верни как package_number.\n"
        "Очень внимательно найди каждый QR-код или Data Matrix. Изучи полный кадр, цветное усиление "
        "и контрастный вариант, мысленно выровняй перспективу и увеличь область кода. Если рядом с кодом "
        "напечатан полный региональный номер, перепиши его посимвольно. Формат строго NN-NNNNNNNNNN: "
        "две цифры, дефис, ровно десять цифр. Сверь каждую цифру между вариантами изображения. "
        "Не угадывай пропущенные цифры и не дополняй номер по диапазону региона. Если хотя бы одна цифра "
        "ненадёжна, qr_data=null, но is_code_visible=true.\n"
        "is_code_obstructed=true только если ручка, маркер, царапина, наклейка или предмет заходит "
        "непосредственно на чёрно-белые элементы кода. Блик, тень, перспектива и размытие сами по себе "
        "не являются перекрытием. Нарисованный, сгенерированный, цифровым монтажом заменённый код или "
        "невозможная сетка модулей означает visual_status=possible_fake_qr и is_edited=true. "
        "Явный иной монтаж означает possible_fake, признаки генерации всего изображения — "
        "possible_ai_generated, иначе clean.\n"
        "Ответь строго одним JSON без Markdown: "
        "{\"package_number\":\"арабская цифра или null\","
        "\"qr_data\":\"NN-NNNNNNNNNN или null\",\"qr_data_confidence\":0..1,"
        "\"is_code_visible\":true или false,\"is_nuppi_1\":true или false,"
        "\"is_reference_design\":true или false,\"is_edited\":true или false,"
        "\"is_code_obstructed\":true или false,"
        "\"visual_status\":\"clean|possible_fake|possible_fake_qr|possible_ai_generated\","
        "\"visual_reason\":\"короткая причина по-русски\"}."
    )
    try:
        original, enhanced, contrast = metadata_image_parts(image_bytes)
        reference_content = [
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(reference_images[0]).decode('ascii')}", "detail": "low"},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(reference_images[1]).decode('ascii')}", "detail": "low"},
        ]
        if CLAUDE_PROMPT_CACHE_ENABLED:
            reference_content[-1]["cache_control"] = {
                "type": "ephemeral",
                "ttl": CLAUDE_PROMPT_CACHE_TTL,
            }
        photo_content = [
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(original).decode('ascii')}", "detail": "high"},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(enhanced).decode('ascii')}", "detail": "high"},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(contrast).decode('ascii')}", "detail": "high"},
        ]
        response = await ai_client.responses.create(
            **ai_response_options(model=CLAUDE_METADATA_MODEL),
            system=prompt,
            input=[{"role": "user", "content": [
                *reference_content,
                *photo_content,
                {"type": "input_text", "text": "Проверь QR и упаковку; верни только JSON."},
            ]}],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        ai_qr_data = canonical_qr_data(str(data.get("qr_data") or ""))
        if ai_qr_data and not QR_REGION_CODE_PATTERN.fullmatch(ai_qr_data):
            ai_qr_data = None
        package_number = normalize_package_number(str(data.get("package_number") or ""))
        is_nuppi_1 = data.get("is_nuppi_1") is True and package_number == "1"
        is_reference_design = data.get("is_reference_design") is True
        is_edited = data.get("is_edited") is True
        is_code_obstructed = data.get("is_code_obstructed") is True
        code_visible = (
            True if data.get("is_code_visible") is True
            else False if data.get("is_code_visible") is False
            else None
        )
        statuses = {
            "clean": "Нет явных признаков",
            "possible_fake": "Возможная подделка",
            "possible_fake_qr": "Подозрительный QR/Data Matrix",
            "possible_ai_generated": "Возможное ИИ-изображение",
        }
        visual_status = statuses.get(
            str(data.get("visual_status") or "").strip().lower(), "Не проверено",
        )
        visual_reason = str(
            data.get("visual_reason") or "Нужна ручная проверка оператором."
        ).strip()[:300]
        if is_edited and visual_status == "Нет явных признаков":
            visual_status = "Возможная подделка"
            visual_reason = "ИИ заметил возможные признаки цифрового редактирования."
        # Date and mother name are intentionally always empty. Only an operator
        # may enter them on the site.
        return (None, None, package_number, ai_qr_data, is_nuppi_1,
                is_reference_design, is_edited, is_code_obstructed, code_visible,
                visual_status, visual_reason, "ok")
    except Exception as error:
        logging.warning("Не удалось проверить QR и упаковку на фото: %s", error)
        error_name = type(error).__name__
        error_statuses = {
            "AuthenticationError": "authentication_error",
            "RateLimitError": "rate_limit_error",
            "BadRequestError": "request_error",
            "NotFoundError": "request_error",
            "APIConnectionError": "connection_error",
            "APITimeoutError": "connection_error",
        }
        return (None, None, None, None, False, False, False, False,
                None, None, None, error_statuses.get(error_name, "unknown_error"))


async def ai_operator_assessment(image_bytes: bytes):
    if not ai_client:
        return "Не проверено", "ИИ не подключён."
    prompt = (
        "Оцени это изображение только как предварительную визуальную проверку для оператора. "
        "Нельзя достоверно установить подделку или происхождение изображения только по одной фотографии. "
        "Если видны явные признаки монтажа/подмены — possible_fake. Если видны характерные признаки "
        "генерации нейросетью — possible_ai_generated. Во всех остальных случаях clean. "
        "Ответь строго JSON без Markdown: "
        "{\"status\": \"clean|possible_fake|possible_ai_generated\", "
        "\"reason\": \"короткая причина по-русски\"}."
    )
    try:
        prepared_image = await asyncio.to_thread(prepare_image_for_analysis, image_bytes)
        response = await ai_client.responses.create(
            **ai_response_options(220),
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{base64.b64encode(prepared_image).decode('ascii')}", "detail": "high"},
                ],
            }],
        )
        raw_text = response.output_text.strip()
        json_text = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(json_text.group(0) if json_text else raw_text)
        statuses = {
            "clean": "Нет явных признаков",
            "possible_fake": "Возможная подделка",
            "possible_ai_generated": "Возможное ИИ-изображение",
        }
        status = statuses.get(str(data.get("status") or "").strip().lower(), "Не проверено")
        reason = str(data.get("reason") or "Нужна ручная проверка оператором.").strip()[:300]
        return status, reason
    except Exception as error:
        logging.warning("ИИ-оценка для оператора не выполнена: %s", error)
        return "Не проверено", "ИИ не смог выполнить оценку; нужна ручная проверка."


async def sightengine_ai_assessment(image_bytes: bytes):
    if not SIGHTENGINE_API_USER or not SIGHTENGINE_API_SECRET:
        return None, None, None

    media_bytes = await asyncio.to_thread(prepare_image_for_analysis, image_bytes)

    def send_request():
        response = requests.post(
            "https://api.sightengine.com/1.0/check.json",
            data={
                "models": "genai",
                "api_user": SIGHTENGINE_API_USER,
                "api_secret": SIGHTENGINE_API_SECRET,
            },
            files={"media": ("telegram_photo.jpg", media_bytes, "image/jpeg")},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    try:
        data = await asyncio.to_thread(send_request)
        if data.get("status") != "success":
            raise RuntimeError(data.get("error", {}).get("description", "неизвестная ошибка Sightengine"))
        score = float(data.get("type", {}).get("ai_generated", 0))
        percent = round(score * 100, 1)
        if score >= SIGHTENGINE_AI_THRESHOLD:
            return "Возможное ИИ-изображение", f"Sightengine: вероятность ИИ-генерации {percent}%.", score
        return "Нет явных признаков", f"Sightengine: вероятность ИИ-генерации {percent}%.", score
    except Exception as error:
        logging.warning("Sightengine-проверка не выполнена: %s", error)
        return "Не проверено", "Sightengine не смог выполнить проверку; нужна ручная проверка.", None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id, include_inactive=True)
    if not user or not user["is_active"]:
        await update.message.reply_text("Доступ к отправке фото не настроен администратором сайта.")
    elif user["role"] == "Оператор":
        await update.message.reply_text(
            "Готово. Вы можете получить отчёт за последние 10 дней, отправить Excel-файл "
            "для подсчёта фотографий по врачам или выбрать другое действие.",
            reply_markup=ReplyKeyboardMarkup(
                [[MENU_OPERATOR_TEN_DAY_REPORT], [MENU_EXCEL_DOCTOR_REPORT], [MENU_PROFILE]],
                resize_keyboard=True,
            ),
        )
    else:
        await update.message.reply_text(
            "Готово. Отправляйте фотографии или посмотрите свой отчёт кнопкой ниже.",
            reply_markup=medic_keyboard(),
        )


async def profile(update, context):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Нет доступа. Администратор должен добавить вас через сайт.")
        return
    await update.message.reply_text(profile_message(user))


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Нет доступа. Администратор должен добавить вас через сайт.")
        return
    buttons = [[MENU_PROFILE]]
    if user["role"] == "Медик":
        buttons.insert(0, [MENU_SEND_PHOTO])
        buttons.insert(1, [MENU_PHOTO_REPORT])
        buttons.insert(2, [MENU_VIEW_HOSPITAL_PHOTOS])
        buttons.insert(3, [MENU_OPERATOR_COMMENTS])
    elif user["role"] == "Оператор":
        buttons.insert(0, [MENU_EXCEL_DOCTOR_REPORT])
        buttons.insert(0, [MENU_OPERATOR_TEN_DAY_REPORT])
    await update.message.reply_text(
        "Выберите действие:", reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )


def medic_keyboard():
    return ReplyKeyboardMarkup(
        [[MENU_SEND_PHOTO], [MENU_PHOTO_REPORT], [MENU_VIEW_HOSPITAL_PHOTOS],
         [MENU_OPERATOR_COMMENTS], [MENU_PROFILE]],
        resize_keyboard=True,
    )


def photo_batch_keyboard():
    return ReplyKeyboardMarkup(
        [[MENU_SEND_MORE_PHOTO], [MENU_FINISH_PHOTOS]], resize_keyboard=True,
    )


async def reply_text_with_retry(message, text, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            return await message.reply_text(text, **kwargs)
        except TimedOut as error:
            # Telegram may have accepted the message even though the response
            # timed out. Retrying here creates the repeated replies users saw.
            logging.warning(
                "Telegram не подтвердил отправку сообщения; повтор отключён, "
                "чтобы не создать дубль: %s",
                error,
            )
            return None
        except NetworkError as error:
            if attempt + 1 >= attempts:
                logging.warning(
                    "Не удалось отправить сообщение в Telegram после %s попыток: %s",
                    attempts, error,
                )
                return None
            await asyncio.sleep(attempt + 1)


async def reply_photo_with_retry(message, photo, attempts=3, **kwargs):
    for attempt in range(attempts):
        try:
            return await message.reply_photo(photo=photo, **kwargs)
        except BadRequest as error:
            # A Telegram file_id can become unusable after a bot migration. The
            # caller can retry with the image stored in the shared database.
            logging.warning("Telegram не принял сохранённый файл фотографии: %s", error)
            return None
        except TimedOut as error:
            logging.warning(
                "Telegram не подтвердил отправку фотографии; повтор отключён, "
                "чтобы не создать дубль: %s",
                error,
            )
            return None
        except NetworkError as error:
            if attempt + 1 >= attempts:
                logging.warning(
                    "Не удалось отправить фотографию в Telegram после %s попыток: %s",
                    attempts, error,
                )
                return None
            await asyncio.sleep(attempt + 1)


def bot_is_open(now: datetime | None = None) -> bool:
    """Return whether the bot accepts messages at the Tashkent local time."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(TASHKENT_TIMEZONE)
    return BOT_WORK_START_HOUR <= local_now.hour < BOT_WORK_END_HOUR


async def enforce_working_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_is_open():
        return
    if update.effective_message:
        await reply_text_with_retry(
            update.effective_message,
            "⏰ Бот работает ежедневно с 07:00 до 19:00. "
            "Пожалуйста, отправьте сообщение в рабочее время.",
        )
    raise ApplicationHandlerStop


def medic_photo_counts(telegram_id: int, now: datetime | None = None):
    local_now = (now or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=5)))
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    tomorrow = today + timedelta(days=1)
    with connect() as con:
        return dict(con.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(julianday(sent_at) >= julianday(?) AND julianday(sent_at) < julianday(?)), 0) AS today,
                      COALESCE(SUM(julianday(sent_at) >= julianday(?) AND julianday(sent_at) < julianday(?)), 0) AS month
               FROM photos WHERE sender_telegram_id = ?""",
            (today.isoformat(), tomorrow.isoformat(), month.isoformat(), tomorrow.isoformat(), telegram_id),
        ).fetchone())


def medic_hospital_photos(
    telegram_id: int,
    hospital: str,
    offset: int = 0,
    limit: int = MEDIC_PHOTO_PAGE_SIZE,
):
    """Return only this medic's photos for one assigned maternity hospital."""
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), MEDIC_PHOTO_PAGE_SIZE))
    with connect() as con:
        parameters = (telegram_id, hospital)
        total = con.execute(
            """SELECT COUNT(*)
               FROM photos AS p
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.sender_telegram_id = ?
                 AND COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                              NULLIF(TRIM(sender.maternity_hospital), '')) = ?""",
            parameters,
        ).fetchone()[0]
        rows = con.execute(
            """SELECT p.id, p.telegram_file_id, p.image, p.doctor_name, p.sent_at,
                      p.review_status, p.code_status, p.qr_data, p.photo_date,
                      p.package_number, p.mother_name,
                      checks.decision, checks.manual_decision, checks.manual_reason,
                      COALESCE(NULLIF(TRIM(sender.city), ''), 'Город не указан') AS city
               FROM photos AS p
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
               WHERE p.sender_telegram_id = ?
                 AND COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                              NULLIF(TRIM(sender.maternity_hospital), '')) = ?
               ORDER BY julianday(p.sent_at) DESC, p.id DESC
               LIMIT ? OFFSET ?""",
            (*parameters, safe_limit, safe_offset),
        ).fetchall()
    return int(total), [dict(row) for row in rows]


def format_bot_photo_time(value) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TASHKENT_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(value)


def medic_photo_caption(record: dict, hospital: str | None = None) -> str:
    decision = record.get("manual_decision") or DECISION_REVIEW
    decision_label = DECISION_LABELS.get(decision, DECISION_LABELS[DECISION_REVIEW])
    city = record.get("city") or "Город не указан"
    hospital = hospital or record.get("maternity_hospital") or "Роддом не указан"
    lines = [
        f"🏙 Город: {city}",
        f"🏥 Роддом: {hospital}",
        f"👩‍⚕️ Врач: {record.get('doctor_name') or 'Не указан'}",
        f"🕒 Отправлено: {format_bot_photo_time(record.get('sent_at'))}",
        f"🔎 Итог: {decision_label}",
    ]
    if record.get("qr_data"):
        lines.append(f"QR/Data Matrix: {record['qr_data']}")
    if record.get("photo_date"):
        lines.append(f"Дата на упаковке: {record['photo_date']}")
    if record.get("mother_name"):
        lines.append(f"Роженица: {record['mother_name']}")
    if record.get("manual_reason"):
        lines.append(f"💬 Комментарий оператора: {record['manual_reason']}")
    return "\n".join(lines)


async def hospital_photos_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        await update.message.reply_text("Просмотр фотографий доступен только активным медикам.")
        return ConversationHandler.END
    hospitals = get_employee_hospitals(user["telegram_id"], user["city"])
    if not hospitals:
        await update.message.reply_text("У вас пока нет назначенных роддомов.")
        return ConversationHandler.END
    context.user_data["view_photo_hospitals"] = hospitals
    context.user_data.pop("view_photo_hospital", None)
    context.user_data.pop("view_photo_offset", None)
    await update.message.reply_text(
        "Выберите роддом. Бот покажет только ваши фотографии из выбранного роддома.",
        reply_markup=ReplyKeyboardMarkup(
            [[hospital] for hospital in hospitals],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return VIEW_PHOTO_HOSPITAL


async def hospital_photos_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    hospitals = context.user_data.get("view_photo_hospitals", [])
    if choice == MENU_VIEW_PHOTOS_DONE:
        context.user_data.pop("view_photo_hospitals", None)
        context.user_data.pop("view_photo_hospital", None)
        context.user_data.pop("view_photo_offset", None)
        await update.message.reply_text("Просмотр завершён.", reply_markup=medic_keyboard())
        return ConversationHandler.END
    if choice == MENU_VIEW_ANOTHER_HOSPITAL:
        return await hospital_photos_start(update, context)
    if choice in {MENU_VIEW_NEXT_PHOTOS, MENU_VIEW_PREVIOUS_PHOTOS}:
        hospital = context.user_data.get("view_photo_hospital")
        if hospital not in hospitals:
            return await hospital_photos_start(update, context)
        offset = int(context.user_data.get("view_photo_offset", 0))
        offset += MEDIC_PHOTO_PAGE_SIZE if choice == MENU_VIEW_NEXT_PHOTOS else -MEDIC_PHOTO_PAGE_SIZE
        offset = max(0, offset)
    elif choice in hospitals:
        hospital = choice
        offset = 0
    else:
        await update.message.reply_text("Выберите роддом с помощью кнопки.")
        return VIEW_PHOTO_HOSPITAL

    total, records = await asyncio.to_thread(
        medic_hospital_photos,
        update.effective_user.id,
        hospital,
        offset,
    )
    if not records:
        await update.message.reply_text(
            f"В роддоме «{hospital}» на этой странице фотографий нет.",
            reply_markup=ReplyKeyboardMarkup(
                [[MENU_VIEW_ANOTHER_HOSPITAL], [MENU_VIEW_PHOTOS_DONE]],
                resize_keyboard=True,
            ),
        )
        return VIEW_PHOTO_HOSPITAL

    context.user_data["view_photo_hospital"] = hospital
    context.user_data["view_photo_offset"] = offset
    await update.message.reply_text(
        f"В роддоме «{hospital}» сохранено {total} фотографий. "
        f"Показываю {offset + 1}–{offset + len(records)}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    delivered = 0
    for record in records:
        caption = medic_photo_caption(record, hospital)
        sent = None
        if record.get("telegram_file_id"):
            sent = await reply_photo_with_retry(
                update.message, record["telegram_file_id"], caption=caption,
            )
        if sent is None and record.get("image"):
            fallback = BytesIO(record["image"])
            fallback.name = f"photo_{record['id']}.jpg"
            sent = await reply_photo_with_retry(update.message, fallback, caption=caption)
        if sent is not None:
            delivered += 1
    if delivered < len(records):
        await update.message.reply_text(
            f"Удалось показать {delivered} из {len(records)} фотографий. "
            "Остальные можно запросить ещё раз.",
        )
    navigation = []
    if offset > 0:
        navigation.append([MENU_VIEW_PREVIOUS_PHOTOS])
    if offset + len(records) < total:
        navigation.append([MENU_VIEW_NEXT_PHOTOS])
    navigation.extend([[MENU_VIEW_ANOTHER_HOSPITAL], [MENU_VIEW_PHOTOS_DONE]])
    await update.message.reply_text(
        "Можно открыть следующую страницу или выбрать другой роддом.",
        reply_markup=ReplyKeyboardMarkup(navigation, resize_keyboard=True),
    )
    return VIEW_PHOTO_HOSPITAL


def medic_photo_status_counts(
    telegram_id: int,
    date_from=None,
    date_to=None,
    hospital: str | None = None,
    city: str | None = None,
    doctor: str | None = None,
):
    """Count operator decisions for one medic and the selected report scope."""
    conditions = ["p.sender_telegram_id = ?"]
    parameters = [telegram_id]
    hospital_expr = """COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                                NULLIF(TRIM(visit.maternity_hospital), ''),
                                NULLIF(TRIM(sender.maternity_hospital), ''),
                                'Роддом не указан')"""
    city_expr = f"""COALESCE(NULLIF(TRIM(visit.city), ''),
                             (SELECT NULLIF(TRIM(eh.city), '')
                                FROM employee_hospitals AS eh
                               WHERE eh.telegram_id = p.sender_telegram_id
                                 AND TRIM(eh.maternity_hospital) = {hospital_expr}
                               LIMIT 1),
                             NULLIF(TRIM(sender.city), ''),
                             'Город не указан')"""
    if hospital:
        conditions.append(f"{hospital_expr} = ?")
        parameters.append(hospital)
    if city:
        conditions.append(f"{city_expr} = ?")
        parameters.append(city)
    if doctor:
        conditions.append("TRIM(COALESCE(p.doctor_name, '')) = ?")
        parameters.append(doctor)
    if date_from is not None:
        period_start = datetime(
            date_from.year, date_from.month, date_from.day, tzinfo=TASHKENT_TIMEZONE
        ).astimezone(timezone.utc)
        conditions.append("julianday(p.sent_at) >= julianday(?)")
        parameters.append(period_start.isoformat())
    if date_to is not None:
        next_day = date_to + timedelta(days=1)
        period_end = datetime(
            next_day.year, next_day.month, next_day.day, tzinfo=TASHKENT_TIMEZONE
        ).astimezone(timezone.utc)
        conditions.append("julianday(p.sent_at) < julianday(?)")
        parameters.append(period_end.isoformat())

    effective_decision = "COALESCE(NULLIF(TRIM(checks.manual_decision), ''), 'review')"
    with connect() as con:
        row = con.execute(
            f"""SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN {effective_decision} = 'accepted' THEN 1 ELSE 0 END), 0) AS accepted,
                       COALESCE(SUM(CASE WHEN {effective_decision} = 'review' THEN 1 ELSE 0 END), 0) AS review,
                       COALESCE(SUM(CASE WHEN {effective_decision} = 'rejected' THEN 1 ELSE 0 END), 0) AS rejected
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                LEFT JOIN visits AS visit ON visit.id = p.visit_id
                LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE {' AND '.join(conditions)}""",
            parameters,
        ).fetchone()
    result = {key: int(row[key] or 0) for key in ("total", "accepted", "review", "rejected")}
    result["not_accepted"] = result["review"] + result["rejected"]
    return result

def medic_report_photos(
    telegram_id: int,
    date_from,
    date_to,
    hospital: str | None = None,
    offset: int = 0,
    limit: int = MEDIC_PHOTO_PAGE_SIZE,
    *,
    city: str | None = None,
    doctor: str | None = None,
):
    """Load one page of this medic's photos for the selected report scope."""
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(int(limit), MEDIC_PHOTO_PAGE_SIZE))
    period_start = datetime(
        date_from.year, date_from.month, date_from.day, tzinfo=TASHKENT_TIMEZONE,
    ).astimezone(timezone.utc)
    next_day = date_to + timedelta(days=1)
    period_end = datetime(
        next_day.year, next_day.month, next_day.day, tzinfo=TASHKENT_TIMEZONE,
    ).astimezone(timezone.utc)
    hospital_expr = """COALESCE(NULLIF(TRIM(p.maternity_hospital), ''),
                                NULLIF(TRIM(visit.maternity_hospital), ''),
                                NULLIF(TRIM(sender.maternity_hospital), ''),
                                'Роддом не указан')"""
    city_expr = f"""COALESCE(NULLIF(TRIM(visit.city), ''),
                             (SELECT NULLIF(TRIM(eh.city), '')
                                FROM employee_hospitals AS eh
                               WHERE eh.telegram_id = p.sender_telegram_id
                                 AND TRIM(eh.maternity_hospital) = {hospital_expr}
                               LIMIT 1),
                             NULLIF(TRIM(sender.city), ''),
                             'Город не указан')"""
    conditions = [
        "p.sender_telegram_id = ?",
        "julianday(p.sent_at) >= julianday(?)",
        "julianday(p.sent_at) < julianday(?)",
    ]
    parameters = [telegram_id, period_start.isoformat(), period_end.isoformat()]
    if hospital:
        conditions.append(f"{hospital_expr} = ?")
        parameters.append(hospital)
    if city:
        conditions.append(f"{city_expr} = ?")
        parameters.append(city)
    if doctor:
        conditions.append("TRIM(COALESCE(p.doctor_name, '')) = ?")
        parameters.append(doctor)
    where = " AND ".join(conditions)
    with connect() as con:
        total = con.execute(
            f"""SELECT COUNT(*) FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                LEFT JOIN visits AS visit ON visit.id = p.visit_id
                LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE {where}""", tuple(parameters),
        ).fetchone()[0]
        rows = con.execute(
            f"""SELECT p.id, p.telegram_file_id, p.image, p.doctor_name, p.sent_at,
                       p.review_status, p.code_status, p.qr_data, p.photo_date,
                       p.package_number, p.mother_name,
                       checks.decision, checks.manual_decision, checks.manual_reason,
                       {city_expr} AS city,
                       {hospital_expr} AS maternity_hospital
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                LEFT JOIN visits AS visit ON visit.id = p.visit_id
                LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE {where}
                ORDER BY julianday(p.sent_at) DESC, p.id DESC
                LIMIT ? OFFSET ?""",
            (*parameters, safe_limit, safe_offset),
        ).fetchall()
    return int(total), [dict(row) for row in rows]

def clear_report_photo_navigation(context) -> None:
    for key in (
        "report_available_cities",
        "report_available_hospitals",
        "report_available_doctors",
        "report_photo_city",
        "report_photo_hospital",
        "report_photo_doctor",
        "report_photo_date_from",
        "report_photo_date_to",
        "report_photo_offset",
    ):
        context.user_data.pop(key, None)


async def send_medic_report_photo_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    date_from,
    date_to,
    hospital: str | None = None,
    offset: int = 0,
    *,
    city: str | None = None,
    doctor: str | None = None,
):
    total, records = await asyncio.to_thread(
        medic_report_photos,
        telegram_id,
        date_from,
        date_to,
        hospital,
        offset,
        MEDIC_PHOTO_PAGE_SIZE,
        city=city,
        doctor=doctor,
    )
    if not records:
        if not total:
            clear_report_photo_navigation(context)
        return

    context.user_data["report_photo_city"] = city
    context.user_data["report_photo_hospital"] = hospital
    context.user_data["report_photo_doctor"] = doctor
    context.user_data["report_photo_date_from"] = date_from.isoformat()
    context.user_data["report_photo_date_to"] = date_to.isoformat()
    context.user_data["report_photo_offset"] = offset
    await reply_text_with_retry(
        update.message,
        f"🖼 Город: {city or 'Все доступные города'}\n"
        f"Роддом: {hospital or 'Все доступные роддома'}\n"
        f"Врач: {doctor or 'Все врачи'}\n"
        f"Период: {date_from.strftime('%d.%m.%Y')}–{date_to.strftime('%d.%m.%Y')}\n"
        f"Всего фотографий: {total}. Показываю {offset + 1}–{offset + len(records)}.",
        reply_markup=ReplyKeyboardRemove(),
    )

    delivered = 0
    for record in records:
        caption = medic_photo_caption(record, hospital or record.get("maternity_hospital"))
        if len(caption) > 1000:
            caption = caption[:997].rstrip() + "…"
        sent = None
        if record.get("telegram_file_id"):
            sent = await reply_photo_with_retry(
                update.message, record["telegram_file_id"], caption=caption,
            )
        if sent is None and record.get("image"):
            fallback = BytesIO(record["image"])
            fallback.name = f"photo_{record['id']}.jpg"
            sent = await reply_photo_with_retry(update.message, fallback, caption=caption)
        if sent is not None:
            delivered += 1
    if delivered < len(records):
        await reply_text_with_retry(
            update.message,
            f"Удалось отправить {delivered} из {len(records)} фотографий этой страницы. "
            "Остальные можно запросить ещё раз.",
        )

    navigation = []
    if offset > 0:
        navigation.append([MENU_REPORT_PREVIOUS_PHOTOS])
    if offset + len(records) < total:
        navigation.append([MENU_REPORT_NEXT_PHOTOS])
    navigation.append([MENU_REPORT_PHOTOS_DONE])
    await reply_text_with_retry(
        update.message,
        "Выберите следующую страницу или завершите просмотр отчёта.",
        reply_markup=ReplyKeyboardMarkup(navigation, resize_keyboard=True),
    )


async def report_photos_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        await update.message.reply_text("Просмотр отчёта доступен только активным медикам.")
        return
    choice = update.message.text.strip()
    if choice == MENU_REPORT_PHOTOS_DONE:
        clear_report_photo_navigation(context)
        await update.message.reply_text("Просмотр отчёта завершён.", reply_markup=medic_keyboard())
        return
    try:
        hospital = context.user_data["report_photo_hospital"]
        city = context.user_data.get("report_photo_city")
        doctor = context.user_data.get("report_photo_doctor")
        date_from = datetime.strptime(
            context.user_data["report_photo_date_from"], "%Y-%m-%d",
        ).date()
        date_to = datetime.strptime(
            context.user_data["report_photo_date_to"], "%Y-%m-%d",
        ).date()
        offset = int(context.user_data.get("report_photo_offset", 0))
    except (KeyError, TypeError, ValueError):
        clear_report_photo_navigation(context)
        await update.message.reply_text(
            "Сначала запросите «Отчёт по фотографиям».", reply_markup=medic_keyboard(),
        )
        return
    if choice == MENU_REPORT_NEXT_PHOTOS:
        offset += MEDIC_PHOTO_PAGE_SIZE
    elif choice == MENU_REPORT_PREVIOUS_PHOTOS:
        offset = max(0, offset - MEDIC_PHOTO_PAGE_SIZE)
    await send_medic_report_photo_page(
        update,
        context,
        update.effective_user.id,
        date_from,
        date_to,
        hospital,
        offset,
        city=city,
        doctor=doctor,
    )

def parse_photo_report_period(values):
    text = " ".join(values or [])
    date_pattern = re.compile(
        r"(?<!\d)(?:(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})"
        r"|(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2}))(?!\d)"
    )
    matches = list(date_pattern.finditer(text))
    if len(matches) not in (1, 2):
        raise ValueError("Укажите одну дату или две даты")

    def parse_date(match):
        day, month, year, iso_year, iso_month, iso_day = match.groups()
        if year is not None:
            return datetime(int(year), int(month), int(day)).date()
        return datetime(int(iso_year), int(iso_month), int(iso_day)).date()

    date_from = parse_date(matches[0])
    date_to = parse_date(matches[-1])
    if date_from > date_to:
        raise ValueError("Начальная дата позже конечной")
    return date_from, date_to


def format_photo_status_block(title: str, counts: dict) -> str:
    return (
        f"{title}\n"
        f"📷 Всего: {counts['total']}\n"
        f"✅ Принято: {counts['accepted']}\n"
        f"🟡 В обработке: {counts['review']}\n"
        f"❌ Отклонено: {counts['rejected']}"
    )


def photo_report_choice_keyboard(values, *, columns: int = 1):
    safe_columns = max(1, int(columns))
    rows = [list(values[index:index + safe_columns]) for index in range(0, len(values), safe_columns)]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def photo_report_hospital_keyboard(hospitals):
    return photo_report_choice_keyboard(hospitals)


async def send_selected_medic_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user,
    city: str,
    hospital: str,
    doctor: str | None,
    date_from,
    date_to,
):
    telegram_id = update.effective_user.id
    status_counts = await asyncio.to_thread(
        medic_photo_status_counts,
        telegram_id,
        date_from,
        date_to,
        hospital,
        city,
        doctor,
    )
    await reply_text_with_retry(
        update.message,
        "📊 Отчёт по фотографиям\n\n"
        f"🏙 Город: {city}\n"
        f"🏥 Роддом: {hospital}\n"
        f"👩‍⚕️ Врач: {doctor or 'Все врачи'}\n"
        + format_photo_status_block(
            f"📅 Период: {date_from.strftime('%d.%m.%Y')}–{date_to.strftime('%d.%m.%Y')}",
            status_counts,
        )
        + "\n\nНиже бот отправит фотографии всех трёх статусов. "
          "Если оператор оставил комментарий, он будет указан под фотографией. "
          "Удалённые с сайта фотографии не учитываются.",
        reply_markup=ReplyKeyboardRemove(),
    )
    if not status_counts["total"]:
        clear_report_photo_navigation(context)
        await reply_text_with_retry(
            update.message,
            "За выбранный период по выбранным городу, роддому и врачу фотографий нет.",
            reply_markup=medic_keyboard(),
        )
        return
    await send_medic_report_photo_page(
        update,
        context,
        telegram_id,
        date_from,
        date_to,
        hospital,
        city=city,
        doctor=doctor,
    )


async def photo_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        await update.message.reply_text(
            "Личный отчёт доступен только активным зарегистрированным медикам."
        )
        return ConversationHandler.END

    cities = get_medic_report_cities(update.effective_user.id)
    if not cities:
        await update.message.reply_text(
            "У вас пока нет назначенных городов и роддомов.", reply_markup=medic_keyboard(),
        )
        return ConversationHandler.END

    clear_report_photo_navigation(context)
    context.user_data["report_available_cities"] = cities
    period_text = ""
    if context.args:
        try:
            date_from, date_to = parse_photo_report_period(context.args)
        except ValueError:
            await update.message.reply_text(
                "Не удалось определить период. Укажите одну дату или две даты:\n"
                "/report 05.10.2026\n"
                "/report 01.10.2026 05.10.2026"
            )
            return ConversationHandler.END
        context.user_data["report_photo_date_from"] = date_from.isoformat()
        context.user_data["report_photo_date_to"] = date_to.isoformat()
        period_text = (
            f"\nВыбран период {date_from.strftime('%d.%m.%Y')}–"
            f"{date_to.strftime('%d.%m.%Y')}."
        )

    await update.message.reply_text(
        "🏙 Выберите свой город для отчёта." + period_text,
        reply_markup=photo_report_choice_keyboard(cities),
    )
    return REPORT_CITY


async def photo_report_city_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        return ConversationHandler.END
    choice = update.message.text.strip()
    cities = context.user_data.get("report_available_cities", [])
    if choice not in cities:
        await update.message.reply_text(
            "Выберите город с помощью кнопки.",
            reply_markup=photo_report_choice_keyboard(cities),
        )
        return REPORT_CITY

    hospitals = get_medic_report_hospitals(update.effective_user.id, choice)
    if not hospitals:
        await update.message.reply_text(
            "Для этого города у вас нет доступных роддомов. Начните отчёт заново.",
            reply_markup=medic_keyboard(),
        )
        clear_report_photo_navigation(context)
        return ConversationHandler.END
    context.user_data["report_photo_city"] = choice
    context.user_data["report_available_hospitals"] = hospitals
    await update.message.reply_text(
        f"🏥 Город: {choice}\nВыберите роддом для отчёта.",
        reply_markup=photo_report_hospital_keyboard(hospitals),
    )
    return REPORT_HOSPITAL


async def photo_report_hospital_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        return ConversationHandler.END
    choice = update.message.text.strip()
    hospitals = context.user_data.get("report_available_hospitals", [])
    if choice not in hospitals:
        await update.message.reply_text(
            "Выберите роддом с помощью кнопки.",
            reply_markup=photo_report_hospital_keyboard(hospitals),
        )
        return REPORT_HOSPITAL

    context.user_data["report_photo_hospital"] = choice
    city = context.user_data.get("report_photo_city")
    cities = context.user_data.get("report_available_cities", [])
    if city not in cities:
        await update.message.reply_text(
            "Сначала заново выберите город через кнопку «Отчёт по фотографиям».",
            reply_markup=medic_keyboard(),
        )
        clear_report_photo_navigation(context)
        return ConversationHandler.END
    doctors = get_medic_report_doctors(update.effective_user.id, city, choice)
    context.user_data["report_available_doctors"] = doctors
    doctor_choices = [MENU_REPORT_ALL_DOCTORS, *doctors]
    await update.message.reply_text(
        f"👩‍⚕️ Город: {city}\nРоддом: {choice}\n"
        "Выберите врача. Чтобы получить общий отчёт, нажмите «Все врачи».",
        reply_markup=photo_report_choice_keyboard(doctor_choices, columns=2),
    )
    return REPORT_DOCTOR


async def photo_report_doctor_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        return ConversationHandler.END
    choice = update.message.text.strip()
    doctors = context.user_data.get("report_available_doctors", [])
    if choice != MENU_REPORT_ALL_DOCTORS and choice not in doctors:
        await update.message.reply_text(
            "Выберите врача с помощью кнопки.",
            reply_markup=photo_report_choice_keyboard(
                [MENU_REPORT_ALL_DOCTORS, *doctors], columns=2,
            ),
        )
        return REPORT_DOCTOR

    doctor = None if choice == MENU_REPORT_ALL_DOCTORS else choice
    context.user_data["report_photo_doctor"] = doctor
    try:
        date_from = datetime.strptime(
            context.user_data["report_photo_date_from"], "%Y-%m-%d",
        ).date()
        date_to = datetime.strptime(
            context.user_data["report_photo_date_to"], "%Y-%m-%d",
        ).date()
    except (KeyError, TypeError, ValueError):
        await update.message.reply_text(
            "📅 Введите дату отчёта в формате ДД.ММ.ГГГГ.\n"
            "Например: 05.10.2026\n\n"
            "Для периода введите две даты через пробел:\n"
            "01.10.2026 05.10.2026",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REPORT_PERIOD

    await send_selected_medic_report(
        update,
        context,
        user,
        context.user_data["report_photo_city"],
        context.user_data["report_photo_hospital"],
        doctor,
        date_from,
        date_to,
    )
    return ConversationHandler.END


async def photo_report_period_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        return ConversationHandler.END
    hospital = context.user_data.get("report_photo_hospital")
    city = context.user_data.get("report_photo_city")
    doctor = context.user_data.get("report_photo_doctor")
    hospitals = context.user_data.get("report_available_hospitals", [])
    cities = context.user_data.get("report_available_cities", [])
    doctors = context.user_data.get("report_available_doctors", [])
    if city not in cities or hospital not in hospitals or (doctor is not None and doctor not in doctors):
        await update.message.reply_text(
            "Сначала заново выберите город, роддом и врача через кнопку «Отчёт по фотографиям».",
            reply_markup=medic_keyboard(),
        )
        clear_report_photo_navigation(context)
        return ConversationHandler.END
    try:
        date_from, date_to = parse_photo_report_period([update.message.text])
    except ValueError:
        await update.message.reply_text(
            "Не удалось определить дату. Введите одну дату или период:\n"
            "05.10.2026\n"
            "01.10.2026 05.10.2026"
        )
        return REPORT_PERIOD

    context.user_data["report_photo_date_from"] = date_from.isoformat()
    context.user_data["report_photo_date_to"] = date_to.isoformat()
    await send_selected_medic_report(
        update, context, user, city, hospital, doctor, date_from, date_to,
    )
    return ConversationHandler.END

def medic_operator_comments(telegram_id: int, limit: int = 20):
    """Return recent human comments only for photos sent by this medic."""
    safe_limit = min(50, max(1, int(limit)))
    with connect() as con:
        return con.execute(
            """SELECT p.id, p.sent_at, p.doctor_name, p.maternity_hospital,
                      p.telegram_file_id,
                      checks.manual_decision, checks.manual_reason, checks.manual_at
               FROM photos AS p
               JOIN photo_checks AS checks ON checks.photo_id = p.id
               WHERE p.sender_telegram_id = ?
                 AND TRIM(COALESCE(checks.manual_reason, '')) <> ''
               ORDER BY COALESCE(checks.manual_at, p.sent_at) DESC, p.id DESC
               LIMIT ?""",
            (telegram_id, safe_limit),
        ).fetchall()


def photo_image_for_comment(photo_id: int):
    """Load one fallback image without keeping every commented photo in memory."""
    with connect() as con:
        row = con.execute("SELECT image FROM photos WHERE id = ?", (photo_id,)).fetchone()
    return bytes(row["image"]) if row and row["image"] else None


def _telegram_comment_time(value: str | None) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TASHKENT_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "дата не указана"


async def operator_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Медик":
        await update.message.reply_text(
            "Комментарии к фотографиям доступны только активным зарегистрированным медикам."
        )
        return
    comments = await asyncio.to_thread(medic_operator_comments, update.effective_user.id)
    if not comments:
        await update.message.reply_text(
            "💬 Оператор пока не оставлял комментариев к вашим фотографиям.",
            reply_markup=medic_keyboard(),
        )
        return

    await reply_text_with_retry(
        update.message,
        "💬 Последние комментарии оператора к вашим фотографиям:",
    )
    for index, comment in enumerate(comments):
        decision = DECISION_LABELS.get(comment["manual_decision"], "Решение оператора")
        details = " · ".join(filter(None, (
            (comment["maternity_hospital"] or "").strip(),
            (comment["doctor_name"] or "").strip(),
        ))) or "роддом и врач не указаны"
        comment_text = comment["manual_reason"].strip()
        caption = (
            f"Фото №{comment['id']} · {_telegram_comment_time(comment['sent_at'])}\n"
            f"{details}\nИтог: {decision}\nКомментарий: {comment_text}"
        )
        # Photo captions are limited to 1024 characters by Telegram.
        if len(caption) > 1000:
            caption = caption[:997].rstrip() + "…"
        send_kwargs = {
            "caption": caption,
            "reply_markup": medic_keyboard() if index == len(comments) - 1 else None,
        }
        sent = await reply_photo_with_retry(
            update.message,
            comment["telegram_file_id"],
            **send_kwargs,
        )
        if sent is None:
            image_bytes = await asyncio.to_thread(photo_image_for_comment, comment["id"])
            if image_bytes:
                image_file = BytesIO(image_bytes)
                image_file.name = f"photo_{comment['id']}.jpg"
                sent = await reply_photo_with_retry(
                    update.message,
                    image_file,
                    **send_kwargs,
                )
        if sent is None:
            await reply_text_with_retry(
                update.message,
                caption + "\n\n⚠️ Не удалось прикрепить фотографию.",
                reply_markup=send_kwargs["reply_markup"],
            )


def operator_ten_day_photo_report(now: datetime | None = None):
    local_timezone = timezone(timedelta(hours=5))
    local_now = (now or datetime.now(timezone.utc)).astimezone(local_timezone)
    period_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    period_start = period_end - timedelta(days=OPERATOR_REPORT_DAYS)

    with connect() as con:
        rows = con.execute(
            """
            WITH report_photos AS (
                SELECT
                    COALESCE(NULLIF(TRIM(sender.city), ''), 'Город не указан') AS city,
                    COALESCE(
                        NULLIF(TRIM(p.maternity_hospital), ''),
                        NULLIF(TRIM(sender.maternity_hospital), ''),
                        'Роддом не указан'
                    ) AS maternity_hospital,
                    COALESCE(
                        NULLIF(TRIM(p.doctor_name), ''),
                        NULLIF(TRIM(medic.full_name), ''),
                        'Врач не указан'
                    ) AS doctor_name
                FROM photos AS p
                LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
                JOIN photo_checks AS checks ON checks.photo_id = p.id
                WHERE julianday(p.sent_at) >= julianday(?)
                  AND julianday(p.sent_at) < julianday(?)
                  AND checks.manual_decision = 'accepted'
            )
            SELECT city, maternity_hospital, doctor_name, COUNT(*) AS photo_count
            FROM report_photos
            GROUP BY city, maternity_hospital, doctor_name
            ORDER BY city COLLATE NOCASE, maternity_hospital COLLATE NOCASE, doctor_name COLLATE NOCASE
            """,
            (period_start.isoformat(), period_end.isoformat()),
        ).fetchall()

    report_rows = [dict(row) for row in rows]
    total_photos = sum(row["photo_count"] for row in report_rows)
    return {
        "date_from": period_start.date(),
        "date_to": (period_end - timedelta(days=1)).date(),
        "rows": report_rows,
        "total_photos": total_photos,
        "total_amount": total_photos * PHOTO_PAYMENT_RATE_SUM,
    }


def build_operator_ten_day_report(report_data) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Отчёт за 10 дней"
    sheet.sheet_view.showGridLines = False
    sheet.append(["Город", "Роддом", "Имя врача", "Количество фото", "Сумма, сум"])

    header_fill = PatternFill("solid", fgColor="5B3A86")
    light_border = Border(bottom=Side(style="thin", color="D9D2E3"))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for record in report_data["rows"]:
        photo_count = int(record["photo_count"])
        sheet.append([
            record["city"],
            record["maternity_hospital"],
            record["doctor_name"],
            photo_count,
            photo_count * PHOTO_PAYMENT_RATE_SUM,
        ])
        current_row = sheet.max_row
        for cell in sheet[current_row]:
            cell.border = light_border
            cell.alignment = Alignment(vertical="center", wrap_text=cell.column <= 3)
        sheet.cell(current_row, 4).number_format = "#,##0"
        sheet.cell(current_row, 5).number_format = '#,##0 "сум"'

    total_row = sheet.max_row + 1
    sheet.append(["", "", "Итого", report_data["total_photos"], report_data["total_amount"]])
    for cell in sheet[total_row]:
        cell.font = Font(bold=True, color="375623")
        cell.fill = PatternFill("solid", fgColor="E2F0D9")
        cell.border = Border(top=Side(style="medium", color="548235"))
        cell.alignment = Alignment(vertical="center")
    sheet.cell(total_row, 4).number_format = "#,##0"
    sheet.cell(total_row, 5).number_format = '#,##0 "сум"'

    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 38
    sheet.column_dimensions["C"].width = 34
    sheet.column_dimensions["D"].width = 20
    sheet.column_dimensions["E"].width = 22
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:E{max(1, total_row - 1)}"

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output


async def operator_ten_day_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Оператор":
        await update.message.reply_text("Отчёт за 10 дней доступен только зарегистрированным операторам.")
        return

    try:
        report_data = await asyncio.to_thread(operator_ten_day_photo_report)
        report = await asyncio.to_thread(build_operator_ten_day_report, report_data)
    except Exception:
        logging.exception("Не удалось сформировать операторский отчёт за 10 дней")
        await update.message.reply_text("Не удалось сформировать отчёт. Попробуйте ещё раз.")
        return

    date_from = report_data["date_from"].strftime("%d.%m.%Y")
    date_to = report_data["date_to"].strftime("%d.%m.%Y")
    total_amount = f'{report_data["total_amount"]:,}'.replace(",", " ")
    output_name = f"Отчет_за_10_дней_{date_from}-{date_to}.xlsx"
    await update.message.reply_document(
        document=report,
        filename=output_name,
        caption=(
            f"Отчёт за {date_from}–{date_to}: "
            f'{report_data["total_photos"]} фотографий, сумма {total_amount} сум. '
            "Учтены только фотографии, вручную принятые оператором."
        ),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def photo_start(update, context):
    if await deny_unlisted_user(update):
        return ConversationHandler.END
    user = get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("Нет доступа. Администратор должен добавить вас через сайт.")
        return ConversationHandler.END
    if user["role"] != "Медик":
        await update.message.reply_text("Отправлять фотографии могут только зарегистрированные медики.")
        return ConversationHandler.END
    previous_visit_id = context.user_data.get("visit_id")
    if previous_visit_id:
        await asyncio.to_thread(finish_visit, previous_visit_id, "interrupted")
    context.user_data.clear()
    context.user_data["batch_photos"] = []
    context.user_data["medic_telegram_id"] = user["telegram_id"]
    context.user_data["medic_code"] = user["medic_code"]
    context.user_data["photo_city"] = user["city"]
    hospitals = get_employee_hospitals(user["telegram_id"], user["city"])
    if not hospitals:
        await update.message.reply_text("Для вас не назначен роддом. Обратитесь к создателю сайта.")
        return ConversationHandler.END
    context.user_data["photo_hospitals"] = hospitals
    await update.message.reply_text(
        "Выберите роддом, за который отправляете фотографии:",
        reply_markup=ReplyKeyboardMarkup([[hospital] for hospital in hospitals], resize_keyboard=True, one_time_keyboard=True),
    )
    if cv2 is None:
        await update.message.reply_text(
            "Проверка QR-кодов пока не включена: установите зависимости из обновлённого requirements.txt и перезапустите бота."
        )
    return PHOTO_HOSPITAL


async def select_photo_hospital(update, context):
    hospital = update.message.text.strip()
    if hospital not in context.user_data.get("photo_hospitals", []):
        await update.message.reply_text("Выберите роддом с помощью кнопки.")
        return PHOTO_HOSPITAL
    context.user_data["photo_hospital"] = hospital
    await update.message.reply_text(
        f"Роддом: {hospital}\nВведите ФИО врача.", reply_markup=ReplyKeyboardRemove()
    )
    return DOCTOR_NAME


def load_photo_processing_data(photo_id: int):
    with connect() as con:
        photo = con.execute(
            "SELECT image, image_digest FROM photos WHERE id = ?",
            (photo_id,),
        ).fetchone()
    if not photo:
        return None
    return bytes(photo["image"]), photo["image_digest"]


def load_duplicate_metadata(original_photo_id: int):
    """Reuse paid analysis only when the incoming upload is the exact same file."""
    with connect() as con:
        photo = con.execute(
            """SELECT p.photo_date, p.mother_name, p.package_number, p.qr_data,
                      checks.product_status, checks.material_status
               FROM photos AS p
               LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
               WHERE p.id = ?""",
            (original_photo_id,),
        ).fetchone()
    if not photo:
        return None
    is_nuppi_1 = (
        True if photo["product_status"] == "NUPPI 1 обнаружен"
        else False if photo["product_status"] == "NUPPI 1 не подтверждён"
        else None
    )
    is_reference_design = (
        True if photo["material_status"] == "Эталонный дизайн подтверждён"
        else False if photo["material_status"] == "Эталонный дизайн не подтверждён"
        else None
    )
    reused_qr_data = canonical_qr_data(photo["qr_data"])
    if reused_qr_data and not QR_REGION_CODE_PATTERN.fullmatch(reused_qr_data):
        reused_qr_data = None
    return (
        photo["photo_date"], photo["mother_name"], photo["package_number"],
        reused_qr_data, is_nuppi_1, is_reference_design, False, False,
        True if reused_qr_data else None,
        "Нет явных признаков", "Данные проверки перенесены с оригинальной фотографии.", "ok",
    )


def load_duplicate_ai_generated_score(original_photo_id: int):
    """Reuse Sightengine's result for the exact same image bytes."""
    with connect() as con:
        photo = con.execute(
            "SELECT ai_generated_score FROM photos WHERE id = ?",
            (original_photo_id,),
        ).fetchone()
    if not photo or photo["ai_generated_score"] is None:
        return None
    return float(photo["ai_generated_score"])


def insert_incoming_photo(update, context, attachment, image_bytes: bytes, image_digest: str,
                          doctor_name: str, hospital: str):
    with connect(timeout_seconds=5) as con:
        cursor = con.execute(
            """INSERT INTO photos
               (sender_telegram_id, medic_telegram_id, medic_code, doctor_name, image_hash, crop_hash, image_digest, qr_data, photo_date, maternity_hospital,
                package_number, mother_name, mother_phone, review_status, review_reason, ai_generated_score, code_status, telegram_file_id, image, sent_at,
                visit_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                update.effective_user.id, context.user_data["medic_telegram_id"], context.user_data["medic_code"],
                doctor_name, None, None, image_digest, None, None, hospital, None, None, None,
                "Проверяется в фоне", "Фотография сохранена; идёт автоматическая проверка.", None,
                "Проверяется", attachment.file_id, image_bytes, datetime.now(timezone.utc).isoformat(),
                context.user_data.get("visit_id"),
            ),
        )
        pending = automatic_photo_decision("Проверяется в фоне", "Проверяется")
        con.execute(
            """INSERT OR REPLACE INTO photo_checks
               (photo_id, product_status, material_status, location_status,
                duplicate_status, editing_status, assignment_status, decision,
                decision_reason, automated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cursor.lastrowid, pending["product_status"], pending["material_status"],
                pending["location_status"], pending["duplicate_status"],
                pending["editing_status"], pending["assignment_status"],
                pending["decision"], pending["decision_reason"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cursor.lastrowid


def insert_incoming_photo_with_retry(*args, attempts: int | None = None):
    """Retry only transient SQLite contention; the transaction stays atomic."""
    attempts = max(1, attempts or PHOTO_SAVE_RETRY_ATTEMPTS)
    for attempt in range(attempts):
        try:
            return insert_incoming_photo(*args)
        except sqlite3.OperationalError as error:
            if not _is_transient_sqlite_error(error) or attempt + 1 >= attempts:
                raise
            logging.warning(
                "База временно занята при сохранении фотографии, попытка %s/%s",
                attempt + 1,
                attempts,
            )
            time.sleep(0.25 * (2 ** attempt))


def is_readable_image(image_bytes: bytes) -> bool:
    """Return whether Pillow can decode the uploaded bytes as an image."""
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.verify()
        return True
    except Exception:
        return False


async def download_photo_bytes(bot, attachment, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            telegram_file = await bot.get_file(attachment.file_id)
            return bytes(await telegram_file.download_as_bytearray())
        except BadRequest:
            # BadRequest наследуется от NetworkError, но повтор не поможет:
            # например, файл больше 20 МБ или file_id недействителен.
            raise
        except (TimedOut, NetworkError) as error:
            last_error = error
            logging.warning(
                "Не удалось скачать фотографию из Telegram, попытка %s/%s: %s",
                attempt + 1, attempts, error,
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(attempt + 1)
    raise last_error


async def process_photo_in_background(
        photo_id: int, city: str, hospital: str,
        duplicate_fallback_limit: int | None = None):
    try:
        async with photo_processing_semaphore:
            photo_data = await asyncio.to_thread(load_photo_processing_data, photo_id)
            if not photo_data:
                logging.warning("Фотография id=%s удалена до фоновой проверки", photo_id)
                return False
            image_bytes, image_digest = photo_data
            photo_hash, crop_hash, visual_hashes = await asyncio.to_thread(
                lambda: (
                    str(imagehash.phash(Image.open(BytesIO(image_bytes)))),
                    calculate_crop_hash(image_bytes),
                    calculate_visual_hashes(image_bytes),
                )
            )
            raw_qr_data, local_qr_detected = await asyncio.to_thread(
                read_qr_code_details, image_bytes,
            )
            decoded_qr_data = canonical_qr_data(raw_qr_data)
            # Only a complete regional number participates in duplicate checks.
            # An unrelated URL/product QR must not prevent the AI fallback from
            # reading the printed regional number next to the Data Matrix.
            qr_data = (
                decoded_qr_data
                if decoded_qr_data and QR_REGION_CODE_PATTERN.fullmatch(decoded_qr_data)
                else None
            )
            # Keep any actually decoded payload for duplicate lookup. A foreign
            # URL/code is invalid for acceptance, but reusing that same payload
            # on another box is still a repeated QR and must be highlighted.
            qr_lookup_data = decoded_qr_data
            code_status = qr_code_status(decoded_qr_data, city)
            with connect() as con:
                con.execute(
                    """UPDATE photos
                       SET image_hash = ?, crop_hash = ?, visual_hashes = ?,
                           qr_data = ?, code_status = ?, qr_scan_version = ?
                       WHERE id = ?""",
                    (
                        photo_hash, crop_hash, visual_hashes, decoded_qr_data,
                        code_status, QR_SCAN_VERSION, photo_id,
                    ),
                )

            review_status = "Не проверено"
            review_reason = "Нужна ручная проверка оператором."
            original_photo_id = None
            previous_qr_photo = (
                await asyncio.to_thread(find_photo_by_qr, qr_lookup_data, photo_id)
                if qr_lookup_data else None
            )
            if previous_qr_photo:
                review_status = "Возможная копия"
                original_photo_id = previous_qr_photo["id"]
                review_reason = (
                    f"Такой же QR-код есть у фотографии от "
                    f"{previous_qr_photo['sent_at']}."
                )

            # A cryptographic digest is the only image-based duplicate rule.
            # Perceptual/crop hashes and vision similarity are deliberately not
            # allowed to create a duplicate link: standard package designs can
            # look nearly identical while still being different photographs.
            if review_status != "Возможная копия":
                exact_photo = await asyncio.to_thread(
                    find_exact_photo_by_digest, image_digest, photo_id,
                )
                if exact_photo:
                    review_status = "Возможная копия"
                    original_photo_id = exact_photo["id"]
                    review_reason = (
                        "Повторно отправлен тот же файл фотографии от "
                        f"{exact_photo['sent_at']}."
                    )

            metadata = await extract_photo_metadata(image_bytes)
            sightengine = (None, None, None)
            # Sightengine remains independent from Claude and is always run for
            # the current image when enabled.
            if SIGHTENGINE_ENABLED:
                sightengine = await sightengine_ai_assessment(image_bytes)
            (_ignored_photo_date, _ignored_mother_name, package_number, ai_qr_data,
             is_nuppi_1, is_reference_design, is_edited, is_code_obstructed,
             code_visible, visual_status, visual_reason, ai_status) = metadata
            # Never store handwritten date/name produced by image analysis, even
            # if an older adapter happens to return them during a rolling deploy.
            photo_date = None
            mother_name = None
            reported_ai_qr_data = ai_qr_data
            metadata_qr_candidate = (
                ai_qr_data if ai_qr_is_plausible_for_city(ai_qr_data, city) else None
            )
            qr_integrity_suspect = visual_status == "Подозрительный QR/Data Matrix"
            if (decoded_qr_data and reported_ai_qr_data
                    and QR_REGION_CODE_PATTERN.fullmatch(decoded_qr_data)
                    and QR_REGION_CODE_PATTERN.fullmatch(reported_ai_qr_data)
                    and decoded_qr_data != reported_ai_qr_data):
                qr_integrity_suspect = True
                visual_reason = (
                    "Локальный декодер и визуальная проверка прочитали разные полные "
                    "номера QR/Data Matrix; нужна ручная проверка подлинности кода."
                )
            if reported_ai_qr_data and not metadata_qr_candidate:
                logging.warning(
                    "ИИ-номер QR %s отклонён: он не соответствует городу %s или диапазону",
                    reported_ai_qr_data, city,
                )

            # A number read visually by the economical product pass is accepted
            # only after the dedicated QR pass confirms it. Dates and names are
            # never requested from either model.
            ai_qr_data = None
            qr_recovery = None
            if (not qr_data and ai_client and QR_DEDICATED_RECOVERY_ENABLED
                    and QR_STRICT_RECOVERY_FOR_ALL_MISSING):
                qr_recovery = await recover_missing_qr_data(
                    image_bytes,
                    # Only a real local symbol detection may skip the economical
                    # confirmation pass. A model's isolated "code visible" guess
                    # must not turn a package without QR into an error.
                    code_visible_hint=True if local_qr_detected else False,
                )
                if qr_recovery:
                    recovered_candidate = canonical_qr_data(qr_recovery["qr_data"])
                    if recovered_candidate and ai_qr_is_plausible_for_city(
                            recovered_candidate, city):
                        if (not metadata_qr_candidate
                                or recovered_candidate == metadata_qr_candidate):
                            ai_qr_data = recovered_candidate
                        else:
                            qr_integrity_suspect = True
                            visual_reason = (
                                "Два независимых ИИ-прохода прочитали разные полные "
                                "номера QR/Data Matrix; нужна проверка оператора."
                            )
                            logging.warning(
                                "ИИ-проходы дали разные номера QR: %s и %s",
                                metadata_qr_candidate, recovered_candidate,
                            )
                    is_code_obstructed = (
                        is_code_obstructed or qr_recovery["code_obstructed"]
                    )
                    qr_integrity_suspect = (
                        qr_integrity_suspect
                        or qr_recovery.get("code_suspicious") is True
                    )
            sightengine_status, sightengine_reason, ai_generated_score = sightengine
            assignment = await asyncio.to_thread(photo_assignment, photo_id)
            assignment_status = assignment_check_status(
                assignment, is_nuppi_1, is_reference_design, ai_status,
            )

            # ZXing decodes the actual symbol. If a very small or damaged symbol
            # cannot be decoded, use only a complete, clearly printed regional
            # number read during the already-running metadata request.
            if not qr_data and not decoded_qr_data and ai_qr_data:
                qr_data = ai_qr_data
                registry_status = qr_code_status(qr_data, city)
                code_status = (
                    f"Требует проверки оператора: номер {qr_data} прочитан визуально ИИ, "
                    f"но сам QR/Data Matrix машинно не декодировался. {registry_status}"
                )
                with connect() as con:
                    con.execute(
                        "UPDATE photos SET qr_data = ?, code_status = ? WHERE id = ?",
                        (qr_data, code_status, photo_id),
                    )
                if review_status != "Возможная копия":
                    previous_qr_photo = await asyncio.to_thread(find_photo_by_qr, qr_data, photo_id)
                    if previous_qr_photo:
                        review_status = "Возможная копия"
                        original_photo_id = previous_qr_photo["id"]
                        review_reason = f"Такой же QR-код есть у фотографии от {previous_qr_photo['sent_at']}."

            if review_status != "Возможная копия":
                if sightengine_status:
                    review_status = sightengine_status
                    review_reason = sightengine_reason
                elif ai_status != "ok":
                    review_status = "Не проверено"
                    review_reason = "ИИ недоступен или не смог выполнить проверку."
                else:
                    review_status = visual_status or "Не проверено"
                    review_reason = visual_reason or "Нужна ручная проверка оператором."

            if not qr_data and not decoded_qr_data and (
                    local_qr_detected
                    or (qr_recovery and qr_recovery["code_visible"])):
                code_status = "Ошибка: QR/Data Matrix виден, но полный номер не прочитан"
                if review_status != "Возможная копия" and not is_code_obstructed:
                    reason_labels = {
                        "blurred": "код размыт",
                        "glare": "код закрыт бликом",
                        "cropped": "код обрезан краем фотографии",
                        "too_small": "код слишком мелкий",
                        "suspected_fake": "код выглядит нарисованным, сгенерированным или заменённым",
                    }
                    unreadable_reason = reason_labels.get(
                        qr_recovery["unreadable_reason"] if qr_recovery else "",
                        "часть кода не декодируется",
                    )
                    review_status = "Ошибка: QR-код не прочитан"
                    review_reason = (
                        f"QR/Data Matrix обнаружен, но полный номер не удалось прочитать: "
                        f"{unreadable_reason}. Нужна проверка оператора; цифры не угадывались."
                    )
            elif (not qr_data and not decoded_qr_data and ai_client
                  and QR_DEDICATED_RECOVERY_ENABLED and qr_recovery is None):
                code_status = "Не проверено: финальная проверка QR/Data Matrix не выполнена"
                if review_status != "Возможная копия":
                    review_status = "Не проверено"
                    review_reason = (
                        "Не удалось завершить финальную проверку QR/Data Matrix; "
                        "фотография требует проверки оператора."
                    )

            # Перекрытие самого кода — отдельная ошибка и имеет приоритет над общей
            # оценкой «Нет явных признаков». Для копии сохраняем статус копии и ссылку
            # на оригинал, но ошибка всё равно явно отображается в проверке кода.
            if is_code_obstructed:
                code_status = "Ошибка: на QR/Data Matrix нанесена надпись или код перекрыт"
                if review_status != "Возможная копия":
                    review_status = "Ошибка: QR-код перекрыт"
                    review_reason = (
                        "На элементах QR/Data Matrix обнаружена надпись, линия, закрашивание "
                        "или другое перекрытие. Фотография требует проверки оператора."
                    )

            if qr_integrity_suspect:
                code_status = (
                    "Ошибка: QR/Data Matrix выглядит нарисованным, сгенерированным "
                    "или заменённым"
                )
                if review_status != "Возможная копия":
                    review_status = "Возможная подделка QR-кода"
                    review_reason = (
                        visual_reason
                        or "QR/Data Matrix имеет признаки ручной отрисовки, ИИ-генерации "
                           "или цифровой замены. Нужна проверка оператора."
                    )

            with connect() as con:
                con.execute(
                    """UPDATE photos SET photo_date = ?, package_number = ?, mother_name = ?,
                       review_status = ?, review_reason = ?, ai_generated_score = ?,
                       code_status = ?, original_photo_id = ?,
                       full_qr_recheck_version = ?, full_qr_recheck_failures = 0
                       WHERE id = ?""",
                    (photo_date, package_number, mother_name, review_status, review_reason,
                     ai_generated_score, code_status, original_photo_id,
                     FULL_QR_RECHECK_VERSION, photo_id),
                )
            await asyncio.to_thread(
                save_photo_check,
                photo_id,
                automatic_photo_decision(
                    review_status,
                    code_status,
                    is_nuppi_1=is_nuppi_1 if ai_status == "ok" else None,
                    is_reference_design=is_reference_design if ai_status == "ok" else None,
                    original_photo_id=original_photo_id,
                    assignment_status=assignment_status,
                ),
            )
            logging.info("Фоновая проверка фотографии id=%s завершена", photo_id)
            return True
    except Exception:
        logging.exception("Фоновая проверка фотографии id=%s не выполнена", photo_id)
        try:
            with connect() as con:
                con.execute(
                    "UPDATE photos SET review_status = ?, review_reason = ? WHERE id = ?",
                    ("Не проверено", "Не удалось завершить автоматическую проверку; нужна ручная проверка.", photo_id),
                )
            await asyncio.to_thread(
                save_photo_check,
                photo_id,
                automatic_photo_decision("Не проверено", "Не проверено"),
            )
        except Exception:
            logging.exception("Не удалось сохранить ошибку фоновой проверки id=%s", photo_id)
        return False
    finally:
        gc.collect()


async def receive_photo(update, context):
    if not is_allowed_user(update.effective_user.id):
        await deny_unlisted_user(update)
        return ConversationHandler.END
    medic = get_user(update.effective_user.id)
    if not medic or medic["role"] != "Медик":
        await update.message.reply_text("Отправлять фотографии могут только зарегистрированные медики.")
        return ConversationHandler.END
    doctor_name = context.user_data.get("doctor_name")
    hospital = context.user_data.get("photo_hospital")
    city = context.user_data.get("photo_city")
    if not doctor_name or not hospital or not city:
        await update.message.reply_text("Сначала выберите роддом и укажите ФИО врача: /send_photo")
        return ConversationHandler.END
    attachment = update.message.photo[-1] if update.message.photo else update.message.document
    if getattr(attachment, "file_size", None) and attachment.file_size > PHOTO_UPLOAD_MAX_BYTES:
        await reply_text_with_retry(
            update.message,
            "Файл больше 20 МБ, Telegram не позволяет боту его скачать. "
            "Отправьте снимок как обычное фото (не файлом).",
            reply_markup=photo_batch_keyboard(),
        )
        return PHOTO
    try:
        image_bytes = await download_photo_bytes(context.bot, attachment)
    except BadRequest as error:
        logging.warning("Telegram отказал в скачивании фотографии: %s", error)
        await reply_text_with_retry(
            update.message,
            "Telegram не отдал этот файл боту. Отправьте снимок как обычное фото (не файлом).",
            reply_markup=photo_batch_keyboard(),
        )
        return PHOTO
    except (TimedOut, NetworkError):
        await reply_text_with_retry(
            update.message,
            "Не удалось скачать фотографию из Telegram. Отправьте только эту фотографию ещё раз.",
            reply_markup=photo_batch_keyboard(),
        )
        return PHOTO
    if not await asyncio.to_thread(is_readable_image, image_bytes):
        # Например, HEIC с iPhone, отправленный файлом: Pillow его не открывает,
        # и фоновая проверка гарантированно упала бы. Лучше сразу попросить фото.
        await reply_text_with_retry(
            update.message,
            "Не удалось открыть изображение (формат не поддерживается). "
            "Отправьте снимок как обычное фото, а не файлом.",
            reply_markup=photo_batch_keyboard(),
        )
        return PHOTO
    image_digest = hashlib.sha256(image_bytes).hexdigest()
    try:
        photo_id = await asyncio.to_thread(
            insert_incoming_photo_with_retry,
            update, context, attachment, image_bytes, image_digest, doctor_name, hospital,
        )
    except Exception:
        logging.exception("Не удалось сохранить полученную фотографию")
        await reply_text_with_retry(
            update.message,
            "Не удалось сохранить фотографию. Отправьте только эту фотографию ещё раз.",
            reply_markup=photo_batch_keyboard(),
        )
        return PHOTO

    context.user_data.setdefault("batch_photos", []).append(photo_id)
    context.application.create_task(
        process_photo_in_background(photo_id, city, hospital),
        update=update,
    )
    logging.info(
        "Фотография id=%s сохранена для пользователя %s",
        photo_id, update.effective_user.id,
    )
    await reply_text_with_retry(
        update.message,
        "Фотография получена и отправлена на проверку. Отправьте следующую фотографию или нажмите «Завершить отправку».",
        reply_markup=photo_batch_keyboard(),
    )
    return PHOTO


async def continue_photo_batch(update, context):
    await reply_text_with_retry(
        update.message,
        "Отправьте следующую фотографию. Когда закончите, нажмите «Завершить отправку».",
        reply_markup=photo_batch_keyboard(),
    )
    return PHOTO


async def finish_photo_batch(update, context):
    visit_id = context.user_data.get("visit_id")
    await asyncio.to_thread(finish_visit, visit_id, "completed")
    context.user_data.clear()
    await reply_text_with_retry(
        update.message,
        "Фотографии сохранены. Для новой отправки используйте /send_photo",
        reply_markup=medic_keyboard(),
    )
    return ConversationHandler.END


async def save_doctor_name(update, context):
    doctor_name = update.message.text.strip()
    if not doctor_name:
        await update.message.reply_text("Введите ФИО врача:")
        return DOCTOR_NAME
    context.user_data["doctor_name"] = doctor_name
    try:
        context.user_data["visit_id"] = await asyncio.to_thread(
            create_visit,
            update.effective_user.id,
            context.user_data["photo_city"],
            context.user_data["photo_hospital"],
            doctor_name,
        )
    except Exception:
        # A visit is additional supervisor metadata.  Photo reception must keep
        # working even if this auxiliary record cannot be created.
        logging.exception("Не удалось создать запись визита")
        context.user_data["visit_id"] = None
    await reply_text_with_retry(
        update.message,
        "ФИО врача сохранено. Теперь отправляйте фотографии — каждая будет сразу добавлена в базу. "
        "Для завершения нажмите «Завершить отправку» или используйте /done.",
        reply_markup=photo_batch_keyboard(),
    )
    return PHOTO


async def cancel(update, context):
    visit_id = context.user_data.get("visit_id")
    await asyncio.to_thread(finish_visit, visit_id, "cancelled")
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --- Подсказки вместо молчания -------------------------------------------------
# Раньше бот ничего не отвечал, если сообщение не подходило к текущему шагу
# (текст вместо фото, фото до ввода врача, фото после перезапуска бота и т. п.).

async def photo_step_text_hint(update, context):
    await reply_text_with_retry(
        update.message,
        "Сейчас бот ждёт фотографию. Отправьте фото или нажмите «Завершить отправку».",
        reply_markup=photo_batch_keyboard(),
    )
    return PHOTO


async def hospital_step_hint(update, context):
    hospitals = context.user_data.get("photo_hospitals") or []
    await reply_text_with_retry(
        update.message,
        "Сначала выберите роддом кнопкой ниже, затем введите ФИО врача.",
        reply_markup=(
            ReplyKeyboardMarkup([[hospital] for hospital in hospitals],
                                resize_keyboard=True, one_time_keyboard=True)
            if hospitals else None
        ),
    )
    return PHOTO_HOSPITAL


async def doctor_step_hint(update, context):
    await reply_text_with_retry(
        update.message,
        "Сначала введите ФИО врача текстом, потом отправляйте фотографии.",
    )
    return DOCTOR_NAME


async def photo_outside_session(update, context):
    """Фото пришло вне сеанса отправки — например, после перезапуска бота."""
    if await deny_unlisted_user(update):
        return
    await reply_text_with_retry(
        update.message,
        "Фото не сохранено: сначала начните отправку командой /send_photo "
        "или кнопкой «📷 Отправить фото», выберите роддом и введите ФИО врача.",
    )


async def unknown_message(update, context):
    if await deny_unlisted_user(update):
        return
    await reply_text_with_retry(
        update.message,
        "Не понял сообщение. Откройте меню командой /menu.",
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик ошибок: пишет в лог и вежливо отвечает пользователю."""
    error = context.error
    if isinstance(error, Conflict):
        logging.error(
            "Telegram сообщает о конфликте: с этим токеном уже запущен другой экземпляр бота. "
            "Остановите лишний процесс (локальный или на сервере)."
        )
        return
    if isinstance(error, (TimedOut, NetworkError)) and not isinstance(error, BadRequest):
        logging.warning("Временная сетевая ошибка Telegram: %s", error)
        return
    logging.error("Необработанная ошибка при обработке обновления", exc_info=error)
    message = getattr(update, "effective_message", None) if isinstance(update, Update) else None
    if message is not None:
        try:
            await message.reply_text(
                "Произошла ошибка. Попробуйте ещё раз или начните заново командой /menu."
            )
        except Exception:
            logging.warning("Не удалось сообщить пользователю об ошибке", exc_info=True)


BOT_COMMANDS = (
    ("menu", "Главное меню"),
    ("send_photo", "Отправить фотографии"),
    ("done", "Завершить отправку фото"),
    ("report", "Отчёт по фотографиям"),
    ("my_photos", "Мои фото по роддому"),
    ("comments", "Комментарии оператора"),
    ("profile", "Мой профиль"),
    ("my_id", "Узнать свой Telegram ID"),
    ("cancel", "Отменить текущее действие"),
)


async def on_startup(application: Application):
    """post_init: меню команд в Telegram и возобновление фоновых проверок."""
    try:
        await application.bot.set_my_commands(list(BOT_COMMANDS))
    except Exception:
        logging.warning("Не удалось установить меню команд бота", exc_info=True)
    if CREATOR_TELEGRAM_ID is None:
        logging.warning(
            "CREATOR_TELEGRAM_ID не задан в .env: в бот смогут войти только сотрудники "
            "из списка доступа. Узнайте свой ID командой /my_id."
        )
    await resume_pending_photo_processing(application)


def safe_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", value).strip(". ") or "роддом"


def normalize_excel_header(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", " ", text).strip()


def excel_doctor_header(value) -> bool:
    header = normalize_excel_header(value)
    return header in {
        "врач", "доктор", "фио врача", "ф и о врача", "фамилия имя отчество врача",
    }


def excel_photo_count_header(value) -> bool:
    header = normalize_excel_header(value)
    return (
        header in {"количество фото", "кол во фото", "число фото"}
        or ("количество" in header and ("фото" in header or "фотограф" in header))
    )


def excel_photo_count(value, default=1) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip().replace(" ", "").replace(",", "."))
        except ValueError:
            return default
    if number < 0 or not number.is_integer():
        return default
    return int(number)


def extract_doctor_photo_counts(file_bytes: bytes):
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
            unpacked_size = sum(item.file_size for item in archive.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Файл повреждён или не является книгой Excel.") from exc
    if unpacked_size > EXCEL_UPLOAD_MAX_UNPACKED_BYTES:
        raise ValueError("Файл слишком большой после распаковки. Уменьшите его и отправьте снова.")

    try:
        workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Не удалось открыть Excel-файл. Пересохраните его в формате .xlsx.") from exc

    counts = {}
    display_names = {}
    rows_scanned = 0
    skipped_sheet_names = {
        "итог", "итоги", "сводка", "summary", "итог по врачам",
        "количество фотографий по врачам",
    }

    def add_count(raw_name, amount):
        doctor_name = re.sub(r"\s+", " ", str(raw_name or "")).strip()
        normalized_name = doctor_name.casefold().replace("ё", "е")
        if not doctor_name or normalized_name in {
            "врач", "фио врача", "ф и о врача", "итого", "всего", "общий итог", "total", "grand total",
        }:
            return
        display_names.setdefault(normalized_name, doctor_name)
        counts[normalized_name] = counts.get(normalized_name, 0) + amount

    try:
        for sheet in workbook.worksheets:
            if len(workbook.worksheets) > 1 and normalize_excel_header(sheet.title) in skipped_sheet_names:
                continue
            max_column = min(max(sheet.max_column or 1, 1), 100)
            max_header_row = min(max(sheet.max_row or 1, 1), 50)
            doctor_column = None
            count_column = None
            header_row = None

            for row_number, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=max_header_row, max_col=max_column, values_only=True),
                start=1,
            ):
                for column_number, value in enumerate(row, start=1):
                    if doctor_column is None and excel_doctor_header(value):
                        doctor_column = column_number
                    if count_column is None and excel_photo_count_header(value):
                        count_column = column_number
                if doctor_column is not None:
                    header_row = row_number
                    break

            if header_row is not None:
                needed_column = max(doctor_column, count_column or 0)
                for row in sheet.iter_rows(
                    min_row=header_row + 1, max_col=needed_column, values_only=True,
                ):
                    rows_scanned += 1
                    if rows_scanned > EXCEL_UPLOAD_MAX_ROWS:
                        raise ValueError(
                            "В файле слишком много строк. Оставьте не более 200 000 строк с фотографиями."
                        )
                    doctor_name = row[doctor_column - 1]
                    amount = excel_photo_count(row[count_column - 1], 1) if count_column else 1
                    add_count(doctor_name, amount)
                continue
            summary_count = None
            for row in sheet.iter_rows(
                min_row=1, max_row=min(max(sheet.max_row or 1, 1), 10),
                max_col=min(max_column, 20), values_only=True,
            ):
                for column_index, value in enumerate(row):
                    if excel_photo_count_header(value) and column_index + 1 < len(row):
                        summary_count = excel_photo_count(row[column_index + 1], 0)
                        break
                if summary_count is not None:
                    break
            if summary_count is not None:
                add_count(sheet.title, summary_count)
    finally:
        workbook.close()

    if not counts:
        raise ValueError(
            "В файле не найден столбец «Врач» и строки с фотографиями. "
            "Проверьте заголовки таблицы."
        )
    return sorted(
        ((display_names[key], count) for key, count in counts.items()),
        key=lambda item: (-item[1], item[0].casefold()),
    )


def build_doctor_photo_count_report(counts, source_filename: str) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Итог по врачам"
    sheet.sheet_view.showGridLines = False
    sheet["A1"] = "Количество фотографий по врачам"
    sheet["A1"].font = Font(size=15, bold=True, color="282139")
    sheet["A2"] = f"Источник: {source_filename}"
    sheet["A2"].font = Font(italic=True, color="665F72")
    sheet.append([])
    sheet.append(["Врач", "Количество фотографий"])

    header_fill = PatternFill("solid", fgColor="5B3A86")
    light_border = Border(bottom=Side(style="thin", color="D9D2E3"))
    for cell in sheet[4]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for doctor_name, photo_count in counts:
        sheet.append([doctor_name, photo_count])
        current_row = sheet.max_row
        sheet.cell(current_row, 1).alignment = Alignment(vertical="center")
        sheet.cell(current_row, 2).alignment = Alignment(horizontal="right", vertical="center")
        sheet.cell(current_row, 2).number_format = "0"
        for cell in sheet[current_row]:
            cell.border = light_border

    total_row = sheet.max_row + 1
    sheet.cell(total_row, 1, "Всего")
    sheet.cell(total_row, 2, sum(count for _, count in counts))
    for cell in sheet[total_row]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="EEE8F5")
    sheet.cell(total_row, 2).number_format = "0"

    sheet.column_dimensions["A"].width = 42
    sheet.column_dimensions["B"].width = 26
    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:B{max(4, total_row - 1)}"

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output


async def excel_doctor_report_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Оператор":
        await update.message.reply_text("Обработка Excel доступна только зарегистрированным операторам.")
        return
    context.user_data["awaiting_excel_doctor_report"] = True
    await update.message.reply_text(
        "Отправьте Excel-файл в формате .xlsx или .xlsm. "
        "Бот посчитает фотографии для каждого врача и вернёт итоговую таблицу."
    )


async def operator_excel_doctor_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if not user or user["role"] != "Оператор":
        await update.message.reply_text("Обработка Excel доступна только зарегистрированным операторам.")
        return

    document = update.message.document
    filename = document.file_name or "таблица.xlsx"
    extension = Path(filename).suffix.casefold()
    if extension == ".xls":
        await update.message.reply_text(
            "Старый формат .xls не поддерживается. Откройте файл в Excel, "
            "сохраните как .xlsx и отправьте снова."
        )
        return
    if extension not in {".xlsx", ".xlsm"}:
        await update.message.reply_text("Отправьте файл в формате .xlsx или .xlsm.")
        return
    if document.file_size and document.file_size > EXCEL_UPLOAD_MAX_BYTES:
        await update.message.reply_text("Excel-файл слишком большой. Максимальный размер — 20 МБ.")
        return

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        file_bytes = bytes(await telegram_file.download_as_bytearray())
        if len(file_bytes) > EXCEL_UPLOAD_MAX_BYTES:
            raise ValueError("Excel-файл слишком большой. Максимальный размер — 20 МБ.")
        counts = await asyncio.to_thread(extract_doctor_photo_counts, file_bytes)
        report = await asyncio.to_thread(build_doctor_photo_count_report, counts, filename)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        logging.exception("Не удалось обработать загруженный Excel-файл %s", filename)
        await update.message.reply_text(
            "Не удалось обработать файл. Проверьте, что это исправная книга Excel формата .xlsx."
        )
        return
    finally:
        context.user_data.pop("awaiting_excel_doctor_report", None)

    output_name = safe_filename(f"{Path(filename).stem}_итог_по_врачам") + ".xlsx"
    total_photos = sum(count for _, count in counts)
    await update.message.reply_document(
        document=report,
        filename=output_name,
        caption=f"Готово: {len(counts)} врачей, {total_photos} фотографий.",
    )


def excel_sheet_title(value: str, existing_titles):
    base = re.sub(r'[\\/:*?\[\]]+', " ", value).strip() or "Без врача"
    base = base[:31]
    title = base
    number = 2
    while title in existing_titles:
        suffix = f" ({number})"
        title = base[:31 - len(suffix)] + suffix
        number += 1
    return title


def record_is_copy_or_crop(record):
    try:
        status = (record["review_status"] or "").casefold()
    except (IndexError, KeyError):
        status = ""
    return "коп" in status or "обрез" in status


def fill_doctor_sheet(sheet, records):
    sheet.sheet_view.showGridLines = False
    sheet["C1"] = "Количество фото"
    sheet["D1"] = len(records)
    sheet["I1"] = "Сумма"
    sheet["J1"] = 0
    for cell in (sheet["C1"], sheet["D1"], sheet["I1"], sheet["J1"]):
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    headers = [
        "Дата отправленного фото", "Врач", "Номер телефона врача", "Фото", "Количество фотографий",
        "Комментарий к фото", "Дата на упаковке", "ФИО Роженицы", "Номер телефона", "Цена",
        "Номер на упаковке",
    ]
    sheet.append([])
    sheet.append(headers)
    header_fill = PatternFill("solid", fgColor="FFFF00")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    for cell in sheet[3]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    widths = [22, 28, 23, 28, 10, 30, 22, 28, 22, 14, 20]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    sheet.freeze_panes = "A4"

    for photo_number, record in enumerate(records, start=1):
        row_number = photo_number + 3
        sent_at = datetime.fromisoformat(record["sent_at"]).replace(tzinfo=None)
        package_date = None
        if record["photo_date"]:
            try:
                package_date = datetime.strptime(record["photo_date"], "%Y-%m-%d")
            except ValueError:
                package_date = record["photo_date"]
        sheet.append([
            sent_at, record["doctor_name"], "", "", 1, "", package_date,
            record["mother_name"] or "", "", "", record["package_number"] or "",
        ])
        sheet.row_dimensions[row_number].height = 115
        for cell in sheet[row_number]:
            cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
            cell.border = thin_border
        sheet.cell(row_number, 1).number_format = "DD.MM.YYYY"
        sheet.cell(row_number, 7).number_format = "DD.MM.YYYY"
        try:
            image = ExcelImage(BytesIO(record["image"]))
            image.width = 150
            image.height = 110
            sheet.add_image(image, f"D{row_number}")
        except Exception:
            sheet.cell(row_number, 4).value = "Не удалось встроить фотографию"


def build_hospital_report(records, city: str, hospital: str, requested_date: str, report_path: Path):
    workbook = Workbook()
    records = [record for record in records if not record_is_copy_or_crop(record)]
    if not records:
        sheet = workbook.active
        sheet.title = "Нет подходящих фото"
        sheet["A1"] = "Копии и обрезанные фотографии исключены из этого файла."
        workbook.save(report_path)
        return
    doctors = {}
    for record in records:
        doctor = (record["doctor_name"] or "Без указанного врача").strip()
        doctors.setdefault(doctor, []).append(record)

    for index, (doctor, doctor_records) in enumerate(doctors.items()):
        sheet = workbook.active if index == 0 else workbook.create_sheet()
        sheet.title = excel_sheet_title(doctor, workbook.sheetnames[:-1] if index else [])
        fill_doctor_sheet(sheet, doctor_records)
    workbook.save(report_path)


def update_hospital_excel_file(city: str, hospital: str):
    with connect() as con:
        records = con.execute(
            """
            SELECT p.id, p.image, p.sent_at, p.photo_date, p.package_number,
                   sender.city, COALESCE(p.maternity_hospital, sender.maternity_hospital) AS maternity_hospital,
                   COALESCE(p.doctor_name, medic.full_name, '') AS doctor_name,
                   p.mother_name, p.mother_phone, p.review_status
            FROM photos AS p
            JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
            WHERE sender.city = ? AND COALESCE(p.maternity_hospital, sender.maternity_hospital) = ?
            ORDER BY p.sent_at
            """,
            (city, hospital),
        ).fetchall()
    if not records:
        return
    city_folder = EXCEL_REPORTS_DIR / safe_filename(city)
    city_folder.mkdir(parents=True, exist_ok=True)
    report_path = city_folder / f"{safe_filename(hospital)}.xlsx"
    build_hospital_report(records, city, hospital, "все_даты", report_path)


def backfill_missing_qr_batch(limit: int = QR_BACKFILL_BATCH_SIZE):
    """Rescan every photo created by an older scanner without erasing good data."""
    with connect() as con:
        rows = con.execute(
            """SELECT p.id, p.image, p.qr_data,
                      COALESCE(medic.city, sender.city, '') AS city
               FROM photos AS p
               LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE COALESCE(p.qr_scan_version, 0) < ?
                 AND (
                   p.qr_data IS NULL OR TRIM(p.qr_data) = ''
                   OR TRIM(p.qr_data) NOT GLOB
                      '[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
                 )
               ORDER BY CASE WHEN p.qr_data IS NULL OR TRIM(p.qr_data) = '' THEN 0 ELSE 1 END,
                        p.id DESC
               LIMIT ?""",
            (QR_SCAN_VERSION, limit),
        ).fetchall()

    found = 0
    updated = 0
    for row in rows:
        scanned_qr_data = None
        try:
            scanned_qr_data = canonical_qr_data(read_qr_code(row["image"]))
        except Exception:
            logging.exception("Повторное сканирование QR для фото id=%s не выполнено", row["id"])

        current_qr_data = canonical_qr_data(row["qr_data"])
        # A successfully decoded regional serial is stronger than an unrelated
        # product URL. If this pass cannot decode anything, retain previously
        # confirmed data instead of making it disappear from the website.
        if scanned_qr_data and (
            QR_REGION_CODE_PATTERN.fullmatch(scanned_qr_data)
            or not current_qr_data
            or not QR_REGION_CODE_PATTERN.fullmatch(current_qr_data)
        ):
            qr_data = scanned_qr_data
        else:
            qr_data = current_qr_data

        if scanned_qr_data:
            found += 1
        if qr_data != current_qr_data:
            updated += 1
        with connect() as con:
            con.execute(
                """UPDATE photos
                   SET qr_data = ?, code_status = ?, qr_scan_version = ?
                   WHERE id = ?""",
                (qr_data, qr_code_status(qr_data, row["city"]), QR_SCAN_VERSION, row["id"]),
            )
    return len(rows), found, updated


async def backfill_missing_qr_codes():
    total_processed = 0
    total_found = 0
    total_updated = 0
    while True:
        processed, found, updated = await asyncio.to_thread(backfill_missing_qr_batch)
        total_processed += processed
        total_found += found
        total_updated += updated
        if not processed:
            break
        logging.info(
            "Повторная проверка QR: обработано %s, распознано %s, обновлено %s",
            total_processed,
            total_found,
            total_updated,
        )
        # The portal and bot share one small Render instance. Throttling the
        # historical rescan prevents it from starving normal website requests.
        await asyncio.sleep(2)
    # Recalculate automatic decisions once after all QR values are refreshed.
    # Manual operator decisions remain untouched and continue to take priority.
    with connect() as con:
        backfill_photo_checks(con)
    logging.info(
        "Повторная проверка QR завершена: обработано %s, распознано %s, обновлено %s",
        total_processed,
        total_found,
        total_updated,
    )


def pending_full_qr_recheck_photo_ids(limit: int = FULL_QR_RECHECK_BATCH_SIZE):
    """Return historical photos whose complete regional QR number is still absent."""
    with connect() as con:
        rows = con.execute(
            """SELECT p.id
               FROM photos AS p
               WHERE COALESCE(p.full_qr_recheck_version, 0) < ?
                 AND (
                   COALESCE(p.full_qr_recheck_failure_version, 0) <> ?
                   OR COALESCE(p.full_qr_recheck_failures, 0) < ?
                 )
                 AND COALESCE(p.review_status, '') <> 'Проверяется в фоне'
                 AND (
                   p.qr_data IS NULL OR TRIM(p.qr_data) = ''
                   OR TRIM(p.qr_data) NOT GLOB
                      '[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
                   OR LOWER(COALESCE(p.code_status, '')) LIKE '%вне диапазона%'
                   OR LOWER(COALESCE(p.code_status, '')) LIKE '%код другого региона%'
                 )
               ORDER BY COALESCE(p.full_qr_recheck_failures, 0), p.id DESC
               LIMIT ?""",
            (
                FULL_QR_RECHECK_VERSION, FULL_QR_RECHECK_VERSION,
                FULL_QR_RECHECK_MAX_FAILURES, limit,
            ),
        ).fetchall()
    return [row["id"] for row in rows]


def load_full_qr_recheck_photo(photo_id: int):
    with connect() as con:
        return con.execute(
            """SELECT p.id, p.image, p.qr_data, p.review_status, p.review_reason,
                      p.original_photo_id,
                      COALESCE(medic.city, sender.city, '') AS city
               FROM photos AS p
               LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.id = ?""",
            (photo_id,),
        ).fetchone()


def mark_full_qr_recheck_failure(photo_id: int):
    with connect() as con:
        con.execute(
            """UPDATE photos
               SET full_qr_recheck_failures = CASE
                     WHEN COALESCE(full_qr_recheck_failure_version, 0) = ?
                     THEN full_qr_recheck_failures + 1 ELSE 1 END,
                   full_qr_recheck_failure_version = ?
               WHERE id = ?""",
            (FULL_QR_RECHECK_VERSION, FULL_QR_RECHECK_VERSION, photo_id),
        )


def refresh_stored_photo_check(photo_id: int):
    """Recalculate the automatic decision while preserving the operator override."""
    with connect() as con:
        row = con.execute(
            """SELECT p.review_status, p.code_status, p.original_photo_id,
                      checks.product_status, checks.material_status,
                      checks.location_status, checks.assignment_status
               FROM photos AS p
               LEFT JOIN photo_checks AS checks ON checks.photo_id = p.id
               WHERE p.id = ?""",
            (photo_id,),
        ).fetchone()
    if not row:
        return
    product_status = str(row["product_status"] or "").strip()
    material_status = str(row["material_status"] or "").strip()
    is_nuppi_1 = (
        True if product_status == "NUPPI 1 обнаружен"
        else False if product_status == "NUPPI 1 не подтверждён"
        else None
    )
    is_reference_design = (
        True if material_status == "Эталонный дизайн подтверждён"
        else False if material_status == "Эталонный дизайн не подтверждён"
        else None
    )
    save_photo_check(
        photo_id,
        automatic_photo_decision(
            row["review_status"],
            row["code_status"],
            is_nuppi_1=is_nuppi_1,
            is_reference_design=is_reference_design,
            original_photo_id=row["original_photo_id"],
            assignment_status=row["assignment_status"],
            location_required=(
                str(row["location_status"] or "").strip() == "Требуется"
            ),
        ),
    )


def _qr_recheck_review_state(row, qr_data, recovery):
    review_status = row["review_status"] or "Не проверено"
    review_reason = row["review_reason"] or "Нужна ручная проверка оператором."
    normalized_review = review_status.casefold()
    is_duplicate = bool(row["original_photo_id"]) or "коп" in normalized_review
    code_visible = bool(qr_data) or bool(recovery and recovery["code_visible"])
    code_obstructed = bool(recovery and recovery["code_obstructed"])

    if qr_data:
        code_status = qr_code_status(qr_data, row["city"])
    elif code_obstructed:
        code_status = "Ошибка: на QR/Data Matrix нанесена надпись или код перекрыт"
        if not is_duplicate:
            review_status = "Ошибка: QR-код перекрыт"
            review_reason = (
                "На элементах QR/Data Matrix обнаружена надпись, линия, закрашивание "
                "или другое перекрытие. Фотография требует проверки оператора."
            )
    elif code_visible:
        code_status = "Ошибка: QR/Data Matrix виден, но полный номер не прочитан"
        if not is_duplicate:
            reason_labels = {
                "blurred": "код размыт",
                "glare": "код закрыт бликом",
                "cropped": "код обрезан краем фотографии",
                "too_small": "код слишком мелкий",
            }
            unreadable_reason = reason_labels.get(
                recovery["unreadable_reason"], "часть цифр неразборчива",
            )
            review_status = "Ошибка: QR-код не прочитан"
            review_reason = (
                "QR/Data Matrix обнаружен, но полный номер не удалось прочитать: "
                f"{unreadable_reason}. Нужна проверка оператора; цифры не угадывались."
            )
    else:
        code_status = "Код отсутствует (допустимо)"

    # Remove only an obsolete automatic QR error. Other visual, duplicate and
    # operator decisions must survive the historical recheck unchanged.
    if (qr_data or not code_visible) and normalized_review in {
        "ошибка: qr-код не прочитан", "ошибка: qr-код перекрыт",
    }:
        review_status = "Нет явных признаков"
        review_reason = "QR/Data Matrix повторно проверен по исходной фотографии."
    return review_status, review_reason, code_status


async def recheck_historical_photo_qr(photo_id: int):
    """Run the dedicated high-accuracy QR pipeline without other paid checks."""
    row = await asyncio.to_thread(load_full_qr_recheck_photo, photo_id)
    if not row:
        return False, "deleted"

    current_qr = canonical_qr_data(row["qr_data"])
    if (current_qr and QR_REGION_CODE_PATTERN.fullmatch(current_qr)
            and ai_qr_is_plausible_for_city(current_qr, row["city"])):
        with connect() as con:
            con.execute(
                """UPDATE photos
                   SET qr_data = ?, code_status = ?, qr_scan_version = ?,
                       full_qr_recheck_version = ?, full_qr_recheck_failures = 0
                   WHERE id = ?""",
                (
                    current_qr, qr_code_status(current_qr, row["city"]),
                    QR_SCAN_VERSION, FULL_QR_RECHECK_VERSION, photo_id,
                ),
            )
        await asyncio.to_thread(refresh_stored_photo_check, photo_id)
        return True, "already_read"

    try:
        locally_read = canonical_qr_data(
            await asyncio.to_thread(read_qr_code, row["image"])
        )
    except Exception:
        logging.exception("Локальное повторное чтение QR для фото id=%s не выполнено", photo_id)
        locally_read = None

    recovery = None
    if not (locally_read and QR_REGION_CODE_PATTERN.fullmatch(locally_read)):
        recovery = await recover_missing_qr_data(row["image"])

    recovered_qr = canonical_qr_data(
        recovery["qr_data"] if recovery else None
    )
    if (recovered_qr and QR_REGION_CODE_PATTERN.fullmatch(recovered_qr)
            and ai_qr_is_plausible_for_city(recovered_qr, row["city"])):
        qr_data = recovered_qr
    elif locally_read:
        # A decoded foreign/product QR is retained so policy can reject it.
        qr_data = locally_read
    else:
        qr_data = None

    # With neither a local payload nor a completed focused pass we cannot
    # honestly decide that the code is absent. Leave the row resumable.
    if qr_data is None and recovery is None:
        await asyncio.to_thread(mark_full_qr_recheck_failure, photo_id)
        return False, "retry"

    review_status, review_reason, code_status = _qr_recheck_review_state(
        row, qr_data, recovery,
    )
    with connect() as con:
        con.execute(
            """UPDATE photos
               SET qr_data = ?, code_status = ?, review_status = ?, review_reason = ?,
                   qr_scan_version = ?, full_qr_recheck_version = ?,
                   full_qr_recheck_failures = 0
               WHERE id = ?""",
            (
                qr_data, code_status, review_status, review_reason,
                QR_SCAN_VERSION, FULL_QR_RECHECK_VERSION, photo_id,
            ),
        )
    await asyncio.to_thread(refresh_stored_photo_check, photo_id)
    if qr_data and QR_REGION_CODE_PATTERN.fullmatch(qr_data):
        return True, "read"
    if qr_data:
        return True, "foreign"
    if recovery["code_visible"]:
        return True, "unreadable"
    return True, "absent"


def full_qr_recheck_remaining_count():
    with connect() as con:
        return con.execute(
            """SELECT COUNT(*) FROM photos
               WHERE COALESCE(full_qr_recheck_version, 0) < ?
                 AND (
                   qr_data IS NULL OR TRIM(qr_data) = ''
                   OR TRIM(qr_data) NOT GLOB
                      '[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
                   OR LOWER(COALESCE(code_status, '')) LIKE '%вне диапазона%'
                   OR LOWER(COALESCE(code_status, '')) LIKE '%код другого региона%'
                 )""",
            (FULL_QR_RECHECK_VERSION,),
        ).fetchone()[0]


async def run_full_historical_qr_recheck():
    """Resume until every historical photo has a QR-only result or three failures."""
    if not ai_client:
        logging.warning(
            "Полная QR-перепроверка Claude приостановлена: ANTHROPIC_API_KEY не задан."
        )
        return
    totals = {"read": 0, "foreign": 0, "unreadable": 0, "absent": 0, "failed": 0}
    processed = 0
    while True:
        photo_ids = await asyncio.to_thread(pending_full_qr_recheck_photo_ids)
        if not photo_ids:
            break
        for photo_id in photo_ids:
            try:
                success, result = await recheck_historical_photo_qr(photo_id)
            except Exception:
                logging.exception("Полная QR-перепроверка фото id=%s не выполнена", photo_id)
                await asyncio.to_thread(mark_full_qr_recheck_failure, photo_id)
                success, result = False, "failed"
            processed += 1
            if success and result in totals:
                totals[result] += 1
            elif not success:
                totals["failed"] += 1
            if processed == 1 or processed % 10 == 0:
                logging.info(
                    "Полная QR-перепроверка: обработано %s; номера %s; "
                    "посторонние %s; не читаются %s; без кода %s; ошибок %s",
                    processed, totals["read"], totals["foreign"],
                    totals["unreadable"], totals["absent"], totals["failed"],
                )
            await asyncio.sleep(FULL_QR_RECHECK_DELAY_SECONDS)

    remaining = await asyncio.to_thread(full_qr_recheck_remaining_count)
    if remaining:
        logging.warning(
            "Полная QR-перепроверка остановлена после повторных ошибок: осталось %s фото. "
            "После восстановления API увеличьте FULL_QR_RECHECK_VERSION для нового прохода.",
            remaining,
        )
    else:
        logging.info(
            "Полная QR-перепроверка завершена: обработано %s; номера %s; "
            "посторонние %s; не читаются %s; без кода %s.",
            processed, totals["read"], totals["foreign"],
            totals["unreadable"], totals["absent"],
        )


def tashkent_date_range_utc(date_from: str, date_to: str) -> tuple[str, str]:
    """Return inclusive Tashkent calendar dates as a half-open UTC range."""
    first_date = datetime.strptime(date_from, "%Y-%m-%d").date()
    last_date = datetime.strptime(date_to, "%Y-%m-%d").date()
    if first_date > last_date:
        raise ValueError("Начальная дата перепроверки позже конечной.")
    start_local = datetime.combine(first_date, datetime.min.time(), tzinfo=TASHKENT_TIMEZONE)
    end_local = datetime.combine(
        last_date + timedelta(days=1), datetime.min.time(), tzinfo=TASHKENT_TIMEZONE,
    )
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


def pending_targeted_no_qr_recheck_jobs():
    start_utc, end_utc = tashkent_date_range_utc(
        TARGETED_NO_QR_RECHECK_DATE_FROM,
        TARGETED_NO_QR_RECHECK_DATE_TO,
    )
    with connect() as con:
        rows = con.execute(
            """SELECT p.id,
                      COALESCE(medic.city, sender.city, '') AS city,
                      COALESCE(p.maternity_hospital, medic.maternity_hospital,
                               sender.maternity_hospital, '') AS hospital
               FROM photos AS p
               LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.sent_at >= ? AND p.sent_at < ?
                 AND (p.qr_data IS NULL OR TRIM(p.qr_data) = '')
                 AND COALESCE(p.targeted_recheck_version, 0) < ?
               ORDER BY p.id""",
            (start_utc, end_utc, TARGETED_NO_QR_RECHECK_VERSION),
        ).fetchall()
    return [(row["id"], row["city"], row["hospital"]) for row in rows]


def mark_targeted_no_qr_recheck_complete(photo_id: int):
    with connect() as con:
        con.execute(
            "UPDATE photos SET targeted_recheck_version = ? WHERE id = ?",
            (TARGETED_NO_QR_RECHECK_VERSION, photo_id),
        )


async def run_targeted_no_qr_recheck():
    """Throttled one-time audit for missing-QR photos on the requested dates."""
    try:
        jobs = await asyncio.to_thread(pending_targeted_no_qr_recheck_jobs)
    except (TypeError, ValueError):
        logging.exception(
            "Некорректный период целевой перепроверки: %s — %s",
            TARGETED_NO_QR_RECHECK_DATE_FROM,
            TARGETED_NO_QR_RECHECK_DATE_TO,
        )
        return

    total = len(jobs)
    if not total:
        logging.info(
            "Целевая перепроверка %s — %s уже завершена или подходящих фото нет.",
            TARGETED_NO_QR_RECHECK_DATE_FROM,
            TARGETED_NO_QR_RECHECK_DATE_TO,
        )
        return

    logging.info(
        "Начинается целевая перепроверка %s фотографий без QR за %s — %s.",
        total,
        TARGETED_NO_QR_RECHECK_DATE_FROM,
        TARGETED_NO_QR_RECHECK_DATE_TO,
    )
    completed = 0
    failed = 0
    for photo_id, city, hospital in jobs:
        success = await process_photo_in_background(
            photo_id,
            city,
            hospital,
            duplicate_fallback_limit=TARGETED_NO_QR_DUPLICATE_FALLBACK,
        )
        if success:
            await asyncio.to_thread(mark_targeted_no_qr_recheck_complete, photo_id)
            completed += 1
        else:
            failed += 1
        logging.info(
            "Целевая перепроверка: готово %s/%s, ошибок %s",
            completed + failed,
            total,
            failed,
        )
        # Keep CPU and the shared SQLite database available to the site and bot.
        await asyncio.sleep(TARGETED_NO_QR_RECHECK_DELAY_SECONDS)

    logging.info(
        "Целевая перепроверка завершена: успешно %s, ошибок %s.",
        completed,
        failed,
    )


def pending_urgench_duplicate_audit_photo_ids():
    """Return every not-yet-audited Urgench photo across all its hospitals."""
    with connect() as con:
        rows = con.execute(
            """SELECT p.id,
                      COALESCE(medic.city, sender.city, '') AS city
               FROM photos AS p
               LEFT JOIN users AS medic ON medic.telegram_id = p.medic_telegram_id
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE COALESCE(p.duplicate_audit_version, 0) < ?
               ORDER BY p.id""",
            (URGENCH_DUPLICATE_AUDIT_VERSION,),
        ).fetchall()
    return [
        int(row["id"]) for row in rows
        if "ургенч" in str(row["city"] or "").strip().casefold()
    ]


def load_duplicate_audit_photo(photo_id: int):
    with connect() as con:
        row = con.execute(
            """SELECT id, sent_at, image, image_digest, image_hash, crop_hash,
                      visual_hashes, qr_data, photo_date, mother_name,
                      review_status, review_reason, code_status, original_photo_id
               FROM photos WHERE id = ?""",
            (photo_id,),
        ).fetchone()
    return dict(row) if row else None


def store_duplicate_audit_fingerprints(
        photo_id: int, image_digest: str, image_hash: str,
        crop_hash: str | None, visual_hashes: str | None):
    with connect() as con:
        con.execute(
            """UPDATE photos
               SET image_digest = ?, image_hash = ?, crop_hash = ?, visual_hashes = ?
               WHERE id = ?""",
            (image_digest, image_hash, crop_hash, visual_hashes, photo_id),
        )


def finish_duplicate_audit_photo(
        photo_id: int, duplicate=None, reason: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    with connect() as con:
        if duplicate:
            con.execute(
                """UPDATE photos
                   SET original_photo_id = ?, review_status = 'Возможная копия',
                       review_reason = ?, duplicate_audit_version = ?
                   WHERE id = ?""",
                (
                    duplicate["id"], reason, URGENCH_DUPLICATE_AUDIT_VERSION,
                    photo_id,
                ),
            )
            con.execute(
                """INSERT INTO photo_checks
                   (photo_id, duplicate_status, decision, decision_reason, automated_at)
                   VALUES (?, 'Возможный повтор', 'review', ?, ?)
                   ON CONFLICT(photo_id) DO UPDATE SET
                     duplicate_status = 'Возможный повтор',
                     decision = 'review',
                     decision_reason = excluded.decision_reason,
                     automated_at = excluded.automated_at""",
                (photo_id, reason, now),
            )
        else:
            con.execute(
                """UPDATE photos SET duplicate_audit_version = ? WHERE id = ?""",
                (URGENCH_DUPLICATE_AUDIT_VERSION, photo_id),
            )
            con.execute(
                """UPDATE photo_checks
                   SET duplicate_status = CASE
                         WHEN EXISTS (
                           SELECT 1 FROM photos
                           WHERE id = ? AND original_photo_id IS NOT NULL
                         ) THEN 'Возможный повтор'
                         ELSE 'Повтор не обнаружен'
                       END,
                       automated_at = ?
                   WHERE photo_id = ?""",
                (photo_id, now, photo_id),
            )


async def audit_urgench_duplicate_photo(photo_id: int):
    """Run deterministic duplicate checks without visual-similarity guesses."""
    async with photo_processing_semaphore:
        photo = await asyncio.to_thread(load_duplicate_audit_photo, photo_id)
        if not photo:
            return True, None
        if photo["original_photo_id"]:
            await asyncio.to_thread(finish_duplicate_audit_photo, photo_id)
            return True, photo["original_photo_id"]

        image_bytes = bytes(photo["image"])
        image_digest = photo["image_digest"] or hashlib.sha256(image_bytes).hexdigest()
        if not photo["image_digest"]:
            await asyncio.to_thread(
                store_duplicate_audit_fingerprints,
                photo_id, image_digest, photo["image_hash"], photo["crop_hash"],
                photo["visual_hashes"],
            )

        duplicate = None
        reason = ""
        qr_data = canonical_qr_data(photo["qr_data"])
        if qr_data:
            duplicate = await asyncio.to_thread(
                find_photo_by_qr, qr_data, photo_id,
            )
            if duplicate:
                reason = (
                    "Такой же QR-код есть у фотографии от "
                    f"{duplicate['sent_at']}."
                )
        if not duplicate:
            duplicate = await asyncio.to_thread(
                find_exact_photo_by_digest, image_digest, photo_id,
            )
            if duplicate:
                reason = (
                    "Повторно отправлен тот же файл фотографии от "
                    f"{duplicate['sent_at']}."
                )
        await asyncio.to_thread(
            finish_duplicate_audit_photo, photo_id, duplicate, reason,
        )
        return True, duplicate["id"] if duplicate else None


async def run_urgench_duplicate_audit():
    """Throttled, resumable audit of all Urgench hospitals."""
    await asyncio.sleep(20)
    jobs = await asyncio.to_thread(pending_urgench_duplicate_audit_photo_ids)
    total = len(jobs)
    if not total:
        logging.info("Полная проверка копий по Ургенчу уже завершена.")
        return
    logging.info(
        "Начинается полная проверка копий по Ургенчу: %s фотографий, все роддома.",
        total,
    )
    completed = 0
    duplicates = 0
    failed = 0
    for photo_id in jobs:
        try:
            success, duplicate_id = await audit_urgench_duplicate_photo(photo_id)
        except Exception:
            logging.exception(
                "Проверка копий по Ургенчу не выполнена для id=%s", photo_id,
            )
            success, duplicate_id = False, None
        if success:
            completed += 1
            duplicates += duplicate_id is not None
        else:
            failed += 1
        processed = completed + failed
        if processed == 1 or processed % 25 == 0 or processed == total:
            logging.info(
                "Проверка копий по Ургенчу: %s/%s, найдено %s, ошибок %s.",
                processed, total, duplicates, failed,
            )
        await asyncio.sleep(URGENCH_DUPLICATE_AUDIT_DELAY_SECONDS)
    logging.info(
        "Проверка копий по Ургенчу завершена: проверено %s, найдено %s, ошибок %s.",
        completed, duplicates, failed,
    )


def photo_database_audit():
    """Check every saved photo row without loading or reprocessing images."""
    with connect() as con:
        summary = con.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN p.image IS NULL OR LENGTH(p.image) = 0 THEN 1 ELSE 0 END), 0) AS missing_image,
                      COALESCE(SUM(CASE WHEN TRIM(COALESCE(p.telegram_file_id, '')) = '' THEN 1 ELSE 0 END), 0) AS missing_telegram_file,
                      COALESCE(SUM(CASE WHEN TRIM(COALESCE(p.sent_at, '')) = '' THEN 1 ELSE 0 END), 0) AS missing_sent_at,
                      COALESCE(SUM(CASE WHEN sender.telegram_id IS NULL THEN 1 ELSE 0 END), 0) AS missing_sender,
                      COALESCE(SUM(CASE WHEN TRIM(COALESCE(p.maternity_hospital, sender.maternity_hospital, '')) = '' THEN 1 ELSE 0 END), 0) AS missing_hospital,
                      COALESCE(SUM(CASE WHEN p.review_status = 'Проверяется в фоне' THEN 1 ELSE 0 END), 0) AS pending,
                      COUNT(DISTINCT p.sender_telegram_id) AS senders,
                      MIN(p.sent_at) AS first_sent_at,
                      MAX(p.sent_at) AS last_sent_at
               FROM photos AS p
               LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id"""
        ).fetchone()
        duplicate_file_rows = con.execute(
            """SELECT COALESCE(SUM(amount - 1), 0)
               FROM (
                   SELECT COUNT(*) AS amount
                   FROM photos
                   WHERE TRIM(COALESCE(telegram_file_id, '')) <> ''
                   GROUP BY telegram_file_id
                   HAVING COUNT(*) > 1
               )"""
        ).fetchone()[0]
        integrity = con.execute("PRAGMA quick_check").fetchone()[0]
    result = dict(summary)
    result["duplicate_telegram_files"] = int(duplicate_file_rows or 0)
    result["integrity"] = str(integrity)
    return result


async def audit_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await deny_unlisted_user(update):
        return
    user = get_user(update.effective_user.id)
    if update.effective_user.id != CREATOR_TELEGRAM_ID and (
        not user or user["role"] != "Оператор"
    ):
        await update.message.reply_text("Полная проверка базы доступна только оператору.")
        return
    result = await asyncio.to_thread(photo_database_audit)
    problems = (
        result["missing_image"] + result["missing_telegram_file"]
        + result["missing_sent_at"] + result["missing_sender"]
        + result["missing_hospital"]
    )
    await update.message.reply_text(
        "🔍 Полная проверка базы фотографий\n\n"
        f"Проверено записей: {result['total']}\n"
        f"Медиков-отправителей: {result['senders']}\n"
        f"Повреждённых/пустых изображений: {result['missing_image']}\n"
        f"Без Telegram file_id: {result['missing_telegram_file']}\n"
        f"Без даты отправки: {result['missing_sent_at']}\n"
        f"Без карточки отправителя: {result['missing_sender']}\n"
        f"Без роддома: {result['missing_hospital']}\n"
        f"Ещё обрабатываются: {result['pending']}\n"
        f"Повторно использованный Telegram-файл: {result['duplicate_telegram_files']}\n"
        f"Целостность SQLite: {result['integrity']}\n\n"
        + (
            "✅ Нарушений, из-за которых сохранённая фотография может не отображаться на сайте, не найдено."
            if problems == 0 and result["integrity"].casefold() == "ok"
            else "⚠️ Обнаружены нарушения. Перед исправлением требуется проверить указанные строки."
        )
    )


def pending_photo_jobs():
    with connect() as con:
        rows = con.execute(
            """SELECT p.id,
                      COALESCE(sender.city, '') AS city,
                      COALESCE(p.maternity_hospital, sender.maternity_hospital, '') AS hospital
               FROM photos AS p
               JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
               WHERE p.review_status = 'Проверяется в фоне'
               ORDER BY p.id"""
        ).fetchall()
    return [(row["id"], row["city"], row["hospital"]) for row in rows]


def backfill_next_visual_hash():
    """Fill one historical local fingerprint without calling a paid API."""
    with connect() as con:
        photo = con.execute(
            """SELECT id, image FROM photos
               WHERE visual_hashes IS NULL OR TRIM(visual_hashes) = ''
               ORDER BY id LIMIT 1"""
        ).fetchone()
    if not photo:
        return None
    try:
        visual_hashes = calculate_visual_hashes(bytes(photo["image"]))
    except Exception as error:
        logging.warning(
            "Не удалось создать визуальный отпечаток для фото id=%s: %s",
            photo["id"], error,
        )
        # Do not retry a corrupt row forever on every deployment.
        visual_hashes = "[]"
    with connect() as con:
        con.execute(
            "UPDATE photos SET visual_hashes = ? WHERE id = ?",
            (visual_hashes, photo["id"]),
        )
    return photo["id"]


async def backfill_visual_hashes():
    """Very slowly index old photos while leaving CPU/SQLite free for the site."""
    await asyncio.sleep(30)
    completed = 0
    while True:
        photo_id = await asyncio.to_thread(backfill_next_visual_hash)
        if photo_id is None:
            logging.info("Визуальные отпечатки старых фотографий полностью обновлены.")
            return
        completed += 1
        if completed == 1 or completed % 100 == 0:
            logging.info(
                "Фоновая индексация повторов: обработано %s фотографий, последняя id=%s",
                completed, photo_id,
            )
        await asyncio.sleep(DUPLICATE_VISUAL_BACKFILL_DELAY_SECONDS)


async def enqueue_pending_photo_jobs(application: Application, jobs):
    while not application.running:
        await asyncio.sleep(0.1)
    for photo_id, city, hospital in jobs:
        application.create_task(process_photo_in_background(photo_id, city, hospital))


async def resume_pending_photo_processing(application: Application):
    application.bot_data["chat_log_flush_task"] = asyncio.create_task(
        flush_chat_log_loop()
    )
    jobs = await asyncio.to_thread(pending_photo_jobs)
    if jobs:
        logging.info("Возобновляется фоновая проверка %s фотографий", len(jobs))
        application.bot_data["pending_photo_resume_task"] = asyncio.create_task(
            enqueue_pending_photo_jobs(application, jobs)
        )
    if QR_BACKFILL_ENABLED:
        application.bot_data["qr_backfill_task"] = asyncio.create_task(backfill_missing_qr_codes())
    else:
        logging.info(
            "Повторное сканирование старых фотографий отключено, "
            "чтобы не замедлять сайт и Telegram-бота."
        )
    if FULL_QR_RECHECK_ENABLED:
        application.bot_data["full_qr_recheck_task"] = asyncio.create_task(
            run_full_historical_qr_recheck()
        )
    if TARGETED_NO_QR_RECHECK_ENABLED:
        application.bot_data["targeted_no_qr_recheck_task"] = asyncio.create_task(
            run_targeted_no_qr_recheck()
        )
    if DUPLICATE_VISUAL_BACKFILL_ENABLED:
        application.bot_data["duplicate_visual_backfill_task"] = asyncio.create_task(
            backfill_visual_hashes()
        )
    if URGENCH_DUPLICATE_AUDIT_ENABLED:
        application.bot_data["urgench_duplicate_audit_task"] = asyncio.create_task(
            run_urgench_duplicate_audit()
        )


def main():
    if not TOKEN or TOKEN == 'PASTE_NEW_TOKEN_HERE':
        raise RuntimeError("Создайте .env и добавьте в него TELEGRAM_BOT_TOKEN.")
    init_db()
    app = (
        Application.builder()
        .bot(ChatLoggingBot(token=TOKEN))
        .concurrent_updates(False)
        .post_init(on_startup)
        .build()
    )
    app.add_error_handler(handle_error)
    # Store every incoming item before access and working-hours checks. Bot
    # replies are recorded by ChatLoggingBot after Telegram accepts them.
    app.add_handler(MessageHandler(filters.ALL, log_incoming_chat), group=-2)
    # Keep the Render worker alive, but block all Telegram messages outside
    # 07:00-19:00 in Tashkent. Group -1 runs before commands and conversations.
    app.add_handler(MessageHandler(filters.ALL, enforce_working_hours), group=-1)
    upload = ConversationHandler(entry_points=[
        CommandHandler('send_photo', photo_start),
        MessageHandler(filters.Regex('^' + re.escape(MENU_SEND_PHOTO) + '$'), photo_start),
    ], states={
        PHOTO_HOSPITAL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, select_photo_hospital),
            MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, hospital_step_hint),
        ],
        PHOTO: [
            CommandHandler('done', finish_photo_batch),
            MessageHandler(filters.Regex('^' + re.escape(MENU_FINISH_PHOTOS) + '$'), finish_photo_batch),
            MessageHandler(filters.Regex('^' + re.escape(MENU_SEND_MORE_PHOTO) + '$'), continue_photo_batch),
            MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_photo),
            MessageHandler(filters.UpdateType.MESSAGE & ~filters.COMMAND, photo_step_text_hint),
        ],
        DOCTOR_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_doctor_name),
            MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, doctor_step_hint),
        ]},
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    hospital_photo_viewer = ConversationHandler(
        entry_points=[
            CommandHandler('my_photos', hospital_photos_start),
            MessageHandler(filters.Regex(VIEW_HOSPITAL_PHOTOS_PATTERN), hospital_photos_start),
        ],
        states={
            VIEW_PHOTO_HOSPITAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hospital_photos_show),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    photo_report_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(['photo_report', 'report'], photo_report),
            MessageHandler(filters.Regex(PHOTO_REPORT_PATTERN), photo_report),
        ],
        states={
            REPORT_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_report_city_choice),
            ],
            REPORT_HOSPITAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_report_hospital_choice),
            ],
            REPORT_DOCTOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_report_doctor_choice),
            ],
            REPORT_PERIOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, photo_report_period_choice),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('my_id', my_id))
    app.add_handler(CommandHandler('menu', menu))
    app.add_handler(CommandHandler('profile', profile))
    app.add_handler(CommandHandler('audit_photos', audit_photos))
    app.add_handler(photo_report_conversation)
    app.add_handler(MessageHandler(
        filters.Regex(REPORT_PHOTO_NAVIGATION_PATTERN), report_photos_navigate,
    ))
    app.add_handler(CommandHandler(['comments', 'operator_comments'], operator_comments))
    app.add_handler(MessageHandler(filters.Regex(OPERATOR_COMMENTS_PATTERN), operator_comments))
    app.add_handler(CommandHandler(['operator_report', 'report_10_days'], operator_ten_day_report))
    app.add_handler(MessageHandler(
        filters.Regex('^' + re.escape(MENU_OPERATOR_TEN_DAY_REPORT) + '$'),
        operator_ten_day_report,
    ))
    app.add_handler(MessageHandler(filters.Regex('^' + re.escape(MENU_PROFILE) + '$'), profile))
    app.add_handler(MessageHandler(
        filters.Regex('^' + re.escape(MENU_EXCEL_DOCTOR_REPORT) + '$'),
        excel_doctor_report_prompt,
    ))
    excel_documents = (
        filters.Document.FileExtension("xlsx")
        | filters.Document.FileExtension("xlsm")
        | filters.Document.FileExtension("xls")
    )
    app.add_handler(MessageHandler(excel_documents, operator_excel_doctor_report))
    app.add_handler(upload)
    app.add_handler(hospital_photo_viewer)
    # Последними: всё, что не подошло ни одному обработчику, получает подсказку
    # вместо молчания (например, фото, присланное после перезапуска бота).
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & (filters.PHOTO | filters.Document.IMAGE),
        photo_outside_session,
    ))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT, unknown_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
