"""Read saved client email/message text on explicit UI request."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import load_local_communication_bundle
from bitrix.customer_history import (
    _parse_mirrored_message,
    build_normalized_communications,
    clean_text,
    merge_activity_detail,
    result_items,
)
from storage import rop_db as storage


TEXT_CHANNELS = frozenset({"email", "message", "whatsapp", "telegram", "max"})
MAX_CONTENT_CHARS = 1_000_000


class DealCommunicationContentNotFound(LookupError):
    """The communication or its locally saved text is unavailable."""


def _source_id(event: dict[str, Any]) -> str:
    values = event.get("source_ids") if isinstance(event.get("source_ids"), list) else []
    if values:
        return str(values[0] or "").strip()
    event_id = str(event.get("event_id") or "")
    return event_id.split(":", 1)[1].strip() if event_id.startswith("crm_activity:") else ""


def _normalized_events(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    values = bundle.get("normalized_communications")
    if not isinstance(values, list):
        values = build_normalized_communications(bundle)
    return [item for item in values if isinstance(item, dict)]


def _raw_activity_text(bundle: dict[str, Any], source_id: str) -> str:
    histories = bundle.get("activities_by_entity")
    for history in histories.values() if isinstance(histories, dict) else []:
        if not isinstance(history, dict):
            continue
        details = history.get("activity_details") if isinstance(history.get("activity_details"), dict) else {}
        for activity in result_items(history.get("activities")):
            merged = merge_activity_detail(activity, details)
            if str(merged.get("ID") or "") == source_id:
                return clean_text(merged.get("DESCRIPTION"))
    return ""


def _raw_mirrored_message_text(bundle: dict[str, Any], source_id: str) -> str:
    comments = bundle.get("timeline_comments_by_entity")
    for responses in comments.values() if isinstance(comments, dict) else []:
        for response in responses if isinstance(responses, list) else []:
            for item in result_items(response):
                if str(item.get("ID") or "") != source_id:
                    continue
                raw = clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"))
                _speaker, content = _parse_mirrored_message(raw)
                return clean_text(content)
    return ""


def _stored_daily_event(
    db_path: str | Path,
    deal_id: str,
    event_id: str,
) -> dict[str, Any] | None:
    rows = storage.list_deal_control_deals(db_path, active_only=False)
    deal = next(
        (item for item in rows if str(item.get("deal_id") or "") == str(deal_id)),
        None,
    )
    summary = deal.get("communications_today") if isinstance(deal, dict) else None
    items = summary.get("items") if isinstance(summary, dict) else []
    return next(
        (
            item
            for item in items or []
            if isinstance(item, dict) and str(item.get("event_id") or "") == str(event_id)
        ),
        None,
    )


def get_deal_communication_content(
    deal_id: str,
    event_id: str,
    *,
    db_path: str | Path = storage.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return text only for an external communication belonging to this deal."""

    wanted_event_id = str(event_id or "").strip()
    bundle = load_local_communication_bundle(str(deal_id))
    event = next(
        (
            item
            for item in _normalized_events(bundle)
            if str(item.get("event_id") or "") == wanted_event_id
        ),
        None,
    )
    if event is None:
        event = _stored_daily_event(db_path, str(deal_id), wanted_event_id)
    channel = str((event or {}).get("channel") or "").lower()
    if not isinstance(event, dict) or channel not in TEXT_CHANNELS:
        raise DealCommunicationContentNotFound(
            "Письмо или сообщение не найдено в истории этой сделки"
        )

    source_id = _source_id(event)
    text = ""
    is_excerpt = True
    if bundle and source_id:
        if str(event.get("source_type") or "") == "crm_timeline_comment":
            text = _raw_mirrored_message_text(bundle, source_id)
        else:
            text = _raw_activity_text(bundle, source_id)
        is_excerpt = not bool(text)
    if not text:
        text = str(event.get("content") or event.get("preview") or "").strip()
    if not text:
        raise DealCommunicationContentNotFound(
            "Текст этого письма или сообщения пока недоступен"
        )

    truncated = len(text) > MAX_CONTENT_CHARS
    return {
        "deal_id": str(deal_id),
        "event_id": wanted_event_id,
        "channel": channel,
        "text": text[:MAX_CONTENT_CHARS],
        "is_excerpt": is_excerpt,
        "truncated": truncated,
    }
