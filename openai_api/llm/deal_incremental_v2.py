"""Two-stage semantic update and partial materialization for deal V2."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from openai_api.llm.deal_semantic_dependencies import resolve_affected_sections
from openai_api.llm.deal_semantic_state import (
    SCHEMA_VERSION,
    SemanticStateValidationError,
    semantic_changed_domains,
    validate_semantic_state_v1,
)
from openai_api.llm.llm_client import call_analysis_json, call_validated_analysis_json
from openai_api.llm.validation import AnalysisValidationError, normalize_analysis_for_validation, validate_deal_analysis


class IncrementalV2Error(ValueError):
    pass


@dataclass(frozen=True)
class IncrementalV2Result:
    analysis: dict[str, Any]
    semantic_state: dict[str, Any]
    changed_domains: list[str]
    affected_sections: list[str]
    metadata: dict[str, Any]


def _identity_normalizer(_value: dict[str, Any]) -> list[str]:
    return []


def _semantic_validator(value: dict[str, Any]) -> None:
    # Technical coverage is deterministic and intentionally omitted from the
    # model output to save tokens. It is replaced with the proven delta after
    # the semantic response passes business-shape validation.
    value.setdefault("evidence_coverage", {})
    try:
        validate_semantic_state_v1(value)
    except SemanticStateValidationError as error:
        raise AnalysisValidationError(str(error)) from error


def _materialization_validator(expected: set[str], previous_analysis: dict[str, Any]):
    def validate(value: dict[str, Any]) -> None:
        sections = value.get("sections")
        if not isinstance(sections, dict):
            raise AnalysisValidationError("sections must be an object")
        actual = set(sections)
        if actual != expected:
            raise AnalysisValidationError(
                f"sections mismatch: missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
            )
        if any(not isinstance(sections[key], (dict, list)) for key in expected):
            raise AnalysisValidationError("each materialized section must preserve its object/list shape")
        candidate = copy.deepcopy(previous_analysis)
        candidate.update(sections)
        normalize_analysis_for_validation(candidate)
        validate_deal_analysis(candidate)
    return validate


def _usage_summary(metadata_rows: list[dict[str, Any]]) -> dict[str, Any]:
    usages = [row.get("usage") for row in metadata_rows if isinstance(row.get("usage"), dict)]
    costs = [row.get("estimated_cost") for row in metadata_rows if isinstance(row.get("estimated_cost"), dict)]
    return {
        "calls": len(metadata_rows),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in usages),
        "cached_input_tokens": sum(int((row.get("input_tokens_details") or {}).get("cached_tokens") or 0) for row in usages),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in usages),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usages),
        "estimated_cost_usd": round(sum(float(row.get("estimated_cost_usd") or 0) for row in costs), 6),
        "estimated_cost_rub": round(sum(float(row.get("estimated_cost_rub") or 0) for row in costs), 2),
        "latency_seconds": round(sum(float(row.get("latency_seconds") or 0) for row in metadata_rows), 4),
    }


def _prompt_budget(prompt: str, **blocks: Any) -> dict[str, Any]:
    rendered = {
        name: json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
        for name, value in blocks.items()
    }
    accounted = sum(len(value) for value in rendered.values())
    return {
        "total_chars": len(prompt),
        "blocks": {name: {"chars": len(value)} for name, value in rendered.items()},
        "other_chars": max(0, len(prompt) - accounted),
        "accounted_chars": min(len(prompt), accounted + max(0, len(prompt) - accounted)),
        "composition_warnings": [] if accounted <= len(prompt) else ["accounted_chars_exceed_total"],
    }


def build_semantic_update_prompt(
    *, previous_state: dict[str, Any], evidence_delta: list[dict[str, Any]], crm_delta: dict[str, Any]
) -> str:
    model_state = copy.deepcopy(previous_state)
    model_state.pop("evidence_coverage", None)
    return f"""Ты обновляешь компактную семантическую память сделки NeuroROP.
Верни только полный JSON-объект schema_version={SCHEMA_VERSION}, той же структуры, что PREVIOUS_SEMANTIC_STATE.
Не возвращай evidence_coverage: код добавит его детерминированно.
Пиши компактный JSON без отступов. Не переформулируй домен, если новое evidence не меняет его бизнес-смысл.
Сохраняй stable id элемента, если бизнес-смысл сохранился. Не добавляй presentation-тексты, рекомендации менеджеру или черновики сообщений.
NEW_OR_REVISED_EVIDENCE является единственным новым клиентским evidence. CRM delta не доказывает контакт или слова клиента.
Не выдумывай неизвестные факты. Старые подтверждённые факты сохраняй, если новое evidence им не противоречит.

## PREVIOUS_SEMANTIC_STATE
{json.dumps(model_state, ensure_ascii=False, indent=2)}

## NEW_OR_REVISED_EVIDENCE
{json.dumps(evidence_delta, ensure_ascii=False, indent=2)}

## CRM_DELTA
{json.dumps(crm_delta, ensure_ascii=False, indent=2)}
"""


def build_materialization_prompt(
    *, previous_analysis: dict[str, Any], semantic_state: dict[str, Any], evidence_delta: list[dict[str, Any]],
    affected_sections: list[str], stage_policy: dict[str, Any], prior_recommendation: dict[str, Any] | None,
    daily_checklist: dict[str, Any] | None,
) -> str:
    templates = {key: previous_analysis.get(key) for key in affected_sections}
    return f"""Ты частично пересобираешь полный validated deal analysis NeuroROP.
Верни строго JSON {{"sections": {{...}}}} и ровно перечисленные AFFECTED_SECTIONS. Не возвращай остальные поля.
Пиши компактный JSON без отступов и пояснений.
Для каждого поля сохрани структуру и типы из PREVIOUS_SECTION_TEMPLATES, но обнови содержание по NEW_SEMANTIC_STATE и NEW_OR_REVISED_EVIDENCE.
CRM stage/task сами по себе не доказывают клиентский контакт. Recommendation feedback подтверждается только evidence после соответствующей materialized recommendation.
Transient-поля описывают только текущий запуск. Не переноси их механически.
PREVIOUS_SECTION_TEMPLATES является обязательным структурным контрактом:
- во всех элементах deal_context сохраняй обязательный непустой массив evidence из template, пока новое evidence явно не заменяет его;
- category critical fact использует только deadline|budget|need|authority|technical|delivery|payment|competitor|commitment|other;
- длины списков не увеличивай сверх длины template и текущих validator limits;
- daily_checklist_update содержит business_date, base_revision и массивы add/retire/reopen; add-элемент всегда {{"text":"...","reason":"..."}}, retire/reopen — {{"item_id":"...","reason":"..."}};
- не создавай checklist add/reopen/retire без фактической необходимости из нового evidence.

## AFFECTED_SECTIONS
{json.dumps(affected_sections, ensure_ascii=False)}

## PREVIOUS_SECTION_TEMPLATES
{json.dumps(templates, ensure_ascii=False, indent=2)}

## NEW_SEMANTIC_STATE
{json.dumps(semantic_state, ensure_ascii=False, indent=2)}

## NEW_OR_REVISED_EVIDENCE
{json.dumps(evidence_delta, ensure_ascii=False, indent=2)}

## CRM_STAGE_POLICY
{json.dumps(stage_policy, ensure_ascii=False, indent=2)}

## PRIOR_NEURO_ROP_RECOMMENDATION
{json.dumps(prior_recommendation, ensure_ascii=False, indent=2)}

## CURRENT_DAILY_MANAGER_CHECKLIST
{json.dumps(daily_checklist, ensure_ascii=False, indent=2)}
"""


def run_incremental_v2(
    *, deal_id: str, previous_analysis: dict[str, Any], previous_semantic_state: dict[str, Any],
    evidence_delta: list[dict[str, Any]], next_evidence_coverage: dict[str, Any], crm_delta: dict[str, Any],
    stage_policy: dict[str, Any], prior_recommendation: dict[str, Any] | None,
    daily_checklist: dict[str, Any] | None, source_fingerprint: str, model: str,
) -> IncrementalV2Result:
    if not evidence_delta:
        raise IncrementalV2Error("no_genuinely_new_or_revised_evidence")
    semantic_prompt = build_semantic_update_prompt(
        previous_state=previous_semantic_state, evidence_delta=evidence_delta, crm_delta=crm_delta
    )
    semantic_budget = _prompt_budget(
        semantic_prompt,
        semantic_state={key: value for key, value in previous_semantic_state.items() if key != "evidence_coverage"},
        new_evidence=evidence_delta,
        crm_delta=crm_delta,
    )
    semantic_state, semantic_metadata = call_validated_analysis_json(
        semantic_prompt,
        validator=_semantic_validator,
        normalizer=_identity_normalizer,
        validation_error_types=(AnalysisValidationError,),
        model=model,
        analysis_caller=call_analysis_json,
        call_type="deal_incremental_v2_semantic_update",
        prompt_cache_key="neuro-rop:deal-incremental-v2:semantic:v1",
        trace_entity_type="deal",
        trace_entity_id=deal_id,
        preview_prompt=False,
        preview_response_errors=False,
    )
    semantic_state["schema_version"] = SCHEMA_VERSION
    semantic_state["deal_id"] = str(deal_id)
    semantic_state["source_fingerprint"] = str(source_fingerprint)
    semantic_state["evidence_coverage"] = copy.deepcopy(next_evidence_coverage)
    validate_semantic_state_v1(semantic_state)
    changed_domains = semantic_changed_domains(previous_semantic_state, semantic_state)
    affected_sections = resolve_affected_sections(changed_domains)
    materialization_prompt = build_materialization_prompt(
        previous_analysis=previous_analysis,
        semantic_state=semantic_state,
        evidence_delta=evidence_delta,
        affected_sections=affected_sections,
        stage_policy=stage_policy,
        prior_recommendation=prior_recommendation,
        daily_checklist=daily_checklist,
    )
    materialization_budget = _prompt_budget(
        materialization_prompt,
        semantic_state=semantic_state,
        new_evidence=evidence_delta,
        policy=stage_policy,
        prior_recommendation=prior_recommendation,
        daily_checklist=daily_checklist,
        output_contract={key: previous_analysis.get(key) for key in affected_sections},
    )
    materialized, materialization_metadata = call_validated_analysis_json(
        materialization_prompt,
        validator=_materialization_validator(set(affected_sections), previous_analysis),
        normalizer=_identity_normalizer,
        validation_error_types=(AnalysisValidationError,),
        model=model,
        analysis_caller=call_analysis_json,
        call_type="deal_incremental_v2_materialization",
        prompt_cache_key="neuro-rop:deal-incremental-v2:materialization:v1",
        trace_entity_type="deal",
        trace_entity_id=deal_id,
        preview_prompt=False,
        preview_response_errors=False,
    )
    candidate = copy.deepcopy(previous_analysis)
    candidate.update(materialized["sections"])
    normalize_analysis_for_validation(candidate)
    validate_deal_analysis(candidate)
    metadata_rows = [semantic_metadata, materialization_metadata]
    raw_outputs = [str(row.get("raw_output_text") or "") for row in metadata_rows]
    safe_metadata_rows = [
        {key: value for key, value in row.items() if key != "raw_output_text"}
        for row in metadata_rows
    ]
    return IncrementalV2Result(
        analysis=candidate,
        semantic_state=semantic_state,
        changed_domains=changed_domains,
        affected_sections=affected_sections,
        metadata={
            "model": model,
            "usage": _usage_summary(metadata_rows),
            "prompt_budget": {"semantic_update": semantic_budget, "materialization": materialization_budget},
            "calls": safe_metadata_rows,
            "_raw_outputs": raw_outputs,
        },
    )
