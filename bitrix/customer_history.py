"""
Read-only customer history bundle builder for lead/deal analysis.

The bundle broadens a root lead/deal context to the customer level:
root entity -> contact(s) -> related contact deals -> CRM activities and
timeline comments. It does not write anything to Bitrix.
"""

from __future__ import annotations

import hashlib
import copy
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.client import BitrixReadOnlyClient, as_list, load_json
from bitrix.context_sync import OVERLAP, source_since, timeline_delta
from bitrix.internal_im_chat import append_internal_chat_events, fetch_internal_im_chats, internal_chat_events
from setup import BASE_DIR, MSK_TZ


LEAD_OWNER_TYPE_ID = 1
DEAL_OWNER_TYPE_ID = 2
CONTACT_OWNER_TYPE_ID = 3

DEFAULT_HISTORY_DAYS = 365
INCREMENTAL_OVERLAP = OVERLAP

WAZZUP_CHANNEL_MARKERS = {
    "whatsapp": ("/whatsapp.", "whatsapp"),
    "max": ("/max.", " max"),
    "telegram": ("/telegram.", "telegram"),
}


def get_result(call_result: dict[str, Any] | None) -> Any:
    if not call_result or not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def result_item(call_result: dict[str, Any] | None) -> dict[str, Any]:
    result = get_result(call_result)
    return result if isinstance(result, dict) else {}


def result_items(call_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_result:
        return []
    if isinstance(call_result.get("items"), list):
        return [item for item in call_result["items"] if isinstance(item, dict)]
    result = get_result(call_result)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return [item for item in result["items"] if isinstance(item, dict)]
    return []


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2", text, flags=re.I)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</div\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _normalized_person_name(value: Any) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", str(value or "").lower()))


def _bundle_client_names(bundle: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def add_item(item: dict[str, Any]) -> None:
        variants = [
            " ".join(str(item.get(key) or "").strip() for key in ("NAME", "SECOND_NAME", "LAST_NAME")).strip(),
            " ".join(str(item.get(key) or "").strip() for key in ("NAME", "LAST_NAME")).strip(),
        ]
        for variant in variants:
            normalized = _normalized_person_name(variant)
            if normalized:
                names.add(normalized)

    add_item(result_item(bundle.get("lead")))
    for contact_container in (bundle.get("contacts") or {}).values():
        add_item(result_item(contact_container if isinstance(contact_container, dict) else None))
    return names


# Совпадает с openai_api.audio.short_call: короче 20 сек записи — недозвон/автоответчик.
CALL_RECORDING_MIN_TALK_SECONDS = 20.0
CALL_NO_ANSWER_TOKENS = (
    "НЕ ОТВЕТ",
    "НЕДОЗВОН",
    "НЕ ДОЗВОН",
    "ЗАНЯТ",
    "BUSY",
    "NO ANSWER",
    "INVALID",
    "НЕВЕРН",
    "ПРОПУЩ",
    "MISSED",
    "FAILED",
)
CALL_BUSY_TOKENS = ("ЗАНЯТ", "BUSY")


def _call_duration_seconds(raw: dict[str, Any]) -> float | None:
    started = parse_bitrix_datetime(raw.get("START_TIME") or raw.get("CREATED"))
    ended = parse_bitrix_datetime(raw.get("END_TIME"))
    if started is None or ended is None:
        return None
    return round(max(0.0, (ended - started).total_seconds()), 1)


def _has_call_recording(raw: dict[str, Any]) -> bool:
    files = raw.get("FILES")
    if isinstance(files, list):
        return any(
            (isinstance(item, dict) and str(item.get("ID") or item.get("id") or "").strip())
            or (not isinstance(item, dict) and str(item or "").strip())
            for item in files
        )
    return bool(files)


def _call_direction(raw: dict[str, Any]) -> str:
    value = str(raw.get("DIRECTION") or "").strip()
    if value == "1":
        return "incoming"
    if value == "2":
        return "outgoing"
    lowered = value.lower()
    if lowered in {"incoming", "in", "входящий"}:
        return "incoming"
    if lowered in {"outgoing", "out", "исходящий"}:
        return "outgoing"
    return "unknown"


def _call_status_haystack(raw: dict[str, Any]) -> str:
    return " ".join(
        str(raw.get(key) or "")
        for key in ("SUBJECT", "PROVIDER_TYPE_ID")
    ).upper()


def classify_call_outcome(
    raw: dict[str, Any] | None,
    *,
    has_transcript: bool = False,
    recording_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Classify a CRM call without treating COMPLETED=Y or START-END as a conversation."""
    activity = raw if isinstance(raw, dict) else {}
    direction = _call_direction(activity)
    haystack = _call_status_haystack(activity)
    busy = any(token in haystack for token in CALL_BUSY_TOKENS)
    explicit_no_answer = any(token in haystack for token in CALL_NO_ANSWER_TOKENS)
    has_recording = _has_call_recording(activity)
    short_recording = (
        recording_duration_seconds is not None
        and float(recording_duration_seconds) < CALL_RECORDING_MIN_TALK_SECONDS
    )

    if short_recording:
        return {
            "call_outcome": "no_answer",
            "talk_duration_seconds": 0.0,
            "status_label": "Занято" if busy else "Не дозвонились",
            "has_recording": has_recording,
        }
    if has_transcript or has_recording:
        duration = None if recording_duration_seconds is None else round(float(recording_duration_seconds), 1)
        return {
            "call_outcome": "connected",
            "talk_duration_seconds": duration,
            "status_label": "Разговор",
            "has_recording": has_recording,
        }
    if direction == "incoming":
        return {
            "call_outcome": "no_answer",
            "talk_duration_seconds": 0.0,
            "status_label": "Пропущенный",
            "has_recording": False,
        }
    if direction == "outgoing" or explicit_no_answer:
        return {
            "call_outcome": "no_answer",
            "talk_duration_seconds": 0.0,
            "status_label": "Занято" if busy else "Не дозвонились",
            "has_recording": False,
        }
    return {
        "call_outcome": "unknown",
        "talk_duration_seconds": None,
        "status_label": "Исход не определён",
        "has_recording": False,
    }


def _communication_channel_from_comment(text: str) -> str | None:
    lowered = text.lower()
    for channel, markers in WAZZUP_CHANNEL_MARKERS.items():
        if any(marker in lowered for marker in markers):
            return channel
    return None


MESSENGER_CHANNELS = frozenset({"message", "whatsapp", "telegram", "max"})
INTERNAL_CHANNELS = frozenset({"internal_chat", "internal_comment"})
EXTERNAL_TEXT_CHANNELS = frozenset({"email", *MESSENGER_CHANNELS})


def is_confirmed_client_reply(event: dict[str, Any]) -> bool:
    """Return true only for explicit incoming text from a confirmed client."""
    return (
        str(event.get("channel") or "").lower() in EXTERNAL_TEXT_CHANNELS
        and str(event.get("direction") or "").lower() == "incoming"
        and str(event.get("participant_role") or "").lower() == "client"
        and str(event.get("contact_class") or "").lower() == "confirmed_contact"
        and bool(str(event.get("content") or "").strip())
    )


def messenger_mirror_from_comment(text: str) -> dict[str, Any] | None:
    """Return channel, speaker and content if the CRM comment is a Wazzup messenger mirror."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    channel = _communication_channel_from_comment(cleaned)
    if not channel:
        return None
    speaker, content = _parse_mirrored_message(cleaned)
    return {
        "channel": channel,
        "speaker": speaker,
        "content": content or cleaned,
    }


def communication_activity_kind(event: dict[str, Any]) -> str | None:
    """Map a normalized communication to deal-control activity kind.

    The same mapping is used by daily communications and current-situation
    context so call/email/message classification stays in one place.
    """
    channel = str(event.get("channel") or "")
    direction = str(event.get("direction") or "")
    content = str(event.get("content") or "").strip()
    contact_class = str(event.get("contact_class") or "")
    if channel == "call":
        outcome = str(event.get("call_outcome") or "")
        if direction == "outgoing":
            return "conversation" if outcome == "connected" else "dial_attempt"
        if direction == "incoming" and outcome == "connected":
            return "conversation"
        return None
    if is_confirmed_client_reply(event):
        return "client_reply"
    if channel == "email":
        if direction == "outgoing":
            return "email"
        return None
    if channel not in MESSENGER_CHANNELS:
        return None
    if contact_class == "internal_information":
        return None
    if direction == "outgoing" or (contact_class == "attempt" and content):
        return "message"
    return None


def _provider_dialog_id(raw: dict[str, Any]) -> str:
    """Read only explicit provider conversation fields; message origin IDs are not dialogs."""
    wanted = {
        "dialog_id",
        "dialogid",
        "chat_id",
        "chatid",
        "external_chat_id",
        "externalchatid",
        "conversation_id",
        "conversationid",
    }

    def visit(value: Any) -> str:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    return visit(json.loads(text))
                except (TypeError, ValueError):
                    return ""
            return ""
        if isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
            return ""
        if not isinstance(value, dict):
            return ""
        for key, child in value.items():
            normalized_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if normalized_key in wanted and str(child or "").strip():
                return str(child).strip()
        for child in value.values():
            found = visit(child)
            if found:
                return found
        return ""

    for key in ("SETTINGS", "PROVIDER_PARAMS", "PROVIDER_DATA"):
        found = visit(raw.get(key))
        if found:
            return found
    return ""


def _bundle_client_identity_map(bundle: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    identities: dict[str, str] = {}
    contact_ids: set[str] = set()

    def add(item: dict[str, Any], identity: str) -> None:
        for variant in (
            " ".join(str(item.get(key) or "").strip() for key in ("NAME", "SECOND_NAME", "LAST_NAME")).strip(),
            " ".join(str(item.get(key) or "").strip() for key in ("NAME", "LAST_NAME")).strip(),
        ):
            normalized = _normalized_person_name(variant)
            if normalized:
                identities[normalized] = identity

    lead = result_item(bundle.get("lead"))
    if lead:
        add(lead, f"lead:{lead.get('ID') or ''}")
    for contact_id, container in (bundle.get("contacts") or {}).items():
        item = result_item(container if isinstance(container, dict) else None)
        if not item:
            continue
        normalized_id = str(item.get("ID") or contact_id or "").strip()
        if normalized_id:
            contact_ids.add(normalized_id)
            add(item, f"contact:{normalized_id}")
    return identities, contact_ids


def _activity_contact_identity(raw: dict[str, Any]) -> str:
    values: list[str] = []
    for item in raw.get("COMMUNICATIONS") or []:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("ENTITY_ID") or item.get("entityId") or "").strip()
        entity_type = str(item.get("ENTITY_TYPE") or item.get("entityType") or "contact").strip().lower()
        if entity_id:
            values.append(f"{entity_type}:{entity_id}")
            continue
        value = normalize_email(item.get("VALUE") or item.get("value"))
        if value:
            values.append(f"value:{value}")
    return sorted(set(values))[0] if values else ""


def _conversation_owner_id(bundle: dict[str, Any]) -> str:
    root = bundle.get("root_entity") if isinstance(bundle.get("root_entity"), dict) else {}
    if root.get("id"):
        return str(root["id"])
    deal_container = bundle.get("deal")
    deal = (
        result_item(deal_container)
        if isinstance(deal_container, dict) and "response" not in deal_container
        else (deal_container or {}).get("item", {})
        if isinstance(deal_container, dict)
        else {}
    )
    return str(deal.get("ID") or bundle.get("deal_id") or bundle.get("lead_id") or "")


def communication_conversation_key(
    event: dict[str, Any],
    *,
    owner_id: str,
    provider_dialog_id: str = "",
    contact_identity: str = "",
) -> tuple[str, str]:
    """Build an opaque stable key without exposing contact identity."""
    channel = str(event.get("channel") or "unknown").lower()
    if provider_dialog_id:
        scope, identity = "provider", provider_dialog_id
    elif contact_identity:
        scope, identity = "contact", contact_identity
    else:
        scope, identity = "channel", channel
    digest = hashlib.sha256(
        f"v1|{scope}|{owner_id}|{channel}|{identity}".encode("utf-8")
    ).hexdigest()[:24]
    return f"conversation:v1:{digest}", scope


def _parse_mirrored_message(text: str) -> tuple[str | None, str]:
    cleaned = re.sub(r"\[img\].*?\[/img\]", "", text, flags=re.I | re.S).strip()
    first_line, _, remainder = cleaned.partition("\n")
    if ":" not in first_line:
        return None, cleaned
    speaker, first_content = first_line.split(":", 1)
    content = "\n".join(part for part in (first_content.strip(), remainder.strip()) if part).strip()
    return speaker.strip() or None, content


def _communication_event_id(prefix: str, occurred_at: Any, identity: str) -> str:
    digest = hashlib.sha1(f"{occurred_at}|{identity}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def build_normalized_communications(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Return an additive, source-aware communication ledger for leads and deals."""
    client_names = _bundle_client_names(bundle)
    client_identities, contact_ids = _bundle_client_identity_map(bundle)
    owner_id = _conversation_owner_id(bundle)
    events: list[dict[str, Any]] = []

    for item in bundle.get("client_touchpoints") or []:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type") or "unknown").lower()
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        raw_direction = str(item.get("direction") or raw.get("DIRECTION") or "")
        direction = "incoming" if raw_direction == "1" else "outgoing" if raw_direction == "2" else "unknown"
        channel = event_type if event_type in {"call", "email", "message"} else "unknown"
        content = clean_text(item.get("text") or raw.get("DESCRIPTION"), 1200)
        source_id = str(item.get("id") or raw.get("ID") or "")
        contact_class = "attempt"
        evidence_level = "unknown" if channel == "call" else "direct"
        participant_role = "client" if direction == "incoming" else "employee" if direction == "outgoing" else "unknown"
        if channel in {"email", "message"} and direction == "incoming" and content:
            contact_class = "confirmed_contact"
        contact_identity = _activity_contact_identity(raw)
        if not contact_identity and len(contact_ids) == 1:
            contact_identity = f"contact:{next(iter(contact_ids))}"
        event_id = f"crm_activity:{source_id}" if source_id else _communication_event_id(
            "crm_activity",
            item.get("when"),
            f"{channel}|{direction}|{item.get('subject')}|{content}",
        )
        event = {
                "event_id": event_id,
                "source_ids": [source_id] if source_id else [],
                "occurred_at": item.get("when"),
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "entity_key": item.get("entity_key"),
                "entity_keys": [str(item.get("entity_key"))] if item.get("entity_key") else [],
                "channel": channel,
                "direction": direction,
                "participant_role": participant_role,
                "participant_name": None,
                "source_type": "crm_activity",
                "source_label": "Активность Bitrix",
                "subject": clean_text(item.get("subject"), 300),
                "content": content,
                "preview": clean_text(content, 360),
                "call_id": source_id if channel == "call" else None,
                "duration_seconds": _call_duration_seconds(raw) if channel == "call" else None,
                "has_recording": bool(raw.get("FILES")) if channel == "call" else False,
                "has_transcript": False,
                "evidence_level": evidence_level,
                "contact_class": contact_class,
                "classification_reason": (
                    "Звонок зафиксирован в CRM; результат разговора требует записи или транскрипта."
                    if channel == "call"
                    else "Входящий текст клиента зафиксирован в CRM."
                    if contact_class == "confirmed_contact"
                    else "Исходящая коммуникация зафиксирована как попытка связи."
                ),
            }
        conversation_key, conversation_scope = communication_conversation_key(
            event,
            owner_id=owner_id,
            provider_dialog_id=_provider_dialog_id(raw),
            contact_identity=contact_identity,
        )
        event["conversation_key"] = conversation_key
        event["conversation_scope"] = conversation_scope
        events.append(event)

    for item in bundle.get("internal_context") or []:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"), 1200)
        category = str(item.get("category") or "")
        source_id = str(item.get("id") or "")
        channel = _communication_channel_from_comment(text)
        if channel:
            speaker, content = _parse_mirrored_message(text)
            normalized_speaker = _normalized_person_name(speaker)
            is_client = bool(normalized_speaker and normalized_speaker in client_names)
            direction = "incoming" if is_client else "unknown"
            participant_role = "client" if is_client else "unknown"
            contact_class = "confirmed_contact" if is_client and content else "attempt" if speaker and content else "unknown"
            evidence_level = "direct" if content else "unknown"
            source_label = f"CRM/{'WhatsApp' if channel == 'whatsapp' else 'Max' if channel == 'max' else 'Telegram'}"
            # Contact enrichment may later refine unknown to incoming. Keep the
            # physical mirror identity independent from that classification.
            # Timestamp is added by _communication_event_id; channel + body also
            # collapse the same mirror stored on both deal and contact.
            identity = f"{channel}|{content}"
            event_id = _communication_event_id("crm_mirror", item.get("when"), identity)
            contact_identity = client_identities.get(normalized_speaker, "")
            if not contact_identity and len(contact_ids) == 1:
                contact_identity = f"contact:{next(iter(contact_ids))}"
            event = {
                    "event_id": event_id,
                    "source_ids": [source_id] if source_id else [],
                    "occurred_at": item.get("when"),
                    "entity_type": item.get("entity_type"),
                    "entity_id": item.get("entity_id"),
                    "entity_key": item.get("entity_key"),
                    "entity_keys": [str(item.get("entity_key"))] if item.get("entity_key") else [],
                    "channel": channel,
                    "direction": direction,
                    "participant_role": participant_role,
                    "participant_name": speaker,
                    "source_type": "crm_timeline_comment",
                    "source_label": source_label,
                    "subject": "",
                    "content": content,
                    "preview": clean_text(content, 360),
                    "call_id": None,
                    "duration_seconds": None,
                    "has_recording": False,
                    "has_transcript": False,
                    "evidence_level": evidence_level,
                    "contact_class": contact_class,
                    "classification_reason": (
                        "Автор сообщения совпадает с контактом клиента."
                        if is_client
                        else "Автор не совпадает с известным контактом клиента; сообщение учтено как попытка, но направление не подтверждено."
                        if speaker
                        else "Автор зеркального сообщения не определён."
                    ),
                }
            conversation_key, conversation_scope = communication_conversation_key(
                event,
                owner_id=owner_id,
                contact_identity=contact_identity,
            )
            event["conversation_key"] = conversation_key
            event["conversation_scope"] = conversation_scope
            events.append(event)
            continue

        source_type = "internal_im_chat" if category == "internal_im_chat" else "crm_timeline_comment"
        event_id = f"{source_type}:{source_id}" if source_id else _communication_event_id(
            source_type,
            item.get("when"),
            text,
        )
        events.append(
            {
                "event_id": event_id,
                "source_ids": [source_id] if source_id else [],
                "occurred_at": item.get("when"),
                "entity_type": item.get("entity_type"),
                "entity_id": item.get("entity_id"),
                "entity_key": item.get("entity_key"),
                "channel": "internal_chat" if source_type == "internal_im_chat" else "internal_comment",
                "direction": "internal",
                "participant_role": "employee",
                "participant_name": item.get("author"),
                "source_type": source_type,
                "source_label": "Внутренний чат" if source_type == "internal_im_chat" else "Комментарий CRM",
                "subject": clean_text(item.get("subject"), 300),
                "content": text,
                "preview": clean_text(text, 360),
                "call_id": None,
                "duration_seconds": None,
                "has_recording": False,
                "has_transcript": False,
                "evidence_level": "reported",
                "contact_class": "internal_information",
                "classification_reason": "Внутренняя запись сотрудника не считается словами клиента.",
            }
        )

    deduplicated: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        existing = deduplicated.get(event_id)
        if existing is None:
            deduplicated[event_id] = event
            continue
        # Prefer a copy whose author is verified as the client, while retaining
        # every explicit entity binding of duplicated CRM mirrors.
        preferred = event if is_confirmed_client_reply(event) and not is_confirmed_client_reply(existing) else existing
        merged = dict(preferred)
        merged["source_ids"] = sorted(
            {str(value) for value in [*(existing.get("source_ids") or []), *(event.get("source_ids") or [])] if value}
        )
        merged["entity_keys"] = sorted({
            str(value)
            for value in [
                existing.get("entity_key"), event.get("entity_key"),
                *(existing.get("entity_keys") or []), *(event.get("entity_keys") or []),
            ]
            if value
        })
        deduplicated[event_id] = merged

    canonical: list[dict[str, Any]] = []
    duplicate_indexes: dict[tuple[str, str], int] = {}
    mirror_channels = {"whatsapp", "telegram", "max"}
    for event in sorted(
        deduplicated.values(),
        key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or "")),
    ):
        content_key = " ".join(str(event.get("content") or "").casefold().split())
        duplicate_key = (str(event.get("occurred_at") or ""), content_key)
        existing_index = duplicate_indexes.get(duplicate_key) if content_key else None
        if existing_index is None:
            duplicate_indexes[duplicate_key] = len(canonical)
            canonical.append(event)
            continue
        existing = canonical[existing_index]
        channels = {str(existing.get("channel") or ""), str(event.get("channel") or "")}
        source_types = {str(existing.get("source_type") or ""), str(event.get("source_type") or "")}
        is_messenger_duplicate = (
            bool(channels & mirror_channels)
            and "crm_timeline_comment" in source_types
            and "crm_activity" in source_types
        )
        if not is_messenger_duplicate:
            duplicate_indexes[(duplicate_key[0], f"{duplicate_key[1]}:{len(canonical)}")] = len(canonical)
            canonical.append(event)
            continue
        preferred = event if str(event.get("channel") or "") in mirror_channels else existing
        merged = dict(preferred)
        merged["source_ids"] = sorted({
            str(value)
            for value in [*(existing.get("source_ids") or []), *(event.get("source_ids") or [])]
            if value
        })
        merged["entity_keys"] = sorted({
            str(value)
            for value in [*(existing.get("entity_keys") or []), *(event.get("entity_keys") or [])]
            if value
        })
        canonical[existing_index] = merged

    return canonical


def parse_bitrix_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed


def history_period(days: int) -> dict[str, Any]:
    normalized_days = max(1, int(days or DEFAULT_HISTORY_DAYS))
    date_to = datetime.now(MSK_TZ)
    date_from = date_to - timedelta(days=normalized_days)
    return {
        "days": normalized_days,
        "date_from": date_from.isoformat(timespec="seconds"),
        "date_to": date_to.isoformat(timespec="seconds"),
    }


def in_period_by_any_date(item: dict[str, Any], period: dict[str, Any], date_keys: tuple[str, ...]) -> bool:
    date_from = parse_bitrix_datetime(period.get("date_from"))
    if not date_from:
        return True
    dates = [parse_bitrix_datetime(item.get(key)) for key in date_keys]
    dates = [value for value in dates if value is not None]
    if not dates:
        return True
    return any(value >= date_from for value in dates)


def build_stage_lookup(pipeline_map_path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = pipeline_map_path or BASE_DIR / "crm_pipeline_map.json"
    if not path.exists():
        return {}
    try:
        crm_map = load_json(path)
    except ValueError:
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    for pipeline in crm_map.get("deal_pipelines", []):
        for stage in pipeline.get("stages", []):
            status_id = stage.get("status_id")
            if not status_id:
                continue
            lookup[str(status_id)] = {
                "stage": stage,
                "pipeline": {
                    "id": pipeline.get("id"),
                    "name": pipeline.get("name"),
                    "sort": pipeline.get("sort"),
                },
            }
    return lookup


def fetch_entity_by_id(client: BitrixReadOnlyClient, method: str, entity_id: Any) -> dict[str, Any]:
    if not is_real_id(entity_id):
        return {"ok": False, "method": method, "payload": {"id": entity_id}, "error": "empty id"}
    return client.safe_call(method, {"id": entity_id})


def is_real_id(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.isdigit() and int(text) == 0:
        return False
    return True


def activity_cursor_value(snapshot: dict[str, Any] | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    sync = snapshot.get("sync")
    if isinstance(sync, dict) and "activity_cursor" in sync:
        value = sync.get("activity_cursor")
    else:
        value = snapshot.get("generated_at")
    return str(value) if value else None


def incremental_since(snapshot: dict[str, Any] | None) -> str | None:
    cursor_value = activity_cursor_value(snapshot)
    cursor = parse_bitrix_datetime(cursor_value)
    if cursor is None:
        return None
    return (cursor - INCREMENTAL_OVERLAP).isoformat(timespec="seconds")


def merge_items_by_id(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    without_id: list[dict[str, Any]] = []
    for item in [*previous, *current]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("ID") or item.get("id") or "").strip()
        if item_id:
            rows[item_id] = item
        else:
            without_id.append(item)
    return [*rows.values(), *without_id]


def activity_details_from_list(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve the legacy detail container without per-activity REST calls."""
    details: dict[str, Any] = {}
    for activity in activities:
        activity_id = str(activity.get("ID") or activity.get("id") or "").strip()
        if not activity_id:
            continue
        details[activity_id] = {
            "ok": True,
            "method": "crm.activity.list",
            "payload": {},
            "response": {"result": activity},
            "reused_from_list": True,
        }
    return details


def fetch_activities_for_owner(
    client: BitrixReadOnlyClient,
    owner_type_id: int,
    owner_id: str,
    *,
    updated_after: str | None = None,
) -> dict[str, Any]:
    activity_filter: dict[str, Any] = {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": owner_id}
    if updated_after:
        activity_filter[">=LAST_UPDATED"] = updated_after
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": activity_filter,
        "select": ["*", "FILES", "COMMUNICATIONS"],
    }
    return client.safe_list_all("crm.activity.list", payload)


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    del client
    return activity_details_from_list(activities)


def fetch_timeline_comments(
    client: BitrixReadOnlyClient,
    entity_id: str,
    *,
    entity_type: str,
    owner_type_id: int,
    created_after: str | None = None,
    known: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    del owner_type_id
    return [timeline_delta(client, entity_type, entity_id, since=created_after, known=known)]


def contact_ids_from_deal(client: BitrixReadOnlyClient, deal_id: str, deal: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    contact_ids = {str(item).strip() for item in as_list(deal.get("CONTACT_ID")) if is_real_id(item)}
    contact_items_response = client.safe_call("crm.deal.contact.items.get", {"id": deal_id})
    contact_items = get_result(contact_items_response)
    if isinstance(contact_items, list):
        for item in contact_items:
            if isinstance(item, dict) and is_real_id(item.get("CONTACT_ID")):
                contact_ids.add(str(item["CONTACT_ID"]).strip())
    return sorted(contact_ids), contact_items_response


def contact_ids_from_lead(lead: dict[str, Any]) -> list[str]:
    return sorted({str(item).strip() for item in as_list(lead.get("CONTACT_ID")) if is_real_id(item)})


def multifield_values(entity: dict[str, Any], field: str) -> list[str]:
    value = entity.get(field) or []
    if isinstance(value, list):
        return [str(item.get("VALUE")) for item in value if isinstance(item, dict) and item.get("VALUE")]
    if value:
        return [str(value)]
    return []


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def phones_match(left: Any, right: Any) -> bool:
    left_digits = normalize_phone(left)
    right_digits = normalize_phone(right)
    if not left_digits or not right_digits:
        return False
    return left_digits == right_digits or left_digits[-10:] == right_digits[-10:]


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def fallback_candidates_from_root(root_type: str, root_item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for phone in multifield_values(root_item, "PHONE"):
        candidates.append({"type": "phone", "value": phone, "source": f"{root_type}.PHONE"})
    for email in multifield_values(root_item, "EMAIL"):
        candidates.append({"type": "email", "value": email, "source": f"{root_type}.EMAIL"})
    company_id = root_item.get("COMPANY_ID")
    if is_real_id(company_id):
        candidates.append({"type": "company_id", "value": str(company_id), "source": f"{root_type}.COMPANY_ID"})
    company_title = root_item.get("COMPANY_TITLE")
    if company_title:
        candidates.append({"type": "company_title", "value": str(company_title), "source": f"{root_type}.COMPANY_TITLE"})
    return candidates


def extract_entity_ids_from_duplicate_result(value: Any, entity_type: str) -> set[str]:
    entity_ids: set[str] = set()
    expected_key = entity_type.upper()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).upper() == expected_key:
                if isinstance(child, list):
                    entity_ids.update(str(item) for item in child if item)
                elif isinstance(child, dict):
                    entity_ids.update(str(item) for item in child.keys() if item)
                    entity_ids.update(str(item) for item in child.values() if isinstance(item, (str, int)) and item)
                elif child:
                    entity_ids.add(str(child))
            else:
                entity_ids.update(extract_entity_ids_from_duplicate_result(child, entity_type))
    elif isinstance(value, list):
        for child in value:
            entity_ids.update(extract_entity_ids_from_duplicate_result(child, entity_type))
    return entity_ids


def extract_contact_ids_from_duplicate_result(value: Any) -> set[str]:
    return extract_entity_ids_from_duplicate_result(value, "CONTACT")


def contact_matches_candidate(contact: dict[str, Any], candidate: dict[str, Any]) -> bool:
    candidate_type = str(candidate.get("type") or "")
    candidate_value = candidate.get("value")
    if candidate_type == "phone":
        return any(phones_match(phone, candidate_value) for phone in multifield_values(contact, "PHONE"))
    if candidate_type == "email":
        expected = normalize_email(candidate_value)
        return any(normalize_email(email) == expected for email in multifield_values(contact, "EMAIL"))
    return False


def lead_matches_candidate(lead: dict[str, Any], candidate: dict[str, Any]) -> bool:
    candidate_type = str(candidate.get("type") or "")
    candidate_value = candidate.get("value")
    if candidate_type == "phone":
        return any(phones_match(phone, candidate_value) for phone in multifield_values(lead, "PHONE"))
    if candidate_type == "email":
        expected = normalize_email(candidate_value)
        return any(normalize_email(email) == expected for email in multifield_values(lead, "EMAIL"))
    return False


def search_contact_ids_by_candidate(
    client: BitrixReadOnlyClient,
    candidate: dict[str, Any],
) -> tuple[set[str], list[dict[str, Any]]]:
    candidate_type = str(candidate.get("type") or "")
    value = str(candidate.get("value") or "").strip()
    attempts: list[dict[str, Any]] = []
    contact_ids: set[str] = set()
    if candidate_type not in {"phone", "email"} or not value:
        return contact_ids, attempts

    comm_type = "PHONE" if candidate_type == "phone" else "EMAIL"
    duplicate_payloads = [
        {"type": comm_type, "values": [value], "entity_type": "CONTACT"},
        {"type": comm_type, "values": [value]},
    ]
    for payload in duplicate_payloads:
        response = client.safe_call("crm.duplicate.findbycomm", payload)
        attempts.append({"method": "crm.duplicate.findbycomm", "payload": payload, "response": response})
        if response.get("ok"):
            contact_ids.update(extract_contact_ids_from_duplicate_result(get_result(response)))

    list_payload = {
        "order": {"ID": "ASC"},
        "filter": {comm_type: value},
        "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME", "PHONE", "EMAIL"],
    }
    list_response = client.safe_list_all("crm.contact.list", list_payload)
    attempts.append({"method": "crm.contact.list", "payload": list_payload, "response": list_response})
    for item in result_items(list_response):
        if item.get("ID"):
            contact_ids.add(str(item["ID"]))

    return contact_ids, attempts


def search_lead_ids_by_candidate(
    client: BitrixReadOnlyClient,
    candidate: dict[str, Any],
) -> tuple[set[str], list[dict[str, Any]]]:
    candidate_type = str(candidate.get("type") or "")
    value = str(candidate.get("value") or "").strip()
    attempts: list[dict[str, Any]] = []
    lead_ids: set[str] = set()
    if candidate_type not in {"phone", "email"} or not value:
        return lead_ids, attempts

    comm_type = "PHONE" if candidate_type == "phone" else "EMAIL"
    duplicate_payloads = [
        {"type": comm_type, "values": [value], "entity_type": "LEAD"},
        {"type": comm_type, "values": [value]},
    ]
    for payload in duplicate_payloads:
        response = client.safe_call("crm.duplicate.findbycomm", payload)
        attempts.append({"method": "crm.duplicate.findbycomm", "payload": payload, "response": response})
        if response.get("ok"):
            lead_ids.update(extract_entity_ids_from_duplicate_result(get_result(response), "LEAD"))

    list_payload = {
        "order": {"ID": "ASC"},
        "filter": {comm_type: value},
        "select": ["*", "UF_*"],
    }
    list_response = client.safe_list_all("crm.lead.list", list_payload)
    attempts.append({"method": "crm.lead.list", "payload": list_payload, "response": list_response})
    for item in result_items(list_response):
        if item.get("ID"):
            lead_ids.add(str(item["ID"]))

    return lead_ids, attempts


def resolve_contact_ids_by_fallback(
    client: BitrixReadOnlyClient,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    verified_matches: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        found_ids, candidate_attempts = search_contact_ids_by_candidate(client, candidate)
        attempts.extend(candidate_attempts)
        for contact_id in sorted(found_ids):
            response = fetch_entity_by_id(client, "crm.contact.get", contact_id)
            attempts.append(
                {
                    "method": "crm.contact.get",
                    "payload": {"id": contact_id},
                    "candidate": candidate,
                    "response": response,
                }
            )
            contact = result_item(response)
            if contact and contact_matches_candidate(contact, candidate):
                verified_matches[contact_id] = {
                    "contact_id": contact_id,
                    "matched_by": candidate.get("type"),
                    "source": candidate.get("source"),
                    "value": candidate.get("value"),
                }

    verified_contact_ids = sorted(verified_matches.keys(), key=lambda item: int(item) if item.isdigit() else item)
    return {
        "attempts": attempts,
        "verified_matches": list(verified_matches.values()),
        "verified_contact_ids": verified_contact_ids,
        "auto_contact_ids": verified_contact_ids if len(verified_contact_ids) == 1 else [],
        "ambiguous": len(verified_contact_ids) > 1,
    }


def resolve_lead_ids_by_fallback(
    client: BitrixReadOnlyClient,
    candidates: list[dict[str, Any]],
    *,
    root_lead_id: str | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    verified_matches: dict[str, dict[str, Any]] = {}
    lead_responses: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        found_ids, candidate_attempts = search_lead_ids_by_candidate(client, candidate)
        attempts.extend(candidate_attempts)
        for lead_id in sorted(found_ids):
            if root_lead_id and str(lead_id) == str(root_lead_id):
                continue
            response = fetch_entity_by_id(client, "crm.lead.get", lead_id)
            attempts.append(
                {
                    "method": "crm.lead.get",
                    "payload": {"id": lead_id},
                    "candidate": candidate,
                    "response": response,
                }
            )
            lead = result_item(response)
            if lead and lead_matches_candidate(lead, candidate):
                lead_responses[str(lead_id)] = response
                verified_matches[str(lead_id)] = {
                    "lead_id": str(lead_id),
                    "matched_by": candidate.get("type"),
                    "source": candidate.get("source"),
                    "value": candidate.get("value"),
                }

    verified_lead_ids = sorted(verified_matches.keys(), key=lambda item: int(item) if item.isdigit() else item)
    return {
        "attempts": attempts,
        "verified_matches": list(verified_matches.values()),
        "verified_lead_ids": verified_lead_ids,
        "lead_responses": lead_responses,
    }


def fetch_contacts(client: BitrixReadOnlyClient, contact_ids: list[str]) -> dict[str, Any]:
    ids = sorted({str(item).strip() for item in contact_ids if is_real_id(item)})
    requests_to_run = [
        (f"contact:{contact_id}", "crm.contact.get", {"id": contact_id})
        for contact_id in ids
    ]
    batch = getattr(client, "safe_batch_call", None)
    responses = (
        batch(requests_to_run)
        if callable(batch)
        else {
            key: client.safe_call(method, payload)
            for key, method, payload in requests_to_run
        }
    )
    return {
        contact_id: responses[f"contact:{contact_id}"]
        for contact_id in ids
    }


def fetch_company(client: BitrixReadOnlyClient, company_id: Any) -> dict[str, Any] | None:
    return fetch_entity_by_id(client, "crm.company.get", company_id) if is_real_id(company_id) else None


def fetch_contact_deals(client: BitrixReadOnlyClient, contact_id: str) -> dict[str, Any]:
    payload = {
        "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
        "filter": {"CONTACT_ID": contact_id},
        "select": ["*", "UF_*"],
    }
    return client.safe_list_all("crm.deal.list", payload)


def fetch_deals_by_lead_id(client: BitrixReadOnlyClient, lead_id: str) -> dict[str, Any]:
    payload = {
        "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
        "filter": {"LEAD_ID": str(lead_id)},
        "select": ["*", "UF_*"],
    }
    return client.safe_list_all("crm.deal.list", payload)


def deal_summary(deal: dict[str, Any], stage_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage_id = str(deal.get("STAGE_ID") or "")
    stage_info = stage_lookup.get(stage_id) or {}
    stage = stage_info.get("stage") or {}
    pipeline = stage_info.get("pipeline") or {}
    return {
        "id": str(deal.get("ID") or ""),
        "title": deal.get("TITLE"),
        "category_id": deal.get("CATEGORY_ID"),
        "pipeline": pipeline,
        "stage_id": stage_id,
        "stage": stage,
        "stage_name": stage.get("name") or stage_id,
        "semantic_id": deal.get("STAGE_SEMANTIC_ID"),
        "opportunity": deal.get("OPPORTUNITY"),
        "currency_id": deal.get("CURRENCY_ID"),
        "date_create": deal.get("DATE_CREATE"),
        "date_modify": deal.get("DATE_MODIFY"),
        "closedate": deal.get("CLOSEDATE"),
        "assigned_by_id": deal.get("ASSIGNED_BY_ID"),
        "source_id": deal.get("SOURCE_ID"),
        "lead_id": deal.get("LEAD_ID"),
        "contact_id": deal.get("CONTACT_ID"),
        "company_id": deal.get("COMPANY_ID"),
        "is_closed": str(deal.get("CLOSED") or "").upper() in ("Y", "1", "TRUE"),
        "raw": deal,
    }


def lead_summary(lead: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(lead.get("ID") or ""),
        "title": lead.get("TITLE"),
        "status_id": lead.get("STATUS_ID"),
        "status_semantic_id": lead.get("STATUS_SEMANTIC_ID"),
        "opportunity": lead.get("OPPORTUNITY"),
        "currency_id": lead.get("CURRENCY_ID"),
        "date_create": lead.get("DATE_CREATE"),
        "date_modify": lead.get("DATE_MODIFY"),
        "date_closed": lead.get("DATE_CLOSED"),
        "assigned_by_id": lead.get("ASSIGNED_BY_ID"),
        "source_id": lead.get("SOURCE_ID"),
        "contact_id": lead.get("CONTACT_ID"),
        "company_id": lead.get("COMPANY_ID"),
        "raw": lead,
    }


def root_entity_response(client: BitrixReadOnlyClient, root_type: str, root_id: str) -> dict[str, Any]:
    if root_type == "lead":
        return fetch_entity_by_id(client, "crm.lead.get", root_id)
    if root_type == "deal":
        return fetch_entity_by_id(client, "crm.deal.get", root_id)
    raise ValueError(f"Unsupported root_type: {root_type}")


def owner_type_id(entity_type: str) -> int:
    if entity_type == "lead":
        return LEAD_OWNER_TYPE_ID
    if entity_type == "deal":
        return DEAL_OWNER_TYPE_ID
    if entity_type == "contact":
        return CONTACT_OWNER_TYPE_ID
    raise ValueError(f"Unsupported entity_type: {entity_type}")


def fetch_entity_history(
    client: BitrixReadOnlyClient,
    entity_type: str,
    entity_id: str,
    period: dict[str, Any],
    *,
    previous_history: dict[str, Any] | None = None,
    updated_after: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(MSK_TZ).isoformat()
    owner_id = owner_type_id(entity_type)
    incremental = bool(previous_history and updated_after)
    activities_response = fetch_activities_for_owner(
        client,
        owner_id,
        entity_id,
        updated_after=updated_after if incremental else None,
    )
    previous_activities = result_items((previous_history or {}).get("activities"))
    current_activities = result_items(activities_response)
    merged_activities = merge_items_by_id(previous_activities, current_activities)
    activities = merge_items_by_id(previous_activities, [
        item
        for item in merged_activities
        if in_period_by_any_date(item, period, ("START_TIME", "DEADLINE", "CREATED", "LAST_UPDATED"))
    ])
    activity_details = fetch_activity_details(client, activities)
    timeline_response = fetch_timeline_comments(
        client,
        entity_id,
        entity_type=entity_type,
        owner_type_id=owner_id,
        created_after=source_since(((previous_history or {}).get("timeline_comments") or [{}])[0]) if incremental else None,
        known=[row for response in (previous_history or {}).get("timeline_comments", []) for row in response.get("items", [])],
    )
    current_timeline = [item for attempt in timeline_response for item in result_items(attempt)]
    previous_timeline = (
        [
            item
            for attempt in (previous_history or {}).get("timeline_comments") or []
            for item in result_items(attempt)
        ]
    )
    merged_timeline = merge_items_by_id(previous_timeline, current_timeline)
    timeline_items = merge_items_by_id(previous_timeline, [
        item
        for item in merged_timeline
        if in_period_by_any_date(item, period, ("CREATED", "DATE_CREATE"))
    ])
    base_timeline_response = dict(timeline_response[0]) if timeline_response else {"ok": False, "items": []}
    base_timeline_response["items"] = timeline_items
    old_timeline = ((previous_history or {}).get("timeline_comments") or [{}])[0]
    base_timeline_response["last_success_at"] = started_at if base_timeline_response.get("ok") else old_timeline.get("last_success_at")
    filtered_timeline = [base_timeline_response]
    activities_container = dict(activities_response)
    activities_container["items"] = activities
    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "sync_mode": "incremental" if incremental else "full",
        "updated_after": updated_after if incremental else None,
        "activities": activities_container,
        "activity_details": activity_details,
        "timeline_comments": filtered_timeline,
    }


def activity_type(activity: dict[str, Any]) -> str:
    type_id = str(activity.get("TYPE_ID") or "")
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()
    if type_id == "2" or "CALL" in provider or "TELPHIN" in provider:
        return "call"
    if type_id == "4" or "EMAIL" in provider:
        return "email"
    if any(token in provider for token in ("IM", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")):
        return "message"
    if type_id == "6" or "TASK" in provider or "TODO" in provider:
        return "task"
    return "activity"


def is_openline_activity(activity: dict[str, Any]) -> bool:
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()
    return "OPENLINE" in provider or "OPEN_LINE" in provider


def merge_activity_detail(activity: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or "")
    detail_container = details.get(activity_id)
    detail = result_item(detail_container) if isinstance(detail_container, dict) else {}
    return {**activity, **detail} if detail else dict(activity)


def raw_activities_by_id(bundle: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """One CRM activity_id -> one merged raw activity from a saved customer-history bundle."""
    rows: dict[str, dict[str, Any]] = {}
    data = bundle if isinstance(bundle, dict) else {}
    for history in (data.get("activities_by_entity") or {}).values():
        if not isinstance(history, dict):
            continue
        details = history.get("activity_details") if isinstance(history.get("activity_details"), dict) else {}
        for activity in result_items(history.get("activities")):
            merged = merge_activity_detail(activity, details)
            activity_id = str(merged.get("ID") or merged.get("id") or "").strip()
            if activity_id:
                rows.setdefault(activity_id, merged)
    for item in data.get("client_touchpoints") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        activity_id = str(item.get("id") or raw.get("ID") or "").strip()
        if activity_id:
            rows.setdefault(activity_id, raw or item)
    return rows


def client_day_scope_entity_keys(
    bundle: dict[str, Any] | None,
    *,
    deal_id: str = "",
    lead_id: str = "",
) -> set[str]:
    """Tracked deal, explicit source lead, and related deals already in this saved bundle.

    Contact keys already stored in the same bundle keep existing mirror behaviour.
    Other leads are not added: only the explicit LEAD_ID is in scope.
    """
    keys: set[str] = set()
    if str(deal_id or "").strip():
        keys.add(f"deal:{str(deal_id).strip()}")
    if str(lead_id or "").strip():
        keys.add(f"lead:{str(lead_id).strip()}")
    data = bundle if isinstance(bundle, dict) else {}
    for deal in data.get("related_deals") or []:
        related_id = str(deal.get("id") or "").strip() if isinstance(deal, dict) else ""
        if related_id:
            keys.add(f"deal:{related_id}")
    for collection in ("activities_by_entity", "timeline_comments_by_entity"):
        mapping = data.get(collection)
        if not isinstance(mapping, dict):
            continue
        for entity_key in mapping:
            kind, _, entity_id = str(entity_key).partition(":")
            if kind in {"deal", "contact"} and entity_id.strip():
                keys.add(f"{kind}:{entity_id.strip()}")
    contacts = data.get("contacts")
    if isinstance(contacts, dict):
        for contact_id in contacts:
            if str(contact_id).strip():
                keys.add(f"contact:{str(contact_id).strip()}")
    return keys


def event_in_client_day_scope(event: dict[str, Any], eligible_keys: set[str]) -> bool:
    """Return whether a normalized event belongs to the tracked deal's client-day scope."""
    if not eligible_keys:
        return True
    entity_keys = {str(value) for value in event.get("entity_keys") or [] if value}
    if event.get("entity_key"):
        entity_keys.add(str(event["entity_key"]))
    if event.get("entity_type") and event.get("entity_id"):
        entity_keys.add(f"{event['entity_type']}:{event['entity_id']}")
    if not entity_keys:
        return True
    return bool(entity_keys & eligible_keys)


def timeline_comment_items(history: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for attempt in history.get("timeline_comments") or []:
        for item in result_items(attempt):
            text = clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"), 500)
            identity = (str(item.get("ID") or ""), str(item.get("CREATED") or item.get("DATE_CREATE") or ""), text)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(item)
    return rows


def build_history_sections(bundle: dict[str, Any]) -> dict[str, Any]:
    client_touchpoints: list[dict[str, Any]] = []
    internal_context: list[dict[str, Any]] = []
    tasks_and_control: list[dict[str, Any]] = []
    system_events: list[dict[str, Any]] = []
    ignored_openline_events: list[dict[str, Any]] = []

    for entity_key, history in (bundle.get("activities_by_entity") or {}).items():
        entity_type = history.get("entity_type")
        entity_id = str(history.get("entity_id") or "")
        details = history.get("activity_details") or {}
        for activity in result_items(history.get("activities")):
            item = merge_activity_detail(activity, details)
            kind = activity_type(item)
            row = {
                "when": item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE"),
                "category": "activity",
                "event_type": kind,
                "entity_key": entity_key,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "id": str(item.get("ID") or ""),
                "subject": clean_text(item.get("SUBJECT"), 300),
                "text": clean_text(item.get("DESCRIPTION"), 900),
                "direction": item.get("DIRECTION"),
                "completed": item.get("COMPLETED"),
                "raw": item,
            }
            if is_openline_activity(item):
                ignored_openline_events.append(row)
            elif kind in {"call", "email", "message"}:
                client_touchpoints.append(row)
            elif kind == "task":
                tasks_and_control.append(row)
            else:
                system_events.append(row)

        for comment in timeline_comment_items(history):
            row = {
                "when": comment.get("CREATED") or comment.get("DATE_CREATE"),
                "category": "timeline_comment",
                "event_type": "internal_comment",
                "entity_key": entity_key,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "id": str(comment.get("ID") or ""),
                "author_id": comment.get("AUTHOR_ID") or comment.get("CREATED_BY"),
                "text": clean_text(comment.get("COMMENT") or comment.get("TEXT") or comment.get("DESCRIPTION"), 1200),
                "raw": comment,
            }
            internal_context.append(row)

    for deal in bundle.get("related_deals") or []:
        system_events.append(
            {
                "when": deal.get("date_modify") or deal.get("date_create"),
                "category": "deal_state",
                "event_type": "related_deal_current_state",
                "entity_key": f"deal:{deal.get('id')}",
                "entity_type": "deal",
                "entity_id": str(deal.get("id") or ""),
                "id": str(deal.get("id") or ""),
                "subject": clean_text(deal.get("title"), 300),
                "text": clean_text(
                    f"Воронка: {(deal.get('pipeline') or {}).get('name') or deal.get('category_id')}; "
                    f"стадия: {deal.get('stage_name')}; сумма: {deal.get('opportunity')} {deal.get('currency_id') or ''}",
                    700,
                ),
            }
        )

    for lead in bundle.get("related_leads") or []:
        system_events.append(
            {
                "when": lead.get("date_modify") or lead.get("date_create"),
                "category": "lead_state",
                "event_type": "related_lead_current_state",
                "entity_key": f"lead:{lead.get('id')}",
                "entity_type": "lead",
                "entity_id": str(lead.get("id") or ""),
                "id": str(lead.get("id") or ""),
                "subject": clean_text(lead.get("title"), 300),
                "text": clean_text(
                    f"Статус: {lead.get('status_id')}; сумма: {lead.get('opportunity')} {lead.get('currency_id') or ''}",
                    700,
                ),
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("when") or ""), str(item.get("id") or ""))

    return {
        "client_touchpoints": sorted(client_touchpoints, key=sort_key),
        "internal_context": sorted(internal_context, key=sort_key),
        "tasks_and_control": sorted(tasks_and_control, key=sort_key),
        "system_events": sorted(system_events, key=sort_key),
        "ignored_openline_events": sorted(ignored_openline_events, key=sort_key),
        "unified_timeline": sorted(
            client_touchpoints + internal_context + tasks_and_control + system_events,
            key=sort_key,
        ),
    }


def build_deal_normalized_communications(
    raw_bundle: dict[str, Any],
    customer_history_bundle: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Use the customer ledger, or adapt the saved raw deal context to that same normalizer."""
    if isinstance(customer_history_bundle, dict):
        return build_normalized_communications(customer_history_bundle)

    deal = (raw_bundle.get("deal") or {}).get("item") or {}
    deal_id = str(deal.get("ID") or raw_bundle.get("deal_id") or "")
    histories: dict[str, Any] = {}
    if deal_id:
        histories[f"deal:{deal_id}"] = {
            "entity_type": "deal",
            "entity_id": deal_id,
            "activities": raw_bundle.get("activities") or {},
            "activity_details": raw_bundle.get("activity_details") or {},
            "timeline_comments": raw_bundle.get("timeline_comments") or [],
        }
    source_lead = raw_bundle.get("source_lead") or {}
    lead_id = str(source_lead.get("lead_id") or "")
    if lead_id:
        histories[f"lead:{lead_id}"] = {
            **source_lead,
            "entity_type": "lead",
            "entity_id": lead_id,
        }
    adapted: dict[str, Any] = {
        "root_entity": {"type": "deal", "id": deal_id},
        "deal": raw_bundle.get("deal"),
        "lead": source_lead.get("lead"),
        "contacts": raw_bundle.get("contacts") or {},
        "activities_by_entity": histories,
    }
    contact = raw_bundle.get("contact")
    if not adapted["contacts"] and isinstance(contact, dict):
        contact_item = result_item(contact)
        contact_id = str(contact_item.get("ID") or "")
        if contact_id:
            adapted["contacts"] = {contact_id: contact}
    adapted.update(build_history_sections(adapted))
    return build_normalized_communications(adapted)


def internal_im_chat_targets(
    *,
    root_type: str,
    root_id: str,
    root_item: dict[str, Any],
    related_deals: list[dict[str, Any]],
    related_leads: list[dict[str, Any]],
) -> list[dict[str, str]]:
    targets: dict[str, dict[str, str]] = {}

    def add(entity_type: str, entity_id: Any, title: Any) -> None:
        if not is_real_id(entity_id):
            return
        normalized_type = str(entity_type).lower().strip()
        if normalized_type not in {"deal", "lead"}:
            return
        key = f"{normalized_type}:{str(entity_id).strip()}"
        targets[key] = {
            "entity_type": normalized_type,
            "entity_id": str(entity_id).strip(),
            "title": clean_text(title, 300),
        }

    add(root_type, root_id, root_item.get("TITLE") or root_item.get("NAME") or root_item.get("COMPANY_TITLE"))
    for deal in related_deals:
        add("deal", deal.get("id"), deal.get("title"))
    for lead in related_leads:
        add("lead", lead.get("id"), lead.get("title"))
    return list(targets.values())


def internal_im_unavailable_sources(internal_chats_by_entity: dict[str, Any]) -> list[dict[str, Any]]:
    unavailable: list[dict[str, Any]] = []
    for entity_key, chat_bundle in internal_chats_by_entity.items():
        for response in chat_bundle.get("search_responses") or []:
            if not response.get("ok"):
                unavailable.append(
                    {
                        "source": "im.search.chat.list",
                        "entity": entity_key,
                        "reason": response.get("error") or "unknown_error",
                    }
                )
        for chat in chat_bundle.get("chats") or []:
            for source, response_key in (
                ("im.chat.get", "chat_response"),
                ("im.dialog.messages.get", "messages_response"),
                ("im.dialog.users.list", "users_response"),
            ):
                response = chat.get(response_key) or {}
                if not response.get("ok"):
                    unavailable.append(
                        {
                            "source": source,
                            "entity": entity_key,
                            "chat_id": chat.get("chat_id"),
                            "reason": response.get("error") or "unknown_error",
                        }
                    )
    return unavailable


def unavailable_sources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    unavailable: list[dict[str, Any]] = [
        {
            "source": "task_comments",
            "reason": "not_implemented",
            "note": "Комментарии к задачам не выгружаются отдельным методом в текущем MVP.",
        }
    ]
    for entity_key, history in (bundle.get("activities_by_entity") or {}).items():
        activities = history.get("activities") or {}
        if not activities.get("ok"):
            unavailable.append({"source": "crm.activity.list", "entity": entity_key, "reason": activities.get("error")})
        timeline_attempts = history.get("timeline_comments") or []
        timeline_has_success = any(attempt.get("ok") for attempt in timeline_attempts)
        if not timeline_has_success:
            for attempt in timeline_attempts:
                if not attempt.get("ok"):
                    unavailable.append(
                        {"source": "crm.timeline.comment.list", "entity": entity_key, "reason": attempt.get("error")}
                    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in unavailable:
        key = (str(item.get("source") or ""), str(item.get("entity") or ""), str(item.get("reason") or item.get("note") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def tasks_by_entity(activities_by_entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for entity_key, history in activities_by_entity.items():
        details = history.get("activity_details") or {}
        tasks = []
        for activity in result_items(history.get("activities")):
            item = merge_activity_detail(activity, details)
            if activity_type(item) == "task":
                tasks.append(item)
        rows[entity_key] = tasks
    return rows


def build_customer_history_bundle(
    client: BitrixReadOnlyClient,
    *,
    root_type: str,
    root_id: str,
    history_days: int = DEFAULT_HISTORY_DAYS,
    include_internal_context: bool = True,
    pipeline_map_path: Path | None = None,
    previous_bundle: dict[str, Any] | None = None,
    root_response_override: dict[str, Any] | None = None,
    root_history_override: dict[str, Any] | None = None,
    root_contact_items_override: dict[str, Any] | None = None,
    preloaded_contacts: dict[str, Any] | None = None,
    preloaded_companies: dict[str, Any] | None = None,
    force_full: bool = False,
    rediscover_chats: bool = False,
) -> dict[str, Any]:
    root_type = root_type.lower().strip()
    if root_type not in {"lead", "deal"}:
        raise ValueError("root_type must be 'lead' or 'deal'")

    period = history_period(history_days)
    sync_started_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    stage_lookup = build_stage_lookup(pipeline_map_path)
    diagnostics: dict[str, Any] = {
        "missing_contact": False,
        "contact_id_missing": False,
        "fallback_match_used": False,
        "fallback_candidates": [],
        "fallback_matches": [],
        "fallback_lead_matches": [],
        "fallback_attempts": [],
        "fallback_related_leads_used": False,
        "unavailable_sources": [],
        "warnings": [],
    }

    root_response = root_response_override or root_entity_response(client, root_type, root_id)
    root_item = result_item(root_response)
    contact_items_response: dict[str, Any] | None = None
    related_lead_responses: dict[str, dict[str, Any]] = {}
    if root_type == "deal":
        if root_contact_items_override is not None:
            contact_items_response = root_contact_items_override
            contact_ids = {str(item).strip() for item in as_list(root_item.get("CONTACT_ID")) if is_real_id(item)}
            for item in get_result(contact_items_response) or []:
                if isinstance(item, dict) and is_real_id(item.get("CONTACT_ID")):
                    contact_ids.add(str(item["CONTACT_ID"]).strip())
            contact_ids = sorted(contact_ids)
        else:
            contact_ids, contact_items_response = contact_ids_from_deal(client, root_id, root_item)
    else:
        contact_ids = contact_ids_from_lead(root_item)

    if not contact_ids:
        diagnostics["contact_id_missing"] = True
        diagnostics["fallback_candidates"] = fallback_candidates_from_root(root_type, root_item)
        fallback_result = resolve_contact_ids_by_fallback(client, diagnostics["fallback_candidates"])
        diagnostics["fallback_attempts"] = fallback_result["attempts"]
        diagnostics["fallback_matches"] = fallback_result["verified_matches"]
        if fallback_result["auto_contact_ids"]:
            contact_ids = fallback_result["auto_contact_ids"]
            diagnostics["fallback_match_used"] = True
            diagnostics["warnings"].append(
                "CONTACT_ID отсутствовал, контакт найден и подтвержден через fallback по телефону/email."
            )
        elif root_type == "lead":
            lead_fallback = resolve_lead_ids_by_fallback(
                client,
                diagnostics["fallback_candidates"],
                root_lead_id=root_id,
            )
            diagnostics["fallback_attempts"].extend(lead_fallback["attempts"])
            diagnostics["fallback_lead_matches"] = lead_fallback["verified_matches"]
            related_lead_responses = lead_fallback["lead_responses"]
            related_contact_ids = []
            for response in related_lead_responses.values():
                related_contact_ids.extend(contact_ids_from_lead(result_item(response)))
            if related_contact_ids:
                contact_ids = sorted({str(item).strip() for item in related_contact_ids if is_real_id(item)})
                diagnostics["fallback_match_used"] = True
                diagnostics["fallback_related_leads_used"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, контакт найден через подтвержденный дубль-лид по телефону/email."
                )
            elif related_lead_responses:
                diagnostics["missing_contact"] = True
                diagnostics["fallback_related_leads_used"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, fallback нашел подтвержденные дубль-лиды, но контакта в них нет."
                )
            elif fallback_result["ambiguous"]:
                diagnostics["missing_contact"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, fallback нашел несколько подтвержденных контактов. Автосклейка не применена."
                )
            else:
                diagnostics["missing_contact"] = True
                diagnostics["warnings"].append(
                    "Контакт по CONTACT_ID не найден. Fallback по телефону/email не нашел подтвержденный контакт или дубль-лид."
                )
        elif fallback_result["ambiguous"]:
            diagnostics["missing_contact"] = True
            diagnostics["warnings"].append(
                "CONTACT_ID отсутствовал, fallback нашел несколько подтвержденных контактов. Автосклейка не применена."
            )
        else:
            diagnostics["missing_contact"] = True
            diagnostics["warnings"].append(
                "Контакт по CONTACT_ID не найден. Fallback по телефону/email не нашел подтвержденный контакт."
            )

    contacts = {
        contact_id: value
        for contact_id, value in (preloaded_contacts or {}).items()
        if contact_id in contact_ids and isinstance(value, dict)
    }
    missing_contact_ids = [contact_id for contact_id in contact_ids if contact_id not in contacts]
    contacts.update(fetch_contacts(client, missing_contact_ids))
    primary_contact_id = contact_ids[0] if contact_ids else None

    related_deals_by_id: dict[str, dict[str, Any]] = {}
    contact_deal_responses: dict[str, Any] = {}
    lead_deal_responses: dict[str, Any] = {}
    if root_type == "lead" and root_item.get("ID"):
        lead_id = str(root_item["ID"])
        response = fetch_deals_by_lead_id(client, lead_id)
        lead_deal_responses[lead_id] = response
        if not response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки лида {lead_id}: {response.get('error')}")
        for deal in result_items(response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    for lead_id, response in related_lead_responses.items():
        lead_deal_response = fetch_deals_by_lead_id(client, str(lead_id))
        lead_deal_responses[str(lead_id)] = lead_deal_response
        if not lead_deal_response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки дубль-лида {lead_id}: {lead_deal_response.get('error')}")
            continue
        for deal in result_items(lead_deal_response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    for contact_id in contact_ids:
        response = fetch_contact_deals(client, contact_id)
        contact_deal_responses[contact_id] = response
        if not response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки контакта {contact_id}: {response.get('error')}")
            continue
        for deal in result_items(response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    if root_type == "deal" and root_item.get("ID"):
        related_deals_by_id[str(root_item["ID"])] = root_item

    related_deals = [
        deal_summary(deal, stage_lookup)
        for deal in sorted(related_deals_by_id.values(), key=lambda item: int(item.get("ID") or 0))
    ]
    related_leads = [
        lead_summary(result_item(response))
        for _lead_id, response in sorted(
            related_lead_responses.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
        )
    ]

    company_ids = {str(item.get("company_id")).strip() for item in related_deals if is_real_id(item.get("company_id"))}
    company_ids.update(str(item.get("company_id")).strip() for item in related_leads if is_real_id(item.get("company_id")))
    if root_type == "lead" and is_real_id(root_item.get("COMPANY_ID")):
        company_ids.add(str(root_item["COMPANY_ID"]).strip())
    companies = {
        company_id: value
        for company_id, value in (preloaded_companies or {}).items()
        if company_id in company_ids and isinstance(value, dict)
    }
    for company_id in sorted(company_ids):
        if company_id not in companies:
            companies[company_id] = fetch_company(client, company_id)

    activities_by_entity: dict[str, Any] = {}
    previous_activities = (previous_bundle or {}).get("activities_by_entity") or {}
    updated_after = None if force_full else incremental_since(previous_bundle)
    if root_item:
        root_key = f"{root_type}:{root_id}"
        activities_by_entity[root_key] = root_history_override or fetch_entity_history(
            client,
            root_type,
            root_id,
            period,
            previous_history=previous_activities.get(root_key),
            updated_after=updated_after,
        )
        if root_history_override and previous_activities.get(root_key):
            history = copy.deepcopy(root_history_override)
            old = previous_activities[root_key]
            history["activities"]["items"] = merge_items_by_id(old.get("activities", {}).get("items", []), history.get("activities", {}).get("items", []))
            history["activity_details"] = activity_details_from_list(history["activities"]["items"])
            old_comments = [row for response in old.get("timeline_comments", []) for row in response.get("items", [])]
            if history.get("timeline_comments"):
                history["timeline_comments"][0]["items"] = merge_items_by_id(old_comments, history["timeline_comments"][0].get("items", []))
            activities_by_entity[root_key] = history
    for lead in related_leads:
        lead_id = str(lead.get("id") or "")
        entity_key = f"lead:{lead_id}"
        if lead_id and entity_key not in activities_by_entity:
            activities_by_entity[entity_key] = fetch_entity_history(
                client, "lead", lead_id, period,
                previous_history=previous_activities.get(entity_key), updated_after=updated_after,
            )
    for contact_id in contact_ids:
        entity_key = f"contact:{contact_id}"
        if entity_key not in activities_by_entity:
            activities_by_entity[entity_key] = fetch_entity_history(
                client, "contact", contact_id, period,
                previous_history=previous_activities.get(entity_key), updated_after=updated_after,
            )
    for deal in related_deals:
        deal_id = str(deal.get("id") or "")
        entity_key = f"deal:{deal_id}"
        if deal_id and entity_key not in activities_by_entity:
            activities_by_entity[entity_key] = fetch_entity_history(
                client, "deal", deal_id, period,
                previous_history=previous_activities.get(entity_key), updated_after=updated_after,
            )
    # A missing relationship or failed discovery is not evidence that previously
    # collected history never existed. Keep it locally with an explicit marker.
    for entity_key, history in previous_activities.items():
        if entity_key not in activities_by_entity:
            activities_by_entity[entity_key] = {**copy.deepcopy(history), "historical_link": True}
    activity_sync_ok = bool(activities_by_entity) and all(
        bool((history.get("activities") or {}).get("ok"))
        for history in activities_by_entity.values()
        if not history.get("historical_link")
    )
    activity_cursor = sync_started_at if activity_sync_ok else activity_cursor_value(previous_bundle)

    bundle: dict[str, Any] = {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "read_only": True,
        "bundle_type": "customer_history_bundle",
        "root_entity": {
            "type": root_type,
            "id": str(root_id),
            "title": root_item.get("TITLE") or root_item.get("NAME") or root_item.get("COMPANY_TITLE"),
        },
        "history_period": period,
        "sync": {
            "mode": "incremental" if updated_after else "full",
            "updated_after": updated_after,
            "activity_cursor": activity_cursor,
            "activity_sync_ok": activity_sync_ok,
            "automatic_full_reconciliation": bool(force_full and previous_bundle),
        },
        "include_internal_context": bool(include_internal_context),
        "lead": root_response if root_type == "lead" else None,
        "deal": {"response": root_response, "item": root_item} if root_type == "deal" else None,
        "contact_resolution": {
            "strategy": (
                "fallback_phone_email"
                if diagnostics["fallback_match_used"]
                else "fallback_related_lead_phone_email"
                if diagnostics["fallback_related_leads_used"]
                else "CONTACT_ID"
            ),
            "primary_contact_id": primary_contact_id,
            "contact_ids": contact_ids,
            "deal_contact_items": contact_items_response,
            "contact_id_missing": diagnostics["contact_id_missing"],
        },
        "contacts": contacts,
        "contact": contacts.get(primary_contact_id) if primary_contact_id else None,
        "companies": companies,
        "contact_deal_responses": contact_deal_responses,
        "lead_deal_responses": lead_deal_responses,
        "related_leads": related_leads,
        "related_lead_responses": related_lead_responses,
        "related_deals": related_deals,
        "activities_by_entity": activities_by_entity,
        "timeline_comments_by_entity": {
            entity_key: history.get("timeline_comments") or []
            for entity_key, history in activities_by_entity.items()
        },
        "tasks_by_entity": tasks_by_entity(activities_by_entity),
        "diagnostics": diagnostics,
    }

    sections = build_history_sections(bundle)
    if not include_internal_context:
        sections["internal_context"] = []
        sections["unified_timeline"] = [
            item
            for item in sections["unified_timeline"]
            if item.get("category") not in {"timeline_comment", "internal_im_chat"}
        ]
    bundle.update(sections)
    internal_chats_by_entity: dict[str, Any] = {}
    internal_chat_rows: list[dict[str, Any]] = []
    if include_internal_context:
        for target in internal_im_chat_targets(
            root_type=root_type,
            root_id=root_id,
            root_item=root_item,
            related_deals=related_deals,
            related_leads=related_leads,
        ):
            entity_key = f"{target['entity_type']}:{target['entity_id']}"
            chat_bundle = fetch_internal_im_chats(
                client,
                entity_type=target["entity_type"],
                entity_id=target["entity_id"],
                title=target.get("title") or "",
                previous_bundle=((previous_bundle or {}).get("internal_im_chats_by_entity") or {}).get(entity_key),
                force_discovery=force_full or rediscover_chats,
                force_full=force_full,
            )
            internal_chats_by_entity[entity_key] = chat_bundle
            internal_chat_rows.extend(
                internal_chat_events(
                    chat_bundle,
                    source_entity_type=target["entity_type"],
                    source_entity_id=target["entity_id"],
                )
            )

    if include_internal_context:
        for entity_key, previous_chats in ((previous_bundle or {}).get("internal_im_chats_by_entity") or {}).items():
            if entity_key not in internal_chats_by_entity:
                historical = {**copy.deepcopy(previous_chats), "historical_link": True}
                internal_chats_by_entity[entity_key] = historical
                entity_type, entity_id = entity_key.split(":", 1)
                internal_chat_rows.extend(internal_chat_events(historical,
                    source_entity_type=entity_type, source_entity_id=entity_id))
    bundle["internal_im_chats_by_entity"] = internal_chats_by_entity
    append_internal_chat_events(bundle, internal_chat_rows)
    bundle["normalized_communications"] = build_normalized_communications(bundle)
    bundle["diagnostics"]["internal_im_chat"] = {
        "enabled": bool(include_internal_context),
        "entities_checked": sorted(internal_chats_by_entity.keys()),
        "events_added": len(internal_chat_rows),
        "chat_ids": sorted({str(event.get("chat_id")) for event in internal_chat_rows if event.get("chat_id")}),
    }
    bundle["diagnostics"]["unavailable_sources"] = unavailable_sources(bundle)
    bundle["diagnostics"]["unavailable_sources"].extend(internal_im_unavailable_sources(internal_chats_by_entity))
    if sections.get("ignored_openline_events"):
        bundle["diagnostics"]["warnings"].append(
            f"Открытые линии не использовались: проигнорировано событий {len(sections['ignored_openline_events'])}."
        )
    return bundle
