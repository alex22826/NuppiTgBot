"""Похожие фото и сводка проверок для карточки фото (ТЗ, разделы 7 и 8.2–8.4).

Сайт ничего не пересчитывает по картинкам: он сравнивает отпечатки, которые
бот уже сохранил (pHash, набор поворотов/контуров), и данные с упаковки.
Одинаковые коробки NUPPI похожи друг на друга, поэтому «похожим снимком»
считается только почти тот же pHash или совпадение сразу двух разных
отпечатков — как и в самом боте.
"""

import json
import os

from .access import region_allowed
from .db import database_table_exists, photo_columns
from .decision_rules import DECISION_LABELS, DECISION_REVIEW
from .text import duplicate_name_key, format_sent_at

# Те же пороги, что у бота (переменные окружения с теми же именами).
PHASH_SAME_SHOT_DISTANCE = max(0, int(os.getenv("SITE_PHASH_SAME_SHOT_DISTANCE", "6")))
VISUAL_THRESHOLDS = {
    "phash": max(8, int(os.getenv("VISUAL_HASH_MAX_DISTANCE", "18"))),
    "edge": max(8, int(os.getenv("VISUAL_EDGE_HASH_MAX_DISTANCE", "18"))),
    "dhash": max(8, int(os.getenv("VISUAL_DHASH_MAX_DISTANCE", "16"))),
}
# Сколько последних фото просматривать по отпечаткам (остальные признаки — по всей базе).
VISUAL_LOOKBACK = max(100, int(os.getenv("SITE_SIMILAR_LOOKBACK", "20000")))
SIMILAR_LIMIT = 12

# Признак совпадения → (подпись, вес для сортировки: чем больше, тем важнее).
REASONS = {
    "same_file": ("Тот же файл", 100),
    "original": ("Оригинал для этой копии", 90),
    "copy": ("Копия этого фото", 90),
    "same_code": ("Тот же код на упаковке", 80),
    "same_name_date": ("Совпадают ФИО и дата", 70),
    "same_shot": ("Почти тот же снимок", 60),
    "visual": ("Похожий снимок: поворот, обрезка или фон", 40),
}


def _hex_distance(first, second):
    try:
        if not first or not second or len(first) != len(second):
            return None
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except (TypeError, ValueError):
        return None


def _hash_groups(single_hash, visual_hashes):
    groups = {"phash": [single_hash] if single_hash else [], "edge": [], "dhash": []}
    if visual_hashes:
        try:
            decoded = json.loads(visual_hashes)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, list):
            groups["phash"].extend(value for value in decoded if isinstance(value, str) and value)
        elif isinstance(decoded, dict):
            for group in groups:
                values = decoded.get(group)
                if isinstance(values, list):
                    groups[group].extend(value for value in values if isinstance(value, str) and value)
    return {group: list(dict.fromkeys(values)) for group, values in groups.items()}


def visual_match(first, second):
    """Какие семейства отпечатков совпали и насколько: {семейство: расстояние}."""
    matched = {}
    for group, maximum in VISUAL_THRESHOLDS.items():
        distances = [
            distance
            for left in first[group]
            for right in second[group]
            if (distance := _hex_distance(left, right)) is not None
        ]
        if distances and min(distances) <= maximum:
            matched[group] = min(distances)
    return matched


def _decision_rows(connection, ids):
    if not ids or not database_table_exists(connection, "photo_checks"):
        return {}
    placeholders = ", ".join("?" for _ in ids)
    return {
        row["photo_id"]: row
        for row in connection.execute(
            f"""SELECT photo_id, COALESCE(NULLIF(manual_decision, ''), NULLIF(decision, ''), ?) AS decision
                FROM photo_checks WHERE photo_id IN ({placeholders})""",
            (DECISION_REVIEW, *ids),
        )
    }


def find_similar_photos(connection, photo, regions=None, limit=SIMILAR_LIMIT):
    """Фото, которые система считает связанными с этим, с причинами совпадения."""
    columns = photo_columns(connection)
    photo_id = photo["id"]
    found = {}

    def add(other_id, reason, detail=""):
        if not other_id or other_id == photo_id:
            return
        entry = found.setdefault(other_id, {"id": other_id, "reasons": []})
        if all(existing["code"] != reason for existing in entry["reasons"]):
            entry["reasons"].append({"code": reason, "label": REASONS[reason][0], "detail": detail})

    def ids(query, params):
        return [row[0] for row in connection.execute(query, params)]

    if "image_digest" in columns and photo.get("image_digest"):
        for other in ids("SELECT id FROM photos WHERE image_digest = ? AND id != ? LIMIT 50",
                         (photo["image_digest"], photo_id)):
            add(other, "same_file")
    if "original_photo_id" in columns:
        if photo.get("original_photo_id"):
            add(photo["original_photo_id"], "original")
        for other in ids("SELECT id FROM photos WHERE original_photo_id = ? LIMIT 50", (photo_id,)):
            add(other, "copy")
    if "qr_data" in columns and photo.get("qr_data"):
        for other in ids("SELECT id FROM photos WHERE qr_data = ? AND id != ? LIMIT 50",
                         (photo["qr_data"], photo_id)):
            add(other, "same_code", photo["qr_data"])
    name_key = duplicate_name_key(photo.get("mother_name"))
    if {"photo_date", "mother_name"} <= columns and photo.get("photo_date") and name_key:
        for row in connection.execute(
            """SELECT id, mother_name FROM photos
               WHERE photo_date = ? AND id != ? AND TRIM(COALESCE(mother_name, '')) != ''""",
            (photo["photo_date"], photo_id),
        ):
            if duplicate_name_key(row["mother_name"]) == name_key:
                add(row["id"], "same_name_date", f"{row['mother_name']}, {photo['photo_date']}")

    if "image_hash" in columns:
        visual_column = "visual_hashes" if "visual_hashes" in columns else "NULL"
        own = connection.execute(
            f"SELECT image_hash, {visual_column} AS visual_hashes FROM photos WHERE id = ?", (photo_id,),
        ).fetchone()
        if own and (own["image_hash"] or own["visual_hashes"]):
            own_groups = _hash_groups(own["image_hash"], own["visual_hashes"])
            for row in connection.execute(
                f"""SELECT id, image_hash, {visual_column} AS visual_hashes FROM photos
                    WHERE id != ? AND (image_hash IS NOT NULL OR {visual_column} IS NOT NULL)
                    ORDER BY id DESC LIMIT ?""",
                (photo_id, VISUAL_LOOKBACK),
            ):
                single = _hex_distance(own["image_hash"], row["image_hash"])
                if single is not None and single <= PHASH_SAME_SHOT_DISTANCE:
                    add(row["id"], "same_shot", f"различие {single} из 64")
                    continue
                matched = visual_match(own_groups, _hash_groups(row["image_hash"], row["visual_hashes"]))
                # Один отпечаток совпадает и у разных одинаковых коробок — нужно два.
                if len(matched) >= 2:
                    add(row["id"], "visual", ", ".join(f"{key} {value}" for key, value in sorted(matched.items())))

    if not found:
        return []
    other_ids = sorted(found)
    hospital = (
        "COALESCE(p.maternity_hospital, sender.maternity_hospital)"
        if "maternity_hospital" in columns else "sender.maternity_hospital"
    )
    placeholders = ", ".join("?" for _ in other_ids)
    details = {
        row["id"]: row
        for row in connection.execute(
            f"""SELECT p.id, p.sent_at, COALESCE(p.doctor_name, '') AS doctor_name,
                       COALESCE(sender.full_name, 'Пользователь удалён') AS sender_name,
                       COALESCE(sender.city, '') AS city, COALESCE({hospital}, '') AS maternity_hospital
                FROM photos AS p LEFT JOIN users AS sender ON sender.telegram_id = p.sender_telegram_id
                WHERE p.id IN ({placeholders})""",
            other_ids,
        )
    }
    decisions = _decision_rows(connection, other_ids)
    result = []
    for other_id, entry in found.items():
        row = details.get(other_id)
        if not row:
            continue
        decision = decisions[other_id]["decision"] if other_id in decisions else DECISION_REVIEW
        entry["reasons"].sort(key=lambda reason: -REASONS[reason["code"]][1])
        entry.update({
            "sent_at": row["sent_at"],
            "sent_at_display": format_sent_at(row["sent_at"]),
            "sender_name": row["sender_name"],
            "doctor_name": row["doctor_name"],
            "city": row["city"],
            "maternity_hospital": row["maternity_hospital"],
            "decision": decision,
            "decision_label": DECISION_LABELS.get(decision, "На проверке"),
            "earlier": (row["sent_at"] or "") < (photo.get("sent_at") or ""),
            "in_scope": region_allowed(regions, row["city"]) or any(
                reason["code"] == "original" for reason in entry["reasons"]
            ),
            "weight": max(REASONS[reason["code"]][1] for reason in entry["reasons"]),
        })
        result.append(entry)
    result.sort(key=lambda item: (-item["weight"], item["sent_at"] or ""))
    return result[:limit]


def _tone(text, good=(), bad=(), warn=()):
    value = (text or "").casefold()
    if any(marker in value for marker in bad):
        return "bad"
    if any(marker in value for marker in warn):
        return "warn"
    if any(marker in value for marker in good):
        return "ok"
    return "neutral"


def photo_checks_summary(photo, similar):
    """Сводка проверок по ТЗ: читаемость, повтор, ФИО и дата, код, правка, товар."""
    filled = [label for key, label in (("qr_data", "код"), ("photo_date", "дата"), ("mother_name", "ФИО"))
              if photo.get(key)]
    missing = [label for key, label in (("qr_data", "код"), ("photo_date", "дата"), ("mother_name", "ФИО"))
               if not photo.get(key)]
    if not missing:
        readability = ("ok", "Код, дата и ФИО прочитаны")
    elif filled:
        readability = ("warn", "Не прочитано: " + ", ".join(missing))
    else:
        readability = ("warn", "Данные с упаковки не прочитаны")

    strong = [item for item in similar if item["weight"] >= REASONS["same_name_date"][1] and item["earlier"]]
    visual = [item for item in similar if item["weight"] < REASONS["same_name_date"][1]]
    if strong:
        repeat = ("bad", f"Найдено раньше: {len(strong)} — " + ", ".join(f"№{item['id']}" for item in strong[:4]))
    elif similar:
        repeat = ("warn", f"Похожих снимков: {len(similar)}" + (f", из них визуально {len(visual)}" if visual else ""))
    else:
        repeat = ("ok", photo.get("duplicate_status") or "Повтор не обнаружен")

    name_date = [item for item in similar if any(reason["code"] == "same_name_date" for reason in item["reasons"])]
    if not (photo.get("mother_name") and photo.get("photo_date")):
        name_check = ("neutral", "Нет ФИО или даты — сверить не с чем")
    elif name_date:
        accepted = [item for item in name_date if item["decision"] == "accepted"]
        name_check = (
            "bad" if accepted else "warn",
            f"Совпадают с №{name_date[0]['id']}" + (" (уже принято)" if accepted else ""),
        )
    else:
        name_check = ("ok", "Совпадений с прошлыми записями нет")

    code_status = photo.get("code_status") or "Нет данных о коде"
    review_status = photo.get("review_status") or "Не проверено"
    editing = photo.get("editing_status") or ""
    # Отдельную строку о правке показываем, только если она что-то добавляет.
    editing_tone = _tone(editing, good=("не обнаруж", "нет явных"))
    edit_text = (
        review_status if not editing or editing == "Не проверено" or editing_tone == "ok"
        else f"{review_status}; {editing}"
    )
    return [
        {"title": "Читаемость", "tone": readability[0], "text": readability[1]},
        {"title": "Повтор", "tone": repeat[0], "text": repeat[1]},
        {"title": "ФИО и дата с прошлыми записями", "tone": name_check[0], "text": name_check[1]},
        {"title": "Код по региону", "tone": _tone(code_status, good=("правильный", "распознан"),
                                                  bad=("чужой", "другого региона", "вне диапазона"),
                                                  warn=("не распознан", "неизвест", "нет данных", "отсутств")),
         "text": code_status},
        {"title": "Правка или монтаж", "tone": _tone(edit_text, good=("нет явных", "не обнаруж"),
                                                    bad=("ии-изображ", "подделк", "монтаж"),
                                                    warn=("коп", "обрез", "возможн", "не провер", "требует")),
         "text": edit_text},
        {"title": "Товар", "tone": _tone(photo.get("product_status"), good=("обнаружен",),
                                         bad=("не подтвержд",), warn=("не провер",)),
         "text": photo.get("product_status") or "Не проверено"},
    ]
