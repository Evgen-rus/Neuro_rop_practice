"""Read one locally saved external conversation for the anchor's Moscow day."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from api.deal_communication_content import (
    TEXT_CHANNELS,
    _normalized_events,
    _stored_daily_event,
    communication_event_text,
)
from api.deal_manager_quick_help import load_local_communication_bundle
from bitrix.customer_history import (
    INTERNAL_CHANNELS,
    communication_conversation_key,
    parse_bitrix_datetime,
)
from setup import MSK_TZ
from storage import rop_db as storage


MAX_THREAD_ITEMS = 50
MAX_MESSAGE_CHARS = 32_000
MAX_THREAD_CHARS = 256_000


class DealCommunicationThreadNotFound(LookupError):
    """The anchor or its locally saved external conversation is unavailable."""


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    value = parse_bitrix_datetime(event.get("occurred_at"))
    return value.astimezone(MSK_TZ) if value else None


def _event_conversation(event: dict[str, Any], deal_id: str) -> tuple[str, str]:
    key = str(event.get("conversation_key") or "")
    scope = str(event.get("conversation_scope") or "")
    if key:
        return key, scope or "unknown"
    return communication_conversation_key(event, owner_id=str(deal_id))


def _allowed_entity_keys(bundle: dict[str, Any], deal_id: str) -> set[str]:
    allowed = {f"deal:{deal_id}"}
    deal = (bundle.get("deal") or {}).get("item") or {}
    lead_id = str(deal.get("LEAD_ID") or "")
    if lead_id:
        allowed.add(f"lead:{lead_id}")
    return allowed


def _belongs_to_deal(event: dict[str, Any], allowed: set[str]) -> bool:
    keys = {str(value) for value in event.get("entity_keys") or [] if value}
    if event.get("entity_key"):
        keys.add(str(event["entity_key"]))
    if not keys:
        entity_type = str(event.get("entity_type") or "")
        entity_id = str(event.get("entity_id") or "")
        if entity_type and entity_id:
            keys.add(f"{entity_type}:{entity_id}")
    return not keys or bool(keys & allowed)


def _public_role(event: dict[str, Any]) -> str:
    role = str(event.get("participant_role") or "unknown")
    return "manager" if role in {"employee", "manager"} else role


def get_deal_communication_thread(
    deal_id: str,
    anchor_event_id: str,
    *,
    db_path: str | Path = storage.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return a bounded chronological text thread without any Bitrix request."""
    wanted = str(anchor_event_id or "").strip()
    bundle = load_local_communication_bundle(str(deal_id))
    events = _normalized_events(bundle) if bundle else []
    anchor = next(
        (event for event in events if str(event.get("event_id") or "") == wanted),
        None,
    )
    if anchor is None:
        anchor = _stored_daily_event(db_path, str(deal_id), wanted)
        events = [anchor] if isinstance(anchor, dict) else []
    channel = str((anchor or {}).get("channel") or "").lower()
    anchor_at = _event_datetime(anchor or {})
    if (
        not isinstance(anchor, dict)
        or channel not in TEXT_CHANNELS
        or channel in INTERNAL_CHANNELS
        or anchor_at is None
    ):
        raise DealCommunicationThreadNotFound(
            "Диалог для выбранного сообщения не найден"
        )

    conversation_key, conversation_scope = _event_conversation(anchor, str(deal_id))
    allowed = _allowed_entity_keys(bundle, str(deal_id))
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        occurred_at = _event_datetime(event)
        event_channel = str(event.get("channel") or "").lower()
        event_key, _scope = _event_conversation(event, str(deal_id))
        if (
            occurred_at is None
            or occurred_at.date() != anchor_at.date()
            or event_channel != channel
            or event_key != conversation_key
            or event_channel in INTERNAL_CHANNELS
            or str(event.get("contact_class") or "") == "internal_information"
            or not _belongs_to_deal(event, allowed)
        ):
            continue
        candidates.append((occurred_at, event))
    candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("event_id") or "")))

    messages: list[dict[str, Any]] = []
    remaining = MAX_THREAD_CHARS
    response_truncated = len(candidates) > MAX_THREAD_ITEMS
    for occurred_at, event in candidates[:MAX_THREAD_ITEMS]:
        text, is_excerpt = communication_event_text(bundle, event)
        if not text:
            continue
        limit = min(MAX_MESSAGE_CHARS, remaining)
        if limit <= 0:
            response_truncated = True
            break
        item_truncated = len(text) > limit
        messages.append({
            "event_id": str(event.get("event_id") or ""),
            "occurred_at": occurred_at.isoformat(),
            "channel": channel,
            "direction": str(event.get("direction") or "unknown"),
            "participant_role": _public_role(event),
            "participant_name": event.get("participant_name"),
            "text": text[:limit],
            "is_excerpt": is_excerpt,
            "truncated": item_truncated,
        })
        remaining -= min(len(text), limit)
        response_truncated = response_truncated or item_truncated

    if not messages:
        raise DealCommunicationThreadNotFound(
            "Текст выбранного диалога пока недоступен"
        )
    return {
        "deal_id": str(deal_id),
        "anchor_event_id": wanted,
        "conversation_key": conversation_key,
        "conversation_scope": conversation_scope,
        "date": anchor_at.date().isoformat(),
        "timezone": "Europe/Moscow",
        "channel": channel,
        "messages": messages,
        "truncated": response_truncated,
    }
