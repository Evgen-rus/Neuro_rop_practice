"""Strict compact attention-delta contracts and shadow prompt builders.

This module is deliberately isolated from the legacy analyzers.  It is used
only by the benchmark/shadow path and must never replace a legacy analysis or
report at runtime.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from openai_api.llm.prompt_budget import render_okf_sections


SEVERITIES = ("low", "medium", "medium_high", "high")
DEAL_REVIEW_TYPES = (
    "none",
    "closed_wrong_qualification",
    "closed_price_lost",
    "closed_no_response",
    "competitor",
    "payment_control",
    "technical_blocker",
    "other",
)
DEAL_REVIEW_DECISIONS = (
    "none",
    "keep_current_state",
    "needs_manual_review",
    "return_to_pipeline",
    "manager_action_required",
)
LEAD_QUALIFICATIONS = ("A", "B", "C", "D", "E", "unknown")
LEAD_FINAL_VERDICTS = (
    "bad_lead",
    "bad_processing",
    "data_gap",
    "needs_nurture",
    "ready_for_deal",
    "unknown",
)
MAX_EVIDENCE_IDS = 7


def _string_array_schema(*, max_items: int = 5) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": max_items}


def _rop_action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": (
            "check",
            "message_to_manager",
            "expected_crm_fact",
            "deadline",
            "success_condition",
            "evidence_ids",
        ),
        "properties": {
            "check": {"type": "string", "minLength": 1},
            "message_to_manager": {"type": "string", "minLength": 1},
            "expected_crm_fact": {"type": "string", "minLength": 1},
            "deadline": {
                "anyOf": [
                    {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    {"type": "null"},
                ]
            },
            "success_condition": {"type": "string", "minLength": 1},
            "evidence_ids": _string_array_schema(max_items=MAX_EVIDENCE_IDS),
        },
    }


def _memory_patch_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": (
            "confirmed_facts_add",
            "open_questions_add",
            "open_questions_resolve",
            "risks_add",
            "risks_resolve",
            "next_step",
        ),
        "properties": {
            "confirmed_facts_add": _string_array_schema(),
            "open_questions_add": _string_array_schema(),
            "open_questions_resolve": _string_array_schema(),
            "risks_add": _string_array_schema(),
            "risks_resolve": _string_array_schema(),
            "next_step": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        },
    }


def _base_schema(entity_type: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": (
            "entity_type",
            "entity_id",
            "attention_required",
            "severity",
            "reason",
            "rop_action",
            "memory_patch",
        ),
        "properties": {
            "entity_type": {"type": "string", "enum": [entity_type]},
            "entity_id": {"type": "string", "minLength": 1},
            "attention_required": {"type": "boolean"},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "reason": {"type": "string", "minLength": 1},
            "rop_action": {"anyOf": [_rop_action_schema(), {"type": "null"}]},
            "memory_patch": _memory_patch_schema(),
        },
    }


def deal_attention_delta_schema() -> dict[str, Any]:
    """Return the strict Responses API schema for a deal shadow result."""
    schema = _base_schema("deal")
    schema["required"] = (*schema["required"], "deal_review")
    schema["properties"]["deal_review"] = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ("type", "decision"),
                "properties": {
                    "type": {"type": "string", "enum": list(DEAL_REVIEW_TYPES)},
                    "decision": {"type": "string", "enum": list(DEAL_REVIEW_DECISIONS)},
                },
            },
            {"type": "null"},
        ]
    }
    return schema


def lead_attention_delta_schema() -> dict[str, Any]:
    """Return the strict Responses API schema for a lead shadow result."""
    schema = _base_schema("lead")
    schema["required"] = (*schema["required"], "lead_review")
    schema["properties"]["lead_review"] = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ("qualification", "final_verdict"),
                "properties": {
                    "qualification": {"type": "string", "enum": list(LEAD_QUALIFICATIONS)},
                    "final_verdict": {"type": "string", "enum": list(LEAD_FINAL_VERDICTS)},
                },
            },
            {"type": "null"},
        ]
    }
    return schema


# Named contracts for callers that need a stable, inspectable structured-output
# schema. Factory functions above return fresh copies for defensive use.
DealAttentionDelta = deal_attention_delta_schema()
LeadAttentionDelta = lead_attention_delta_schema()


def _non_empty_string(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"expected non-empty string at {path}")


def _string_list(value: Any, path: str, *, max_items: int, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"expected list at {path}")
        return
    if len(value) > max_items:
        errors.append(f"too many items at {path}: max {max_items}, got {len(value)}")
    for index, item in enumerate(value):
        _non_empty_string(item, f"{path}[{index}]", errors)


def _strict_object(value: Any, path: str, required: tuple[str, ...], errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"expected object at {path}")
        return None
    missing = [field for field in required if field not in value]
    extra = sorted(set(value) - set(required))
    if missing:
        errors.append(f"missing required fields at {path}: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected fields at {path}: {', '.join(extra)}")
    return value


def _validate_attention_delta(value: dict[str, Any], entity_type: str) -> None:
    errors: list[str] = []
    special_key = "deal_review" if entity_type == "deal" else "lead_review"
    required = (
        "entity_type",
        "entity_id",
        "attention_required",
        "severity",
        "reason",
        "rop_action",
        "memory_patch",
        special_key,
    )
    root = _strict_object(value, "attention_delta", required, errors)
    if root is None:
        raise ValueError("Invalid attention delta: " + "; ".join(errors))
    if root.get("entity_type") != entity_type:
        errors.append(f"entity_type must be {entity_type!r}")
    _non_empty_string(root.get("entity_id"), "entity_id", errors)
    if not isinstance(root.get("attention_required"), bool):
        errors.append("expected boolean at attention_required")
    if root.get("severity") not in SEVERITIES:
        errors.append(f"invalid severity: {root.get('severity')!r}")
    _non_empty_string(root.get("reason"), "reason", errors)

    action = root.get("rop_action")
    if root.get("attention_required") is False and action is not None:
        errors.append("rop_action must be null when attention_required=false")
    if action is not None:
        action_obj = _strict_object(
            action,
            "rop_action",
            ("check", "message_to_manager", "expected_crm_fact", "deadline", "success_condition", "evidence_ids"),
            errors,
        )
        if action_obj is not None:
            for field in ("check", "message_to_manager", "expected_crm_fact", "success_condition"):
                _non_empty_string(action_obj.get(field), f"rop_action.{field}", errors)
            deadline = action_obj.get("deadline")
            if deadline is not None:
                _non_empty_string(deadline, "rop_action.deadline", errors)
                if isinstance(deadline, str):
                    try:
                        date.fromisoformat(deadline)
                    except ValueError:
                        errors.append("rop_action.deadline must be YYYY-MM-DD or null")
            _string_list(action_obj.get("evidence_ids"), "rop_action.evidence_ids", max_items=MAX_EVIDENCE_IDS, errors=errors)

    patch = _strict_object(
        root.get("memory_patch"),
        "memory_patch",
        ("confirmed_facts_add", "open_questions_add", "open_questions_resolve", "risks_add", "risks_resolve", "next_step"),
        errors,
    )
    if patch is not None:
        for field in ("confirmed_facts_add", "open_questions_add", "open_questions_resolve", "risks_add", "risks_resolve"):
            _string_list(patch.get(field), f"memory_patch.{field}", max_items=5, errors=errors)
        if patch.get("next_step") is not None:
            _non_empty_string(patch.get("next_step"), "memory_patch.next_step", errors)

    special = root.get(special_key)
    if special is not None:
        if entity_type == "deal":
            special_obj = _strict_object(special, special_key, ("type", "decision"), errors)
            if special_obj is not None:
                if special_obj.get("type") not in DEAL_REVIEW_TYPES:
                    errors.append(f"invalid enum at {special_key}.type")
                if special_obj.get("decision") not in DEAL_REVIEW_DECISIONS:
                    errors.append(f"invalid enum at {special_key}.decision")
        else:
            special_obj = _strict_object(special, special_key, ("qualification", "final_verdict"), errors)
            if special_obj is not None:
                if special_obj.get("qualification") not in LEAD_QUALIFICATIONS:
                    errors.append(f"invalid enum at {special_key}.qualification")
                if special_obj.get("final_verdict") not in LEAD_FINAL_VERDICTS:
                    errors.append(f"invalid enum at {special_key}.final_verdict")
    if errors:
        raise ValueError("Invalid attention delta: " + "; ".join(errors))


def validate_deal_attention_delta(value: dict[str, Any]) -> None:
    _validate_attention_delta(value, "deal")


def validate_lead_attention_delta(value: dict[str, Any]) -> None:
    _validate_attention_delta(value, "lead")


def _build_shadow_prompt(
    *,
    entity_type: str,
    entity_id: str,
    history_text: str,
    transcript_text: str,
    diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    stage_policy: dict[str, Any] | None = None,
) -> str:
    stage_policy_text = json.dumps(stage_policy, ensure_ascii=False, indent=2) if stage_policy else "Не применимо для лида."
    return f"""Ты ИИ-помощник РОПа ПрактикМ. Это экспериментальный shadow-анализ: верни только компактную дельту внимания для {entity_type} {entity_id}, а не полный отчёт и не legacy-анализ.

<grounding_rules>
- Факты конкретной CRM-сущности бери только из CRM-истории, транскрипта и CRM_STAGE_POLICY.
- OKF-база задаёт правила оценки, но не является источником фактов о клиенте.
- Diagnostics описывает полноту выгрузки, но не является фактом сделки или лида.
- Не выдумывай факты, даты, обещания, evidence IDs или действия клиента.
- Используй только стабильные IDs из предоставленных источников в evidence_ids.
- Если контекст неполный, отражай ограничение в reason или в нужном действии; не считай отсутствие данных доказательством.
- Внутренние комментарии допустимы как evidence для контроля РОПа, но не как слова клиента.
</grounding_rules>

<compact_output_rules>
- Верни только решение: требует ли сущность внимания РОПа, почему и какое одно конкретное действие нужно.
- Не пересказывай всю историю, не создавай готовый Markdown-отчёт и не включай legacy JSON-контракт.
- Не повторяй одну мысль в reason, action и memory_patch.
- Если attention_required=false, rop_action обязан быть null.
- evidence_ids: не более {MAX_EVIDENCE_IDS}; выбери только проверяемые основания.
- memory_patch — только минимальные изменения, пустые массивы допустимы. Он экспериментальный и никуда не применяется.
- Специальный review-блок верни null, если он не нужен для управленческого решения.
</compact_output_rules>

## CRM_STAGE_POLICY
{stage_policy_text}

## CRM HISTORY
{history_text.strip()}

## TRANSCRIPT OR NEW EVENT
{transcript_text.strip()}

## CONTEXT DIAGNOSTICS
{diagnostics_text.strip()}

## OKF RULES
{render_okf_sections(okf_sections)}
"""


def build_deal_attention_delta_prompt(
    deal_id: str,
    history_text: str,
    transcript_text: str,
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    stage_policy: dict[str, Any],
) -> str:
    return _build_shadow_prompt(
        entity_type="deal",
        entity_id=str(deal_id),
        history_text=history_text,
        transcript_text=transcript_text,
        diagnostics_text=context_diagnostics_text,
        okf_sections=okf_sections,
        stage_policy=stage_policy,
    )


def build_lead_attention_delta_prompt(
    lead_id: str,
    history_text: str,
    transcript_text: str,
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
) -> str:
    return _build_shadow_prompt(
        entity_type="lead",
        entity_id=str(lead_id),
        history_text=history_text,
        transcript_text=transcript_text,
        diagnostics_text=context_diagnostics_text,
        okf_sections=okf_sections,
    )
