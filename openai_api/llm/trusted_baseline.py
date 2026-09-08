from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from openai_api.change_detection.decision_engine import (
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
)
from openai_api.llm.validation import AnalysisValidationError, validate_deal_analysis
from storage.rop_db import get_entity_state, get_latest_analysis_run


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
    expected_prompt_version: str,
    expected_logic_version: str,
) -> dict[str, Any] | None:
    run = get_latest_analysis_run(
        db_path,
        entity_type="deal",
        entity_id=str(deal_id),
        statuses=tuple(_TRUSTED_STATUSES),
    )
    state = get_entity_state(db_path, "deal", str(deal_id))
    if not run or not state or run.get("status") not in _TRUSTED_STATUSES:
        return None
    fingerprint = str(run.get("fingerprint") or "").strip()
    provenance = run.get("provenance")
    evidence_ids = run.get("evidence_ids_included")
    evidence_coverage = run.get("evidence_coverage")
    canonical_state = run.get("canonical_state")
    canonical_fingerprint = str(run.get("canonical_fingerprint") or "").strip()
    payload = state.get("last_analysis")
    if (
        not fingerprint
        or run.get("prompt_version") != expected_prompt_version
        or run.get("logic_version") != expected_logic_version
        or not isinstance(provenance, dict)
        or provenance.get("snapshot_fingerprint") != fingerprint
        or not isinstance(evidence_ids, list)
        or not _valid_evidence_coverage(evidence_coverage)
        or set(map(str, evidence_ids)) != set(evidence_coverage)
        or not isinstance(canonical_state, dict)
        or canonical_state.get("schema_id") != "canonical_bitrix_state"
        or canonical_state.get("schema_version") != "1"
        or canonical_state.get("owner") != {"entity_type": "deal", "entity_id": str(deal_id)}
        or canonical_state.get("semantic_fingerprint") != canonical_fingerprint
        or not isinstance(payload, dict)
        or payload.get("analysis_run_id") != run.get("id")
        or payload.get("analysis_mode") not in {"full", "incremental"}
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
