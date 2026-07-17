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
    "qualification_assessment",
}

LEAD_REQUIRED_FIELDS = COMMON_REQUIRED_FIELDS | {
    "lead_id",
    "lead_state",
    "activity_summary",
    "loss_diagnosis",
    "qualification_assessment",
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
    "qualification_assessment.bant.budget.evidence": 7,
    "qualification_assessment.bant.authority.evidence": 7,
    "qualification_assessment.bant.need.evidence": 7,
    "qualification_assessment.bant.timeframe.evidence": 7,
    "qualification_assessment.bant.budget.missing_facts": 7,
    "qualification_assessment.bant.authority.missing_facts": 7,
    "qualification_assessment.bant.need.missing_facts": 7,
    "qualification_assessment.bant.timeframe.missing_facts": 7,
    "qualification_assessment.solution_fit.evidence": 7,
    "qualification_assessment.commercial_fit.evidence": 7,
    "qualification_assessment.lead_category.reason_codes": 7,
    "qualification_assessment.lead_category.bant_factors": 7,
    "qualification_assessment.lead_category.technical_factors": 7,
    "qualification_assessment.lead_category.budget_factors": 7,
    "qualification_assessment.lead_category.missing_facts": 7,
    "qualification_assessment.lead_route.evidence": 7,
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


def normalize_analysis_for_validation(
    analysis: dict[str, Any],
    *,
    allow_legacy_qualification_assessment: bool = False,
) -> list[dict[str, Any]]:
    """Clamp model lists and normalize only schema-safe qualification defaults.

    ``allow_legacy_qualification_assessment`` is for already saved reports created
    before the qualification block existed. New model responses keep the block
    mandatory and are rejected by ``validate_lead_analysis`` when it is absent.
    """

    changes: list[dict[str, Any]] = []
    _normalize_qualification_assessment(
        analysis,
        changes,
        allow_legacy_qualification_assessment=allow_legacy_qualification_assessment,
    )
    if allow_legacy_qualification_assessment:
        loss = analysis.get("loss_diagnosis")
        if isinstance(loss, dict) and "route_quality" not in loss:
            loss["route_quality"] = "unknown"
            changes.append({"path": "loss_diagnosis.route_quality", "action": "added_legacy_fallback"})
        call_attempt = analysis.get("call_attempt_recommendation")
        if isinstance(call_attempt, dict) and "cycle_status" not in call_attempt:
            call_attempt["cycle_status"] = "unknown"
            changes.append({"path": "call_attempt_recommendation.cycle_status", "action": "added_legacy_fallback"})
        lead_state = analysis.get("lead_state")
        assessment = analysis.get("qualification_assessment")
        category = assessment.get("lead_category") if isinstance(assessment, dict) else None
        if isinstance(lead_state, dict) and isinstance(category, dict) and any(
            change.get("action") == "added_legacy_fallback" and change.get("path") == "qualification_assessment"
            for change in changes
        ):
            lead_state["qualification"] = "unknown"
            if isinstance(loss, dict):
                loss["route_quality"] = "unknown"
            if isinstance(call_attempt, dict):
                call_attempt["cycle_status"] = "unknown"
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


def _legacy_qualification_assessment() -> dict[str, Any]:
    labels = {
        "budget": "Бюджет и финансовая готовность",
        "authority": "ЛПР и влияние на решение",
        "need": "Актуальная потребность",
        "timeframe": "Срок покупки или запуска",
    }

    def bant_item(name: str) -> dict[str, Any]:
        item = {
            "label": labels[name],
            "status": "unknown",
            "summary": "В старом анализе нет структурированной оценки.",
            "evidence": [],
            "missing_facts": ["Нет данных в сохранённом формате анализа."],
            "next_question_or_action": "Проверить факты в CRM и уточнить критерий у клиента.",
        }
        if name == "timeframe":
            item["purchase_window"] = "unknown"
            item["decision_timing_status"] = "unknown"
            item["decision_timing"] = None
            item["need_or_launch_timing_status"] = "unknown"
            item["need_or_launch_timing"] = None
        return item

    return {
        "bant": {
            "budget": bant_item("budget"),
            "authority": bant_item("authority"),
            "need": bant_item("need"),
            "timeframe": bant_item("timeframe"),
            "overall_status": "unknown",
            "missing_facts": [],
            "next_question": None,
        },
        "solution_fit": {
            "equipment_type": "unknown",
            "status": "unknown",
            "technical_data_status": "unknown",
            "reason_code": "unknown",
            "evidence": [],
            "missing_facts": [],
            "next_question_or_action": None,
        },
        "commercial_fit": {
            "new_equipment_budget_status": "unknown",
            "budget_named": False,
            "applies_to_new_equipment": "unknown",
            "confirmed_budget_rub": None,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "unknown",
            "evidence": [],
            "missing_facts": [],
            "next_question_or_action": None,
        },
        "lead_category": {
            "value": "unknown",
            "reason": "В старом анализе нет структурированного основания категории.",
            "reason_codes": [],
            "bant_factors": [],
            "technical_factors": [],
            "budget_factors": [],
            "missing_facts": ["Нет данных в сохранённом формате анализа."],
            "next_step": "Проверить факты в CRM и при необходимости обновить анализ.",
        },
        "lead_route": {
            "current_route": "unknown",
            "recommended_route": "unknown",
            "status": "unknown",
            "reason": "В старом анализе нет структурированной проверки маршрута.",
            "controlled_return_required": False,
            "controlled_return_status": "not_required",
            "controlled_return_date": None,
            "recommended_return_date": None,
            "evidence": [],
        },
    }


def _normalize_qualification_assessment(
    analysis: dict[str, Any],
    changes: list[dict[str, Any]],
    *,
    allow_legacy_qualification_assessment: bool,
) -> None:
    assessment = analysis.get("qualification_assessment")
    if assessment is None:
        if allow_legacy_qualification_assessment and "qualification_assessment" not in analysis:
            analysis["qualification_assessment"] = _legacy_qualification_assessment()
            changes.append({"path": "qualification_assessment", "action": "added_legacy_fallback"})
        return
    if not isinstance(assessment, dict):
        return

    bant = assessment.get("bant")
    if isinstance(bant, dict):
        for name in ("budget", "authority", "need", "timeframe"):
            item = bant.get(name)
            if not isinstance(item, dict):
                continue
            if item.get("status") is None:
                item["status"] = "unknown"
                changes.append({"path": f"qualification_assessment.bant.{name}.status", "action": "null_to_unknown"})
            if item.get("evidence") is None:
                item["evidence"] = []
                changes.append({"path": f"qualification_assessment.bant.{name}.evidence", "action": "null_to_empty_list"})
            if name == "timeframe" and allow_legacy_qualification_assessment:
                for field, default in (
                    ("decision_timing_status", "unknown"),
                    ("decision_timing", None),
                    ("need_or_launch_timing_status", "unknown"),
                    ("need_or_launch_timing", None),
                ):
                    if field not in item:
                        item[field] = default
                        changes.append(
                            {
                                "path": f"qualification_assessment.bant.timeframe.{field}",
                                "action": "added_legacy_fallback",
                            }
                        )
        if bant.get("overall_status") is None:
            bant["overall_status"] = "unknown"
            changes.append({"path": "qualification_assessment.bant.overall_status", "action": "null_to_unknown"})
        if bant.get("missing_facts") is None:
            bant["missing_facts"] = []
            changes.append({"path": "qualification_assessment.bant.missing_facts", "action": "null_to_empty_list"})

    solution_fit = assessment.get("solution_fit")
    if isinstance(solution_fit, dict):
        for field in ("equipment_type", "status"):
            if solution_fit.get(field) is None:
                solution_fit[field] = "unknown"
                changes.append({"path": f"qualification_assessment.solution_fit.{field}", "action": "null_to_unknown"})
        for field in ("evidence", "missing_facts"):
            if solution_fit.get(field) is None:
                solution_fit[field] = []
                changes.append({"path": f"qualification_assessment.solution_fit.{field}", "action": "null_to_empty_list"})

    commercial_fit = assessment.get("commercial_fit")
    if isinstance(commercial_fit, dict):
        if commercial_fit.get("new_equipment_budget_status") is None:
            commercial_fit["new_equipment_budget_status"] = "unknown"
            changes.append(
                {
                    "path": "qualification_assessment.commercial_fit.new_equipment_budget_status",
                    "action": "null_to_unknown",
                }
            )
        if commercial_fit.get("new_equipment_minimum_rub") is None:
            commercial_fit["new_equipment_minimum_rub"] = 1_000_000
            changes.append(
                {
                    "path": "qualification_assessment.commercial_fit.new_equipment_minimum_rub",
                    "action": "null_to_default",
                }
            )
        if commercial_fit.get("evidence") is None:
            commercial_fit["evidence"] = []
            changes.append({"path": "qualification_assessment.commercial_fit.evidence", "action": "null_to_empty_list"})

    lead_category = assessment.get("lead_category")
    if isinstance(lead_category, dict):
        category_value = lead_category.get("value")
        reason_codes = lead_category.get("reason_codes")
        if category_value in {"A", "B", "C", "unknown"} and isinstance(reason_codes, list) and reason_codes:
            lead_category["reason_codes"] = []
            changes.append(
                {
                    "path": "qualification_assessment.lead_category.reason_codes",
                    "action": "cleared_non_rejection_reason_codes",
                    "category": category_value,
                    "removed_items": len(reason_codes),
                }
            )

    lead_route = assessment.get("lead_route")
    if isinstance(lead_route, dict) and allow_legacy_qualification_assessment:
        legacy_date = lead_route.get("controlled_return_date")
        defaults = {
            "controlled_return_status": "needs_clarification" if lead_route.get("controlled_return_required") else "not_required",
            "recommended_return_date": None,
        }
        for field, default in defaults.items():
            if field not in lead_route:
                lead_route[field] = default
                changes.append(
                    {"path": f"qualification_assessment.lead_route.{field}", "action": "added_legacy_fallback"}
                )
        if legacy_date and lead_route.get("controlled_return_status") == "needs_clarification":
            lead_route["recommended_return_date"] = legacy_date
            lead_route["controlled_return_date"] = None


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


def _expect_enum(value: Any, path: str, allowed: set[Any], errors: list[str]) -> None:
    if value not in allowed:
        errors.append(f"invalid enum at {path}: expected one of {sorted(map(repr, allowed))}, got {value!r}")


def _expect_non_empty_string(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"expected non-empty string at {path}")


def _expect_max_list_length(value: Any, path: str, max_length: int, errors: list[str]) -> list[Any]:
    items = _expect_list(value, path, errors)
    if len(items) > max_length:
        errors.append(f"too many items at {path}: max {max_length}, got {len(items)}")
    return items


def _validate_short_text_list(value: Any, path: str, max_length: int, errors: list[str]) -> list[Any]:
    items = _expect_max_list_length(value, path, max_length, errors)
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"expected non-empty string at {path}[{index}]")
    return items


def _expect_number_or_none(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        errors.append(f"expected number or null at {path}")


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _validate_qualification_evidence(
    value: Any,
    path: str,
    status: Any,
    empty_allowed_for: set[str],
    errors: list[str],
) -> None:
    evidence = _validate_short_text_list(value, path, 7, errors)
    if not evidence and status not in empty_allowed_for:
        errors.append(f"{path} must not be empty when status is {status!r}")


def _validate_optional_question(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        errors.append(f"expected {path} to be non-empty string or null")


def _validate_bant_item(
    value: Any,
    path: str,
    errors: list[str],
    *,
    lead_contract: bool = False,
    name: str = "",
) -> None:
    item = _expect_dict(value, path, errors)
    if not item:
        return
    required = {"status", "evidence"}
    if name == "timeframe":
        required |= {
            "decision_timing_status",
            "decision_timing",
            "need_or_launch_timing_status",
            "need_or_launch_timing",
        }
    if lead_contract:
        required |= {"label", "summary", "missing_facts", "next_question_or_action"}
        if name == "timeframe":
            required.add("purchase_window")
    _require_fields(item, required, path, errors)
    status = item.get("status")
    statuses = {"confirmed", "not_confirmed", "negative", "unknown"} if lead_contract else {
        "confirmed",
        "missing",
        "unknown",
    }
    _expect_enum(status, f"{path}.status", statuses, errors)
    if name == "timeframe":
        for prefix in ("decision_timing", "need_or_launch_timing"):
            timing_status = item.get(f"{prefix}_status")
            _expect_enum(
                timing_status,
                f"{path}.{prefix}_status",
                {"confirmed", "not_confirmed", "unknown"},
                errors,
            )
            timing_value = item.get(prefix)
            _validate_optional_question(timing_value, f"{path}.{prefix}", errors)
            if timing_status == "confirmed" and timing_value is None:
                errors.append(f"{path}.{prefix} is required when {prefix}_status=confirmed")
            if timing_status != "confirmed" and timing_value is not None:
                errors.append(f"{path}.{prefix} must be null unless {prefix}_status=confirmed")
    empty_allowed = {"not_confirmed", "unknown"} if lead_contract else {"missing", "unknown"}
    _validate_qualification_evidence(item.get("evidence"), f"{path}.evidence", status, empty_allowed, errors)
    if not lead_contract:
        return
    _expect_non_empty_string(item.get("label"), f"{path}.label", errors)
    _expect_non_empty_string(item.get("summary"), f"{path}.summary", errors)
    missing_facts = _validate_short_text_list(item.get("missing_facts"), f"{path}.missing_facts", 7, errors)
    question = item.get("next_question_or_action")
    _validate_optional_question(question, f"{path}.next_question_or_action", errors)
    if status in {"not_confirmed", "unknown"} and not missing_facts and question is None:
        errors.append(f"{path} requires missing_facts or next_question_or_action when status={status}")
    if name == "timeframe":
        _expect_enum(
            item.get("purchase_window"),
            f"{path}.purchase_window",
            {"up_to_60_days", "days_61_to_89", "months_3_to_12", "over_12_months", "unknown"},
            errors,
        )


def _validate_qualification_assessment(
    analysis: dict[str, Any], errors: list[str], *, lead_contract: bool = False
) -> None:
    assessment = _expect_dict(analysis.get("qualification_assessment"), "qualification_assessment", errors)
    if not assessment:
        return
    required = {"bant", "solution_fit", "commercial_fit"}
    if lead_contract:
        required |= {"lead_category", "lead_route"}
    _require_fields(assessment, required, "qualification_assessment", errors)

    bant = _expect_dict(assessment.get("bant"), "qualification_assessment.bant", errors)
    if bant:
        _require_fields(
            bant,
            {"budget", "authority", "need", "timeframe", "overall_status", "missing_facts", "next_question"},
            "qualification_assessment.bant",
            errors,
        )
        for name in ("budget", "authority", "need", "timeframe"):
            _validate_bant_item(
                bant.get(name),
                f"qualification_assessment.bant.{name}",
                errors,
                lead_contract=lead_contract,
                name=name,
            )
        overall_status = bant.get("overall_status")
        _expect_enum(
            overall_status,
            "qualification_assessment.bant.overall_status",
            {"confirmed", "incomplete", "negative", "unknown"} if lead_contract else {"confirmed", "incomplete", "unknown"},
            errors,
        )
        missing_facts = _validate_short_text_list(
            bant.get("missing_facts"),
            "qualification_assessment.bant.missing_facts",
            7,
            errors,
        )
        next_question = bant.get("next_question")
        _validate_optional_question(next_question, "qualification_assessment.bant.next_question", errors)
        if overall_status == "incomplete" and not missing_facts and next_question is None:
            errors.append(
                "qualification_assessment.bant requires missing_facts or next_question when overall_status=incomplete"
            )

    solution_fit = _expect_dict(assessment.get("solution_fit"), "qualification_assessment.solution_fit", errors)
    if solution_fit:
        solution_required = {"equipment_type", "status", "reason_code", "evidence", "missing_facts"}
        if lead_contract:
            solution_required |= {"technical_data_status", "next_question_or_action"}
        _require_fields(
            solution_fit,
            solution_required,
            "qualification_assessment.solution_fit",
            errors,
        )
        status = solution_fit.get("status")
        _expect_enum(
            solution_fit.get("equipment_type"),
            "qualification_assessment.solution_fit.equipment_type",
            {"labeler", "filling_line", "block", "unknown"},
            errors,
        )
        _expect_enum(
            status,
            "qualification_assessment.solution_fit.status",
            {"compatible", "not_compatible", "needs_technical_data", "unknown"},
            errors,
        )
        _expect_enum(
            solution_fit.get("reason_code"),
            "qualification_assessment.solution_fit.reason_code",
            {"technical_mismatch", "unknown", None},
            errors,
        )
        _validate_qualification_evidence(
            solution_fit.get("evidence"),
            "qualification_assessment.solution_fit.evidence",
            status,
            {"unknown"},
            errors,
        )
        missing_facts = _validate_short_text_list(
            solution_fit.get("missing_facts"),
            "qualification_assessment.solution_fit.missing_facts",
            7,
            errors,
        )
        if status == "needs_technical_data" and not missing_facts:
            errors.append(
                "qualification_assessment.solution_fit.missing_facts must not be empty when status=needs_technical_data"
            )
        if status == "not_compatible" and solution_fit.get("reason_code") != "technical_mismatch":
            errors.append(
                "qualification_assessment.solution_fit.reason_code must be technical_mismatch when status=not_compatible"
            )
        if solution_fit.get("reason_code") == "technical_mismatch" and status != "not_compatible":
            errors.append(
                "qualification_assessment.solution_fit.status must be not_compatible when reason_code=technical_mismatch"
            )
        if lead_contract:
            technical_data_status = solution_fit.get("technical_data_status")
            _expect_enum(
                technical_data_status,
                "qualification_assessment.solution_fit.technical_data_status",
                {"sufficient", "insufficient", "unknown"},
                errors,
            )
            _validate_optional_question(
                solution_fit.get("next_question_or_action"),
                "qualification_assessment.solution_fit.next_question_or_action",
                errors,
            )
            if status == "needs_technical_data" and technical_data_status != "insufficient":
                errors.append(
                    "qualification_assessment.solution_fit.technical_data_status must be insufficient when status=needs_technical_data"
                )

    commercial_fit = _expect_dict(assessment.get("commercial_fit"), "qualification_assessment.commercial_fit", errors)
    if commercial_fit:
        commercial_required = {
                "new_equipment_budget_status",
                "confirmed_budget_rub",
                "new_equipment_minimum_rub",
                "reason_code",
                "evidence",
            }
        if lead_contract:
            commercial_required |= {
                "budget_named",
                "applies_to_new_equipment",
                "missing_facts",
                "next_question_or_action",
            }
        _require_fields(
            commercial_fit,
            commercial_required,
            "qualification_assessment.commercial_fit",
            errors,
        )
        status = commercial_fit.get("new_equipment_budget_status")
        _expect_enum(
            status,
            "qualification_assessment.commercial_fit.new_equipment_budget_status",
            {"sufficient", "below_minimum", "unknown"},
            errors,
        )
        _expect_number_or_none(
            commercial_fit.get("confirmed_budget_rub"),
            "qualification_assessment.commercial_fit.confirmed_budget_rub",
            errors,
        )
        if commercial_fit.get("new_equipment_minimum_rub") != 1_000_000:
            errors.append("qualification_assessment.commercial_fit.new_equipment_minimum_rub must equal 1000000")
        _expect_enum(
            commercial_fit.get("reason_code"),
            "qualification_assessment.commercial_fit.reason_code",
            {"budget_below_new_equipment_minimum", "unknown", None},
            errors,
        )
        _validate_qualification_evidence(
            commercial_fit.get("evidence"),
            "qualification_assessment.commercial_fit.evidence",
            status,
            {"unknown"},
            errors,
        )
        budget = commercial_fit.get("confirmed_budget_rub")
        if status == "below_minimum":
            if not _is_number(budget) or budget >= 1_000_000:
                errors.append(
                    "qualification_assessment.commercial_fit.confirmed_budget_rub must be a number below 1000000 when status=below_minimum"
                )
            if commercial_fit.get("reason_code") != "budget_below_new_equipment_minimum":
                errors.append(
                    "qualification_assessment.commercial_fit.reason_code must be budget_below_new_equipment_minimum when status=below_minimum"
                )
        if status == "sufficient" and (not _is_number(budget) or budget < 1_000_000):
            errors.append(
                "qualification_assessment.commercial_fit.confirmed_budget_rub must be a number at least 1000000 when status=sufficient"
            )
        if commercial_fit.get("reason_code") == "budget_below_new_equipment_minimum" and status != "below_minimum":
            errors.append(
                "qualification_assessment.commercial_fit.new_equipment_budget_status must be below_minimum when reason_code=budget_below_new_equipment_minimum"
            )
        if lead_contract:
            _expect_bool(commercial_fit.get("budget_named"), "qualification_assessment.commercial_fit.budget_named", errors)
            _expect_enum(
                commercial_fit.get("applies_to_new_equipment"),
                "qualification_assessment.commercial_fit.applies_to_new_equipment",
                {True, False, "unknown"},
                errors,
            )
            _validate_short_text_list(
                commercial_fit.get("missing_facts"),
                "qualification_assessment.commercial_fit.missing_facts",
                7,
                errors,
            )
            _validate_optional_question(
                commercial_fit.get("next_question_or_action"),
                "qualification_assessment.commercial_fit.next_question_or_action",
                errors,
            )
            if status in {"sufficient", "below_minimum"}:
                if commercial_fit.get("budget_named") is not True:
                    errors.append("qualification_assessment.commercial_fit.budget_named must be true for a known budget")
                if commercial_fit.get("applies_to_new_equipment") is not True:
                    errors.append(
                        "qualification_assessment.commercial_fit.applies_to_new_equipment must be true for a budget category decision"
                    )

    if lead_contract:
        _validate_lead_category(assessment.get("lead_category"), errors)
        _validate_lead_route(assessment.get("lead_route"), errors)


LEAD_CATEGORY_REASONS = {
    "technical_mismatch",
    "budget_below_new_equipment_minimum",
    "timeframe_over_12_months",
    "spam",
    "invalid_contact",
    "call_cycle_completed_no_contact",
}
LEAD_D_REASONS = {
    "technical_mismatch",
    "budget_below_new_equipment_minimum",
    "timeframe_over_12_months",
}
LEAD_E_REASONS = {"spam", "invalid_contact", "call_cycle_completed_no_contact"}
LEAD_ROUTES = {
    "ordinary_deal",
    "op2",
    "clarification",
    "auto_reminder",
    "deferred_demand",
    "disqualified",
    "unknown",
}


def _validate_lead_category(value: Any, errors: list[str]) -> None:
    path = "qualification_assessment.lead_category"
    category = _expect_dict(value, path, errors)
    if not category:
        return
    _require_fields(
        category,
        {
            "value",
            "reason",
            "reason_codes",
            "bant_factors",
            "technical_factors",
            "budget_factors",
            "missing_facts",
            "next_step",
        },
        path,
        errors,
    )
    value_name = category.get("value")
    _expect_enum(value_name, f"{path}.value", {"A", "B", "C", "D", "E", "unknown"}, errors)
    _expect_non_empty_string(category.get("reason"), f"{path}.reason", errors)
    _expect_non_empty_string(category.get("next_step"), f"{path}.next_step", errors)
    reason_codes = _validate_short_text_list(category.get("reason_codes"), f"{path}.reason_codes", 7, errors)
    invalid_reasons = [reason for reason in reason_codes if reason not in LEAD_CATEGORY_REASONS]
    if invalid_reasons:
        errors.append(f"invalid lead category reason_codes: {invalid_reasons!r}")
    for field in ("bant_factors", "technical_factors", "budget_factors", "missing_facts"):
        _validate_short_text_list(category.get(field), f"{path}.{field}", 7, errors)
    if value_name == "D" and (not reason_codes or any(reason not in LEAD_D_REASONS for reason in reason_codes)):
        errors.append("qualification_assessment.lead_category.value=D requires only confirmed D reason_codes")
    if value_name == "E" and (not reason_codes or any(reason not in LEAD_E_REASONS for reason in reason_codes)):
        errors.append("qualification_assessment.lead_category.value=E requires only confirmed E reason_codes")
    if value_name not in {"D", "E"} and reason_codes:
        errors.append("qualification_assessment.lead_category.reason_codes are reserved for D/E grounds")
    if value_name == "unknown" and not category.get("missing_facts"):
        errors.append("qualification_assessment.lead_category.missing_facts must not be empty for unknown")


def _validate_lead_route(value: Any, errors: list[str]) -> None:
    path = "qualification_assessment.lead_route"
    route = _expect_dict(value, path, errors)
    if not route:
        return
    _require_fields(
        route,
        {
            "current_route",
            "recommended_route",
            "status",
            "reason",
            "controlled_return_required",
            "controlled_return_status",
            "controlled_return_date",
            "recommended_return_date",
            "evidence",
        },
        path,
        errors,
    )
    _expect_enum(route.get("current_route"), f"{path}.current_route", LEAD_ROUTES, errors)
    _expect_enum(route.get("recommended_route"), f"{path}.recommended_route", LEAD_ROUTES, errors)
    _expect_enum(
        route.get("status"),
        f"{path}.status",
        {"allowed", "violation", "needs_clarification", "unknown"},
        errors,
    )
    _expect_non_empty_string(route.get("reason"), f"{path}.reason", errors)
    _expect_bool(route.get("controlled_return_required"), f"{path}.controlled_return_required", errors)
    return_status = route.get("controlled_return_status")
    _expect_enum(
        return_status,
        f"{path}.controlled_return_status",
        {"confirmed_in_crm", "missing_in_crm", "needs_clarification", "not_required"},
        errors,
    )
    _validate_optional_question(route.get("controlled_return_date"), f"{path}.controlled_return_date", errors)
    _validate_optional_question(route.get("recommended_return_date"), f"{path}.recommended_return_date", errors)
    _validate_short_text_list(route.get("evidence"), f"{path}.evidence", 7, errors)
    if route.get("controlled_return_required") is False and return_status != "not_required":
        errors.append(f"{path}.controlled_return_status must be not_required when controlled_return_required=false")
    if route.get("controlled_return_required") is True and return_status == "not_required":
        errors.append(f"{path}.controlled_return_status cannot be not_required when controlled_return_required=true")
    if return_status == "confirmed_in_crm":
        if route.get("controlled_return_date") is None:
            errors.append(f"{path}.controlled_return_date is required when controlled_return_status=confirmed_in_crm")
        if not route.get("evidence"):
            errors.append(f"{path}.evidence must confirm the existing CRM return action")
        if route.get("recommended_return_date") is not None:
            errors.append(f"{path}.recommended_return_date must be null when return is confirmed in CRM")
    if return_status == "missing_in_crm":
        if route.get("controlled_return_date") is not None:
            errors.append(f"{path}.controlled_return_date must be null when controlled_return_status=missing_in_crm")
        if route.get("recommended_return_date") is None:
            errors.append(f"{path}.recommended_return_date is required when controlled_return_status=missing_in_crm")
    if return_status == "not_required" and (
        route.get("controlled_return_date") is not None or route.get("recommended_return_date") is not None
    ):
        errors.append(f"{path} return dates must be null when controlled_return_status=not_required")


def _bant_statuses(assessment: dict[str, Any]) -> list[Any]:
    bant = assessment.get("bant")
    if not isinstance(bant, dict):
        return []
    return [bant.get(name, {}).get("status") if isinstance(bant.get(name), dict) else None for name in (
        "budget",
        "authority",
        "need",
        "timeframe",
    )]


def _validate_lead_qualification_consistency(analysis: dict[str, Any], errors: list[str]) -> None:
    lead_state = analysis.get("lead_state")
    assessment = analysis.get("qualification_assessment")
    loss = analysis.get("loss_diagnosis")
    if not isinstance(assessment, dict) or not isinstance(loss, dict):
        return
    if not isinstance(lead_state, dict):
        return
    category = assessment.get("lead_category")
    route = assessment.get("lead_route")
    bant = assessment.get("bant")
    solution_fit = assessment.get("solution_fit")
    commercial_fit = assessment.get("commercial_fit")
    if not isinstance(category, dict) or not isinstance(route, dict) or not isinstance(bant, dict):
        return
    category_value = category.get("value")
    if lead_state.get("qualification") != category_value:
        errors.append("lead_state.qualification must match qualification_assessment.lead_category.value")
    statuses = _bant_statuses(assessment)
    timeframe = bant.get("timeframe") if isinstance(bant.get("timeframe"), dict) else {}
    purchase_window = timeframe.get("purchase_window")
    reason_codes = category.get("reason_codes") if isinstance(category.get("reason_codes"), list) else []
    overall_status = bant.get("overall_status")
    if statuses == ["confirmed", "confirmed", "confirmed", "confirmed"] and overall_status != "confirmed":
        errors.append("qualification_assessment.bant.overall_status must be confirmed when all criteria are confirmed")
    if any(status == "negative" for status in statuses) and overall_status != "negative":
        errors.append("qualification_assessment.bant.overall_status must be negative when a criterion is negative")
    if any(status in {"not_confirmed", "unknown"} for status in statuses) and overall_status == "confirmed":
        errors.append("qualification_assessment.bant.overall_status cannot be confirmed with incomplete criteria")

    if category_value == "A":
        if statuses != ["confirmed", "confirmed", "confirmed", "confirmed"]:
            errors.append("lead category A requires all four BANT criteria confirmed")
        if purchase_window != "up_to_60_days":
            errors.append("lead category A requires timeframe up_to_60_days")
        if not isinstance(solution_fit, dict) or solution_fit.get("status") != "compatible":
            errors.append("lead category A requires compatible solution_fit")
        if not isinstance(commercial_fit, dict) or commercial_fit.get("new_equipment_budget_status") != "sufficient":
            errors.append("lead category A requires sufficient confirmed new-equipment budget")
    elif category_value == "B":
        if statuses[2:3] != ["confirmed"]:
            errors.append("lead category B requires confirmed real need")
        if purchase_window not in {"up_to_60_days", "days_61_to_89"}:
            errors.append("lead category B requires a timeframe shorter than three months")
        needs_clarification = any(status in {"not_confirmed", "unknown"} for status in statuses) or (
            isinstance(solution_fit, dict) and solution_fit.get("status") == "needs_technical_data"
        )
        if not needs_clarification:
            errors.append("lead category B requires an incomplete BANT criterion or missing technical data")
        if any(status == "negative" for status in statuses):
            errors.append("lead category B does not allow a confirmed negative BANT criterion")
        if isinstance(solution_fit, dict) and solution_fit.get("status") == "not_compatible":
            errors.append("lead category B does not allow confirmed technical mismatch")
        if isinstance(commercial_fit, dict) and commercial_fit.get("new_equipment_budget_status") == "below_minimum":
            errors.append("lead category B does not allow confirmed budget below the new-equipment minimum")
    elif category_value == "C":
        if purchase_window != "months_3_to_12":
            errors.append("lead category C requires timeframe months_3_to_12")
        if route.get("controlled_return_required") is not True:
            errors.append("lead category C requires controlled_return_required=true")
        return_status = route.get("controlled_return_status")
        if return_status == "confirmed_in_crm" and not route.get("controlled_return_date"):
            errors.append("lead category C requires an existing CRM return date when return is confirmed")
        if return_status == "missing_in_crm" and route.get("status") != "violation":
            errors.append("lead category C without a CRM return action must be marked as route violation")
        if return_status == "needs_clarification" and route.get("status") != "needs_clarification":
            errors.append("lead category C with unclear return action must use needs_clarification route status")
        if isinstance(solution_fit, dict) and solution_fit.get("status") == "not_compatible":
            errors.append("lead category C does not override confirmed technical mismatch")
        if isinstance(commercial_fit, dict) and commercial_fit.get("new_equipment_budget_status") == "below_minimum":
            errors.append("lead category C does not override confirmed budget below the new-equipment minimum")
    elif category_value == "D":
        confirmed_reasons: set[str] = set()
        if purchase_window == "over_12_months":
            confirmed_reasons.add("timeframe_over_12_months")
        if isinstance(solution_fit, dict) and solution_fit.get("reason_code") == "technical_mismatch":
            confirmed_reasons.add("technical_mismatch")
        if isinstance(commercial_fit, dict) and commercial_fit.get("reason_code") == "budget_below_new_equipment_minimum":
            confirmed_reasons.add("budget_below_new_equipment_minimum")
        if set(reason_codes) != confirmed_reasons:
            errors.append("lead category D reason_codes must exactly match confirmed D grounds")
    elif category_value == "E":
        if not route.get("evidence"):
            errors.append("lead category E requires evidence of spam, invalid contact, or completed call cycle")
        call_attempt = analysis.get("call_attempt_recommendation")
        if (
            "call_cycle_completed_no_contact" in reason_codes
            and (not isinstance(call_attempt, dict) or call_attempt.get("cycle_status") != "completed")
        ):
            errors.append("call_cycle_completed_no_contact requires call_attempt_recommendation.cycle_status=completed")
    elif category_value == "unknown":
        looks_ready_for_a = (
            statuses == ["confirmed", "confirmed", "confirmed", "confirmed"]
            and purchase_window == "up_to_60_days"
            and isinstance(solution_fit, dict)
            and solution_fit.get("status") == "compatible"
            and isinstance(commercial_fit, dict)
            and commercial_fit.get("new_equipment_budget_status") == "sufficient"
        )
        if looks_ready_for_a:
            errors.append("lead category unknown cannot be used when category A is fully confirmed")

    full_bant = statuses == ["confirmed", "confirmed", "confirmed", "confirmed"]
    one_unconfirmed = sum(status in {"not_confirmed", "unknown"} for status in statuses) == 1 and all(
        status != "negative" for status in statuses
    )
    current_route = route.get("current_route")
    route_status = route.get("status")
    expected_route_quality = {
        "allowed": "correct",
        "violation": "violation",
        "needs_clarification": "needs_clarification",
        "unknown": "unknown",
    }.get(route_status)
    if expected_route_quality and loss.get("route_quality") != expected_route_quality:
        errors.append("loss_diagnosis.route_quality must match qualification_assessment.lead_route.status")
    if current_route == "ordinary_deal" and not full_bant and route_status != "violation":
        errors.append("ordinary_deal with incomplete BANT must be marked as route violation")
    if current_route == "ordinary_deal" and full_bant and route_status == "violation":
        errors.append("ordinary_deal with full BANT must not be marked as route violation")
    if current_route == "op2" and one_unconfirmed and route_status == "violation":
        errors.append("op2 with exactly one unconfirmed BANT criterion is allowed")
    if current_route == "op2" and not one_unconfirmed and route_status == "allowed":
        errors.append("op2 is allowed only with exactly one unconfirmed BANT criterion")

    if category_value != "D":
        return
    reason_to_verdict = {
        "technical_mismatch": "technical_mismatch",
        "budget_below_new_equipment_minimum": "budget_below_new_equipment_minimum",
        "timeframe_over_12_months": "timeframe_over_12_months",
    }
    expected_verdicts = {reason_to_verdict[reason] for reason in reason_codes if reason in reason_to_verdict}
    if loss.get("final_verdict") not in expected_verdicts:
        errors.append(
            "loss_diagnosis.final_verdict must match one confirmed D reason_code: "
            f"expected one of {sorted(expected_verdicts)!r}, got {loss.get('final_verdict')!r}"
        )


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
    _validate_qualification_assessment(analysis, errors)
    _validate_deal_management_shapes(analysis, errors)
    _validate_common_shapes(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid deal analysis: " + "; ".join(errors))


def validate_lead_analysis(analysis: dict[str, Any]) -> None:
    errors: list[str] = []
    _require_fields(analysis, LEAD_REQUIRED_FIELDS, "", errors)
    _expect_dict(analysis.get("lead_state"), "lead_state", errors)
    _expect_dict(analysis.get("activity_summary"), "activity_summary", errors)
    call_attempt = _expect_dict(
        analysis.get("call_attempt_recommendation"), "call_attempt_recommendation", errors
    )
    if call_attempt:
        _expect_enum(
            call_attempt.get("cycle_status"),
            "call_attempt_recommendation.cycle_status",
            {"not_started", "in_progress", "completed", "not_applicable", "unknown"},
            errors,
        )
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
            {"enough", "not_enough", "wrong_channel", "not_applicable", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("next_step_quality"),
            "loss_diagnosis.next_step_quality",
            {"clear", "missing", "too_generic", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("route_quality"),
            "loss_diagnosis.route_quality",
            {"correct", "violation", "needs_clarification", "unknown"},
            errors,
        )
        _expect_enum(
            loss.get("final_verdict"),
            "loss_diagnosis.final_verdict",
            {
                "bad_lead",
                "bad_processing",
                "data_gap",
                "needs_nurture",
                "ready_for_deal",
                "technical_mismatch",
                "budget_below_new_equipment_minimum",
                "timeframe_over_12_months",
                "no_contact_after_full_cycle",
                "unknown",
            },
            errors,
        )
        evidence = _expect_max_list_length(loss.get("evidence"), "loss_diagnosis.evidence", 7, errors)
        if not evidence:
            errors.append("loss_diagnosis.evidence must not be empty")
    _validate_qualification_assessment(analysis, errors, lead_contract=True)
    _validate_lead_qualification_consistency(analysis, errors)
    _validate_common_shapes(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid lead analysis: " + "; ".join(errors))
