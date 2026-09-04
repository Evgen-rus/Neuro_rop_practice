"""Structured current-situation context for deal analysis.

Builds a compact, deterministic view of the last substantive client contact
and later manager actions from the existing normalized communication/evidence
layer. It is prompt context for FULL/V2, not a separate analysis or LLM call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bitrix.customer_history import (
    INTERNAL_CHANNELS,
    MESSENGER_CHANNELS,
    build_normalized_communications,
    classify_call_outcome,
    clean_text,
    communication_activity_kind,
    raw_activities_by_id,
)
from bitrix.deals.communication_history import include_saved_source_lead_communications
from openai_api.audio.transcript_context import transcript_items
from openai_api.llm.deal_evidence import collect_deal_evidence


CURRENT_SITUATION_CONTEXT_MARKER = "## CURRENT_SITUATION_CONTEXT"
CONTENT_PREVIEW_LIMIT = 500
MANAGER_ACTIONS_LIMIT = 12
CALL_SUBSTANTIVE_MIN_CHARS = 80

# Короткие подтверждения без нового бизнес-факта. Не единственный критерий:
# после удаления этих слов смотрим, остался ли ещё содержательный текст.
_ACK_TOKEN_RE = re.compile(
    r"\b(?:ок|ok|okay|окей|спасибо|благодарю|thanks|thank you|получил|получила|"
    r"понял|поняла|хорошо|принято|супер|ладно)\b",
    flags=re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\wа-яё]+", flags=re.IGNORECASE)


def _empty_context(*, available: bool) -> dict[str, Any]:
    counts: int | None = 0 if available else None
    return {
        "available": available,
        "last_substantive_client_contact": None,
        "later_non_substantive_client_replies": [],
        "manager_actions_after_contact": [],
        "call_attempts_after_contact": counts,
        "outgoing_messages_after_contact": counts,
        "outgoing_emails_after_contact": counts,
        "has_newer_client_response": False,
    }


def is_substantive_client_content(text: str, *, is_call: bool = False) -> bool:
    """Return whether client text can replace the previous situation anchor.

    Calls with a real transcript stay substantive. Short inbound acks like
    «ок» / «спасибо» do not; «получил КП, посмотрю завтра» does, because
    business content remains after ack tokens are stripped.
    """
    cleaned = clean_text(text).strip()
    if not cleaned:
        return False
    if is_call and len(cleaned) >= CALL_SUBSTANTIVE_MIN_CHARS:
        return True
    residual = _ACK_TOKEN_RE.sub(" ", cleaned)
    residual = _PUNCT_RE.sub(" ", residual)
    residual = " ".join(residual.lower().split())
    return bool(residual)


def _preview(text: Any, limit: int = CONTENT_PREVIEW_LIMIT) -> str:
    return clean_text(text, limit)


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return (str(event.get("occurred_at") or ""), str(event.get("event_id") or ""))


def _source_activity_id(event: dict[str, Any]) -> str:
    values = event.get("source_ids") if isinstance(event.get("source_ids"), list) else []
    if values:
        return str(values[0] or "").strip()
    event_id = str(event.get("event_id") or "")
    if event_id.startswith("crm_activity:"):
        return event_id.split(":", 1)[1].strip()
    return ""


def _evidence_id_for_event(event: dict[str, Any], kind: str | None) -> str:
    activity_id = _source_activity_id(event)
    channel = str(event.get("channel") or "")
    if activity_id and channel == "call":
        return f"call:{activity_id}"
    if activity_id and channel == "email":
        return f"email:{activity_id}"
    if activity_id and channel in MESSENGER_CHANNELS:
        return f"message:{activity_id}"
    if kind in {"conversation", "client_reply"} and activity_id:
        return f"{channel}:{activity_id}"
    return str(event.get("event_id") or "")


def _raw_activities(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return raw_activities_by_id(bundle)


def _transcripts_by_activity(transcripts_dir: Path | None, deal_id: str) -> dict[str, dict[str, Any]]:
    if transcripts_dir is None or not deal_id:
        return {}
    return {
        str(item.get("activity_id") or ""): item
        for item in transcript_items(Path(transcripts_dir), "deal", str(deal_id))
        if str(item.get("activity_id") or "").strip()
    }


def _evidence_by_activity(raw_bundle: dict[str, Any], transcripts_dir: Path | None) -> dict[str, dict[str, Any]]:
    directory = Path(transcripts_dir) if transcripts_dir is not None else Path("__no_transcripts__")
    mapped: dict[str, dict[str, Any]] = {}
    for item in collect_deal_evidence(raw_bundle if isinstance(raw_bundle, dict) else {}, directory):
        activity_id = str(item.get("activity_id") or "").strip()
        if activity_id:
            mapped[activity_id] = item
    return mapped


def _enrich_communication_events(
    events: list[dict[str, Any]],
    *,
    bundle: dict[str, Any],
    transcripts_dir: Path | None,
    deal_id: str,
) -> list[dict[str, Any]]:
    """Attach call_outcome / transcript without changing the source ledger."""
    activities = _raw_activities(bundle)
    transcripts = _transcripts_by_activity(transcripts_dir, deal_id)
    evidence = _evidence_by_activity(bundle.get("raw_bundle") or bundle, transcripts_dir)
    enriched: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        channel = str(item.get("channel") or "")
        activity_id = _source_activity_id(item)
        transcript = transcripts.get(activity_id) if channel == "call" else None
        evidence_item = evidence.get(activity_id)
        if transcript and str(transcript.get("text") or "").strip():
            item["has_transcript"] = True
            item["transcript_text"] = str(transcript.get("text") or "")
            if not item.get("occurred_at"):
                item["occurred_at"] = transcript.get("call_start") or item.get("occurred_at")
        elif evidence_item and evidence_item.get("kind") == "call_transcript":
            item["has_transcript"] = True
            item["transcript_text"] = str(evidence_item.get("text") or "")
        if channel == "call":
            raw = activities.get(activity_id) or {}
            classified = classify_call_outcome(
                raw,
                has_transcript=bool(item.get("has_transcript")),
            )
            item["call_outcome"] = classified["call_outcome"]
            item["talk_duration_seconds"] = classified["talk_duration_seconds"]
            if classified["call_outcome"] == "connected":
                item["contact_class"] = "confirmed_contact"
                item["evidence_level"] = "direct"
            else:
                item["contact_class"] = "attempt"
        elif evidence_item and evidence_item.get("kind") in {"inbound_email", "inbound_message"}:
            if str(evidence_item.get("text") or "").strip() and not str(item.get("content") or "").strip():
                item["content"] = str(evidence_item.get("text") or "")
        enriched.append(item)
    return enriched


def _is_confirmed_client_contact(event: dict[str, Any]) -> bool:
    """True only when the client is directly present in the event text/transcript."""
    if not _client_content(event):
        return False
    kind = communication_activity_kind(event)
    if kind in {"conversation", "client_reply"}:
        return True
    return (
        str(event.get("contact_class") or "") == "confirmed_contact"
        and str(event.get("direction") or "") == "incoming"
    )


def _client_content(event: dict[str, Any]) -> str:
    # Для звонка слова клиента берём только из транскрипта, не из CRM DESCRIPTION.
    if str(event.get("channel") or "") == "call":
        return str(event.get("transcript_text") or "").strip()
    return str(event.get("content") or "").strip()


def _is_substantive_client_event(event: dict[str, Any]) -> bool:
    if not _is_confirmed_client_contact(event):
        return False
    is_call = communication_activity_kind(event) == "conversation"
    return is_substantive_client_content(_client_content(event), is_call=is_call)


def _contact_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    kind = communication_activity_kind(event)
    return {
        "occurred_at": event.get("occurred_at"),
        "channel": event.get("channel"),
        "direction": event.get("direction"),
        "kind": kind,
        "evidence_id": _evidence_id_for_event(event, kind),
        "event_id": event.get("event_id"),
        "content": _preview(_client_content(event)),
    }


def _manager_action_snapshot(event: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "occurred_at": event.get("occurred_at"),
        "channel": event.get("channel"),
        "direction": event.get("direction"),
        "kind": kind,
        "event_id": event.get("event_id"),
        "preview": _preview(event.get("content") or event.get("subject"), 240),
    }


def _manager_followup_kind(event: dict[str, Any]) -> str | None:
    """Actions after the client anchor; never a new client position."""
    channel = str(event.get("channel") or "")
    if channel in INTERNAL_CHANNELS:
        return "internal_comment"
    kind = communication_activity_kind(event)
    if kind == "dial_attempt":
        return "dial_attempt"
    if kind == "message":
        return "outgoing_message"
    if kind == "email":
        return "outgoing_email"
    if kind in {"conversation", "client_reply"}:
        return None
    if str(event.get("contact_class") or "") == "internal_information":
        return "internal_comment"
    return None


def build_deal_current_situation_context(
    bundle: dict[str, Any] | None,
    *,
    transcripts_dir: Path | None = None,
    deal_id: str = "",
) -> dict[str, Any]:
    """Build full-history current-situation context before compact Markdown limits."""
    if not isinstance(bundle, dict) or not bundle:
        return _empty_context(available=False)
    events = bundle.get("normalized_communications")
    if not isinstance(events, list):
        events = build_normalized_communications(bundle)
    events = [dict(item) for item in events if isinstance(item, dict)]
    events = _enrich_communication_events(
        events,
        bundle=bundle,
        transcripts_dir=transcripts_dir,
        deal_id=str(deal_id or ""),
    )
    events.sort(key=_event_sort_key)
    if not events:
        return _empty_context(available=True)

    substantive = [item for item in events if _is_substantive_client_event(item)]
    anchor = substantive[-1] if substantive else None
    later = [item for item in events if anchor is not None and _event_sort_key(item) > _event_sort_key(anchor)]
    later_client = [item for item in later if _is_confirmed_client_contact(item)]
    later_non_substantive = [
        _contact_snapshot(item) for item in later_client if not _is_substantive_client_event(item)
    ]
    manager_actions: list[dict[str, Any]] = []
    call_attempts = 0
    outgoing_messages = 0
    outgoing_emails = 0
    for item in later:
        followup = _manager_followup_kind(item)
        if followup is None:
            continue
        if followup == "dial_attempt":
            call_attempts += 1
        elif followup == "outgoing_message":
            outgoing_messages += 1
        elif followup == "outgoing_email":
            outgoing_emails += 1
        manager_actions.append(_manager_action_snapshot(item, followup))

    return {
        "available": True,
        "last_substantive_client_contact": _contact_snapshot(anchor) if anchor else None,
        "later_non_substantive_client_replies": later_non_substantive,
        "manager_actions_after_contact": manager_actions[-MANAGER_ACTIONS_LIMIT:],
        "call_attempts_after_contact": call_attempts,
        "outgoing_messages_after_contact": outgoing_messages,
        "outgoing_emails_after_contact": outgoing_emails,
        "has_newer_client_response": bool(later_client),
    }


def load_deal_current_situation_context(deal_dir: Path, deal_id: str) -> dict[str, Any]:
    """Load workspace customer history, including the explicit source lead."""
    bundle_path = Path(deal_dir) / "raw" / f"deal_{deal_id}_customer_history_bundle.json"
    if not bundle_path.is_file():
        return _empty_context(available=False)
    try:
        value = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_context(available=False)
    if not isinstance(value, dict):
        return _empty_context(available=False)
    context_path = bundle_path.with_name(f"deal_{deal_id}_context.json")
    bundle = include_saved_source_lead_communications(value, context_path, deal_id=str(deal_id))
    return build_deal_current_situation_context(
        bundle,
        transcripts_dir=Path(deal_dir) / "transcripts",
        deal_id=str(deal_id),
    )


def render_deal_current_situation_context(context: dict[str, Any] | None) -> str:
    payload = context if isinstance(context, dict) else _empty_context(available=False)
    return json.dumps(payload, ensure_ascii=False, indent=2)
