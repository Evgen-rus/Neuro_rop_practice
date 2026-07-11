"""Deterministic operating playbooks for compact lead shadow analysis only."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from setup import MSK_TZ


RESTORE_NO_CONTACT_PROCESSING = "restore_no_contact_processing"
RETRY_BUSY_NUMBER = "retry_busy_number"

LEAD_ACTION_PLAYBOOKS: dict[str, dict[str, str]] = {
    "none": {"title": "No deterministic playbook"},
    RESTORE_NO_CONTACT_PROCESSING: {"title": "Restore processing when no meaningful contact is confirmed"},
    RETRY_BUSY_NUMBER: {"title": "Retry a busy number"},
    "verify_invalid_number": {"title": "Verify an invalid number"},
    "qualification_followup": {"title": "Qualification follow-up"},
    "move_to_deal": {"title": "Move a qualified lead to a deal"},
    "manual_context_audit": {"title": "Manual context audit"},
}


def _deadline(value: Any, *, today: date | None = None) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return (today or datetime.now(MSK_TZ).date()).isoformat()


def materialize_lead_playbook_action(
    lead_review: dict[str, Any],
    rop_action: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Expand a selected compact playbook into a deterministic ROP action."""
    playbook = lead_review.get("action_playbook")
    if playbook == RETRY_BUSY_NUMBER:
        deadline = _deadline(rop_action.get("deadline"), today=today)
        evidence_ids = rop_action.get("evidence_ids") if isinstance(rop_action.get("evidence_ids"), list) else []
        return {
            "check": "Повторить один звонок примерно через 10 минут после подтверждённого результата «занято»; не запускать общий цикл из трёх попыток до нового результата.",
            "message_to_manager": (
                f"До {deadline} повторите звонок примерно через 10 минут и сразу зафиксируйте в CRM дату, результат и следующий сценарий. "
                "Выбирайте следующий сценарий только по фактическому результату новой попытки."
            ),
            "expected_crm_fact": "В CRM зафиксированы дата и результат повторного звонка после «занято», а следующий шаг выбран по фактическому результату этой попытки.",
            "deadline": deadline,
            "success_condition": "Есть CRM-след повторного звонка и датированный следующий шаг; общий no-contact цикл не запускается без нового фактического результата.",
            "evidence_ids": evidence_ids,
        }
    if playbook != RESTORE_NO_CONTACT_PROCESSING:
        return rop_action

    deadline = _deadline(rop_action.get("deadline"), today=today)
    evidence_ids = rop_action.get("evidence_ids") if isinstance(rop_action.get("evidence_ids"), list) else []
    case_specific_check = str(rop_action.get("check") or "").strip()
    check = case_specific_check or "Проверить восстановление обработки по карточке лида."
    check += " Контроль: три попытки звонка, альтернативный канал и CRM-след каждой попытки."
    return {
        "check": check,
        "message_to_manager": (
            f"До {deadline} выполните 3 попытки звонка в разные интервалы дня с интервалом не менее 2 часов; "
            "минимум одну попытку сделайте в 11:00–13:00. Если номер занят, повторите звонок примерно через 10 минут. "
            "После безуспешных звонков отправьте короткое сообщение в доступный мессенджер. Сразу фиксируйте в CRM дату "
            "и результат каждой попытки, текст или факт сообщения и создайте задачу со следующим шагом. Если номер невалидный "
            "или подтверждена нецелевая заявка, зафиксируйте основание и установите корректный статус."
        ),
        "expected_crm_fact": (
            "В CRM есть 3 попытки звонка с датой и результатом каждой, факт или текст сообщения в мессенджер после недозвона "
            "и задача со следующим шагом и датой; при невалидном номере или нецелевости — документированное основание и корректный статус."
        ),
        "deadline": deadline,
        "success_condition": (
            "До срока в CRM зафиксированы результаты трёх попыток, альтернативный канал после недозвона и следующая задача; "
            "либо есть документированное основание невалидного номера или нецелевости."
        ),
        "evidence_ids": evidence_ids,
    }


def playbook_preview_lines(playbook: Any) -> list[str]:
    if playbook == RETRY_BUSY_NUMBER:
        return [
            "Повторить один звонок примерно через 10 минут после подтверждённого результата «занято».",
            "В CRM: дата и результат повторной попытки, затем следующий шаг только по её фактическому результату.",
        ]
    if playbook != RESTORE_NO_CONTACT_PROCESSING:
        return []
    return [
        "Три попытки звонка в разные интервалы дня с интервалом не менее 2 часов.",
        "Минимум одна попытка в 11:00–13:00; при занятом номере — повтор примерно через 10 минут.",
        "После недозвонов — короткое сообщение в доступный мессенджер.",
        "В CRM: дата и результат каждой попытки, факт или текст сообщения и задача со следующим шагом.",
    ]
