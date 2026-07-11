"""Deterministic operating playbooks for compact deal shadow analysis only."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from setup import MSK_TZ


DISPUTED_CLOSED_DEAL_REVIEW = "disputed_closed_deal_review"
DATED_TECHNICAL_INPUT_CONTROL = "dated_technical_input_control"
INVOICE_PRICE_COMPETITOR_RISK = "invoice_price_competitor_risk"
MANUAL_CONTEXT_AUDIT = "manual_context_audit"

DEAL_ACTION_PLAYBOOKS: tuple[str, ...] = (
    "none",
    DISPUTED_CLOSED_DEAL_REVIEW,
    DATED_TECHNICAL_INPUT_CONTROL,
    INVOICE_PRICE_COMPETITOR_RISK,
    MANUAL_CONTEXT_AUDIT,
)

DEAL_ACTION_OWNER = "Ответственный менеджер сделки"


def _deadline(value: Any, *, today: date | None = None) -> str:
    """Return an internal control date, never a claimed client commitment."""
    if isinstance(value, str) and value.strip():
        return value
    return (today or datetime.now(MSK_TZ).date()).isoformat()


def _evidence_ids(rop_action: dict[str, Any]) -> list[str]:
    evidence = rop_action.get("evidence_ids")
    return evidence if isinstance(evidence, list) else []


def _base_action(*, check: str, message: str, crm_fact: str, deadline: str, success: str, evidence_ids: list[str]) -> dict[str, Any]:
    return {
        "check": check,
        "message_to_manager": message,
        "expected_crm_fact": crm_fact,
        "deadline": deadline,
        "success_condition": success,
        "owner": DEAL_ACTION_OWNER,
        "evidence_ids": evidence_ids,
    }


def _materialize_disputed_closed(review: dict[str, Any], action: dict[str, Any], deadline: str) -> dict[str, Any]:
    closure_status = review.get("closure_status")
    if closure_status == "confirmed":
        return _base_action(
            check="Проверить, что подтверждающее основание закрытия и причина закрытия отражены в CRM.",
            message=(
                f"{DEAL_ACTION_OWNER}: до {deadline} проверьте подтверждающее основание закрытия и приведите CRM-причину "
                "в соответствие с evidence. Не возвращайте сделку в работу без новых фактов."
            ),
            crm_fact="В CRM зафиксированы подтверждающее evidence и корректная причина закрытия; решение: закрытие подтверждено.",
            deadline=deadline,
            success="В CRM есть проверяемое основание корректного закрытия, и сделка не переоткрыта без фактического основания.",
            evidence_ids=_evidence_ids(action),
        )
    return _base_action(
        check="Проверить соответствие причины закрытия CRM-истории и звонкам; установить, подтверждено ли закрытие.",
        message=(
            f"{DEAL_ACTION_OWNER}: до {deadline} выполните CRM-задачу по проверке закрытия: сопоставьте причину, историю и evidence. "
            "Зафиксируйте одно решение: закрытие подтверждено, причина исправлена, сделка возвращена в работу по фактам "
            "или требуется ручной аудит. Не возвращайте сделку в работу только из-за спорного статуса."
        ),
        crm_fact=(
            "В CRM зафиксированы причина закрытия, подтверждающее или противоречащее evidence и итог проверки: закрытие подтверждено, "
            "причина исправлена, сделка возвращена в работу по фактам либо назначен ручной аудит."
        ),
        deadline=deadline,
        success="В CRM есть документированное решение проверки закрытия с evidence, владельцем и следующим действием.",
        evidence_ids=_evidence_ids(action),
    )


def _materialize_technical_inputs(review: dict[str, Any], action: dict[str, Any], deadline: str) -> dict[str, Any]:
    inputs = [str(item).strip() for item in review.get("required_technical_inputs", []) if str(item).strip()]
    required_inputs = "; ".join(inputs) if inputs else "обязательные технические данные из подтверждённого evidence"
    return _base_action(
        check=(
            "Проверить, что активная продажа не зависла до КП/расчёта: получить от клиента дату передачи "
            f"следующих данных: {required_inputs}."
        ),
        message=(
            f"{DEAL_ACTION_OWNER}: до {deadline} создайте и выполните CRM-задачу на уточняющий контакт. Запросите {required_inputs} "
            "и отдельную клиентскую дату их передачи. Внутренний срок контроля не является обещанием клиента. "
            "Не запускайте глубокую инженерную проработку до получения обязательных входных данных."
        ),
        crm_fact=(
            f"В CRM есть задача с владельцем и внутренним сроком; зафиксированы перечень данных ({required_inputs}), "
            "клиентская дата передачи только если она подтверждена клиентом, и следующий шаг, привязанный к этой дате."
        ),
        deadline=deadline,
        success=(
            "Продажа остаётся активной; в CRM есть контролируемая задача, перечень входных данных и подтверждённая клиентская дата "
            "либо явно зафиксировано, что дата ещё не согласована."
        ),
        evidence_ids=_evidence_ids(action),
    )


def _materialize_invoice_risk(review: dict[str, Any], action: dict[str, Any], deadline: str) -> dict[str, Any]:
    confirmed_refusal = review.get("confirmed_refusal") is True and bool(_evidence_ids(action))
    closure_clause = (
        "Закрытие допустимо только при уже подтверждённом явном отказе."
        if confirmed_refusal
        else "Не закрывайте сделку по вероятному проигрышу, отсутствию оплаты или формулировке «скорее не купят»."
    )
    return _base_action(
        check=(
            "Проверить уточняющий контакт по риску цены/сравнения: предмет сравнения, расхождение по цене или условиям, "
            "реальный бюджет, ЛПР, дату решения, согласованность счёта и фактор, способный изменить решение."
        ),
        message=(
            f"{DEAL_ACTION_OWNER}: до {deadline} создайте и выполните CRM-задачу на уточняющий контакт. Выясните: "
            "(1) что именно сравнивает клиент; (2) расхождение по цене или условиям; (3) реальный бюджет или допустимый ориентир; "
            "(4) кто принимает итоговое решение; (5) когда оно будет принято; (6) был ли счёт согласованным следующим шагом; "
            f"(7) что может изменить решение. {closure_clause}"
        ),
        crm_fact=(
            "В CRM после контакта зафиксированы предмет сравнения, расхождение по цене/условиям, бюджет или ценовой ориентир, ЛПР, "
            "дата решения, статус согласования счёта и конкретный следующий шаг; при явном отказе — документированное основание отказа."
        ),
        deadline=deadline,
        success=(
            "В CRM есть выполненная задача ответственного менеджера и все обязательные результаты уточнения; статус счёта отделён "
            "от подтверждённого намерения платить и даты оплаты."
        ),
        evidence_ids=_evidence_ids(action),
    )


def materialize_deal_playbook_action(
    deal_review: dict[str, Any], rop_action: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    """Expand a selected compact deal playbook into an auditable ROP action."""
    playbook = deal_review.get("action_playbook")
    deadline = _deadline(rop_action.get("deadline"), today=today)
    if playbook == DISPUTED_CLOSED_DEAL_REVIEW:
        return _materialize_disputed_closed(deal_review, rop_action, deadline)
    if playbook == DATED_TECHNICAL_INPUT_CONTROL:
        return _materialize_technical_inputs(deal_review, rop_action, deadline)
    if playbook == INVOICE_PRICE_COMPETITOR_RISK:
        return _materialize_invoice_risk(deal_review, rop_action, deadline)
    return rop_action


def playbook_preview_lines(playbook: Any) -> list[str]:
    titles = {
        DISPUTED_CLOSED_DEAL_REVIEW: "Проверка спорного закрытия: решение документируется evidence, а не спорным статусом.",
        DATED_TECHNICAL_INPUT_CONTROL: "Контроль техвходов: внутренний срок отделён от клиентской даты; инженерная проработка ждёт входные данные.",
        INVOICE_PRICE_COMPETITOR_RISK: "Риск цены/конкурента: счёт не равен обещанию оплаты; обязателен уточняющий контакт.",
    }
    title = titles.get(playbook)
    return [title] if title else []
