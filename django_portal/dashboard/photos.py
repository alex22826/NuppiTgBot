"""Карточки фотографий, превью и поиск оригинала для копий."""

import hashlib
import re
import secrets
from io import BytesIO

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

from .db import database_table_exists, photo_columns
from .decision_rules import DECISION_LABELS, DECISION_REVIEW
from .text import format_sent_at

PHOTO_THUMBNAIL_SIZE = (480, 480)


PHOTO_THUMBNAIL_QUALITY = 72


def photo_thumbnail_cache_path(photo_id, image_digest):
    """Хранит восстанавливаемые превью вне постоянного диска с основной БД."""
    cache_directory = settings.PHOTO_THUMBNAIL_CACHE_DIR
    safe_digest = re.sub(r"[^a-zA-Z0-9_-]", "", image_digest or "")[:32] or "unknown"
    return cache_directory / f"{photo_id}-{safe_digest}.jpg"


def make_photo_thumbnail(image_bytes):
    """Создаёт лёгкое превью, не меняя оригинал в базе данных."""
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            # Для JPEG draft уменьшает расход памяти ещё до полного декодирования.
            source.draft("RGB", PHOTO_THUMBNAIL_SIZE)
            image = ImageOps.exif_transpose(source)
            image.thumbnail(PHOTO_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=PHOTO_THUMBNAIL_QUALITY,
                optimize=True,
            )
            return output.getvalue()
    except (OSError, UnidentifiedImageError):
        # Старые/повреждённые записи продолжают открываться как раньше.
        return image_bytes


def cached_photo_thumbnail(photo_id, image_digest, image_bytes):
    if not image_digest:
        image_digest = hashlib.sha256(image_bytes).hexdigest()
    cache_path = photo_thumbnail_cache_path(photo_id, image_digest)
    try:
        if cache_path.exists():
            return cache_path.read_bytes()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        thumbnail = make_photo_thumbnail(image_bytes)
        temporary_path = cache_path.with_suffix(f".{secrets.token_hex(6)}.tmp")
        try:
            temporary_path.write_bytes(thumbnail)
            temporary_path.replace(cache_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return thumbnail
    except OSError:
        # Недоступный кэш не должен мешать просмотру сайта.
        return make_photo_thumbnail(image_bytes)


def photo_review_group(photo):
    """Группирует результаты: красный — ИИ/монтаж, жёлтый — копия или сомнительный код."""
    review = (photo["review_status"] or "").casefold()
    code = (photo["code_status"] or "").casefold()
    if "ии-изображ" in review or "подделк" in review:
        return "red"
    if "коп" in review or "обрезк" in review:
        return "yellow"
    # Отсутствующий код разрешён, а распознанный код из правильного диапазона считается нормальным.
    # Несовпадение региона, неизвестный формат и выход за диапазон требуют внимания оператора.
    if code and not any(marker in code for marker in (
        "правильный код", "код распознан", "нет данных", "отсутствует", "проверка по базе пока выключена",
    )):
        return "yellow"
    if "не проверено" in review:
        return "yellow"
    if is_ai_normal_status(review):
        return "clear"
    return "clear"


def is_copy_or_crop_status(value):
    status = (value or "").casefold()
    return "коп" in status or "обрез" in status


def is_ai_normal_status(value):
    status = (value or "").casefold()
    return "нет явных признаков" in status or "нормаль" in status or "принято" in status


def photo_details(connection, photo_id):
    """Одна запись из всей базы, независимо от фильтров и лимита списка."""
    columns = photo_columns(connection)
    fields = ("photo_date", "package_number", "mother_name", "review_status", "review_reason",
              "image_digest", "qr_data", "original_photo_id", "code_status",
              "ai_generated_score")
    optional = ", ".join(f"p.{field}" if field in columns else f"NULL AS {field}" for field in fields)
    hospital = "COALESCE(p.maternity_hospital, sender.maternity_hospital)" if "maternity_hospital" in columns else "sender.maternity_hospital"
    row = connection.execute(
        f"""SELECT p.id, p.sent_at, p.sender_telegram_id, {optional},
                   COALESCE(p.doctor_name, '') AS doctor_name,
                   COALESCE(sender.full_name, 'Пользователь удалён') AS sender_name,
                   COALESCE(sender.city, 'Не указан') AS city,
                   COALESCE({hospital}, 'Не указан') AS maternity_hospital
            FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
            WHERE p.id = ?""", (photo_id,),
    ).fetchone()
    if not row:
        return None
    photo = dict(row)
    photo["sent_at_display"] = format_sent_at(photo["sent_at"])
    return photo


def photo_details_for_operator(connection, photo_id):
    """Return one photo with the operator's effective decision, across old schemas too."""
    photo = photo_details(connection, photo_id)
    if not photo:
        return None

    check = None
    if database_table_exists(connection, "photo_checks"):
        check = connection.execute(
            """SELECT decision, decision_reason, manual_decision, manual_reason,
                      manual_reviewer, manual_at
               FROM photo_checks WHERE photo_id = ?""",
            (photo_id,),
        ).fetchone()

    automatic_decision = (
        check["decision"] if check and check["decision"] else DECISION_REVIEW
    )
    manual_decision = check["manual_decision"] if check else None
    effective_decision = manual_decision or automatic_decision or DECISION_REVIEW
    photo["decision"] = effective_decision
    photo["decision_label"] = DECISION_LABELS.get(effective_decision, "На проверке")
    photo["manual_decision"] = manual_decision
    photo["manual_reason"] = (check["manual_reason"] or "") if check else ""
    photo["manual_reviewer"] = (check["manual_reviewer"] or "") if check else ""
    photo["manual_at_display"] = format_sent_at(check["manual_at"]) if check and check["manual_at"] else ""
    photo["decision_reason"] = (
        (check["manual_reason"] or check["decision_reason"] or "")
        if check else (photo.get("review_reason") or "")
    )
    photo["is_copy"] = is_copy_or_crop_status(photo.get("review_status"))
    score = photo.get("ai_generated_score")
    photo["ai_generated_percent"] = score * 100 if score is not None else None
    return photo


def legacy_original_id(connection, photo):
    """Восстанавливает старую связь только из сохранённых свидетельств, не из pHash."""
    if photo["image_digest"]:
        row = connection.execute(
            "SELECT id FROM photos WHERE image_digest = ? AND id < ? ORDER BY id LIMIT 1",
            (photo["image_digest"], photo["id"]),
        ).fetchone()
        if row:
            return row["id"]
    # Старый бот сохранял дату исходного снимка внутри причины проверки.
    match = re.search(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
        photo["review_reason"] or "",
    )
    if match:
        rows = connection.execute(
            "SELECT id FROM photos WHERE sent_at = ? AND id != ? ORDER BY id LIMIT 2",
            (match.group(0), photo["id"]),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    if photo["qr_data"]:
        row = connection.execute(
            "SELECT id FROM photos WHERE qr_data = ? AND id < ? ORDER BY id LIMIT 1",
            (photo["qr_data"], photo["id"]),
        ).fetchone()
        if row:
            return row["id"]
    return None


def resolve_original_photo(connection, photo):
    """Идёт по цепочке копий до оригинала; удалённые и циклические связи не подменяет."""
    seen = {photo["id"]}
    current = photo
    while True:
        original_id = current["original_photo_id"]
        if original_id is None:
            original_id = legacy_original_id(connection, current)
        if original_id is None or original_id in seen:
            return None
        seen.add(original_id)
        current = photo_details(connection, original_id)
        if not current:
            return None
        if not is_copy_or_crop_status(current["review_status"]):
            return current
