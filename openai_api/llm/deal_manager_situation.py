"""Structured situation projection for the manager deal screen.

The manager screen deliberately uses a smaller, allow-listed projection of the
full deal analysis.  The module does not read CRM or SQLite; callers provide
the already projected deal, task, and situation context.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai_api.config import ANALYSIS_MODEL, ANALYSIS_REASONING_EFFORT
from openai_api.llm.llm_client import call_structured_output_json, prompt_prefix_before


MANAGER_MODEL = os.getenv("DEAL_MANAGER_MODEL", ANALYSIS_MODEL).strip() or ANALYSIS_MODEL
MANAGER_REASONING_EFFORT = (
    os.getenv("DEAL_MANAGER_REASONING_EFFORT", ANALYSIS_REASONING_EFFORT).strip()
    or ANALYSIS_REASONING_EFFORT
)
MAX_MANAGER_CONTEXT_CHARS = 4000
MAX_SITUATION_OUTPUT_TOKENS = 2400

_ANALYSIS_FIELDS = (
    "deal_state",
    "deal_control_brief",
    "client_communication_profile",
    "qualification_assessment",
    "deal_context",
    "deal_mode",
    "main_risk",
    "payment_blocker",
    "money_path_diagnosis",
    "competitor_defense_checklist",
    "manager_action_block",
    "rop_manager_message_block",
    "price_comparability_check",
    "objection_handling",
    "priority_recommendation",
    "new_event",
)
_DEAL_FIELDS = (
    "deal_id",
    "title",
    "stage_id",
    "stage_name",
    "pipeline_id",
    "amount",
    "currency_id",
    "manager_id",
    "manager_name",
    "probability",
    "expected_payment_period",
    "next_control_at",
    "modified_at_crm",
)
_TASK_FIELDS = (
    "activity_id",
    "subject",
    "description",
    "deadline",
    "completed",
    "completion_state",
    "local_completed",
)
_PROJECTION_FIELDS = (
    "current_situation",
    "what_to_check_now",
    "rop_focus",
    "manager_coaching",
    "known",
    "unknowns",
    "contact_goal",
    "questions",
    "script",
    "script_variants",
    "crm_checklist",
    "script_channel",
)


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON-like values while bounding prompt size and nesting."""
    if depth >= 4:
        if isinstance(value, (dict, list)):
            return "[содержимое сокращено]"
    if isinstance(value, str):
        text = value.strip()
        return text[:1200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _compact_value(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, list):
        return [_compact_value(item, depth=depth + 1) for item in value[:12]]
    return str(value)[:1200]


def unwrap_analysis(value: Any) -> dict[str, Any]:
    """Return the inner analysis object from either supported report shape."""
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        nested = current.get("analysis")
        if isinstance(nested, dict):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else {}


def compact_analysis_projection(report_json: Any) -> dict[str, Any]:
    """Keep only analysis blocks useful to the manager screen."""
    analysis = unwrap_analysis(report_json)
    return {
        field: _compact_value(analysis[field])
        for field in _ANALYSIS_FIELDS
        if field in analysis
    }


def project_deal(deal: dict[str, Any]) -> dict[str, Any]:
    return {
        field: _compact_value(deal[field])
        for field in _DEAL_FIELDS
        if field in deal
    }


def project_bitrix_task(task: Any) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    return {
        field: _compact_value(task[field])
        for field in _TASK_FIELDS
        if field in task
    }


def project_manager_projection(value: Any) -> dict[str, Any]:
    """Normalize a saved manager projection without carrying unknown fields."""
    if isinstance(value, dict):
        for nested_key in ("manager_projection", "situation_projection", "projection", "refined_coaching"):
            if isinstance(value.get(nested_key), dict):
                value = value[nested_key]
                break
    if not isinstance(value, dict):
        return {}
    return {
        field: _compact_value(value[field])
        for field in _PROJECTION_FIELDS
        if field in value
    }


def _text(value: Any, *keys: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:1200]
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:1200]
            if isinstance(item, dict):
                nested = _text(item, "text", "summary", "description", "value")
                if nested:
                    return nested
    return ""


def _text_list(value: Any, *, limit: int = 4) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        candidate = _text(item, "text", "summary", "description", "value")
        if candidate and candidate not in result:
            result.append(candidate)
        if len(result) >= limit:
            break
    return result


def build_confirmed_manager_projection(report_json: Any) -> dict[str, Any]:
    """Build a deterministic projection for the no-LLM confirm action."""
    analysis = unwrap_analysis(report_json)
    brief = analysis.get("deal_control_brief") if isinstance(analysis.get("deal_control_brief"), dict) else {}
    deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
    risk = analysis.get("main_risk") if isinstance(analysis.get("main_risk"), dict) else {}
    payment = analysis.get("payment_blocker") if isinstance(analysis.get("payment_blocker"), dict) else {}
    money = analysis.get("money_path_diagnosis") if isinstance(analysis.get("money_path_diagnosis"), dict) else {}
    manager = analysis.get("manager_action_block") if isinstance(analysis.get("manager_action_block"), dict) else {}
    rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
    price = analysis.get("price_comparability_check") if isinstance(analysis.get("price_comparability_check"), dict) else {}

    current_situation = (
        _text(brief, "current_situation", "summary")
        or _text(deal_state, "summary", "description")
        or _text(analysis, "current_situation", "summary")
        or "В анализе нет отдельного описания текущей ситуации."
    )
    rop_focus = (
        _text(brief, "rop_focus")
        or _text(analysis.get("deal_mode"), "rop_focus")
        or _text(rop, "check_for_rop")
    )
    what_to_check_now = (
        _text(brief, "what_to_check_now", "next_step")
        or _text(manager, "next_step", "expected_result")
        or _text(money, "next_required_fact", "next_step")
        or _text(rop, "expected_crm_update")
    )
    manager_coaching = (
        _text(brief, "manager_coaching")
        or _text(rop, "message_to_manager")
        or _text(manager, "goal", "primary_action")
    )
    known = _text_list(brief.get("known_facts"), limit=4)
    if not known:
        known = _text_list([*(rop.get("evidence") or []), *(money.get("evidence") or [])], limit=4)
    unknowns = _text_list(brief.get("missing_facts"), limit=4)
    if not unknowns:
        unknowns = _text_list(
            [*(price.get("what_is_unclear") or []), *(payment.get("missing_confirmation") or [])],
            limit=4,
        )
    contact_goal = _text(brief, "contact_goal") or _text(manager, "goal")
    questions = _text_list(brief.get("contact_questions"), limit=4)
    if not questions:
        questions = _text_list(
            [
                *(price.get("what_is_unclear") or []),
                *(payment.get("missing_confirmation") or []),
                _text(analysis.get("shaker_question"), "question"),
            ],
            limit=4,
        )
    primary = manager.get("primary_text") if isinstance(manager.get("primary_text"), dict) else {}
    backups = manager.get("backup_texts") if isinstance(manager.get("backup_texts"), list) else []
    script = _text(brief, "call_script") or _text(primary, "text", "call_script")
    if not script:
        script = next((_text(item, "text") for item in backups if isinstance(item, dict)), "")
    script_variants = _text_list(brief.get("call_opening_variants"), limit=2)
    if not script_variants:
        script_variants = _text_list([item.get("text") for item in backups if isinstance(item, dict)], limit=2)
    crm_checklist = _text_list(manager.get("manager_checklist"), limit=4)
    script_channel = _text(manager, "recommended_channel")
    return {
        "current_situation": current_situation,
        "what_to_check_now": what_to_check_now,
        "rop_focus": rop_focus,
        "manager_coaching": manager_coaching,
        "known": known,
        "unknowns": unknowns,
        "contact_goal": contact_goal,
        "questions": questions,
        "script": script,
        "script_variants": script_variants,
        "crm_checklist": crm_checklist,
        "script_channel": script_channel,
    }


def situation_schema() -> dict[str, Any]:
    projection = {
        "type": "object",
        "additionalProperties": False,
        "required": list(_PROJECTION_FIELDS),
        "properties": {
            "current_situation": {"type": "string", "minLength": 1, "maxLength": 1600},
            "what_to_check_now": {"type": "string", "minLength": 1, "maxLength": 1600},
            "rop_focus": {"type": "string", "minLength": 1, "maxLength": 1600},
            "manager_coaching": {"type": "string", "minLength": 1, "maxLength": 1600},
            "known": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 800},
                "maxItems": 4,
            },
            "unknowns": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 800},
                "maxItems": 4,
            },
            "contact_goal": {"type": "string", "minLength": 1, "maxLength": 1600},
            "questions": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 800},
                "maxItems": 4,
            },
            "script": {"type": "string", "minLength": 1, "maxLength": 2000},
            "script_variants": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 800},
                "maxItems": 2,
            },
            "crm_checklist": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 800},
                "maxItems": 4,
            },
            "script_channel": {"type": "string", "minLength": 1, "maxLength": 160},
        },
    }
    return projection


def validate_situation_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Ситуация менеджера должна быть JSON-объектом")
    missing = [field for field in _PROJECTION_FIELDS if not isinstance(value.get(field), (str, list))]
    if missing:
        raise ValueError("В ситуации отсутствуют обязательные поля")
    for field in ("current_situation", "what_to_check_now", "rop_focus", "manager_coaching", "contact_goal", "script", "script_channel"):
        if not isinstance(value.get(field), str) or not str(value.get(field) or "").strip():
            raise ValueError("Пустое обязательное поле ситуации")
    list_limits = {
        "known": 4,
        "unknowns": 4,
        "questions": 4,
        "script_variants": 2,
        "crm_checklist": 4,
    }
    normalized: dict[str, Any] = {}
    for field in ("current_situation", "what_to_check_now", "rop_focus", "manager_coaching", "contact_goal", "script", "script_channel"):
        normalized[field] = str(value[field]).strip()[:2000]
    for field, limit in list_limits.items():
        items = value.get(field)
        if not isinstance(items, list) or len(items) > limit or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} должен содержать не более {limit} строк")
        normalized[field] = [str(item).strip()[:800] for item in items[:limit]]
    return normalized


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_situation_prompt(
    *,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    previous_manager_projection: dict[str, Any],
    manager_context: str,
) -> str:
    context = str(manager_context or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы уточняешь рабочую ситуацию менеджера по одной сделке.",
            "RULES:\n"
            "- Опирайся только на переданные CONFIRMED_ANALYSIS_CONTEXT, DEAL_CONTEXT, CURRENT_BITRIX_TASK, "
            "PREVIOUS_MANAGER_PROJECTION и NEW_MANAGER_CONTEXT.\n"
            "- Не выдумывай факты, слова клиента, договорённости, даты, суммы или результат звонка.\n"
            "- Bitrix-задача показывает рабочее поручение и не доказывает контакт с клиентом.\n"
            "- Если контекст менеджера расходится с анализом, явно отрази неопределённость и вынеси вопрос в facts_to_clarify.\n"
            "- Не объявляй звонок контактом и не утверждай, что сделка продвинулась без подтверждённого клиентского факта.\n"
            "- client_communication_profile используй только когда status tentative или supported: адаптируй тон и структуру, но не меняй факты, цель контакта или следующий шаг. При insufficient_evidence не угадывай DISC.\n"
            "- Не повторяй уже известные факты как вопросы. Основной текст и сценарии пиши без плейсхолдеров.\n"
            "- Верни только полный объект по JSON-схеме. Пиши спокойно, прямо и по-русски.",
            _section("CONFIRMED_ANALYSIS_CONTEXT", analysis_projection),
            _section("DEAL_CONTEXT", project_deal(deal)),
            _section("CURRENT_BITRIX_TASK", current_bitrix_task),
            _section("PREVIOUS_MANAGER_PROJECTION", previous_manager_projection),
            _section("NEW_MANAGER_CONTEXT", context),
        ]
    )


def generate_deal_manager_situation(
    *,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    previous_manager_projection: dict[str, Any],
    manager_context: str,
    model: str = MANAGER_MODEL,
    reasoning_effort: str = MANAGER_REASONING_EFFORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_situation_prompt(
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        previous_manager_projection=previous_manager_projection,
        manager_context=manager_context,
    )
    result, metadata = call_structured_output_json(
        prompt,
        schema=situation_schema(),
        schema_name="deal_manager_situation",
        model=model,
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_SITUATION_OUTPUT_TOKENS,
        log_title="deal manager situation prompt",
        call_type="deal_manager_situation",
        prompt_cache_key="neuro-rop:deal-manager-situation:v2",
        stable_prefix=prompt_prefix_before(prompt, "PREVIOUS_MANAGER_PROJECTION:"),
    )
    return validate_situation_projection(result), metadata
