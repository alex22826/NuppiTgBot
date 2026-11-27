"""История переписки пользователей с ботом."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from ..access import is_creator
from ..db import bot_database, database_table_exists
from ..periods import local_day_bounds, requested_day
from ..text import format_sent_at

CHAT_HISTORY_LIMIT = 300


@login_required
def chat_history(request):
    """Show an authenticated operator a read-only Telegram chat history."""
    creator_view = is_creator(request.user)
    connection = bot_database()
    if not connection:
        return render(request, "dashboard/chat_history.html", {
            "database_error": "Не найдена база medical_bot.db.",
            "is_creator": creator_view,
        })
    try:
        if not database_table_exists(connection, "chat_messages"):
            return render(request, "dashboard/chat_history.html", {
                "chat_unavailable": True,
                "is_creator": creator_view,
            })
        conversations = [dict(row) for row in connection.execute(
            """SELECT cm.telegram_id,
                      COALESCE(NULLIF(TRIM(u.full_name), ''),
                               NULLIF(TRIM(MAX(cm.sender_name)), ''),
                               'Пользователь ' || cm.telegram_id) AS full_name,
                      COALESCE(NULLIF(TRIM(u.username), ''),
                               NULLIF(TRIM(MAX(cm.username)), ''), '') AS username,
                      COUNT(*) AS message_count,
                      MAX(cm.created_at) AS last_message_at,
                      (SELECT last.text FROM chat_messages AS last
                       WHERE last.telegram_id = cm.telegram_id
                       ORDER BY julianday(last.created_at) DESC, last.id DESC
                       LIMIT 1) AS last_text,
                      (SELECT last.direction FROM chat_messages AS last
                       WHERE last.telegram_id = cm.telegram_id
                       ORDER BY julianday(last.created_at) DESC, last.id DESC
                       LIMIT 1) AS last_direction
               FROM chat_messages AS cm
               LEFT JOIN users AS u ON u.telegram_id = cm.telegram_id
               GROUP BY cm.telegram_id, u.full_name, u.username
               ORDER BY julianday(last_message_at) DESC, cm.telegram_id"""
        )]
        requested_user = request.GET.get("telegram_id", "").strip()
        if requested_user.lstrip("-").isdigit():
            selected_telegram_id = int(requested_user)
        else:
            selected_telegram_id = conversations[0]["telegram_id"] if conversations else None
        selected_date_from = requested_day(request.GET.get("date_from", ""))
        selected_date_to = requested_day(request.GET.get("date_to", ""))
        conditions, parameters = [], []
        if selected_telegram_id is not None:
            conditions.append("cm.telegram_id = ?")
            parameters.append(selected_telegram_id)
        if selected_date_from:
            period_start, _ = local_day_bounds(selected_date_from)
            conditions.append("julianday(cm.created_at) >= julianday(?)")
            parameters.append(period_start)
        if selected_date_to:
            _, period_end = local_day_bounds(selected_date_to)
            conditions.append("julianday(cm.created_at) < julianday(?)")
            parameters.append(period_end)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        chat_messages = [dict(row) for row in connection.execute(
            f"""SELECT cm.*,
                       (SELECT p.id FROM photos AS p
                        WHERE p.telegram_file_id = cm.telegram_file_id
                          AND p.sender_telegram_id = cm.telegram_id
                        ORDER BY p.id DESC LIMIT 1) AS photo_id
                FROM chat_messages AS cm{where}
                ORDER BY julianday(cm.created_at) DESC, cm.id DESC
                LIMIT ?""",
            [*parameters, CHAT_HISTORY_LIMIT],
        )]
    finally:
        connection.close()
    chat_messages.reverse()
    type_labels = {
        "text": "Текст",
        "photo": "Фотография",
        "document": "Файл",
        "video": "Видео",
        "voice": "Голосовое сообщение",
        "audio": "Аудио",
        "sticker": "Стикер",
        "location": "Геолокация",
        "contact": "Контакт",
        "service": "Служебное сообщение",
    }
    for conversation in conversations:
        conversation["last_message_display"] = format_sent_at(conversation["last_message_at"])
    for item in chat_messages:
        item["created_at_display"] = format_sent_at(item["created_at"])
        item["day_display"] = item["created_at_display"][:10]
        item["time_display"] = item["created_at_display"][11:]
        item["type_label"] = type_labels.get(item["message_type"], item["message_type"])
    selected_conversation = next(
        (
            conversation for conversation in conversations
            if conversation["telegram_id"] == selected_telegram_id
        ),
        None,
    )
    return render(request, "dashboard/chat_history.html", {
        "is_creator": creator_view,
        "conversations": conversations,
        "selected_telegram_id": selected_telegram_id,
        "selected_conversation": selected_conversation,
        "selected_date_from": selected_date_from,
        "selected_date_to": selected_date_to,
        "chat_messages": chat_messages,
        "chat_history_limit": CHAT_HISTORY_LIMIT,
    })
