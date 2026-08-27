"""
Lightweight validation for model-generated ROP analysis JSON.
"""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any

from business_time import recommendation_due_at
from openai_api.config import COMMUNICATION_QUALITY_AUDIT_ENABLED


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

DEAL_RECOMMENDED_CHANNELS = frozenset({"email", "phone", "messenger", "crm_task"})
DEAL_CONTROL_BRIEF_LIST_LIMITS = {
    "strengths": 5,
    "weaknesses": 5,
    "known_facts": 5,
    "missing_facts": 5,
    "contact_questions": 5,
    "call_opening_variants": 2,
}

DEAL_REQUIRED_FIELDS = COMMON_REQUIRED_FIELDS | {
    "deal_id",
    "deal_state",
    "deal_control_brief",
    "client_communication_profile",
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
    "deal_context",
}

LEAD_REQUIRED_FIELDS = COMMON_REQUIRED_FIELDS | {
    "lead_id",
    "lead_state",
    "activity_summary",
    "closure_review",
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
    "closure_review.evidence": 7,
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
    "deal_context.critical_facts": 10,
    "deal_context.turning_points": 8,
    "deal_context.pain_points": 6,
    "deal_context.pressure_levers": 6,
    "deal_context.commitments": 6,
    "deal_context.journey": 12,
    "deal_context.open_questions": 7,
    "deal_context.source_conflicts": 5,
    "deal_context.decision_path.influencers": 4,
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


TURNING_POINT_STATUS_ALIASES = {
    "current": "active",
}

RECOMMENDATION_FEEDBACK_STATUSES = frozenset({
    "not_done", "attempted", "contacted", "achieved", "unconfirmed",
})

COMMUNICATION_QUALITY_CRITERIA = (
    "next_action",
    "value_development",
    "data_collection",
)


def _normalize_communication_quality_audit(
    analysis: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    audit = analysis.get("communication_quality_audit")
    if not isinstance(audit, dict):
        return
    status = audit.get("status")
    criteria = audit.get("criteria")
    if isinstance(criteria, dict):
        for name in COMMUNICATION_QUALITY_CRITERIA:
            item = criteria.get(name)
            if not isinstance(item, dict):
                continue
            score = item.get("score")
            if status == "assessed" and isinstance(score, str) and score.strip() in {"0", "1"}:
                item["score"] = int(score.strip())
                changes.append(
                    {
                        "path": f"communication_quality_audit.criteria.{name}.score",
                        "action": "numeric_string_to_integer",
                    }
                )
            elif status == "insufficient_evidence" and score is not None:
                item["score"] = None
                changes.append(
                    {
                        "path": f"communication_quality_audit.criteria.{name}.score",
                        "action": "dependent_value_to_null",
                    }
                )

    reasons = audit.get("zero_reasons")
    if status == "insufficient_evidence":
        if reasons != []:
            audit["zero_reasons"] = []
            changes.append(
                {
                    "path": "communication_quality_audit.zero_reasons",
                    "action": "dependent_value_to_empty_list",
                }
            )
        if audit.get("summary_for_rop") is not None:
            audit["summary_for_rop"] = None
            changes.append(
                {
                    "path": "communication_quality_audit.summary_for_rop",
                    "action": "dependent_value_to_null",
                }
            )
        return

    if status != "assessed" or not isinstance(reasons, list):
        return
    ordered: list[Any] = []
    seen: set[str] = set()
    duplicates = 0
    for criterion in COMMUNICATION_QUALITY_CRITERIA:
        for reason in reasons:
            if not isinstance(reason, dict) or reason.get("criterion") != criterion:
                continue
            if criterion in seen:
                duplicates += 1
                continue
            seen.add(criterion)
            ordered.append(reason)
    ordered.extend(
        reason
        for reason in reasons
        if not isinstance(reason, dict) or reason.get("criterion") not in COMMUNICATION_QUALITY_CRITERIA
    )
    if ordered != reasons:
        audit["zero_reasons"] = ordered
        changes.append(
            {
                "path": "communication_quality_audit.zero_reasons",
                "action": "deduplicated_and_ordered",
                "removed_items": duplicates,
            }
        )


def normalize_analysis_for_validation(
    analysis: dict[str, Any],
    *,
    allow_legacy_qualification_assessment: bool = False,
    truncate_lists: bool = True,
) -> list[dict[str, Any]]:
    """Clamp model lists and normalize schema-safe defaults.

    ``allow_legacy_qualification_assessment`` is for already saved reports created
    before the qualification block existed. New model responses keep the block
    mandatory and are rejected by ``validate_lead_analysis`` when it is absent.
    Nearby deal_context enum aliases are rewritten only when the meaning is the
    same, so a one-token mix-up does not spend a second paid correction call.
    """

    changes: list[dict[str, Any]] = []
    _normalize_qualification_assessment(
        analysis,
        changes,
        allow_legacy_qualification_assessment=allow_legacy_qualification_assessment,
    )
    _normalize_deal_context_enum_aliases(analysis, changes)
    _normalize_communication_quality_audit(analysis, changes)
    _normalize_deal_feedback_deadline(analysis, changes)
    if allow_legacy_qualification_assessment:
        if "deal_state" in analysis and "recommendation_feedback" not in analysis:
            analysis["recommendation_feedback"] = {
                "applicable": False,
                "source_report_id": None,
                "status": "unconfirmed",
                "what_manager_did": None,
                "contact_confirmed": False,
                "target_result_achieved": False,
                "evidence": [],
                "next_action_required": False,
                "next_action_text": None,
                "next_action_at": None,
                "next_action_reason": None,
            }
            changes.append({"path": "recommendation_feedback", "action": "added_legacy_fallback"})
        loss = analysis.get("loss_diagnosis")
        if isinstance(loss, dict) and "route_quality" not in loss:
            loss["route_quality"] = "unknown"
            changes.append({"path": "loss_diagnosis.route_quality", "action": "added_legacy_fallback"})
        call_attempt = analysis.get("call_attempt_recommendation")
        if isinstance(call_attempt, dict) and "cycle_status" not in call_attempt:
            call_attempt["cycle_status"] = "unknown"
            changes.append({"path": "call_attempt_recommendation.cycle_status", "action": "added_legacy_fallback"})
        if "closure_review" not in analysis:
            analysis["closure_review"] = {
                "applicable": False,
                "crm_status_id": None,
                "crm_status_name": None,
                "crm_status_semantic_id": "unknown",
                "verdict": "not_applicable",
                "reason": "В старом анализе нет структурированной проверки закрытия.",
                "client_contact_required": True,
                "manager_task_required": True,
                "evidence": [],
            }
            changes.append({"path": "closure_review", "action": "added_legacy_fallback"})
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
    if truncate_lists:
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


def _normalize_deal_feedback_deadline(analysis: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    # The lead contract has no recommendation_feedback; never repair mixed data.
    if "deal_state" not in analysis or "lead_state" in analysis:
        return
    feedback = analysis.get("recommendation_feedback")
    if not isinstance(feedback, dict) or feedback.get("applicable") is not True:
        return
    if feedback.get("next_action_required") is not True:
        return
    value = feedback.get("next_action_at")
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?", value
    ):
        return
    normalized = recommendation_due_at(value)
    if normalized is not None:
        feedback["next_action_at"] = normalized
        changes.append({
            "path": "recommendation_feedback.next_action_at",
            "action": "date_to_business_deadline" if len(value) == 10 else "assumed_moscow_timezone",
        })


def _normalize_deal_context_enum_aliases(
    analysis: dict[str, Any],
    changes: list[dict[str, Any]],
) -> None:
    context = analysis.get("deal_context")
    if not isinstance(context, dict):
        return
    turning_points = context.get("turning_points")
    if not isinstance(turning_points, list):
        return
    for index, raw in enumerate(turning_points):
        if not isinstance(raw, dict):
            continue
        status = raw.get("status")
        if not isinstance(status, str):
            continue
        mapped = TURNING_POINT_STATUS_ALIASES.get(status.strip())
        if mapped is None or mapped == status:
            continue
        raw["status"] = mapped
        changes.append(
            {
                "path": f"deal_context.turning_points[{index}].status",
                "action": "enum_alias",
                "from": status,
                "to": mapped,
            }
        )


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


def _validate_recommendation_feedback(value: Any, errors: list[str]) -> None:
    path = "recommendation_feedback"
    feedback = _expect_dict(value, path, errors)
    if not feedback:
        return
    fields = {
        "applicable",
        "source_report_id",
        "status",
        "what_manager_did",
        "contact_confirmed",
        "target_result_achieved",
        "evidence",
        "next_action_required",
        "next_action_text",
        "next_action_at",
        "next_action_reason",
    }
    _require_fields(feedback, fields, path, errors)
    applicable = feedback.get("applicable")
    _expect_bool(applicable, f"{path}.applicable", errors)
    source_report_id = feedback.get("source_report_id")
    if source_report_id is not None and (
        isinstance(source_report_id, bool)
        or not isinstance(source_report_id, int)
        or source_report_id <= 0
    ):
        errors.append(f"expected positive integer or null at {path}.source_report_id")
    status = feedback.get("status")
    _expect_enum(status, f"{path}.status", RECOMMENDATION_FEEDBACK_STATUSES, errors)
    for field in ("contact_confirmed", "target_result_achieved", "next_action_required"):
        _expect_bool(feedback.get(field), f"{path}.{field}", errors)
    what_manager_did = feedback.get("what_manager_did")
    if what_manager_did is not None:
        _expect_non_empty_string(what_manager_did, f"{path}.what_manager_did", errors)
    evidence = _validate_short_text_list(feedback.get("evidence"), f"{path}.evidence", 7, errors)
    for field in ("next_action_text", "next_action_reason"):
        if feedback.get(field) is not None:
            _expect_non_empty_string(feedback.get(field), f"{path}.{field}", errors)
    next_action_at = feedback.get("next_action_at")
    if next_action_at is not None:
        if not isinstance(next_action_at, str) or not next_action_at.strip():
            errors.append(f"expected ISO datetime string or null at {path}.next_action_at")
        else:
            normalized = next_action_at[:-1] + "+00:00" if next_action_at.endswith("Z") else next_action_at
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    errors.append(f"expected timezone-aware ISO datetime at {path}.next_action_at")
            except ValueError:
                errors.append(f"expected ISO datetime string or null at {path}.next_action_at")

    if applicable is False:
        expected_neutral = {
            "source_report_id": None,
            "status": "unconfirmed",
            "what_manager_did": None,
            "contact_confirmed": False,
            "target_result_achieved": False,
            "evidence": [],
            "next_action_required": False,
            "next_action_text": None,
            "next_action_at": None,
            "next_action_reason": None,
        }
        for field, expected in expected_neutral.items():
            if feedback.get(field) != expected:
                errors.append(f"{path} must be neutral when applicable=false: {field}")
        return

    if source_report_id is None:
        errors.append(f"{path}.source_report_id is required when applicable=true")
    if status == "not_done" and (feedback.get("contact_confirmed") or feedback.get("target_result_achieved")):
        errors.append(f"{path}.not_done cannot confirm contact or result")
    if status == "attempted" and (feedback.get("contact_confirmed") or feedback.get("target_result_achieved")):
        errors.append(f"{path}.attempted cannot confirm contact or result")
    if status == "contacted" and (not feedback.get("contact_confirmed") or feedback.get("target_result_achieved")):
        errors.append(f"{path}.contacted requires contact_confirmed=true and target_result_achieved=false")
    if status == "achieved" and (
        not feedback.get("contact_confirmed") or not feedback.get("target_result_achieved")
    ):
        errors.append(f"{path}.achieved requires contact_confirmed=true and target_result_achieved=true")
    if feedback.get("target_result_achieved") and not feedback.get("contact_confirmed"):
        errors.append(f"{path}.target_result_achieved requires contact_confirmed=true")
    if feedback.get("next_action_required"):
        for field in ("next_action_text", "next_action_at", "next_action_reason"):
            if feedback.get(field) is None:
                errors.append(f"{path}.{field} is required when next_action_required=true")
    elif any(feedback.get(field) is not None for field in ("next_action_text", "next_action_at", "next_action_reason")):
        errors.append(f"{path} next_action fields must be null when next_action_required=false")


def _validate_daily_checklist_update(value: Any, errors: list[str]) -> None:
    path = "daily_checklist_update"
    update = _expect_dict(value, path, errors)
    if not update:
        return
    _require_fields(update, {"business_date", "base_revision", "add", "retire", "reopen"}, path, errors)
    business_date = update.get("business_date")
    if not isinstance(business_date, str):
        errors.append(f"expected ISO date string at {path}.business_date")
    else:
        try:
            datetime.strptime(business_date, "%Y-%m-%d")
        except ValueError:
            errors.append(f"expected ISO date string at {path}.business_date")
    base_revision = update.get("base_revision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        errors.append(f"expected non-negative integer at {path}.base_revision")

    for field in ("add", "retire", "reopen"):
        actions = _expect_max_list_length(update.get(field), f"{path}.{field}", 5, errors)
        for index, action in enumerate(actions):
            action_path = f"{path}.{field}[{index}]"
            item = _expect_dict(action, action_path, errors)
            if not item:
                continue
            required = {"text", "reason"} if field == "add" else {"item_id", "reason"}
            _require_fields(item, required, action_path, errors)
            if field == "add":
                _expect_non_empty_string(item.get("text"), f"{action_path}.text", errors)
            else:
                item_id = item.get("item_id")
                if isinstance(item_id, bool) or not isinstance(item_id, (str, int)) or not str(item_id).strip():
                    errors.append(f"expected string or integer item id at {action_path}.item_id")
            _expect_non_empty_string(item.get("reason"), f"{action_path}.reason", errors)


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
        closure_review = analysis.get("closure_review")
        no_client_contact = (
            isinstance(closure_review, dict)
            and closure_review.get("verdict") == "confirmed_correct"
            and closure_review.get("client_contact_required") is False
        )
        if not no_client_contact:
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


def _validate_deal_recommendation_materialization_fields(
    analysis: dict[str, Any],
    errors: list[str],
) -> None:
    rop = analysis.get("rop_manager_message_block")
    if not isinstance(rop, dict):
        errors.append("expected object at rop_manager_message_block for deal materialization")
        return
    for field in ("message_to_manager", "success_condition"):
        _expect_non_empty_string(rop.get(field), f"rop_manager_message_block.{field}", errors)
    deadline = rop.get("deadline")
    if not isinstance(deadline, str) or not deadline.strip():
        errors.append("rop_manager_message_block.deadline must be a non-empty YYYY-MM-DD string for deal materialization")
    else:
        try:
            date.fromisoformat(deadline.strip())
        except ValueError:
            errors.append("rop_manager_message_block.deadline must use YYYY-MM-DD format for deal materialization")

    manager_action = analysis.get("manager_action_block")
    if not isinstance(manager_action, dict):
        errors.append("expected object at manager_action_block for deal materialization")
        return
    recommended_channel = manager_action.get("recommended_channel")
    _expect_enum(
        recommended_channel,
        "manager_action_block.recommended_channel",
        DEAL_RECOMMENDED_CHANNELS,
        errors,
    )


def validate_deal_recommendation_materialization(analysis: dict[str, Any]) -> None:
    errors: list[str] = []
    _validate_deal_recommendation_materialization_fields(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid deal recommendation materialization: " + "; ".join(errors))


def _validate_client_communication_profile(value: Any, errors: list[str]) -> None:
    path = "client_communication_profile"
    profile = _expect_dict(value, path, errors)
    if not profile:
        return
    _require_fields(
        profile,
        {
            "status",
            "primary_style",
            "secondary_style",
            "role_separation_confidence",
            "profile_confidence",
            "evidence",
            "insufficient_reason",
            "recommended_communication",
        },
        path,
        errors,
    )
    status = profile.get("status")
    primary = profile.get("primary_style")
    secondary = profile.get("secondary_style")
    role_confidence = profile.get("role_separation_confidence")
    profile_confidence = profile.get("profile_confidence")
    _expect_enum(status, f"{path}.status", {"supported", "tentative", "insufficient_evidence"}, errors)
    _expect_enum(primary, f"{path}.primary_style", {"D", "I", "S", "C", None}, errors)
    _expect_enum(secondary, f"{path}.secondary_style", {"D", "I", "S", "C", None}, errors)
    _expect_enum(role_confidence, f"{path}.role_separation_confidence", {"low", "medium", "high"}, errors)
    _expect_enum(profile_confidence, f"{path}.profile_confidence", {"low", "medium", "high"}, errors)
    evidence = _validate_short_text_list(profile.get("evidence"), f"{path}.evidence", 5, errors)
    insufficient_reason = profile.get("insufficient_reason")
    if insufficient_reason is not None and (
        not isinstance(insufficient_reason, str) or not insufficient_reason.strip()
    ):
        errors.append(f"expected {path}.insufficient_reason to be non-empty string or null")
    if primary is not None and primary == secondary:
        errors.append(f"{path}.secondary_style must differ from primary_style")

    guidance = _expect_dict(profile.get("recommended_communication"), f"{path}.recommended_communication", errors)
    if guidance:
        _require_fields(guidance, {"tone", "structure", "emphasize", "avoid"}, f"{path}.recommended_communication", errors)
        tone = guidance.get("tone")
        structure = guidance.get("structure")
        for field, item in (("tone", tone), ("structure", structure)):
            if item is not None and (not isinstance(item, str) or not item.strip()):
                errors.append(
                    f"expected {path}.recommended_communication.{field} to be non-empty string or null"
                )
        emphasize = _validate_short_text_list(
            guidance.get("emphasize"), f"{path}.recommended_communication.emphasize", 4, errors
        )
        avoid = _validate_short_text_list(
            guidance.get("avoid"), f"{path}.recommended_communication.avoid", 4, errors
        )
    else:
        tone = structure = None
        emphasize = avoid = []

    if status == "insufficient_evidence":
        if primary is not None or secondary is not None:
            errors.append(f"{path} styles must be null when evidence is insufficient")
        if profile_confidence != "low":
            errors.append(f"{path}.profile_confidence must be 'low' when evidence is insufficient")
        if not isinstance(insufficient_reason, str) or not insufficient_reason.strip():
            errors.append(f"{path}.insufficient_reason is required when evidence is insufficient")
        if evidence:
            errors.append(f"{path}.evidence must be empty when evidence is insufficient")
        if tone is not None or structure is not None or emphasize or avoid:
            errors.append(f"{path}.recommended_communication must be empty when evidence is insufficient")
    elif status in {"tentative", "supported"}:
        if primary is None:
            errors.append(f"{path}.primary_style is required when status is {status!r}")
        if insufficient_reason is not None:
            errors.append(f"{path}.insufficient_reason must be null when status is {status!r}")
        minimum_evidence = 2 if status == "supported" else 1
        if len(evidence) < minimum_evidence:
            errors.append(f"{path}.evidence must contain at least {minimum_evidence} item(s) when status is {status!r}")
        if not isinstance(tone, str) or not tone.strip() or not isinstance(structure, str) or not structure.strip():
            errors.append(f"{path}.recommended_communication tone and structure are required when status is {status!r}")
        if status == "supported":
            if role_confidence == "low":
                errors.append(f"{path}.role_separation_confidence cannot be 'low' when status is 'supported'")
            if profile_confidence not in {"medium", "high"}:
                errors.append(f"{path}.profile_confidence must be medium or high when status is 'supported'")


def validate_client_communication_profile(value: Any) -> None:
    errors: list[str] = []
    _validate_client_communication_profile(value, errors)
    if errors:
        raise AnalysisValidationError("Invalid client communication profile: " + "; ".join(errors))


def _validate_deal_context(value: Any, errors: list[str]) -> None:
    path = "deal_context"
    context = _expect_dict(value, path, errors)
    if not context:
        return
    _require_fields(
        context,
        {
            "deal_card", "current_truth", "decision_path", "commitments", "critical_facts",
            "turning_points", "journey", "pain_points", "pressure_levers", "open_questions",
            "source_conflicts",
        },
        path,
        errors,
    )
    card = _expect_dict(context.get("deal_card"), f"{path}.deal_card", errors)
    if card:
        _require_fields(
            card,
            {"company", "equipment", "manufacturing_days", "amount", "responsible"},
            f"{path}.deal_card",
            errors,
        )
        for field in ("company", "equipment", "responsible"):
            _expect_non_empty_string(card.get(field), f"{path}.deal_card.{field}", errors)
        for field in ("manufacturing_days", "amount"):
            value = card.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                errors.append(f"expected {path}.deal_card.{field} to be a string, number or null")
            elif isinstance(value, str) and not value.strip():
                errors.append(f"expected {path}.deal_card.{field} to be a string, number or null")

    truth = _expect_dict(context.get("current_truth"), f"{path}.current_truth", errors)
    if truth:
        required_truth = {
            "client_profile", "current_need", "desired_outcome", "current_status",
            "current_task", "next_checkpoint", "next_step_owner",
        }
        _require_fields(truth, required_truth, f"{path}.current_truth", errors)
        for field in ("client_profile", "current_need", "desired_outcome", "current_status", "current_task"):
            _expect_non_empty_string(truth.get(field), f"{path}.current_truth.{field}", errors)
        _validate_optional_question(truth.get("next_checkpoint"), f"{path}.current_truth.next_checkpoint", errors)
        _expect_enum(
            truth.get("next_step_owner"), f"{path}.current_truth.next_step_owner",
            {"manager", "client", "rop", "finance", "leasing", "unknown"}, errors,
        )

    decision = _expect_dict(context.get("decision_path"), f"{path}.decision_path", errors)
    if decision:
        _require_fields(
            decision,
            {"decision_maker", "influencers", "approval_path", "current_step_owner", "basis_status", "evidence"},
            f"{path}.decision_path",
            errors,
        )
        for field in ("decision_maker", "approval_path"):
            _expect_non_empty_string(decision.get(field), f"{path}.decision_path.{field}", errors)
        _expect_enum(
            decision.get("current_step_owner"), f"{path}.decision_path.current_step_owner",
            {"manager", "client", "rop", "finance", "leasing", "unknown"}, errors,
        )
        _expect_enum(
            decision.get("basis_status"), f"{path}.decision_path.basis_status",
            {"confirmed", "needs_confirmation", "inferred"}, errors,
        )
        _validate_short_text_list(decision.get("influencers"), f"{path}.decision_path.influencers", 4, errors)
        evidence = _validate_short_text_list(decision.get("evidence"), f"{path}.decision_path.evidence", 5, errors)
        if not evidence:
            errors.append(f"{path}.decision_path.evidence must not be empty")

    def validate_evidence(item: dict[str, Any], item_path: str) -> None:
        evidence = _validate_short_text_list(item.get("evidence"), f"{item_path}.evidence", 5, errors)
        if not evidence:
            errors.append(f"{item_path}.evidence must not be empty")

    facts = _expect_max_list_length(context.get("critical_facts"), f"{path}.critical_facts", 10, errors)
    for index, raw in enumerate(facts):
        item_path = f"{path}.critical_facts[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"fact_id", "category", "fact", "status", "importance", "observed_at", "source_type", "evidence"}, item_path, errors)
        for field in ("fact_id", "fact"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _expect_enum(item.get("category"), f"{item_path}.category", {"deadline", "budget", "need", "authority", "technical", "delivery", "payment", "competitor", "commitment", "other"}, errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"confirmed", "needs_confirmation", "conflicted", "outdated"}, errors)
        _expect_enum(item.get("importance"), f"{item_path}.importance", {"high", "medium", "low"}, errors)
        _expect_enum(item.get("source_type"), f"{item_path}.source_type", {"client_communication", "crm_fact", "manager_comment", "internal_context", "model_inference"}, errors)
        _validate_optional_question(item.get("observed_at"), f"{item_path}.observed_at", errors)
        validate_evidence(item, item_path)

    turning_points = _expect_max_list_length(context.get("turning_points"), f"{path}.turning_points", 8, errors)
    for index, raw in enumerate(turning_points):
        item_path = f"{path}.turning_points[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"turning_point_id", "occurred_at", "title", "what_happened", "impact", "status", "evidence"}, item_path, errors)
        for field in ("turning_point_id", "title", "what_happened", "impact"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _validate_optional_question(item.get("occurred_at"), f"{item_path}.occurred_at", errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"active", "resolved", "superseded"}, errors)
        validate_evidence(item, item_path)

    commitments = _expect_max_list_length(context.get("commitments"), f"{path}.commitments", 6, errors)
    for index, raw in enumerate(commitments):
        item_path = f"{path}.commitments[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(
            item,
            {"commitment_id", "party", "promise", "due_at", "status", "basis_status", "evidence"},
            item_path,
            errors,
        )
        for field in ("commitment_id", "promise"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _expect_enum(item.get("party"), f"{item_path}.party", {"client", "company", "manager"}, errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"open", "done", "broken", "unknown"}, errors)
        _expect_enum(item.get("basis_status"), f"{item_path}.basis_status", {"confirmed", "needs_confirmation", "inferred"}, errors)
        _validate_optional_question(item.get("due_at"), f"{item_path}.due_at", errors)
        validate_evidence(item, item_path)

    journey = _expect_max_list_length(context.get("journey"), f"{path}.journey", 12, errors)
    for index, raw in enumerate(journey):
        item_path = f"{path}.journey[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"entry_id", "occurred_at", "title", "what_happened", "learned", "missing", "status"}, item_path, errors)
        for field in ("entry_id", "title", "what_happened"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _validate_optional_question(item.get("occurred_at"), f"{item_path}.occurred_at", errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"past", "current"}, errors)
        _validate_short_text_list(item.get("learned"), f"{item_path}.learned", 4, errors)
        _validate_short_text_list(item.get("missing"), f"{item_path}.missing", 4, errors)

    pain_points = _expect_max_list_length(context.get("pain_points"), f"{path}.pain_points", 6, errors)
    for index, raw in enumerate(pain_points):
        item_path = f"{path}.pain_points[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"pain_id", "title", "description", "status", "impact", "evidence"}, item_path, errors)
        for field in ("pain_id", "title", "description", "impact"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"active", "partially_resolved", "resolved", "unknown"}, errors)
        validate_evidence(item, item_path)

    levers = _expect_max_list_length(context.get("pressure_levers"), f"{path}.pressure_levers", 6, errors)
    used_priorities: set[int] = set()
    for index, raw in enumerate(levers):
        item_path = f"{path}.pressure_levers[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"lever_id", "type", "title", "fact", "why_important", "business_consequence", "basis_status", "status", "ai_priority", "evidence"}, item_path, errors)
        for field in ("lever_id", "title", "fact", "why_important", "business_consequence"):
            _expect_non_empty_string(item.get(field), f"{item_path}.{field}", errors)
        _expect_enum(item.get("type"), f"{item_path}.type", {"deadline", "budget", "operational_impact", "technical", "authority", "competitor", "trust", "other"}, errors)
        _expect_enum(item.get("basis_status"), f"{item_path}.basis_status", {"confirmed", "inferred", "needs_confirmation"}, errors)
        _expect_enum(item.get("status"), f"{item_path}.status", {"active", "weakened", "resolved", "unknown"}, errors)
        priority = item.get("ai_priority")
        if priority is not None:
            if isinstance(priority, bool) or priority not in {1, 2, 3}:
                errors.append(f"{item_path}.ai_priority must be 1, 2, 3 or null")
            elif priority in used_priorities:
                errors.append(f"{path}.pressure_levers contains duplicate ai_priority {priority}")
            else:
                used_priorities.add(priority)
        validate_evidence(item, item_path)
    if len(levers) < 2:
        errors.append(f"{path}.pressure_levers must contain at least 2 items")

    _validate_short_text_list(context.get("open_questions"), f"{path}.open_questions", 7, errors)
    conflicts = _expect_max_list_length(context.get("source_conflicts"), f"{path}.source_conflicts", 5, errors)
    for index, raw in enumerate(conflicts):
        item_path = f"{path}.source_conflicts[{index}]"
        item = _expect_dict(raw, item_path, errors)
        if not item:
            continue
        _require_fields(item, {"description", "sources", "next_check"}, item_path, errors)
        _expect_non_empty_string(item.get("description"), f"{item_path}.description", errors)
        _expect_non_empty_string(item.get("next_check"), f"{item_path}.next_check", errors)
        sources = _validate_short_text_list(item.get("sources"), f"{item_path}.sources", 4, errors)
        if not sources:
            errors.append(f"{item_path}.sources must not be empty")


def _validate_deal_management_shapes(analysis: dict[str, Any], errors: list[str]) -> None:
    _validate_deal_recommendation_materialization_fields(analysis, errors)
    _validate_client_communication_profile(analysis.get("client_communication_profile"), errors)
    audit = analysis.get("communication_quality_audit")
    if COMMUNICATION_QUALITY_AUDIT_ENABLED or audit is not None:
        path = "communication_quality_audit"
        audit = _expect_dict(audit, path, errors)
        if audit:
            status = audit.get("status")
            _expect_enum(
                status,
                f"{path}.status",
                {"assessed", "insufficient_evidence"},
                errors,
            )
            _expect_non_empty_string(audit.get("scope_summary"), f"{path}.scope_summary", errors)
            criteria = _expect_dict(audit.get("criteria"), f"{path}.criteria", errors)
            scores: dict[str, Any] = {}
            for name in ("next_action", "value_development", "data_collection"):
                item = _expect_dict(criteria.get(name), f"{path}.criteria.{name}", errors) if criteria else {}
                score = item.get("score") if item else None
                scores[name] = score
                if status == "assessed" and (not isinstance(score, int) or isinstance(score, bool) or score not in {0, 1}):
                    errors.append(f"{path}.criteria.{name}.score must be 0 or 1 when status is 'assessed'")
                if status == "insufficient_evidence" and score is not None:
                    errors.append(f"{path}.criteria.{name}.score must be null when evidence is insufficient")
            reasons = _expect_list(audit.get("zero_reasons"), f"{path}.zero_reasons", errors)
            reason_criteria: set[str] = set()
            for index, reason in enumerate(reasons):
                reason_path = f"{path}.zero_reasons[{index}]"
                reason = _expect_dict(reason, reason_path, errors)
                if not reason:
                    continue
                criterion = reason.get("criterion")
                _expect_enum(
                    criterion,
                    f"{reason_path}.criterion",
                    {"next_action", "value_development", "data_collection"},
                    errors,
                )
                _expect_non_empty_string(reason.get("explanation"), f"{reason_path}.explanation", errors)
                _expect_non_empty_string(reason.get("quote"), f"{reason_path}.quote", errors)
                if criterion:
                    if criterion in reason_criteria:
                        errors.append(f"{path}.zero_reasons contains duplicate criterion {criterion!r}")
                    reason_criteria.add(criterion)
            expected_reasons = {name for name, score in scores.items() if score == 0}
            if status == "assessed":
                if reason_criteria != expected_reasons:
                    errors.append(f"{path}.zero_reasons must cover exactly the criteria scored 0")
                _expect_non_empty_string(audit.get("summary_for_rop"), f"{path}.summary_for_rop", errors)
                if audit.get("insufficient_reason") is not None:
                    errors.append(f"{path}.insufficient_reason must be null when status is 'assessed'")
            elif status == "insufficient_evidence":
                if reasons:
                    errors.append(f"{path}.zero_reasons must be empty when evidence is insufficient")
                if audit.get("summary_for_rop") is not None:
                    errors.append(f"{path}.summary_for_rop must be null when evidence is insufficient")
                _expect_non_empty_string(audit.get("insufficient_reason"), f"{path}.insufficient_reason", errors)
    control_brief = _expect_dict(analysis.get("deal_control_brief"), "deal_control_brief", errors)
    if control_brief:
        for field in (
            "current_situation",
            "rop_focus",
            "what_to_check_now",
            "manager_coaching",
            "contact_goal",
            "call_script",
        ):
            _expect_non_empty_string(control_brief.get(field), f"deal_control_brief.{field}", errors)
        for field in ("strengths", "weaknesses", "known_facts", "missing_facts", "contact_questions"):
            _validate_short_text_list(
                control_brief.get(field),
                f"deal_control_brief.{field}",
                DEAL_CONTROL_BRIEF_LIST_LIMITS[field],
                errors,
            )
        opening_variants = control_brief.get("call_opening_variants")
        _validate_short_text_list(
            opening_variants,
            "deal_control_brief.call_opening_variants",
            DEAL_CONTROL_BRIEF_LIST_LIMITS["call_opening_variants"],
            errors,
        )
        manager_action = analysis.get("manager_action_block")
        recommended_channel = (
            manager_action.get("recommended_channel")
            if isinstance(manager_action, dict)
            else None
        )
        if recommended_channel == "phone" and (
            not isinstance(opening_variants, list) or len(opening_variants) != 2
        ):
            errors.append(
                "deal_control_brief.call_opening_variants must contain exactly 2 items for phone contact"
            )
        direct_question = control_brief.get("direct_manager_question")
        if direct_question is not None:
            _expect_non_empty_string(
                direct_question,
                "deal_control_brief.direct_manager_question",
                errors,
            )

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
    _validate_deal_context(analysis.get("deal_context"), errors)
    if "recommendation_feedback" in analysis:
        _validate_recommendation_feedback(analysis.get("recommendation_feedback"), errors)
    if "daily_checklist_update" in analysis:
        _validate_daily_checklist_update(analysis.get("daily_checklist_update"), errors)
    _validate_deal_management_shapes(analysis, errors)
    _validate_common_shapes(analysis, errors)
    if errors:
        raise AnalysisValidationError("Invalid deal analysis: " + "; ".join(errors))


def _requires_missing_crm_return_task(analysis: dict[str, Any]) -> bool:
    assessment = analysis.get("qualification_assessment")
    if not isinstance(assessment, dict):
        return False
    category = assessment.get("lead_category")
    route = assessment.get("lead_route")
    return (
        isinstance(category, dict)
        and category.get("value") == "C"
        and isinstance(route, dict)
        and route.get("controlled_return_required") is True
        and route.get("controlled_return_status") == "missing_in_crm"
        and isinstance(route.get("recommended_return_date"), str)
        and bool(route.get("recommended_return_date").strip())
    )


def validate_lead_analysis(analysis: dict[str, Any]) -> None:
    errors: list[str] = []
    _require_fields(analysis, LEAD_REQUIRED_FIELDS, "", errors)
    _expect_dict(analysis.get("lead_state"), "lead_state", errors)
    _expect_dict(analysis.get("activity_summary"), "activity_summary", errors)
    closure_review = _expect_dict(analysis.get("closure_review"), "closure_review", errors)
    closure_verdict = None
    no_manager_action = False
    no_client_contact = False
    missing_crm_return_task = _requires_missing_crm_return_task(analysis)
    if closure_review:
        _require_fields(
            closure_review,
            {
                "applicable",
                "crm_status_id",
                "crm_status_name",
                "crm_status_semantic_id",
                "verdict",
                "reason",
                "client_contact_required",
                "manager_task_required",
                "evidence",
            },
            "closure_review",
            errors,
        )
        _expect_bool(closure_review.get("applicable"), "closure_review.applicable", errors)
        _expect_bool(
            closure_review.get("client_contact_required"),
            "closure_review.client_contact_required",
            errors,
        )
        _expect_bool(
            closure_review.get("manager_task_required"),
            "closure_review.manager_task_required",
            errors,
        )
        closure_verdict = closure_review.get("verdict")
        _expect_enum(
            closure_verdict,
            "closure_review.verdict",
            {"confirmed_correct", "disputed", "insufficient_evidence", "not_applicable"},
            errors,
        )
        _expect_enum(
            closure_review.get("crm_status_semantic_id"),
            "closure_review.crm_status_semantic_id",
            {"F", "S", "P", "unknown"},
            errors,
        )
        _expect_non_empty_text_without_markers(
            closure_review.get("reason"),
            "closure_review.reason",
            errors,
        )
        closure_evidence = _expect_max_list_length(
            closure_review.get("evidence"),
            "closure_review.evidence",
            7,
            errors,
        )
        is_closed_lost = closure_review.get("crm_status_semantic_id") == "F"
        if is_closed_lost != bool(closure_review.get("applicable")):
            errors.append("closure_review.applicable must match crm_status_semantic_id=F")
        if is_closed_lost and closure_verdict == "not_applicable":
            errors.append("closed-lost lead closure_review.verdict must not be not_applicable")
        if not is_closed_lost and closure_verdict != "not_applicable":
            errors.append("non-closed lead closure_review.verdict must be not_applicable")
        if is_closed_lost and not closure_evidence:
            errors.append("closed-lost closure_review.evidence must not be empty")
        no_client_contact = closure_verdict == "confirmed_correct"
        no_manager_action = no_client_contact and not missing_crm_return_task
        if no_client_contact:
            if closure_review.get("client_contact_required") is not False:
                errors.append("confirmed_correct closure must not require client contact")
            if closure_review.get("manager_task_required") is not False:
                errors.append("confirmed_correct closure must not require a closure-review manager task")
        elif closure_verdict in {"disputed", "insufficient_evidence"}:
            if closure_review.get("client_contact_required") is not True:
                errors.append(f"{closure_verdict} closure must require client contact")
            if closure_review.get("manager_task_required") is not True:
                errors.append(f"{closure_verdict} closure must require manager task")
        elif closure_verdict == "not_applicable":
            if closure_review.get("client_contact_required") is not True:
                errors.append("non-closed lead must preserve client contact workflow")
            if closure_review.get("manager_task_required") is not True:
                errors.append("non-closed lead must preserve manager task workflow")
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
    rop_manager = analysis.get("rop_manager_message_block")
    if isinstance(rop_manager, dict):
        review_text = rop_manager.get("manager_review_text")
        _expect_non_empty_text_without_markers(
            review_text,
            "rop_manager_message_block.manager_review_text",
            errors,
        )
        if isinstance(review_text, str) and len(review_text.strip()) > 500:
            errors.append("rop_manager_message_block.manager_review_text must be at most 500 characters")
        deadline = rop_manager.get("deadline")
        if no_manager_action:
            if deadline is not None:
                errors.append("confirmed_correct closure must use null rop_manager_message_block.deadline")
        elif not isinstance(deadline, str) or not deadline.strip():
            errors.append("rop_manager_message_block.deadline must be a non-empty YYYY-MM-DD string for lead analysis")
        else:
            try:
                date.fromisoformat(deadline.strip())
            except ValueError:
                errors.append("rop_manager_message_block.deadline must use YYYY-MM-DD format for lead analysis")
    manager_action = analysis.get("manager_action_block")
    if isinstance(manager_action, dict):
        primary = manager_action.get("primary_text")
        backups = _expect_list(
            manager_action.get("backup_texts"),
            "manager_action_block.backup_texts",
            errors,
        )
        if no_client_contact and primary is not None:
            errors.append("confirmed_correct closure manager_action_block.primary_text must be null")
        if no_client_contact and backups:
            errors.append("confirmed_correct closure manager_action_block.backup_texts must be empty")
        if no_client_contact and manager_action.get("manager_checklist") != []:
            errors.append("confirmed_correct closure manager_action_block.manager_checklist must be empty")
        if not no_client_contact and len(backups) != 2:
            errors.append("manager_action_block.backup_texts must contain exactly 2 items for lead analysis")
        option_values: list[tuple[str, Any]] = []
        client_options: list[dict[str, Any]] = []
        if isinstance(primary, dict):
            client_options.append(primary)
            option_values.append(("manager_action_block.primary_text.text", primary.get("text")))
        for index, option in enumerate(backups):
            if isinstance(option, dict):
                client_options.append(option)
                option_values.append((f"manager_action_block.backup_texts[{index}].text", option.get("text")))
        expected_titles = [
            "Деловой и прямой",
            "Партнёрский и доброжелательный",
            "Спокойный и консультативный",
        ]
        for index, (option, expected_title) in enumerate(zip(client_options, expected_titles)):
            if str(option.get("title") or "").strip() != expected_title:
                errors.append(
                    f"manager_action_block client option {index + 1} must use title {expected_title!r}"
                )
        normalized_options: list[str] = []
        for path, option in option_values:
            _expect_non_empty_text_without_markers(option, path, errors)
            if isinstance(option, str):
                normalized = option.strip()
                normalized_options.append(normalized)
                if len(normalized) > 1200:
                    errors.append(f"{path} must be at most 1200 characters")
        if not no_client_contact and len(option_values) != 3:
            errors.append("manager_action_block must contain exactly 3 client message texts for lead analysis")
        if len(set(normalized_options)) != len(normalized_options):
            errors.append("manager_action_block client message texts must be distinct")
    rop_action = analysis.get("rop_action")
    if no_manager_action and isinstance(rop_action, dict) and rop_action.get("required") is not False:
        errors.append("confirmed_correct closure rop_action.required must be false")
    if missing_crm_return_task and isinstance(rop_action, dict) and rop_action.get("required") is not True:
        errors.append("missing CRM return task must require rop_action")
    if errors:
        raise AnalysisValidationError("Invalid lead analysis: " + "; ".join(errors))
