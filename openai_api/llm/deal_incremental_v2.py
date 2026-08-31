"""Two-stage semantic update and partial materialization for deal V2."""

from __future__ import annotations

from openai_api.llm.deal_daily_quality import (
    DAILY_QUALITY_MARKER, DAILY_QUALITY_RULE, render_daily_quality_context, stamp_daily_quality_scope,
)

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_api.llm.deal_current_situation import render_deal_current_situation_context
from openai_api.llm.deal_semantic_dependencies import (
    ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE,
    resolve_affected_sections,
)
from openai_api.llm.deal_semantic_state import (
    SCHEMA_VERSION,
    SEMANTIC_DOMAINS,
    SemanticStateValidationError,
    semantic_domain_changed,
    semantic_changed_domains,
    validate_semantic_state_v1,
)
from openai_api.llm.section_repair import _preserve_missing_object_keys, merge_sections
from openai_api.llm.llm_client import call_analysis_json, call_validated_analysis_json
from openai_api.llm.validation import (
    DEAL_CONTROL_BRIEF_LIST_LIMITS,
    DEAL_RECOMMENDED_CHANNELS,
    MAX_LIST_LIMITS,
    RECOMMENDATION_FEEDBACK_STATUSES,
    AnalysisValidationError,
    normalize_analysis_for_validation,
    remove_retired_deal_fields,
    validate_deal_analysis,
)


class IncrementalV2Error(ValueError):
    pass


@dataclass(frozen=True)
class IncrementalV2Result:
    analysis: dict[str, Any]
    semantic_state: dict[str, Any]
    changed_domains: list[str]
    affected_sections: list[str]
    metadata: dict[str, Any]


V2_POLICY_HEADINGS: dict[str, tuple[str, ...]] = {
    "technical_data.md": (
        "Общий принцип", "Линия розлива", "Этикетировщик",
        "Ограничения по этикетировке", "Продукты и материалы", "Честный знак",
    ),
    "risk_signals.md": (
        "Главный принцип", "Высокий риск зависания", "Средний риск",
        "Недозвон или автоответчик", "Когда нужен контроль РОПа",
    ),
    "call_attempt_rules.md": (
        "Первые 3 дня", "Если линия занята", "Если не дозвонились",
        "Повторная волна", "Как использовать в анализе",
    ),
    "commercial_offer_followup.md": (
        "Хороший результат после КП", "Что выяснить при сравнении с конкурентами",
        "Плохие формулировки", "Как использовать в анализе",
    ),
    "objections.md": tuple(),
    "funnel.md": (
        "Общая логика", "Примеры рабочих следующих шагов", "Плохие следующие шаги",
        "После отправки КП", "После получения части технических данных",
    ),
}

SECTION_POLICY_FILES: dict[str, frozenset[str]] = {
    "call_attempt_recommendation": frozenset({"call_attempt_rules.md", "risk_signals.md"}),
    "communication_quality_audit": frozenset({"call_attempt_rules.md", "risk_signals.md"}),
    "manager_quality": frozenset({"call_attempt_rules.md", "risk_signals.md", "funnel.md"}),
    "deal_control_brief": frozenset({"risk_signals.md", "commercial_offer_followup.md", "funnel.md"}),
    "manager_action_block": frozenset({"risk_signals.md", "commercial_offer_followup.md", "funnel.md"}),
    "rop_manager_message_block": frozenset({"risk_signals.md", "commercial_offer_followup.md", "funnel.md"}),
    "rop_action": frozenset({"risk_signals.md", "funnel.md"}),
    "priority_recommendation": frozenset({"risk_signals.md", "funnel.md"}),
    "recommendation_feedback": frozenset({"funnel.md"}),
    "memory_update": frozenset({"funnel.md"}),
    "qualification_assessment": frozenset({"technical_data.md", "funnel.md"}),
    "price_comparability_check": frozenset({"commercial_offer_followup.md", "objections.md"}),
    "payment_blocker": frozenset({"objections.md", "funnel.md"}),
    "money_path_diagnosis": frozenset({"risk_signals.md", "objections.md", "funnel.md"}),
    "competitor_defense_checklist": frozenset({"commercial_offer_followup.md", "objections.md"}),
    "objection_handling": frozenset({"objections.md", "commercial_offer_followup.md"}),
    "deal_context": frozenset({"technical_data.md", "risk_signals.md", "funnel.md"}),
    "main_risk": frozenset({"risk_signals.md"}),
}


def build_v2_compact_policy(
    *,
    deal_dir: Path,
    deal_id: str,
    context_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic compact excerpts from the FULL deal OKF sources."""
    del deal_id, context_diagnostics
    sources: dict[str, str] = {}
    for path in _v2_knowledge_files(deal_dir):
        headings = V2_POLICY_HEADINGS.get(path.name, ())
        sources[path.name] = _project_markdown_sections(path.read_text(encoding="utf-8"), headings)
    return {
        "knowledge_files": sorted(sources),
        "sources": sources,
    }


def _v2_knowledge_files(deal_dir: Path):
    from openai_api.llm.analyze_deal import DEFAULT_KNOWLEDGE_DIR, knowledge_files

    knowledge_dir = Path(DEFAULT_KNOWLEDGE_DIR)
    if not knowledge_dir.is_dir():
        fallback = deal_dir.parents[1] / "knowledge" / "clients" / "praktikm"
        if fallback.is_dir():
            knowledge_dir = fallback
    return knowledge_files(knowledge_dir, entity_type="deal")


def _project_markdown_sections(text: str, headings: tuple[str, ...]) -> str:
    body = text
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    if not headings:
        return body.strip()
    selected: list[str] = []
    current: list[str] = []
    keep = False
    for line in body.splitlines():
        if line.startswith("## "):
            if keep and current:
                selected.extend(current)
            title = line[3:].strip()
            keep = title in headings
            current = [line] if keep else []
        elif keep:
            current.append(line)
    if keep and current:
        selected.extend(current)
    return "\n".join(selected).strip()


def render_v2_compact_policy(
    policy: dict[str, Any],
    *,
    affected_sections: list[str] | None = None,
) -> str:
    sources = policy.get("sources") if isinstance(policy.get("sources"), dict) else {}
    selected_files = set(sources)
    if affected_sections is not None:
        selected_files = set()
        for section in affected_sections:
            selected_files.update(SECTION_POLICY_FILES.get(section, ()))
    blocks = [
        f"### SOURCE OKF: {name}\n{str(sources[name]).strip()}"
        for name in sorted(selected_files)
        if name in sources and str(sources[name]).strip()
    ]
    return "\n\n".join(blocks)


def build_v2_compact_diagnostics(context_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    diagnostics = context_diagnostics if isinstance(context_diagnostics, dict) else {}
    summary = diagnostics.get("summary") if isinstance(diagnostics.get("summary"), dict) else {}
    gaps = [gap for gap in (diagnostics.get("gaps") or []) if isinstance(gap, dict)]
    available = diagnostics.get("available_sources") or diagnostics.get("sources_available") or []
    return {
        "context_completeness": str(diagnostics.get("context_completeness") or "unknown"),
        "available_sources": [str(item) for item in available][:12] if isinstance(available, list) else [],
        "critical_missing": [str(item) for item in (diagnostics.get("critical_missing") or [])][:10],
        "summary": {key: summary.get(key) for key in sorted(summary)},
        "gap_types": [
            str(gap.get("type") or gap.get("title") or gap.get("kind") or "").strip()
            for gap in gaps[:10]
            if str(gap.get("type") or gap.get("title") or gap.get("kind") or "").strip()
        ],
        "evidence_absence_rule": "Отсутствие evidence не доказывает отсутствие факта; фиксируй ограничение контекста.",
    }


def render_v2_compact_diagnostics(diagnostics: dict[str, Any]) -> str:
    return json.dumps(diagnostics, ensure_ascii=False, indent=2)



def _identity_normalizer(_value: dict[str, Any]) -> list[str]:
    return []


def _materialization_normalizer(
    expected: set[str], previous_analysis: dict[str, Any]
):
    def normalize(value: dict[str, Any]) -> list[dict[str, Any]]:
        sections = value.get("sections")
        if not isinstance(sections, dict):
            return []
        changes: list[dict[str, Any]] = []
        for section in expected:
            if section not in sections:
                continue
            _preserve_missing_object_keys(
                sections[section],
                previous_analysis.get(section),
                path=section,
                changes=changes,
            )

        candidate = merge_sections(previous_analysis, sections)
        changes.extend(normalize_analysis_for_validation(candidate, truncate_lists=False))
        for section in expected:
            if section in sections and section in candidate:
                sections[section] = candidate[section]
        return changes

    return normalize


def _semantic_envelope_validator(
    previous_state: dict[str, Any],
    evidence_delta: list[dict[str, Any]],
    crm_delta: dict[str, Any],
):
    evidence_ids = {str(item.get("evidence_id") or "") for item in evidence_delta}
    crm_change_types = {str(item) for item in crm_delta.get("change_types") or []}

    def validate(value: dict[str, Any]) -> None:
        if set(value) != {"changed_domains", "change_reasons", "semantic_state"}:
            raise AnalysisValidationError("semantic envelope requires changed_domains, change_reasons, semantic_state")
        declared = value.get("changed_domains")
        reasons = value.get("change_reasons")
        state = value.get("semantic_state")
        if not isinstance(declared, list) or len(set(declared)) != len(declared):
            raise AnalysisValidationError("changed_domains must be a unique list")
        unknown = sorted(set(declared) - set(SEMANTIC_DOMAINS))
        if unknown:
            raise AnalysisValidationError("unknown changed semantic domains: " + ", ".join(unknown))
        if not isinstance(reasons, dict) or set(reasons) != set(declared):
            raise AnalysisValidationError("change_reasons must match changed_domains exactly")
        if not isinstance(state, dict):
            raise AnalysisValidationError("semantic_state must be an object")
        state.setdefault("evidence_coverage", {})
        try:
            validate_semantic_state_v1(state)
        except SemanticStateValidationError as error:
            raise AnalysisValidationError(str(error)) from error
        for domain in SEMANTIC_DOMAINS:
            if domain not in declared and semantic_domain_changed(previous_state, state, domain):
                raise AnalysisValidationError(f"undeclared semantic domain changed: {domain}")
        for domain in declared:
            reason = reasons.get(domain)
            if not isinstance(reason, dict) or not str(reason.get("reason") or "").strip():
                raise AnalysisValidationError(f"change reason missing for domain: {domain}")
            refs = reason.get("evidence_refs") or []
            crm_refs = reason.get("crm_change_types") or []
            if not isinstance(refs, list) or not set(map(str, refs)).issubset(evidence_ids):
                raise AnalysisValidationError(f"invalid evidence refs for domain: {domain}")
            if not isinstance(crm_refs, list) or not set(map(str, crm_refs)).issubset(crm_change_types):
                raise AnalysisValidationError(f"invalid CRM refs for domain: {domain}")
            if not refs and not crm_refs:
                raise AnalysisValidationError(f"changed domain has no current delta basis: {domain}")
    return validate


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
        candidate = merge_sections(previous_analysis, sections)
        normalize_analysis_for_validation(candidate, truncate_lists=False)
        validate_deal_analysis(candidate)
    return validate


def _usage_summary(metadata_rows: list[dict[str, Any]]) -> dict[str, Any]:
    usages = [row.get("usage") for row in metadata_rows if isinstance(row.get("usage"), dict)]
    costs = [row.get("estimated_cost") for row in metadata_rows if isinstance(row.get("estimated_cost"), dict)]
    api_attempts = sum(int(row.get("semantic_attempt_count") or 1) for row in metadata_rows)
    return {
        "calls": api_attempts,
        "logical_calls": len(metadata_rows),
        "api_attempts": api_attempts,
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in usages),
        "cached_input_tokens": sum(int((row.get("input_tokens_details") or {}).get("cached_tokens") or 0) for row in usages),
        "cache_write_tokens": sum(int((row.get("input_tokens_details") or {}).get("cache_write_tokens") or 0) for row in usages),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in usages),
        "reasoning_tokens": sum(int((row.get("output_tokens_details") or {}).get("reasoning_tokens") or 0) for row in usages),
        "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usages),
        "estimated_cost_usd": round(sum(float(row.get("estimated_cost_usd") or 0) for row in costs), 6),
        "estimated_cost_rub": round(sum(float(row.get("estimated_cost_rub") or 0) for row in costs), 2),
        "latency_seconds": round(sum(float(row.get("latency_seconds") or 0) for row in metadata_rows), 4),
    }


def _estimated_cost_summary(metadata_rows: list[dict[str, Any]], usage: dict[str, Any]) -> dict[str, Any]:
    costs = [row.get("estimated_cost") for row in metadata_rows if isinstance(row.get("estimated_cost"), dict)]
    first = costs[0] if costs else {}
    return {
        "model": next((str(row.get("model")) for row in metadata_rows if row.get("model")), None),
        "usd_rub_rate": first.get("usd_rub_rate"),
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "cache_write_tokens": usage.get("cache_write_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "billable_input_tokens": sum(int(row.get("billable_input_tokens") or 0) for row in costs),
        "estimated_cost_usd": usage.get("estimated_cost_usd", 0),
        "estimated_cost_rub": usage.get("estimated_cost_rub", 0),
        "pricing_source": first.get("pricing_source"),
    }


def _compact_shape(value: Any, path: str) -> Any:
    if isinstance(value, dict):
        return {key: _compact_shape(item, f"{path}.{key}" if path else key) for key, item in value.items()}
    if isinstance(value, list):
        return {
            "type": "array",
            "max_items": MAX_LIST_LIMITS.get(path),
            "item_shape": _compact_shape(value[0], path + "[]") if value else "unknown",
        }
    if value is None:
        return "null_or_contract_type"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _compact_continuity(value: Any, *, string_limit: int = 120, list_limit: int = 1) -> Any:
    """Keep continuity anchors without resending the previous report verbatim."""
    if isinstance(value, dict):
        return {
            key: _compact_continuity(item, string_limit=string_limit, list_limit=list_limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _compact_continuity(item, string_limit=string_limit, list_limit=list_limit)
            for item in value[:list_limit]
        ]
    if isinstance(value, str) and len(value) > string_limit:
        return value[:string_limit].rstrip() + "…"
    return value


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_materialization_contract(
    previous_analysis: dict[str, Any], affected_sections: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    previous_analysis = copy.deepcopy(previous_analysis)
    remove_retired_deal_fields(previous_analysis)
    if isinstance(previous_analysis.get("communication_quality_audit"), dict):
        previous_analysis["communication_quality_audit"].pop("daily_scope", None)
    compact_sections = set(ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE)
    structural = {
        section: _compact_shape(previous_analysis.get(section), section)
        for section in affected_sections
    }
    continuity = {
        section: _compact_continuity(previous_analysis.get(section))
        for section in affected_sections
        if section not in compact_sections
    }
    constraints: dict[str, Any] = {
        "list_limits": {
            path: limit
            for path, limit in MAX_LIST_LIMITS.items()
            if path.split(".", 1)[0] in affected_sections
        } | (
            {
                f"deal_control_brief.{field}": limit
                for field, limit in DEAL_CONTROL_BRIEF_LIST_LIMITS.items()
            }
            if "deal_control_brief" in affected_sections
            else {}
        )
    }
    if "recommendation_feedback" in affected_sections:
        constraints["recommendation_feedback.status"] = sorted(RECOMMENDATION_FEEDBACK_STATUSES)
    if "manager_action_block" in affected_sections:
        constraints["manager_action_block.recommended_channel"] = sorted(DEAL_RECOMMENDED_CHANNELS)
    if "rop_manager_message_block" in affected_sections:
        constraints["rop_manager_message_block.deadline"] = "required calendar date in exact YYYY-MM-DD format"
    if "deal_context" in affected_sections:
        constraints["deal_context.decision_path"] = {
            "required_fields": [
                "process", "criteria", "current_step", "current_step_owner",
                "influencers", "basis_status", "evidence",
            ],
            "influencers": "required array, max 4",
            "evidence": "required non-empty array, max 5",
        }
    if "communication_quality_audit" in affected_sections:
        from openai_api.llm.analyze_deal import COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE

        constraints["communication_quality_audit"] = {
            "daily_scope_rule": DAILY_QUALITY_RULE,
            "assessed": (
                "all three criteria scores are integer 0 or 1; zero_reasons contains "
                "one {criterion, explanation, quote} object for every and only score=0 criterion; "
                "summary_for_rop is non-empty; insufficient_reason is null"
            ),
            "insufficient_evidence": (
                "all three scores are null; zero_reasons is []; summary_for_rop is null; "
                "insufficient_reason is non-empty"
            ),
            "next_action": COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE,
            "next_action_warning": (
                "optional null or {status:'cancelled_without_replacement', explanation, quote}; "
                "client cancellation does not by itself downgrade a previously confirmed daily score"
            ),
        }
    return structural, continuity, constraints


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
    *, previous_state: dict[str, Any], evidence_delta: list[dict[str, Any]], crm_delta: dict[str, Any],
    compact_policy_text: str, compact_diagnostics_text: str = "", stage_policy: dict[str, Any] | None = None,
) -> str:
    model_state = copy.deepcopy(previous_state)
    model_state.pop("evidence_coverage", None)
    return f"""Ты обновляешь компактную семантическую память сделки NeuroROP.
Верни внутренний JSON envelope ровно с полями changed_domains, change_reasons, semantic_state.
semantic_state — полный объект schema_version={SCHEMA_VERSION}, той же структуры, что PREVIOUS_SEMANTIC_STATE.
Не возвращай evidence_coverage внутри semantic_state: код добавит его детерминированно.
changed_domains содержит только реально изменившиеся semantic domains из списка: {json.dumps(sorted(SEMANTIC_DOMAINS), ensure_ascii=False)}.
Для каждого changed domain верни change_reasons[domain]={{"reason":"...","evidence_refs":[],"crm_change_types":[]}}.
evidence_refs могут ссылаться только на evidence_id из NEW_OR_REVISED_EVIDENCE; crm_change_types — только на CRM_DELTA.change_types.
Если новое evidence уже полностью отражено в PREVIOUS_SEMANTIC_STATE и не меняет бизнес-смысл, верни changed_domains=[] и не переписывай domain objects.
Undeclared domains сохраняй структурно эквивалентными предыдущему state. Не улучшай формулировки ради стиля.
Пиши компактный JSON без отступов. Не переформулируй домен, если новое evidence не меняет его бизнес-смысл.
Сохраняй stable id элемента, если бизнес-смысл сохранился. Не добавляй presentation-тексты, рекомендации менеджеру или черновики сообщений.
NEW_OR_REVISED_EVIDENCE является единственным новым клиентским evidence. CRM delta не доказывает контакт или слова клиента.
Не выдумывай неизвестные факты. Старые подтверждённые факты сохраняй, если новое evidence им не противоречит.

## V2_COMPACT_POLICY
{compact_policy_text.strip()}

## CONTEXT_DIAGNOSTICS
{compact_diagnostics_text.strip()}

## CRM_STAGE_POLICY
{json.dumps(stage_policy or {}, ensure_ascii=False, indent=2)}

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
    compact_policy_text: str,
    compact_diagnostics_text: str = "",
    current_situation_context: dict[str, Any] | None = None,
    daily_quality_context: dict[str, Any] | None = None,
) -> str:
    structural_templates, continuity_content, constraints = build_materialization_contract(
        previous_analysis, affected_sections
    )
    situation_context_text = ""
    situation_rule = ""
    if "deal_control_brief" in affected_sections:
        situation_context_text = render_deal_current_situation_context(current_situation_context)
        situation_rule = (
            "deal_control_brief.current_situation строй только от CURRENT_SITUATION_CONTEXT: "
            "якорь — last_substantive_client_contact, затем действия менеджера после него. "
            "4–6 коротких предложений. Если has_newer_client_response=false, прямо напиши, "
            "что нового подтверждённого ответа клиента нет. Попытка, исходящее сообщение, "
            "email, задача или комментарий менеджера не являются новой позицией клиента. "
            "Простые «ок/спасибо/получил» без нового бизнес-факта якорь не заменяют. "
            "Не выдумывай количества, если в контексте null.\n"
        )
    situation_section = (
        f"\n## CURRENT_SITUATION_CONTEXT\n{situation_context_text}\n"
        if situation_context_text
        else ""
    )
    if "communication_quality_audit" in affected_sections:
        situation_section += f"\n{DAILY_QUALITY_MARKER}\n{render_daily_quality_context(daily_quality_context)}\n"
    return f"""Ты частично пересобираешь полный validated deal analysis NeuroROP.
Верни строго JSON {{"sections": {{...}}}} и ровно перечисленные AFFECTED_SECTIONS. Не возвращай остальные поля.
Пиши компактный JSON без отступов и пояснений.
Для каждого поля соблюдай COMPACT_STRUCTURAL_TEMPLATES и VALIDATION_CONSTRAINTS, но обнови содержание по NEW_SEMANTIC_STATE и NEW_OR_REVISED_EVIDENCE.
CRM stage/task сами по себе не доказывают клиентский контакт. Recommendation feedback подтверждается только evidence после соответствующей materialized recommendation.
Transient-поля описывают только текущий запуск. Не переноси их механически.
{situation_rule}COMPACT_STRUCTURAL_TEMPLATES является обязательным структурным контрактом:
- каждый ключ object-template обязателен в результате; не сокращай вложенные объекты;
- во всех элементах deal_context сохраняй обязательный непустой массив evidence из template, пока новое evidence явно не заменяет его;
- category critical fact использует только deadline|budget|need|authority|technical|delivery|payment|competitor|commitment|other;
- длины списков не увеличивай сверх длины template и текущих validator limits;

## V2_COMPACT_POLICY
{compact_policy_text.strip()}

## CONTEXT_DIAGNOSTICS
{compact_diagnostics_text.strip()}

## AFFECTED_SECTIONS
{_json_compact(affected_sections)}

## COMPACT_STRUCTURAL_TEMPLATES
{_json_compact(structural_templates)}

## VALIDATION_CONSTRAINTS
{_json_compact(constraints)}

## PREVIOUS_CONTINUITY_CONTENT
{_json_compact(continuity_content)}

## NEW_SEMANTIC_STATE
{_json_compact(semantic_state)}

## NEW_OR_REVISED_EVIDENCE
{_json_compact(evidence_delta)}

## CRM_STAGE_POLICY
{_json_compact(stage_policy)}

## PRIOR_NEURO_ROP_RECOMMENDATION
{_json_compact(prior_recommendation)}
{situation_section}"""


def _materialization_repair_prompt_builder(
    *,
    affected_sections: list[str],
    structural_templates: dict[str, Any],
    constraints: dict[str, Any],
    compact_policy_text: str,
):
    def build(_original_prompt: str, validation_error: str, raw_output_text: str) -> str:
        previous = raw_output_text[-30_000:]
        return f"""Ты выполняешь узкий repair уже готового V2 materialization JSON.
Не анализируй сделку заново. Не добавляй новые факты, выводы, evidence или рекомендации.
Верни полный JSON {{"sections":{{...}}}} ровно для AFFECTED_SECTIONS, не JSON Patch.
Сохрани все корректные поля и формулировки предыдущего ответа без изменений.
Исправь только перечисленные VALIDATION_ERRORS.

Если список длиннее лимита:
- сначала удали точные и смысловые дубликаты;
- объедини пункты, которые выражают один бизнес-факт;
- если пунктов всё ещё больше лимита, выбери наиболее важные по влиянию на продажу и силе evidence;
- не обрезай механически первые N и не придумывай замену удалённым пунктам.
Enum выбирай только из VALIDATION_CONSTRAINTS. Дату верни строго YYYY-MM-DD.

## AFFECTED_SECTIONS
{_json_compact(affected_sections)}

## COMPACT_STRUCTURAL_TEMPLATES
{_json_compact(structural_templates)}

## VALIDATION_CONSTRAINTS
{_json_compact(constraints)}

## RELEVANT_POLICY
{compact_policy_text.strip()}

## VALIDATION_ERRORS
{validation_error}

## PREVIOUS_MATERIALIZATION_JSON
{previous}
"""

    return build


def run_incremental_v2(
    *, deal_id: str, previous_analysis: dict[str, Any], previous_semantic_state: dict[str, Any],
    evidence_delta: list[dict[str, Any]], next_evidence_coverage: dict[str, Any], crm_delta: dict[str, Any],
    stage_policy: dict[str, Any], prior_recommendation: dict[str, Any] | None,
    source_fingerprint: str, model: str,
    compact_policy_text: str = "", compact_policy: dict[str, Any] | None = None,
    compact_diagnostics_text: str = "",
    current_situation_context: dict[str, Any] | None = None,
    daily_quality_context: dict[str, Any] | None = None,
) -> IncrementalV2Result:
    previous_analysis = copy.deepcopy(previous_analysis)
    remove_retired_deal_fields(previous_analysis)
    if not evidence_delta:
        raise IncrementalV2Error("no_genuinely_new_or_revised_evidence")
    policy_payload = compact_policy or build_v2_compact_policy(
        deal_dir=Path("reports/rop_assistant/deals") / f"deal_{deal_id}",
        deal_id=str(deal_id),
    )
    policy_text = compact_policy_text or render_v2_compact_policy(policy_payload)
    semantic_prompt = build_semantic_update_prompt(
        previous_state=previous_semantic_state, evidence_delta=evidence_delta, crm_delta=crm_delta,
        compact_policy_text=policy_text, compact_diagnostics_text=compact_diagnostics_text,
        stage_policy=stage_policy,
    )
    semantic_budget = _prompt_budget(
        semantic_prompt,
        policy=policy_text,
        diagnostics=compact_diagnostics_text,
        stage_policy=stage_policy,
        semantic_state={key: value for key, value in previous_semantic_state.items() if key != "evidence_coverage"},
        new_evidence=evidence_delta,
        crm_delta=crm_delta,
    )
    semantic_envelope, semantic_metadata = call_validated_analysis_json(
        semantic_prompt,
        validator=_semantic_envelope_validator(previous_semantic_state, evidence_delta, crm_delta),
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
    semantic_state = semantic_envelope["semantic_state"]
    semantic_state["schema_version"] = SCHEMA_VERSION
    semantic_state["deal_id"] = str(deal_id)
    semantic_state["source_fingerprint"] = str(source_fingerprint)
    semantic_state["evidence_coverage"] = copy.deepcopy(next_evidence_coverage)
    validate_semantic_state_v1(semantic_state)
    changed_domains = semantic_changed_domains(previous_semantic_state, semantic_state)
    affected_sections = resolve_affected_sections(changed_domains, evidence_delta)
    materialization_policy_text = (
        compact_policy_text
        if compact_policy_text and compact_policy is None
        else render_v2_compact_policy(policy_payload, affected_sections=affected_sections)
    )
    materialization_prompt = build_materialization_prompt(
        previous_analysis=previous_analysis,
        semantic_state=semantic_state,
        evidence_delta=evidence_delta,
        affected_sections=affected_sections,
        stage_policy=stage_policy,
        prior_recommendation=prior_recommendation,
        compact_policy_text=materialization_policy_text,
        compact_diagnostics_text=compact_diagnostics_text,
        current_situation_context=current_situation_context,
        daily_quality_context=daily_quality_context,
    )
    structural_templates, continuity_content, constraints = build_materialization_contract(
        previous_analysis, affected_sections
    )
    materialization_budget = _prompt_budget(
        materialization_prompt,
        policy=materialization_policy_text,
        diagnostics=compact_diagnostics_text,
        semantic_state=semantic_state,
        new_evidence=evidence_delta,
        stage_policy=stage_policy,
        prior_recommendation=prior_recommendation,
        structural_templates=structural_templates,
        validation_constraints=constraints,
        previous_continuity_content=continuity_content,
        current_situation_context=current_situation_context or {},
    )
    materialized, materialization_metadata = call_validated_analysis_json(
        materialization_prompt,
        validator=_materialization_validator(set(affected_sections), previous_analysis),
        normalizer=_materialization_normalizer(set(affected_sections), previous_analysis),
        validation_error_types=(AnalysisValidationError,),
        model=model,
        analysis_caller=call_analysis_json,
        call_type="deal_incremental_v2_materialization",
        prompt_cache_key="neuro-rop:deal-incremental-v2:materialization:v2",
        trace_entity_type="deal",
        trace_entity_id=deal_id,
        preview_prompt=False,
        preview_response_errors=False,
        correction_prompt_builder=_materialization_repair_prompt_builder(
            affected_sections=affected_sections,
            structural_templates=structural_templates,
            constraints=constraints,
            compact_policy_text=materialization_policy_text,
        ),
    )
    candidate = copy.deepcopy(previous_analysis)
    candidate.update(materialized["sections"])
    stamp_daily_quality_scope(candidate, daily_quality_context)
    normalize_analysis_for_validation(candidate, truncate_lists=False)
    validate_deal_analysis(candidate)
    metadata_rows = [semantic_metadata, materialization_metadata]
    usage_summary = _usage_summary(metadata_rows)
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
            "usage": usage_summary,
            "estimated_cost": _estimated_cost_summary(metadata_rows, usage_summary),
            "prompt_budget": {"semantic_update": semantic_budget, "materialization": materialization_budget},
            "calls": safe_metadata_rows,
            "_raw_outputs": raw_outputs,
        },
    )
