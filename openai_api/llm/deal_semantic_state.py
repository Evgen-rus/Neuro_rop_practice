"""Versioned compact semantic memory for deal incremental V2."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


SCHEMA_VERSION = "deal-semantic-state-v1"
CONTINUITY_LISTS = (
    "critical_facts",
    "commitments",
    "turning_points",
    "active_pain_points",
    "active_pressure_levers",
    "open_questions",
)


class SemanticStateValidationError(ValueError):
    pass


def _stable_item_id(prefix: str, item: Any) -> str:
    if isinstance(item, dict):
        for key in ("fact", "commitment", "event", "pain", "lever", "question", "summary", "text"):
            if item.get(key):
                value = item[key]
                break
        else:
            value = item
    else:
        value = item
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return f"{prefix}_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"


def _with_ids(prefix: str, values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values if isinstance(values, list) else []:
        row = copy.deepcopy(value) if isinstance(value, dict) else {"text": value}
        row.setdefault("id", _stable_item_id(prefix, row))
        result.append(row)
    return result


def _project_items(
    prefix: str,
    values: Any,
    *,
    fields: tuple[str, ...],
    id_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values if isinstance(values, list) else []:
        source = value if isinstance(value, dict) else {"text": value}
        row = {key: copy.deepcopy(source[key]) for key in fields if key in source}
        existing_id = next((str(source.get(key) or "").strip() for key in id_fields if source.get(key)), "")
        row["id"] = existing_id or _stable_item_id(prefix, row)
        result.append(row)
    return result


def _without_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_evidence(item)
            for key, item in value.items()
            if key not in {"evidence", "evidence_quote", "quotes"}
        }
    if isinstance(value, list):
        return [_without_evidence(item) for item in value]
    return copy.deepcopy(value)


def bootstrap_semantic_state(
    analysis: dict[str, Any],
    *,
    deal_id: str,
    source_analysis_run_id: int | None,
    source_fingerprint: str,
    evidence_coverage: dict[str, Any],
) -> dict[str, Any]:
    context = analysis.get("deal_context") if isinstance(analysis.get("deal_context"), dict) else {}
    state = {
        "schema_version": SCHEMA_VERSION,
        "deal_id": str(deal_id),
        "source_analysis_run_id": source_analysis_run_id,
        "source_fingerprint": str(source_fingerprint),
        "evidence_coverage": copy.deepcopy(evidence_coverage),
        "current_truth": copy.deepcopy(context.get("current_truth") or {}),
        "critical_facts": _project_items(
            "fact", context.get("critical_facts"),
            fields=("category", "fact", "importance", "observed_at", "source_type", "status"),
            id_fields=("fact_id", "id"),
        ),
        "commitments": _project_items(
            "commitment", context.get("commitments"),
            fields=("basis_status", "due_at", "party", "promise", "status"),
            id_fields=("commitment_id", "id"),
        ),
        "decision_path": _without_evidence(context.get("decision_path") or {}),
        "open_questions": _with_ids("question", context.get("open_questions")),
        "source_conflicts": copy.deepcopy(context.get("source_conflicts") or []),
        "qualification": _without_evidence(analysis.get("qualification_assessment") or {}),
        "commercial_state": {
            key: copy.deepcopy((analysis.get("price_comparability_check") or {}).get(key))
            for key in ("applicable", "price_gap_signal", "summary", "what_is_unclear")
            if key in (analysis.get("price_comparability_check") or {})
        },
        "payment_state": {
            key: copy.deepcopy((analysis.get("payment_blocker") or {}).get(key))
            for key in ("applicable", "blocker_type", "confirmed_payment_date", "current_status", "payer", "payment_recipient")
            if key in (analysis.get("payment_blocker") or {})
        },
        "competitor_state": {
            key: copy.deepcopy((analysis.get("competitor_defense_checklist") or {}).get(key))
            for key in ("applicable", "competitor_type", "risk_if_not_defended")
            if key in (analysis.get("competitor_defense_checklist") or {})
        },
        "money_path": _without_evidence(analysis.get("money_path_diagnosis") or {}),
        "risk_state": copy.deepcopy(analysis.get("main_risk") or {}),
        "turning_points": _project_items(
            "turn", context.get("turning_points"),
            fields=("impact", "occurred_at", "status", "title", "what_happened"),
            id_fields=("turning_point_id", "id"),
        ),
        "active_pain_points": _project_items(
            "pain", context.get("pain_points"),
            fields=("description", "impact", "status", "title"),
            id_fields=("pain_id", "id"),
        ),
        "active_pressure_levers": _project_items(
            "lever", context.get("pressure_levers"),
            fields=("ai_priority", "basis_status", "business_consequence", "fact", "status", "title", "type"),
            id_fields=("lever_id", "id"),
        ),
        "communication_profile": {
            key: copy.deepcopy((analysis.get("client_communication_profile") or {}).get(key))
            for key in ("status", "primary_style", "secondary_style", "profile_confidence", "role_separation_confidence", "insufficient_reason")
            if key in (analysis.get("client_communication_profile") or {})
        },
        "continuity_summary": {
            "deal_state": {
                key: copy.deepcopy((analysis.get("deal_state") or {}).get(key))
                for key in ("stage", "amount", "summary") if key in (analysis.get("deal_state") or {})
            },
            "deal_mode": {
                key: copy.deepcopy((analysis.get("deal_mode") or {}).get(key))
                for key in ("mode", "reason") if key in (analysis.get("deal_mode") or {})
            },
        },
    }
    validate_semantic_state_v1(state)
    return state


def validate_semantic_state_v1(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise SemanticStateValidationError("semantic state must be an object")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise SemanticStateValidationError("unsupported semantic schema_version")
    if not str(state.get("deal_id") or "").strip():
        raise SemanticStateValidationError("deal_id is required")
    coverage = state.get("evidence_coverage")
    if not isinstance(coverage, dict):
        raise SemanticStateValidationError("evidence_coverage must be an object")
    for evidence_id, value in coverage.items():
        if not str(evidence_id).strip() or not isinstance(value, dict) or not str(value.get("content_hash") or ""):
            raise SemanticStateValidationError("invalid evidence coverage entry")
        if int(value.get("revision") or 0) < 1:
            raise SemanticStateValidationError("evidence revision must be positive")
    for key in CONTINUITY_LISTS:
        values = state.get(key)
        if not isinstance(values, list):
            raise SemanticStateValidationError(f"{key} must be a list")
        ids = [str(item.get("id") or "") for item in values if isinstance(item, dict)]
        if len(ids) != len(values) or any(not value for value in ids) or len(set(ids)) != len(ids):
            raise SemanticStateValidationError(f"{key} requires unique stable ids")
    for key in (
        "current_truth", "decision_path", "qualification", "commercial_state", "payment_state",
        "competitor_state", "money_path", "risk_state", "communication_profile", "continuity_summary",
    ):
        if not isinstance(state.get(key), dict):
            raise SemanticStateValidationError(f"{key} must be an object")
    json.dumps(state, ensure_ascii=False)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items()) if key not in {"source_analysis_run_id", "source_fingerprint"}}
    if isinstance(value, list):
        return sorted((_canonical(item) for item in value), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip()).lower()
    return value


def semantic_changed_domains(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    ignored = {"schema_version", "deal_id", "source_analysis_run_id", "source_fingerprint", "evidence_coverage"}
    domains: list[str] = []
    for key in sorted((set(previous) | set(current)) - ignored):
        if _canonical(previous.get(key)) != _canonical(current.get(key)):
            domains.append(key)
    return domains
