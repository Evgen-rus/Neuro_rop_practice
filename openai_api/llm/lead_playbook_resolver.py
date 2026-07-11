"""Normalize compact lead playbooks from reliable, structured call outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


BUSY_PLAYBOOK = "retry_busy_number"
INVALID_NUMBER_PLAYBOOK = "verify_invalid_number"
SCHEDULED_NURTURE_FOLLOWUP = "scheduled_nurture_followup"


@dataclass(frozen=True)
class ReliableCallOutcome:
    """A narrowly classified outcome from a dated transcript call section."""

    outcome: str
    evidence_id: str
    occurred_at: datetime | None


@dataclass(frozen=True)
class NurtureSignal:
    """Client-grounded deferred-need signal extracted from sent source text."""

    evidence_id: str | None
    client_date: str | None
    client_time_hint: str | None
    scheduled_task_exists: bool


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


_NURTURE_MARKERS = (
    "\u043e\u0441\u0435\u043d",  # autumn
    "\u0432 \u043a\u043e\u043d\u0446\u0435 \u043b\u0435\u0442\u0430",
    "\u043a \u043a\u043e\u043d\u0446\u0443 \u043b\u0435\u0442\u0430",
    "\u0432 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u043c \u0433\u043e\u0434\u0443",
    "\u043d\u0435 \u0440\u0430\u043d\u044c\u0448\u0435",
    "\u0447\u0435\u0440\u0435\u0437 \u043c\u0435\u0441\u044f\u0446",
    "\u0447\u0435\u0440\u0435\u0437 \u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\u043e \u043c\u0435\u0441\u044f\u0446",
    "\u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u043f\u0443\u0441\u043a\u0430",
    "\u043a\u043e\u0433\u0434\u0430 \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u0431\u044e\u0434\u0436\u0435\u0442",
    "\u0432\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435",
    "\u0441\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435",
)
_REFUSAL_MARKERS = (
    "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043b \u043e\u0442\u043a\u0430\u0437",
    "\u043f\u043e\u043a\u0443\u043f\u0430\u0442\u044c \u043d\u0435 \u0431\u0443\u0434\u0435\u043c",
    "\u043e\u043a\u043e\u043d\u0447\u0430\u0442\u0435\u043b\u044c\u043d\u043e \u043e\u0442\u043a\u0430\u0437\u0430\u043b\u0441\u044f",
)


def _transcript_text_sections(transcript_text: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^### .*activity_id=([^\s]+)\s*$", transcript_text)
    result: list[tuple[str, str]] = []
    for index in range(1, len(parts), 2):
        evidence_id = parts[index].strip()
        section = parts[index + 1]
        match = re.search(r"(?ms)^```text\s*\n(.*?)^```", section)
        if evidence_id and match:
            result.append((evidence_id, match.group(1).lower()))
    return result


def _nurture_signal(history_text: str, transcript_text: str) -> NurtureSignal | None:
    """Require a deferred-need phrase from a client-facing transcript/history source.

    An internal note alone never enables nurture.  A scheduled CRM task can
    prove correct processing only after a client-grounded deferred-need signal
    already exists.
    """
    scheduled_task = bool(re.search(r"(?im)(?:type=task|\|\s*task\s*\|)", history_text))
    for evidence_id, body in _transcript_text_sections(transcript_text):
        if any(marker in body for marker in _NURTURE_MARKERS):
            date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", body)
            hint = next((marker for marker in _NURTURE_MARKERS if marker in body), None)
            return NurtureSignal(evidence_id, date_match.group(1) if date_match else None, hint, scheduled_task)
    for line in history_text.splitlines():
        lowered = line.lower()
        if "internal_comment" in lowered or "\u0432\u043d\u0443\u0442\u0440\u0435\u043d\u043d" in lowered:
            continue
        if any(marker in lowered for marker in _NURTURE_MARKERS):
            evidence_match = re.search(r"\bid=([A-Za-z0-9:_-]+)\b", line)
            date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", line)
            hint = next((marker for marker in _NURTURE_MARKERS if marker in lowered), None)
            return NurtureSignal(
                evidence_match.group(1) if evidence_match else None,
                date_match.group(1) if date_match else None,
                hint,
                scheduled_task,
            )
    return None


def _has_confirmed_refusal(history_text: str, transcript_text: str) -> bool:
    sources = [body for _id, body in _transcript_text_sections(transcript_text)]
    sources.extend(
        line.lower()
        for line in history_text.splitlines()
        if "internal_comment" not in line.lower() and "\u0432\u043d\u0443\u0442\u0440\u0435\u043d\u043d" not in line.lower()
    )
    return any(any(marker in source for marker in _REFUSAL_MARKERS) for source in sources)


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
    if not isinstance(review, dict):
        return result, audit
    nurture = _nurture_signal(history_text, transcript_text)
    if (
        review.get("final_verdict") == "needs_nurture"
        and review.get("meaningful_contact") is True
        and nurture is not None
        and not _has_confirmed_refusal(history_text, transcript_text)
    ):
        normalized_review = dict(review)
        normalized_review["action_playbook"] = SCHEDULED_NURTURE_FOLLOWUP
        result["lead_review"] = normalized_review
        if isinstance(action, dict):
            evidence_ids = [nurture.evidence_id] if nurture.evidence_id else []
            evidence_ids.extend(str(item) for item in action.get("evidence_ids", []) if str(item) not in evidence_ids)
            result["rop_action"] = {**action, "evidence_ids": evidence_ids[:7]}
        result["_nurture_context"] = {
            "client_date": nurture.client_date,
            "client_time_hint": nurture.client_time_hint,
            "scheduled_task_exists": nurture.scheduled_task_exists,
        }
        audit.update(
            {
                "normalized_action_playbook": SCHEDULED_NURTURE_FOLLOWUP,
                "normalization_reason": "grounded_meaningful_contact_needs_nurture",
                "normalization_evidence_ids": [nurture.evidence_id] if nurture.evidence_id else [],
            }
        )
        return result, audit
    if not isinstance(action, dict):
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
