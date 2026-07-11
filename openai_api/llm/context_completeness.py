"""Compact, non-factual diagnostics for attention-delta shadow prompts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json_diagnostics(paths: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for value in paths:
        path = Path(value)
        if path.suffix.lower() != ".json" or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if not context and isinstance(payload.get("summary"), dict):
            context = payload
        candidates = payload.get("gaps") or payload.get("items") or []
        if not isinstance(candidates, list):
            continue
        for gap in candidates:
            if not isinstance(gap, dict):
                continue
            key = (str(gap.get("source") or ""), str(gap.get("activity_id") or ""), str(gap.get("reason") or ""))
            if key not in seen:
                seen.add(key)
                gaps.append(gap)
    return context, gaps


def _source_state(*, available: bool, partial: bool = False) -> str:
    if not available:
        return "unavailable"
    return "partial" if partial else "available"


def build_context_completeness_block(
    diagnostics_paths: list[str],
    *,
    history_available: bool,
    transcript_available: bool,
    stage_policy_available: bool | None,
) -> str:
    """Build the only diagnostics text sent to the compact model.

    Diagnostics constrain evidence use but never assert facts about a client.
    Paths, commands and recovery instructions intentionally remain local.
    """
    context, gaps = _read_json_diagnostics(diagnostics_paths)
    summary = context.get("summary") if isinstance(context.get("summary"), dict) else {}
    expected_calls = sum(
        value for value in (summary.get("crm_calls_found"), summary.get("related_crm_calls_found")) if isinstance(value, int)
    )
    expected_calls_value: int | None = expected_calls if expected_calls else None
    loaded_ids = summary.get("transcript_activity_ids_found") if isinstance(summary.get("transcript_activity_ids_found"), list) else []
    loaded_calls_value: int | None = len(loaded_ids) if expected_calls_value is not None else None
    missing_calls = sum(
        value for value in (summary.get("calls_without_transcript"), summary.get("related_calls_without_transcript")) if isinstance(value, int)
    )
    missing_calls_value: int | None = missing_calls if expected_calls_value is not None else None

    gap_sources = {str(gap.get("source") or "") for gap in gaps}
    transcript_partial = bool(missing_calls_value) or any(source.startswith("call_") for source in gap_sources)
    history_partial = "contact_resolution" in gap_sources
    comments_unavailable = "task_comments" in gap_sources or "crm_comments" in gap_sources
    tasks_unavailable = any(source in {"crm_tasks", "tasks"} for source in gap_sources)

    history_state = _source_state(available=history_available, partial=history_partial)
    transcript_state = _source_state(available=transcript_available, partial=transcript_partial)
    comments_state = "unavailable" if comments_unavailable else "unknown"
    tasks_state = "unavailable" if tasks_unavailable else "unknown"
    policy_state = "not_applicable" if stage_policy_available is None else _source_state(available=stage_policy_available)

    raw_status = str(context.get("context_completeness") or "").lower()
    has_critical_gap = bool(context.get("critical_missing")) or any(
        str(gap.get("severity") or "").lower() == "critical" for gap in gaps
    )
    if not history_available or not transcript_available:
        status = "insufficient"
        manual_review_required = True
        manual_review_reason = "required history or transcript source is unavailable"
    elif raw_status == "complete" and not has_critical_gap and not (
        transcript_partial or history_partial or comments_unavailable or tasks_unavailable
    ):
        status = "complete"
        manual_review_required = False
        manual_review_reason = "null"
    elif raw_status in {"partial", "insufficient"} or has_critical_gap or (
        transcript_partial or history_partial or comments_unavailable or tasks_unavailable
    ):
        status = "partial"
        manual_review_required = False
        manual_review_reason = "null"
    else:
        status = "complete"
        manual_review_required = False
        manual_review_reason = "null"

    expected = str(expected_calls_value) if expected_calls_value is not None else "null"
    loaded = str(loaded_calls_value) if loaded_calls_value is not None else "null"
    missing = str(missing_calls_value) if missing_calls_value is not None else "null"
    return "\n".join(
        (
            "## CONTEXT_COMPLETENESS",
            f"status: {status}",
            f"sources: history={history_state}; transcript={transcript_state}; crm_comments={comments_state}; crm_tasks={tasks_state}; stage_policy={policy_state}",
            f"transcript_coverage: expected_calls={expected}; loaded_calls={loaded}; missing_calls={missing}",
            "constraints: missing sources are not evidence of absence; do not infer client facts from diagnostics; use only evidence IDs visible in supplied history/transcript; request manual review only when the decision depends on a missing source",
            f"manual_review_required: {str(manual_review_required).lower()}",
            f"manual_review_reason: {manual_review_reason}",
        )
    )
