"""
Lightweight validation for model-generated ROP analysis JSON.
"""

from __future__ import annotations

from typing import Any


class AnalysisValidationError(ValueError):
    """Raised when the model output is valid JSON but not valid analysis data."""


FORBIDDEN_CLIENT_TEXT_MARKERS = (
    "ДОБАВИТЬ",
    "{данные}",
    "{{данные}}",
    "[данные]",
    "<данные>",
    "todo",
    "tbd",
    "n/a",
    "...",
)

COMMON_REQUIRED_FIELDS = {
    "main_risk",
    "manager_quality",
    "call_attempt_recommendation",
    "manager_action_block",
    "rop_manager_message_block",
    "rop_action",
    "memory_update",
}

DEAL_REQUIRED_FIELDS = COMMON_REQUIRED_FIELDS | {
    "deal_id",
    "deal_state",
    "deal_mode",
    "closed_deal_review",
    "new_event",
    "objection_handling",
    "what_changed",
    "deal_progress",
    "payment_blocker",
    "price_comparability_check",
    "money_path_diagnosis",
    "resource_control",
    "shaker_question",
    "competitor_defense_checklist",
    "priority_recommendation",
}

LEAD_REQUIRED_FIELDS = COMMON_REQUIRED_FIELDS | {
    "lead_id",
    "lead_state",
    "activity_summary",
    "loss_diagnosis",
}

MAX_LIST_LIMITS = {
    "rop_manager_message_block.evidence": 7,
    "closed_deal_review.why_closed_questionable": 5,
    "closed_deal_review.why_closed_may_be_valid": 5,
    "resource_control.allowed_work": 5,
    "resource_control.blocked_work": 5,
    "payment_blocker.missing_confirmation": 5,
    "payment_blocker.next_actions": 5,
    "money_path_diagnosis.evidence": 7,
    "price_comparability_check.what_is_unclear": 5,
    "price_comparability_check.what_rop_should_check": 5,
    "price_comparability_check.evidence": 7,
    "objection_handling.likely_objections": 3,
    "competitor_defense_checklist.defense_points": 5,
    "competitor_defense_checklist.questions_to_client": 5,
    "loss_diagnosis.evidence": 7,
}


def _field_path(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_value_at_path(value: dict[str, Any], path: str, new_value: Any) -> None:
    current: Any = value
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(part)
    if isinstance(current, dict):
        current[parts[-1]] = new_value


def normalize_analysis_for_validation(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Clamp long model lists to validator limits without changing input context."""

    changes: list[dict[str, Any]] = []
    for path, limit in MAX_LIST_LIMITS.items():
        value = _value_at_path(analysis, path)
        if not isinstance(value, list) or len(value) <= limit:
            continue
        _set_value_at_path(analysis, path, value[:limit])
        changes.append(
            {
                "path": path,
                "action": "truncated_list",
                "max_items": limit,
                "original_items": len(value),
                "removed_items": len(value) - limit,
            }
        )
    return changes


def _require_fields(value: dict[str, Any], required_fields: set[str], parent: str, errors: list[str]) -> None:
    for field in sorted(required_fields):
        if field not in value:
            errors.append(f"missing required field: {_field_path(parent, field)}")


def _expect_dict(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    errors.append(f"expected object at {path}")
    return {}


def _expect_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if isinstance(value, list):
        return value
    errors.append(f"expected list at {path}")
    return []


def _client_text_values(manager_action: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    primary = manager_action.get("primary_text")
    if isinstance(primary, dict):
        for field in ("subject", "text"):
            text = primary.get(field)
            if isinstance(text, str):
                values.append((f"manager_action_block.primary_text.{field}", text))

    backup_texts = manager_action.get("backup_texts")
    if isinstance(backup_texts, list):
        for index, item in enumerate(backup_texts):
            if not isinstance(item, dict):
                continue
            for field in ("title", "text"):
                text = item.get(field)
                if isinstance(text, str):
                    values.append((f"manager_action_block.backup_texts[{index}].{field}", text))
    return values


def _validate_client_texts(manager_action: dict[str, Any], errors: list[str]) -> None:
    for path, text in _client_text_values(manager_action):
        _validate_no_forbidden_markers(path, text, errors)


def _validate_no_forbidden_markers(path: str, text: str, errors: list[str]) -> None:
    lowered = text.lower()
    for marker in FORBIDDEN_CLIENT_TEXT_MARKERS:
        if marker.lower() in lowered:
            errors.append(f"forbidden placeholder '{marker}' found at {path}")


def _expect_bool(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, bool):
        errors.append(f"expected boolean at {path}")


def _expect_non_empty_text_without_markers(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"expected non-empty string at {path}")
        return
    _validate_no_forbidden_markers(path, value, errors)


def _validate_common_shapes(analysis: dict[str, Any], errors: list[str]) -> None:
    _expect_dict(analysis.get("main_risk"), "main_risk", errors)
    _expect_dict(analysis.get("manager_quality"), "manager_quality", errors)
    _expect_dict(analysis.get("call_attempt_recommendation"), "call_attempt_recommendation", errors)
    _expect_dict(analysis.get("rop_action"), "rop_action", errors)
    _expect_dict(analysis.get("memory_update"), "memory_update", errors)

    rop_manager = _expect_dict(
        analysis.get("rop_manager_message_block"),
        "rop_manager_message_block",
        errors,
    )
    if rop_manager:
        for field in (
            "check_for_rop",
            "why_it_matters",
            "message_to_manager",
            "expected_crm_update",
            "success_condition",
        ):
            _expect_non_empty_text_without_markers(
                rop_manager.get(field),
                f"rop_manager_message_block.{field}",
                errors,
            )
        deadline = rop_manager.get("deadline")
        if deadline is not None and (not isinstance(deadline, str) or not deadline.strip()):
            errors.append("expected rop_manager_message_block.deadline to be non-empty string or null")
        evidence = _expect_max_list_length(
            rop_manager.get("evidence"),
            "rop_manager_message_block.evidence",
            7,
            errors,
        )
        if not evidence:
            errors.append("rop_manager_message_block.evidence must not be empty")

    manager_action = _expect_dict(analysis.get("manager_action_block"), "manager_action_block", errors)
    if manager_action:
        _expect_dict(manager_action.get("primary_text"), "manager_action_block.primary_text", errors)
        _expect_list(manager_action.get("backup_texts"), "manager_action_block.backup_texts", errors)
        _expect_list(manager_action.get("manager_checklist"), "manager_action_block.manager_checklist", errors)
        _validate_client_texts(manager_action, errors)


def _expect_enum(value: Any, path: str, allowed: set[str], errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"invalid enum at {path}: expected one of {sorted(allowed)}, got {value!r}")


def _expect_non_empty_string(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"expected non-empty string at {path}")


def _expect_max_list_length(value: Any, path: str, max_length: int, errors: list[str]) -> list[Any]:
    items = _expect_list(value, path, errors)
    if len(items) > max_length:
        errors.append(f"too many items at {path}: max {max_length}, got {len(items)}")
    return items


def _validate_deal_management_shapes(analysis: dict[str, Any], errors: list[str]) -> None:
    deal_mode = _expect_dict(analysis.get("deal_mode"), "deal_mode", errors)
    if deal_mode:
        _expect_enum(
            deal_mode.get("mode"),
            "deal_mode.mode",
            {
                "active_sale",
                "payment_control",
                "managed_pause",
                "hard_qualification",
                "nurture",
                "disqualify",
                "lost_risk",
                "unknown",
            },
            errors,
        )
        for field in ("reason", "manager_behavior", "rop_focus"):
            _expect_non_empty_string(deal_mode.get(field), f"deal_mode.{field}", errors)

    closed_review = _expect_dict(analysis.get("closed_deal_review"), "closed_deal_review", errors)
    if closed_review:
        applicable = closed_review.get("applicable")
        _expect_bool(applicable, "closed_deal_review.applicable", errors)
        _expect_bool(closed_review.get("crm_closed"), "closed_deal_review.crm_closed", errors)
        _expect_bool(closed_review.get("reopen_candidate"), "closed_deal_review.reopen_candidate", errors)
        _expect_bool(
            closed_review.get("client_reactivation_allowed"),
            "closed_deal_review.client_reactivation_allowed",
            errors,
        )
        _expect_enum(
            closed_review.get("closed_reason_type"),
            "closed_deal_review.closed_reason_type",
            {
                "duplicate",
                "lost_to_competitor",
                "integration_blocker",
                "price_lost",
                "postponed",
                "wrong_qualification",
                "cannot_produce",
                "not_relevant",
                "no_response",
                "won",
                "unknown",
                "not_applicable",
            },
            errors,
        )
        _expect_enum(
            closed_review.get("confidence"),
            "closed_deal_review.confidence",
            {"high", "medium", "low", "unknown"},
            errors,
        )
        _expect_enum(
            closed_review.get("rop_decision"),
            "closed_deal_review.rop_decision",
            {"return_to_pipeline", "keep_closed", "needs_manual_review", "not_applicable"},
            errors,
        )
        for field in ("stage_id", "stage_name", "recommended_pipeline_action", "client_text_usage_note"):
            _expect_non_empty_string(closed_review.get(field), f"closed_deal_review.{field}", errors)
        questionable = _expect_max_list_length(
            closed_review.get("why_closed_questionable"),
            "closed_deal_review.why_closed_questionable",
            5,
            errors,
        )
        may_be_valid = _expect_max_list_length(
            closed_review.get("why_closed_may_be_valid"),
            "closed_deal_review.why_closed_may_be_valid",
            5,
            errors,
        )
        if applicable is True:
            if closed_review.get("closed_reason_type") == "not_applicable":
                errors.append("closed_deal_review.closed_reason_type must not be not_applicable when applicable=true")
            if closed_review.get("rop_decision") == "not_applicable":
                errors.append("closed_deal_review.rop_decision must not be not_applicable when applicable=true")
            if not questionable and not may_be_valid:
                errors.append(
                    "closed_deal_review needs at least one reason in why_closed_questionable or why_closed_may_be_valid when applicable=true"
                )

    resource_control = _expect_dict(analysis.get("resource_control"), "resource_control", errors)
    if resource_control:
        if not isinstance(resource_control.get("should_spend_engineering_time"), bool):
            errors.append("expected boolean at resource_control.should_spend_engineering_time")
        _expect_non_empty_string(resource_control.get("reason"), "resource_control.reason", errors)
        _expect_max_list_length(resource_control.get("allowed_work"), "resource_control.allowed_work", 5, errors)
        _expect_max_list_length(resource_control.get("blocked_work"), "resource_control.blocked_work", 5, errors)

    payment_blocker = _expect_dict(analysis.get("payment_blocker"), "payment_blocker", errors)
    if payment_blocker:
        applicable = payment_blocker.get("applicable")
        _expect_bool(applicable, "payment_blocker.applicable", errors)
        _expect_enum(
            payment_blocker.get("blocker_type"),
            "payment_blocker.blocker_type",
            {
                "advance_payment",
                "leasing_payment",
                "invoice_payment",
                "internal_approval",
                "documents",
                "unknown",
                "not_applicable",
            },
            errors,
        )
        for field in ("payer", "payment_recipient", "current_status", "post_payment_next_step", "escalation_condition"):
            _expect_non_empty_string(payment_blocker.get(field), f"payment_blocker.{field}", errors)
        confirmed_payment_date = payment_blocker.get("confirmed_payment_date")
        if confirmed_payment_date is not None and (
            not isinstance(confirmed_payment_date, str) or not confirmed_payment_date.strip()
        ):
            errors.append("expected payment_blocker.confirmed_payment_date to be non-empty string or null")
        missing_confirmation = _expect_max_list_length(
            payment_blocker.get("missing_confirmation"),
            "payment_blocker.missing_confirmation",
            5,
            errors,
        )
        next_actions = _expect_max_list_length(
            payment_blocker.get("next_actions"),
            "payment_blocker.next_actions",
            5,
            errors,
        )
        if applicable is True:
            if payment_blocker.get("blocker_type") == "not_applicable":
                errors.append("payment_blocker.blocker_type must not be not_applicable when applicable=true")
            if not missing_confirmation:
                errors.append("payment_blocker.missing_confirmation must not be empty when applicable=true")
            if not next_actions:
                errors.append("payment_blocker.next_actions must not be empty when applicable=true")

    money_path = _expect_dict(analysis.get("money_path_diagnosis"), "money_path_diagnosis", errors)
    if money_path:
        _expect_enum(
            money_path.get("stuck_point"),
            "money_path_diagnosis.stuck_point",
            {
                "source",
                "call_attempt",
                "manager",
                "next_step",
                "stage",
                "payment",
                "client_pause",
                "unknown",
            },
            errors,
        )
        _expect_enum(
            money_path.get("current_owner_of_next_step"),
            "money_path_diagnosis.current_owner_of_next_step",
            {"manager", "client", "rop", "finance", "leasing", "unknown"},
            errors,
        )
        for field in ("why_money_is_at_risk", "next_required_fact"):
            _expect_non_empty_string(money_path.get(field), f"money_path_diagnosis.{field}", errors)
        evidence = _expect_max_list_length(money_path.get("evidence"), "money_path_diagnosis.evidence", 7, errors)
        if not evidence:
            errors.append("money_path_diagnosis.evidence must not be empty")

    price_check = _expect_dict(analysis.get("price_comparability_check"), "price_comparability_check", errors)
    if price_check:
        applicable = price_check.get("applicable")
        _expect_bool(applicable, "price_comparability_check.applicable", errors)
        _expect_enum(
            price_check.get("price_gap_signal"),
            "price_comparability_check.price_gap_signal",
            {"none", "minor", "substantial", "unknown"},
            errors,
        )
        for field in ("summary", "when_closing_is_valid", "when_to_return_to_pipeline"):
            _expect_non_empty_string(price_check.get(field), f"price_comparability_check.{field}", errors)
        unclear = _expect_max_list_length(
            price_check.get("what_is_unclear"),
            "price_comparability_check.what_is_unclear",
            5,
            errors,
        )
        checks = _expect_max_list_length(
            price_check.get("what_rop_should_check"),
            "price_comparability_check.what_rop_should_check",
            5,
            errors,
        )
        evidence = _expect_max_list_length(
            price_check.get("evidence"),
            "price_comparability_check.evidence",
            7,
            errors,
        )
        if applicable is True:
            if not unclear:
                errors.append("price_comparability_check.what_is_unclear must not be empty when applicable=true")
            if not checks:
                errors.append("price_comparability_check.what_rop_should_check must not be empty when applicable=true")
            if not evidence:
                errors.append("price_comparability_check.evidence must not be empty when applicable=true")

    objection_handling = _expect_dict(analysis.get("objection_handling"), "objection_handling", errors)
    if objection_handling:
        applicable = objection_handling.get("applicable")
        _expect_bool(applicable, "objection_handling.applicable", errors)
        _expect_non_empty_string(objection_handling.get("summary"), "objection_handling.summary", errors)
        objections = _expect_max_list_length(
            objection_handling.get("likely_objections"),
            "objection_handling.likely_objections",
            3,
            errors,
        )
        if applicable is True and not objections:
            errors.append("objection_handling.likely_objections must not be empty when applicable=true")
        for index, objection in enumerate(objections):
            path = f"objection_handling.likely_objections[{index}]"
            item = _expect_dict(objection, path, errors)
            if not item:
                continue
            _expect_enum(
                item.get("objection_type"),
                f"{path}.objection_type",
                {
                    "price",
                    "budget",
                    "china",
                    "competitor",
                    "pause",
                    "decision_maker",
                    "technical_doubt",
                    "timing",
                    "internal_approval",
                    "payment_delay",
                    "unknown",
                },
                errors,
            )
            _expect_enum(item.get("probability"), f"{path}.probability", {"high", "medium", "low"}, errors)
            for field in (
                "evidence",
                "client_phrase",
                "manager_reply",
                "follow_up_question",
                "next_step_goal",
                "what_not_to_do",
            ):
                _expect_non_empty_text_without_markers(item.get(field), f"{path}.{field}", errors)

    shaker_question = _expect_dict(analysis.get("shaker_question"), "shaker_question", errors)
    if shaker_question:
        _expect_non_empty_string(shaker_question.get("question"), "shaker_question.question", errors)
        _expect_non_empty_string(shaker_question.get("why_this_question"), "shaker_question.why_this_question", errors)
        _expect_non_empty_string(shaker_question.get("when_to_use"), "shaker_question.when_to_use", errors)

    competitor = _expect_dict(
        analysis.get("competitor_defense_checklist"),
        "competitor_defense_checklist",
        errors,
    )
    if competitor:
        if not isinstance(competitor.get("applicable"), bool):
            errors.append("expected boolean at competitor_defense_checklist.applicable")
        _expect_enum(
            competitor.get("competitor_type"),
            "competitor_defense_checklist.competitor_type",
            {"china", "direct_competitor", "alternative_supplier", "internal_solution", "unknown", "not_applicable"},
            errors,
        )
        _expect_max_list_length(competitor.get("defense_points"), "competitor_defense_checklist.defense_points", 5, errors)
        _expect_max_list_length(competitor.get("questions_to_client"), "competitor_defense_checklist.questions_to_client", 5, errors)
        _expect_non_empty_string(
            competitor.get("risk_if_not_defended"),
            "competitor_defense_checklist.risk_if_not_defended",
            errors,
        )

    priority = _expect_dict(analysis.get("priority_recommendation"), "priority_recommendation", errors)
    if priority:
        _expect_enum(
            priority.get("priority"),
            "priority_recommendation.priority",
            {"high", "medium", "low", "pause", "disqualify"},
            errors,
        )
        for field in ("reason", "time_allocation", "what_must_happen_to_raise_priority"):
            _expect_non_empty_string(priority.get(field), f"priority_recommendation.{field}", errors)
        next_review = priority.get("next_review_date")
        if next_review is not None and (not isinstance(next_review, str) or not next_review.strip()):
            errors.append("expected next_review_date to be non-empty string or null")


def validate_deal_analysis(analysis: dict[str, Any]) -> None:
    errors: list[str] = []
    _require_fields(analysis, DEAL_REQUIRED_FIELDS, "", errors)
    _expect_dict(analysis.get("deal_state"), "deal_state", errors)
    _expect_dict(analysis.get("new_event"), "new_event", errors)
    _expect_list(analysis.get("what_changed"), "what_changed", errors)
    _expect_dict(analysis.get("deal_progress"), "deal_progress", errors)
    _validate_deal_management_shapes(analysis, errors)
    _validate_common_shapes(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid deal analysis: " + "; ".join(errors))


def validate_lead_analysis(analysis: dict[str, Any]) -> None:
    errors: list[str] = []
    _require_fields(analysis, LEAD_REQUIRED_FIELDS, "", errors)
    _expect_dict(analysis.get("lead_state"), "lead_state", errors)
    _expect_dict(analysis.get("activity_summary"), "activity_summary", errors)
    loss = _expect_dict(analysis.get("loss_diagnosis"), "loss_diagnosis", errors)
    if loss:
        _expect_enum(loss.get("lead_quality"), "loss_diagnosis.lead_quality", {"good", "weak", "bad", "unknown"}, errors)
        _expect_enum(
            loss.get("processing_quality"),
            "loss_diagnosis.processing_quality",
            {"good", "weak", "bad", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("source_signal"),
            "loss_diagnosis.source_signal",
            {"good_source", "weak_source", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("call_attempt_quality"),
            "loss_diagnosis.call_attempt_quality",
            {"enough", "not_enough", "wrong_channel", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("next_step_quality"),
            "loss_diagnosis.next_step_quality",
            {"clear", "missing", "too_generic", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("final_verdict"),
            "loss_diagnosis.final_verdict",
            {"bad_lead", "bad_processing", "data_gap", "needs_nurture", "ready_for_deal", "unknown"},
            errors,
        )
        evidence = _expect_max_list_length(loss.get("evidence"), "loss_diagnosis.evidence", 7, errors)
        if not evidence:
            errors.append("loss_diagnosis.evidence must not be empty")
    _validate_common_shapes(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid lead analysis: " + "; ".join(errors))
