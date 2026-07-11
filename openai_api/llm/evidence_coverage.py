"""Validate that compact evidence IDs exist in the exact prompt source blocks."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


# These are stable CRM references used by the current compact contexts.  The
# parser deliberately accepts IDs only in a typed source position, never from
# arbitrary digit sequences in prose.
_NAMESPACE_ID = re.compile(r"\b(?:im|lead|deal|activity|call|task|comment):[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*\b")
_KEYED_ID = re.compile(r"\b(?:activity_id|task_id|comment_id|call_id|id)\s*[=:]\s*([A-Za-z0-9:_-]+)\b", re.IGNORECASE)
_HISTORY_ROW = re.compile(r"(?m)^\|[^\n|]*\|[^\n|]*\|[^\n|]*\|\s*([A-Za-z0-9:_-]+)\s*\|")
_TRANSCRIPT_SECTION = re.compile(
    r"(?ms)^### .*?activity_id=([A-Za-z0-9:_-]+)\s*$.*?^```(?:text)?\s*\n.*?^```"
)


def _typed_ids(text: str) -> set[str]:
    ids = set(_NAMESPACE_ID.findall(text))
    ids.update(_KEYED_ID.findall(text))
    ids.update(_HISTORY_ROW.findall(text))
    return {value for value in ids if value}


def _structured_ids(value: Any, *, key: str | None = None) -> set[str]:
    """Read IDs only from explicitly named structured-ID fields."""
    ids: set[str] = set()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            ids.update(_structured_ids(child_value, key=str(child_key)))
    elif isinstance(value, list):
        for item in value:
            ids.update(_structured_ids(item, key=key))
    elif isinstance(value, (str, int)) and key and key.lower() in {
        "id", "entity_id", "lead_id", "deal_id", "activity_id", "task_id", "comment_id", "call_id"
    }:
        candidate = str(value).strip()
        if candidate and re.fullmatch(r"[A-Za-z0-9:_-]+", candidate):
            ids.add(candidate)
            if key.lower() in {"lead_id", "deal_id"}:
                ids.add(f"{key.lower()[:-3]}:{candidate}")
    return ids


def collect_evidence_ids(value: Any) -> list[str]:
    """Collect all nested evidence_ids arrays after deterministic materialization."""
    collected: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ids" and isinstance(item, list):
                collected.extend(str(entry).strip() for entry in item if str(entry).strip())
            else:
                collected.extend(collect_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            collected.extend(collect_evidence_ids(item))
    return list(dict.fromkeys(collected))


def build_evidence_inventory(
    *,
    history_text: str,
    transcript_text: str,
    stage_policy: dict[str, Any] | None = None,
    structured_blocks: Iterable[Any] = (),
) -> dict[str, set[str]]:
    """Return source-aware evidence inventory for blocks actually sent to model."""
    transcript_text_ids = {match.group(1) for match in _TRANSCRIPT_SECTION.finditer(transcript_text)}
    transcript_ids = _typed_ids(transcript_text)
    index_only = transcript_ids - transcript_text_ids
    covered = _typed_ids(history_text) | transcript_text_ids
    covered.update(_structured_ids(stage_policy or {}))
    for block in structured_blocks:
        covered.update(_structured_ids(block))
        if isinstance(block, str):
            covered.update(_typed_ids(block))
    return {"covered": covered, "index_only": index_only}


def validate_evidence_context_coverage(
    value: dict[str, Any],
    *,
    history_text: str,
    transcript_text: str,
    stage_policy: dict[str, Any] | None = None,
    structured_blocks: Iterable[Any] = (),
) -> dict[str, Any]:
    """Validate all nested evidence IDs against source blocks present in prompt.

    IDs present only in a transcript index are reported separately.  They do
    not ground client/commercial facts because the underlying source text was
    not included in the compact prompt.
    """
    referenced = collect_evidence_ids(value)
    inventory = build_evidence_inventory(
        history_text=history_text,
        transcript_text=transcript_text,
        stage_policy=stage_policy,
        structured_blocks=structured_blocks,
    )
    covered = [evidence_id for evidence_id in referenced if evidence_id in inventory["covered"]]
    index_only = [evidence_id for evidence_id in referenced if evidence_id in inventory["index_only"] and evidence_id not in covered]
    missing = [evidence_id for evidence_id in referenced if evidence_id not in covered and evidence_id not in index_only]
    action_required = value.get("attention_required") is True and isinstance(value.get("rop_action"), dict)
    if missing or index_only or (action_required and not referenced):
        status = "failed"
    else:
        status = "passed"
    coverage_percent = 100 if not referenced else round(100 * len(covered) / len(referenced))
    return {
        "status": status,
        "referenced_ids": referenced,
        "covered_ids": covered,
        "missing_ids": missing if referenced else (["required_action_evidence"] if action_required else []),
        "index_only_ids": index_only,
        "coverage_percent": coverage_percent,
    }
