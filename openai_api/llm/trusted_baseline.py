from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Collection

from openai_api.change_detection.decision_engine import (
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
)
from openai_api.llm.validation import AnalysisValidationError, validate_deal_analysis
from storage.rop_db import get_analysis_run, get_entity_state


_TRUSTED_STATUSES = {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS}


def _valid_evidence_coverage(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for evidence_id, item in value.items():
        if not str(evidence_id).strip() or not isinstance(item, dict):
            return False
        if not str(item.get("content_hash") or "").strip():
            return False
        try:
            if int(item.get("revision") or 0) < 1:
                return False
        except (TypeError, ValueError):
            return False
    return True


def get_trusted_deal_baseline(
    db_path: str | Path,
    deal_id: str,
    *,
    compatible_prompt_versions: Collection[str],
    expected_logic_version: str,
) -> dict[str, Any] | None:
    prompt_versions = {str(value) for value in compatible_prompt_versions}
    state = get_entity_state(db_path, "deal", str(deal_id))
    payload = state.get("last_analysis") if state else None
    run_id = payload.get("analysis_run_id") if isinstance(payload, dict) else None
    run = get_analysis_run(db_path, run_id) if isinstance(run_id, int) else None
    if not run or not state or run.get("status") not in _TRUSTED_STATUSES:
        return None
    fingerprint = str(run.get("fingerprint") or "").strip()
    provenance = run.get("provenance")
    evidence_ids = run.get("evidence_ids_included")
    evidence_coverage = run.get("evidence_coverage")
    canonical_state = run.get("canonical_state")
    canonical_fingerprint = str(run.get("canonical_fingerprint") or "").strip()
    analysis_mode = payload.get("analysis_mode") if isinstance(payload, dict) else None
    coverage_ids_match = (
        set(map(str, evidence_ids)) == set(evidence_coverage)
        if analysis_mode == "full" and isinstance(evidence_ids, list) and isinstance(evidence_coverage, dict)
        else set(map(str, evidence_ids)) <= set(evidence_coverage)
        if analysis_mode == "incremental" and isinstance(evidence_ids, list) and isinstance(evidence_coverage, dict)
        else False
    )
    if (
        not fingerprint
        or run.get("prompt_version") not in prompt_versions
        or run.get("logic_version") != expected_logic_version
        or not isinstance(provenance, dict)
        or provenance.get("snapshot_fingerprint") != fingerprint
        or not isinstance(evidence_ids, list)
        or not _valid_evidence_coverage(evidence_coverage)
        or not coverage_ids_match
        or not isinstance(canonical_state, dict)
        or canonical_state.get("schema_id") != "canonical_bitrix_state"
        or canonical_state.get("schema_version") != "1"
        or canonical_state.get("owner") != {"entity_type": "deal", "entity_id": str(deal_id)}
        or canonical_state.get("semantic_fingerprint") != canonical_fingerprint
        or not isinstance(payload, dict)
        or payload.get("analysis_run_id") != run.get("id")
        or analysis_mode not in {"full", "incremental"}
    ):
        return None
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        return None
    try:
        validate_deal_analysis(deepcopy(analysis))
    except (AnalysisValidationError, TypeError, ValueError):
        return None
    return {
        "analysis_run_id": run["id"],
        "analysis": deepcopy(analysis),
        "fingerprint": fingerprint,
        "canonical_fingerprint": canonical_fingerprint,
        "canonical_state": deepcopy(canonical_state),
        "prompt_version": run["prompt_version"],
        "logic_version": run["logic_version"],
        "provenance": deepcopy(provenance),
        "evidence_coverage": deepcopy(evidence_coverage),
    }
