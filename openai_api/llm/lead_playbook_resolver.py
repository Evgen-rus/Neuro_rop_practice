"""Normalize compact lead playbooks from reliable, structured call outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


BUSY_PLAYBOOK = "retry_busy_number"
INVALID_NUMBER_PLAYBOOK = "verify_invalid_number"


@dataclass(frozen=True)
class ReliableCallOutcome:
    """A narrowly classified outcome from a dated transcript call section."""

    outcome: str
    evidence_id: str
    occurred_at: datetime | None


def _parse_when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _contains_all(text: str, *phrases: str) -> bool:
    lowered = " ".join(text.lower().split())
    return all(phrase in lowered for phrase in phrases)


def _classify_transcript_outcome(text: str) -> str | None:
    """Classify only known system-result signatures inside a call transcript.

    This deliberately does not search arbitrary CRM history or comments for a
    single word such as "занято". A result is reliable only inside a dated
    transcript section associated with a concrete activity ID.
    """
    if _contains_all(text, "номер не существует") or _contains_all(text, "неверно набран номер"):
        return "invalid_number"
    if _contains_all(text, "линия занята", "голосовой ассистент"):
        return "busy"
    if _contains_all(text, "абонент не отвечает") or _contains_all(text, "нет ответа"):
        return "no_answer"
    if _contains_all(text, "абонент недоступен"):
        return "unavailable"
    if _contains_all(text, "голосовая почта"):
        return "voicemail"
    return None


def extract_reliable_call_outcomes(transcript_text: str) -> list[ReliableCallOutcome]:
    """Extract outcomes from the structured all-calls transcript format only."""
    parts = re.split(r"(?m)^### .*activity_id=([^\s]+)\s*$", transcript_text)
    outcomes: list[ReliableCallOutcome] = []
    for index in range(1, len(parts), 2):
        evidence_id = parts[index].strip()
        section = parts[index + 1]
        when_match = re.search(r"(?m)^- Дата звонка:\s*(.+?)\s*$", section)
        transcript_match = re.search(r"(?ms)^```text\s*\n(.*?)^```", section)
        if not evidence_id or not when_match or not transcript_match:
            continue
        outcome = _classify_transcript_outcome(transcript_match.group(1))
        if outcome:
            outcomes.append(
                ReliableCallOutcome(
                    outcome=outcome,
                    evidence_id=evidence_id,
                    occurred_at=_parse_when(when_match.group(1)),
                )
            )
    return outcomes


def latest_reliable_call_outcome(outcomes: list[ReliableCallOutcome]) -> ReliableCallOutcome | None:
    """Use the latest dated outcome; undated outcomes never override a dated one."""
    dated = [outcome for outcome in outcomes if outcome.occurred_at is not None]
    candidates = dated or outcomes
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda outcome: (
            outcome.occurred_at or datetime.min,
            1 if outcome.outcome == "invalid_number" else 0,
            outcome.evidence_id,
        ),
    )


def normalize_lead_action_playbook(
    value: dict[str, Any],
    *,
    history_text: str,
    transcript_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a safe playbook correction after Structured Output.

    The resolver does not infer client facts. It only prefers a latest reliable
    busy or invalid-number outcome over a generic no-contact playbook.
    """
    result = dict(value)
    review = result.get("lead_review")
    action = result.get("rop_action")
    raw_playbook = review.get("action_playbook") if isinstance(review, dict) else None
    audit: dict[str, Any] = {
        "raw_action_playbook": raw_playbook,
        "normalized_action_playbook": raw_playbook,
        "normalization_reason": None,
        "normalization_evidence_ids": [],
    }
    if not isinstance(review, dict) or not isinstance(action, dict):
        return result, audit
    if review.get("meaningful_contact") is not False:
        return result, audit

    latest = latest_reliable_call_outcome(extract_reliable_call_outcomes(transcript_text))
    if latest is None or latest.evidence_id not in f"{history_text}\n{transcript_text}":
        return result, audit

    normalized_playbook: str | None = None
    reason: str | None = None
    if latest.outcome == "invalid_number":
        normalized_playbook = INVALID_NUMBER_PLAYBOOK
        reason = "latest_reliable_call_outcome_invalid_number"
    elif latest.outcome == "busy":
        normalized_playbook = BUSY_PLAYBOOK
        reason = "latest_reliable_call_outcome_busy"
    if normalized_playbook is None:
        return result, audit

    normalized_review = dict(review)
    normalized_review["action_playbook"] = normalized_playbook
    normalized_action = dict(action)
    previous_evidence = normalized_action.get("evidence_ids")
    evidence_ids = [latest.evidence_id]
    if isinstance(previous_evidence, list):
        evidence_ids.extend(str(item) for item in previous_evidence if str(item) != latest.evidence_id)
    normalized_action["evidence_ids"] = evidence_ids[:7]
    result["lead_review"] = normalized_review
    result["rop_action"] = normalized_action
    audit.update(
        {
            "normalized_action_playbook": normalized_playbook,
            "normalization_reason": reason,
            "normalization_evidence_ids": [latest.evidence_id],
        }
    )
    return result, audit
