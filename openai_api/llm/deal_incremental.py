"""Deterministic preparation of a safe V1 incremental deal-analysis context."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from openai_api.change_detection.snapshot import activity_kind, result_item, result_items
from openai_api.llm.deal_evidence import transcript_evidence_ids_for_input
from openai_api.llm.validation import normalize_analysis_for_validation, validate_deal_analysis


class IncrementalContextError(ValueError):
    """The current delta cannot be proven complete enough for incremental analysis."""


def previous_business_analysis(last_analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(last_analysis, dict):
        raise IncrementalContextError("previous_analysis_missing")
    value = last_analysis.get("analysis")
    analysis = value if isinstance(value, dict) else last_analysis
    if not isinstance(analysis, dict):
        raise IncrementalContextError("previous_analysis_not_object")
    projected = copy.deepcopy(analysis)
    try:
        normalize_analysis_for_validation(projected)
        validate_deal_analysis(projected)
    except Exception as error:
        raise IncrementalContextError("previous_analysis_invalid") from error
    return projected


def _activity_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, container in (("deal", bundle), ("source_lead", bundle.get("source_lead") or {})):
        details = container.get("activity_details") or {}
        for activity in result_items(container.get("activities")):
            activity_id = str(activity.get("ID") or "")
            detail_container = details.get(activity_id) if isinstance(details, dict) else None
            detail = result_item(detail_container) if isinstance(detail_container, dict) else {}
            merged = {**activity, **detail} if detail else activity
            rows.append({
                "id": activity_id,
                "source": source,
                "kind": activity_kind(merged),
                "direction": str(merged.get("DIRECTION") or ""),
                "subject": merged.get("SUBJECT"),
                "description": merged.get("DESCRIPTION"),
                "created": merged.get("CREATED") or "",
                "last_updated": merged.get("LAST_UPDATED") or "",
                "completed": str(merged.get("COMPLETED") or ""),
                "status": str(merged.get("STATUS") or ""),
                "deadline": merged.get("DEADLINE") or "",
            })
    return rows


def _is_inbound(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "in", "incoming", "входящий"}


def build_incremental_context(
    *,
    previous_state: dict[str, Any] | None,
    previous_snapshot: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    diff: dict[str, Any],
    raw_bundle: dict[str, Any],
    transcript_path: Path | None,
) -> dict[str, Any]:
    previous_analysis = previous_business_analysis((previous_state or {}).get("last_analysis"))
    changes = set(diff.get("changes") or [])
    details = diff.get("details") or {}
    new_events: list[dict[str, Any]] = []
    evidence_ids_included: set[str] = set()

    if "transcript_changed" in changes:
        previous_transcript = (previous_snapshot or {}).get("transcript") or {}
        current_transcript = current_snapshot.get("transcript") or {}
        previous_path = str(previous_transcript.get("path") or "")
        current_path = str(current_transcript.get("path") or "")
        if not transcript_path or not transcript_path.exists():
            raise IncrementalContextError("new_transcript_unavailable")
        if previous_path and previous_path == current_path:
            raise IncrementalContextError("transcript_changed_in_place")
        transcript_evidence_ids = transcript_evidence_ids_for_input(
            transcript_path.parent,
            deal_id=str(current_snapshot.get("deal", {}).get("id") or raw_bundle.get("deal_id") or ""),
            transcript_path=transcript_path,
        )
        if not transcript_evidence_ids:
            raise IncrementalContextError("transcript_evidence_identity_missing")
        evidence_ids_included.update(transcript_evidence_ids)
        new_events.append({
            "type": "transcript",
            "evidence_ids": transcript_evidence_ids,
            "source_path_name": transcript_path.name,
            "text": transcript_path.read_text(encoding="utf-8"),
        })

    new_ids = {str(value) for value in details.get("new_activity_ids") or []}
    activity_rows = _activity_rows(raw_bundle)
    for activity in activity_rows:
        if activity["id"] in new_ids and activity["kind"] in {"email", "message"} and _is_inbound(activity["direction"]):
            evidence_id = f"{activity['kind']}:{activity['id']}"
            evidence_ids_included.add(evidence_id)
            new_events.append({"type": "inbound_communication", "evidence_id": evidence_id, **activity})

    if not new_events:
        raise IncrementalContextError("no_materialized_new_events")

    current_deal = current_snapshot.get("deal") or {}
    delta_activity_ids = set(new_ids)
    for key in ("updated_activity_ids", "task_deadline_changed_ids", "task_completed_changed_ids"):
        delta_activity_ids.update(str(value) for value in details.get(key) or [])
    crm_delta = {
        "change_types": sorted(changes),
        "details": details,
        "current_deal": {
            key: current_deal.get(key)
            for key in ("id", "stage_id", "category_id", "opportunity", "currency_id", "assigned_by_id", "closed", "moved_time")
        },
        "activities": [row for row in activity_rows if row["id"] in delta_activity_ids],
    }
    return {
        "previous_analysis": previous_analysis,
        "new_events": new_events,
        "evidence_ids_included": sorted(evidence_ids_included),
        "crm_delta": crm_delta,
        "context_diagnostics": {
            "baseline_kind": "last_successful_analysis",
            "raw_history_available_for_full_fallback": True,
            "old_events_intentionally_omitted": True,
            "instruction": "Не считай отсутствие старых raw-событий доказательством того, что их не было.",
        },
    }
