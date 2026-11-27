"""Shared decision rules for bot checks and the supervisor site.

Automatic checks only describe the detected signals. Every photo waits for a
human operator, who is the only person allowed to accept or reject it.
"""

DECISION_ACCEPTED = "accepted"
DECISION_REVIEW = "review"
DECISION_REJECTED = "rejected"
DECISION_LABELS = {
    DECISION_ACCEPTED: "Принято",
    DECISION_REVIEW: "На проверке",
    DECISION_REJECTED: "Отклонено",
}


def _normalized(value):
    return str(value or "").strip().casefold()


def _is_foreign_qr_status(code):
    return any(marker in code for marker in (
        "посторонн",
        "формат региона не распознан",
        "префикс региона",
        "вне диапазона",
    ))


def automatic_photo_decision(
    review_status,
    code_status,
    *,
    is_nuppi_1=None,
    is_reference_design=None,
    original_photo_id=None,
    assignment_required=False,
    assignment_status=None,
    location_required=False,
):
    """Return structured check results without making a final decision."""
    review = _normalized(review_status)
    code = _normalized(code_status)
    reasons = []

    pending = "проверяется" in review
    duplicate = bool(original_photo_id) or "коп" in review or "обрез" in review
    edited = any(marker in review for marker in ("подделк", "ии-изображ", "монтаж"))
    unchecked = not review or "не проверено" in review
    foreign_code = _is_foreign_qr_status(code)
    code_problem = bool(code) and not any(marker in code for marker in (
        "код распознан",
        "правильный код",
        "отсутствует",
        "нет данных",
        "проверяется",
        "проверка по базе пока выключена",
    ))

    if pending:
        reasons.append("Автоматическая проверка ещё выполняется.")
    if duplicate:
        reasons.append("Обнаружено возможное повторное или обрезанное фото.")
    if edited:
        reasons.append("Есть возможные признаки обработки изображения.")
    if unchecked:
        reasons.append("Автоматическая проверка не завершена или недоступна.")
    if code_problem and not foreign_code:
        reasons.append("QR/Data Matrix требует проверки оператора.")
    if foreign_code:
        reasons.append("QR/Data Matrix отсутствует в разрешённом списке кодов.")
    if is_nuppi_1 is False:
        reasons.append("Упаковка NUPPI 1 не подтверждена.")
    if is_reference_design is False:
        reasons.append("Соответствие эталонному дизайну не подтверждено.")
    normalized_assignment = _normalized(assignment_status)
    assignment_problem = bool(normalized_assignment) and not any(marker in normalized_assignment for marker in (
        "соответствует", "задание не назначено",
    ))
    if assignment_required or assignment_problem:
        reasons.append("Соответствие заданию ещё не подтверждено.")
    if location_required:
        reasons.append("Геолокация визита ещё не подтверждена.")

    decision_reason = " ".join(reasons)
    if decision_reason:
        decision_reason += " Ожидается решение оператора."
    else:
        decision_reason = "Автоматические проверки завершены. Ожидается решение оператора."
    return {
        "decision": DECISION_REVIEW,
        "decision_reason": decision_reason,
        "duplicate_status": "Возможный повтор" if duplicate else "Повтор не обнаружен",
        "editing_status": "Требуется проверка" if edited else "Явные признаки не обнаружены",
        "product_status": (
            "NUPPI 1 обнаружен" if is_nuppi_1 is True
            else "NUPPI 1 не подтверждён" if is_nuppi_1 is False
            else "Не проверено"
        ),
        "material_status": (
            "Эталонный дизайн подтверждён" if is_reference_design is True
            else "Эталонный дизайн не подтверждён" if is_reference_design is False
            else "Не проверено"
        ),
        "location_status": "Требуется" if location_required else "Не запрашивалась",
        "assignment_status": (
            assignment_status if assignment_status
            else "Требуется" if assignment_required
            else "Задание не назначено"
        ),
    }


def legacy_automatic_photo_decision(
    review_status,
    code_status,
    *,
    is_nuppi_1=None,
    is_reference_design=None,
    original_photo_id=None,
    assignment_required=False,
    assignment_status=None,
    location_required=False,
):
    """Reproduce the automatic decision used before manual-only review."""
    result = automatic_photo_decision(
        review_status,
        code_status,
        is_nuppi_1=is_nuppi_1,
        is_reference_design=is_reference_design,
        original_photo_id=original_photo_id,
        assignment_required=assignment_required,
        assignment_status=assignment_status,
        location_required=location_required,
    )
    review = _normalized(review_status)
    code = _normalized(code_status)
    foreign_code = _is_foreign_qr_status(code)
    code_problem = bool(code) and not any(marker in code for marker in (
        "код распознан", "правильный код", "отсутствует", "нет данных",
        "проверяется", "проверка по базе пока выключена",
    ))
    normalized_assignment = _normalized(assignment_status)
    assignment_problem = bool(normalized_assignment) and not any(
        marker in normalized_assignment
        for marker in ("соответствует", "задание не назначено")
    )
    requires_review = any((
        "проверяется" in review,
        bool(original_photo_id) or "коп" in review or "обрез" in review,
        any(marker in review for marker in ("подделк", "ии-изображ", "монтаж")),
        not review or "не проверено" in review,
        code_problem and not foreign_code,
        is_nuppi_1 is False,
        is_reference_design is False,
        assignment_required or assignment_problem,
        location_required,
    ))
    result["decision"] = (
        DECISION_REJECTED if foreign_code
        else DECISION_REVIEW if requires_review
        else DECISION_ACCEPTED
    )
    if result["decision"] == DECISION_ACCEPTED:
        result["decision_reason"] = "Все доступные автоматические проверки пройдены."
    else:
        result["decision_reason"] = result["decision_reason"].removesuffix(
            " Ожидается решение оператора."
        )
    return result


def effective_decision(automatic_decision, manual_decision=None):
    manual = str(manual_decision or "").strip().casefold()
    return manual if manual in DECISION_LABELS else automatic_decision
