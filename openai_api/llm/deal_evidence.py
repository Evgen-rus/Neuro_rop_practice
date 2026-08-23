"""Business-evidence identity for Incremental Deal Analysis V2.

The identity is CRM activity based. File paths and mtimes are intentionally not
part of it, so aggregate and individual transcript representations converge on
the same ``call:<activity_id>`` evidence.
"""

from __future__ import annotations

from typing import Any

from openai_api.audio.transcript_context import transcript_items
from openai_api.change_detection.snapshot import activity_kind, result_item, result_items, text_hash


class EvidenceDeltaError(ValueError):
    """The available sources cannot prove a safe V2 evidence delta."""


def _activity_rows(raw_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, container in (("deal", raw_bundle), ("source_lead", raw_bundle.get("source_lead") or {})):
        details = container.get("activity_details") or {}
        for activity in result_items(container.get("activities")):
            activity_id = str(activity.get("ID") or "").strip()
            if not activity_id:
                continue
            detail_container = details.get(activity_id) if isinstance(details, dict) else None
            detail = result_item(detail_container) if isinstance(detail_container, dict) else {}
            merged = {**activity, **detail} if detail else activity
            rows.append({
                "activity_id": activity_id,
                "source": source,
                "kind": activity_kind(merged),
                "direction": str(merged.get("DIRECTION") or ""),
                "occurred_at": merged.get("START_TIME") or merged.get("CREATED") or "",
                "text": str(merged.get("DESCRIPTION") or "").strip(),
                "subject": str(merged.get("SUBJECT") or "").strip(),
            })
    return rows


def _is_inbound(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "in", "incoming", "входящий"}


def collect_deal_evidence(raw_bundle: dict[str, Any], transcripts_dir: Any) -> list[dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for item in transcript_items(transcripts_dir, "deal", str(raw_bundle.get("deal_id") or "")):
        activity_id = str(item.get("activity_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not activity_id or not text:
            continue
        evidence_id = f"call:{activity_id}"
        evidence[evidence_id] = {
            "evidence_id": evidence_id,
            "kind": "call_transcript",
            "activity_id": activity_id,
            "occurred_at": str(item.get("call_start") or ""),
            "content_hash": text_hash(text),
            "text": text,
        }

    for row in _activity_rows(raw_bundle):
        if row["kind"] not in {"email", "message"} or not _is_inbound(row["direction"]):
            continue
        text = row["text"]
        if not text:
            continue
        prefix = "email" if row["kind"] == "email" else "message"
        evidence_id = f"{prefix}:{row['activity_id']}"
        evidence[evidence_id] = {
            "evidence_id": evidence_id,
            "kind": f"inbound_{prefix}",
            "activity_id": row["activity_id"],
            "occurred_at": str(row["occurred_at"] or ""),
            "content_hash": text_hash(text),
            "subject": row["subject"],
            "text": text,
        }
    return sorted(evidence.values(), key=lambda item: (item["occurred_at"], item["evidence_id"]))


def coverage_from_evidence(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item["evidence_id"]): {
            "content_hash": str(item.get("content_hash") or ""),
            "revision": 1,
            "kind": str(item.get("kind") or ""),
            "occurred_at": str(item.get("occurred_at") or ""),
        }
        for item in items
    }


def evidence_delta(
    current: list[dict[str, Any]],
    previous_coverage: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(previous_coverage, dict):
        raise EvidenceDeltaError("trusted_evidence_coverage_missing")
    next_coverage = {str(key): dict(value) for key, value in previous_coverage.items() if isinstance(value, dict)}
    delta: list[dict[str, Any]] = []
    for item in current:
        evidence_id = str(item.get("evidence_id") or "")
        content_hash = str(item.get("content_hash") or "")
        if not evidence_id or not content_hash:
            raise EvidenceDeltaError("evidence_identity_incomplete")
        old = next_coverage.get(evidence_id)
        if old and str(old.get("content_hash") or "") == content_hash:
            continue
        revision = int((old or {}).get("revision") or 0) + 1
        kind = "evidence_revision" if old else "new_evidence"
        delta.append({**item, "delta_kind": kind, "revision": revision})
        next_coverage[evidence_id] = {
            "content_hash": content_hash,
            "revision": revision,
            "kind": str(item.get("kind") or ""),
            "occurred_at": str(item.get("occurred_at") or ""),
        }
    return delta, next_coverage
