"""Pure canonical Bitrix state projections and upsert merge for schema v1."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


SCHEMA_ID = "canonical_bitrix_state"
SCHEMA_VERSION = "1"
ENTITY_TYPES = {"deal", "activity", "task", "timeline_comment", "im_message"}
SOURCE_ENTITY_TYPES = {
    "deal": "deal",
    "activities": "activity",
    "tasks": "task",
    "timeline_comments": "timeline_comment",
    "im_messages": "im_message",
}
REVISION_METADATA = {
    "deal": ("date_modify", "modify_by_id", "date_create"),
    "activity": ("last_updated", "created", "author_id", "editor_id"),
    "task": ("changed_date",),
    "timeline_comment": (),
    "im_message": (),
}


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _id(value: Any) -> str:
    normalized = "" if value is None else str(value).strip()
    if not normalized:
        raise ValueError("Canonical entity ID must not be empty")
    return normalized


def _text_hash(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_key(entity_type: str, source_id: Any) -> str:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Unsupported canonical entity type: {entity_type}")
    return f"{entity_type}:{_id(source_id)}"


def activity_subtype(raw: dict[str, Any]) -> str:
    by_type = {"2": "call", "4": "email", "6": "task"}
    type_id = _string(raw.get("TYPE_ID")).strip()
    if type_id in by_type:
        return by_type[type_id]

    provider = " ".join(
        (_string(raw.get("PROVIDER_ID")), _string(raw.get("PROVIDER_TYPE_ID")))
    ).upper()
    for subtype, markers in (
        ("call", ("CALL", "TELPHIN")),
        ("email", ("EMAIL",)),
        ("message", ("IM", "OPENLINE", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")),
        ("task", ("TASK", "TODO")),
    ):
        if any(marker in provider for marker in markers):
            return subtype
    return "other"


def normalize_file_ids(files: Any) -> list[str]:
    ids = set()
    for item in files or []:
        if not isinstance(item, dict):
            continue
        value = item.get("id")
        if value is None or str(value).strip() == "":
            value = item.get("ID")
        if value is not None and str(value).strip():
            ids.add(str(value).strip())
    return sorted(ids)


def _entity(
    entity_type: str,
    source_id: Any,
    semantic: dict[str, Any],
    metadata: dict[str, Any],
    *,
    subtype: str | None = None,
) -> dict[str, Any]:
    normalized_id = _id(source_id)
    revision_metadata = {key: metadata[key] for key in REVISION_METADATA[entity_type]}
    return {
        "key": canonical_key(entity_type, normalized_id),
        "entity_type": entity_type,
        "subtype": subtype,
        "source_id": normalized_id,
        "semantic": semantic,
        "metadata": metadata,
        "semantic_fingerprint": _fingerprint(semantic),
        "raw_fingerprint": _fingerprint(
            {"semantic": semantic, "revision_relevant_metadata": revision_metadata}
        ),
    }


def project_deal(raw: dict[str, Any]) -> dict[str, Any]:
    source_id = _id(raw.get("ID"))
    semantic = {
        "id": source_id,
        "title_hash": _text_hash(raw.get("TITLE")),
        "stage_id": _string(raw.get("STAGE_ID")),
        "category_id": _string(raw.get("CATEGORY_ID")),
        "opportunity": _string(raw.get("OPPORTUNITY")),
        "currency_id": _string(raw.get("CURRENCY_ID")),
        "assigned_by_id": _string(raw.get("ASSIGNED_BY_ID")),
        "closed": _string(raw.get("CLOSED")),
        "moved_time": _string(raw.get("MOVED_TIME")),
        "moved_by_id": _string(raw.get("MOVED_BY_ID")),
        "lead_id": _string(raw.get("LEAD_ID")),
        "contact_id": _string(raw.get("CONTACT_ID")),
        "company_id": _string(raw.get("COMPANY_ID")),
    }
    metadata = {
        "date_modify": _string(raw.get("DATE_MODIFY")),
        "modify_by_id": _string(raw.get("MODIFY_BY_ID")),
        "date_create": _string(raw.get("DATE_CREATE")),
        "source": "crm.deal.get",
    }
    return _entity("deal", source_id, semantic, metadata)


def _communications(values: Any) -> list[dict[str, str]]:
    projected = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        projected.append(
            {
                "entity_type_id": _string(value.get("ENTITY_TYPE_ID")),
                "entity_id": _string(value.get("ENTITY_ID")),
            }
        )
    return sorted(projected, key=lambda item: (item["entity_type_id"], item["entity_id"]))


def project_activity(
    raw: dict[str, Any], *, source: str = "crm.activity.list"
) -> dict[str, Any]:
    if source != "crm.activity.list":
        raise ValueError(f"Unsupported canonical activity source: {source}")
    source_id = _id(raw.get("ID"))
    settings = raw.get("SETTINGS") if isinstance(raw.get("SETTINGS"), dict) else {}
    missed_call = settings.get("MISSED_CALL")
    if not isinstance(missed_call, bool):
        missed_call = None
    subtype = activity_subtype(raw)
    semantic = {
        "kind": subtype,
        "type_id": _string(raw.get("TYPE_ID")),
        "provider_id": _string(raw.get("PROVIDER_ID")),
        "provider_type_id": _string(raw.get("PROVIDER_TYPE_ID")),
        "origin_id": _string(raw.get("ORIGIN_ID")),
        "subject_hash": _text_hash(raw.get("SUBJECT")),
        "description_hash": _text_hash(raw.get("DESCRIPTION")),
        "start_time": _string(raw.get("START_TIME")),
        "end_time": _string(raw.get("END_TIME")),
        "deadline": _string(raw.get("DEADLINE")),
        "completed": _string(raw.get("COMPLETED")),
        "status": _string(raw.get("STATUS")),
        "direction": _string(raw.get("DIRECTION")),
        "responsible_id": _string(raw.get("RESPONSIBLE_ID")),
        "owner_type_id": _string(raw.get("OWNER_TYPE_ID")),
        "owner_id": _string(raw.get("OWNER_ID")),
        "missed_call": missed_call,
        "file_ids": normalize_file_ids(raw.get("FILES")),
        "communications": _communications(raw.get("COMMUNICATIONS")),
    }
    metadata = {
        "last_updated": _string(raw.get("LAST_UPDATED")),
        "created": _string(raw.get("CREATED")),
        "author_id": _string(raw.get("AUTHOR_ID")),
        "editor_id": _string(raw.get("EDITOR_ID")),
        "source": source,
    }
    return _entity("activity", source_id, semantic, metadata, subtype=subtype)


def project_task(raw: dict[str, Any]) -> dict[str, Any]:
    source_id = _id(raw.get("id", raw.get("ID")))
    semantic = {
        "id": source_id,
        "status": _string(raw.get("status")),
        "deadline": _string(raw.get("deadline")),
        "closed_date": _string(raw.get("closedDate")),
        "responsible_id": _string(raw.get("responsibleId")),
        "title_hash": _text_hash(raw.get("title")),
        "description_hash": _text_hash(raw.get("description")),
    }
    metadata = {
        "changed_date": _string(raw.get("changedDate")),
        "source": "tasks.task.get",
    }
    return _entity("task", source_id, semantic, metadata)


def project_timeline_comment(raw: dict[str, Any]) -> dict[str, Any]:
    source_id = _id(raw.get("ID", raw.get("id")))
    semantic = {
        "id": source_id,
        "created": _string(raw.get("CREATED")),
        "author_id": _string(raw.get("AUTHOR_ID")),
        "comment_hash": _text_hash(raw.get("COMMENT")),
        "file_ids": normalize_file_ids(raw.get("FILES")),
    }
    return _entity("timeline_comment", source_id, semantic, {})


def project_im_message(raw: dict[str, Any]) -> dict[str, Any]:
    source_id = _id(raw.get("id", raw.get("ID")))
    semantic = {
        "id": source_id,
        "dialog_id": _string(raw.get("dialog_id")),
        "date": _string(raw.get("date")),
        "author_id": _string(raw.get("author_id")),
        "text_hash": _text_hash(raw.get("text")),
    }
    return _entity("im_message", source_id, semantic, {})


def _response_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("items"), list):
        return [item for item in value["items"] if isinstance(item, dict)]
    result = (value.get("response") or {}).get("result") if value.get("ok") else None
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def _response_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("ok"):
        return {}
    result = (value.get("response") or {}).get("result")
    return result if isinstance(result, dict) else {}


def _source_ok(value: Any) -> bool:
    return isinstance(value, dict) and value.get("ok") is True and value.get("refresh_ok") is not False


def merge_deal_bundle(
    state: dict[str, Any] | None,
    bundle: dict[str, Any],
    *,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project one persisted read-only deal bundle into canonical state."""
    deal = (bundle.get("deal") or {}).get("item") or {}
    activities = bundle.get("activities")
    task_responses = bundle.get("bitrix_tasks")
    task_chats = bundle.get("bitrix_task_chats")
    timeline_attempts = bundle.get("timeline_comments")
    source_status = {
        "deal": "ok" if deal.get("ID") else "failed",
        "activities": "ok" if _source_ok(activities) else "failed",
        "tasks": "ok" if isinstance(task_responses, dict) and all(
            _source_ok(response) for response in task_responses.values()
        ) else "failed",
        "timeline_comments": "ok" if isinstance(timeline_attempts, list) and timeline_attempts and all(
            _source_ok(attempt) for attempt in timeline_attempts
        ) else "failed",
        "im_messages": "ok" if isinstance(task_chats, dict) and all(
            _source_ok(response) for response in task_chats.values()
        ) else "failed",
    }
    entities = [project_deal(deal)] if source_status["deal"] == "ok" else []

    details = bundle.get("activity_details") or {}
    for row in _response_items(activities):
        activity_id = str(row.get("ID") or "")
        detail = _response_item(details.get(activity_id)) if isinstance(details, dict) else {}
        entities.append(project_activity({**row, **detail} if detail else row))

    for response in task_responses.values() if isinstance(task_responses, dict) else ():
        result = _response_item(response)
        task = result.get("task") if isinstance(result.get("task"), dict) else result
        if task.get("id") or task.get("ID"):
            entities.append(project_task(task))

    for attempt in timeline_attempts if isinstance(timeline_attempts, list) else ():
        entities.extend(project_timeline_comment(item) for item in _response_items(attempt))

    for task_id, response in task_chats.items() if isinstance(task_chats, dict) else ():
        task_result = _response_item(task_responses.get(task_id)) if isinstance(task_responses, dict) else {}
        task = task_result.get("task") if isinstance(task_result.get("task"), dict) else task_result
        chat_id = task.get("chatId") or task.get("chat_id")
        messages = _response_item(response).get("messages")
        if not chat_id or not isinstance(messages, list):
            continue
        for message in messages:
            if isinstance(message, dict) and (message.get("id") or message.get("ID")):
                entities.append(project_im_message({**message, "dialog_id": f"chat{chat_id}"}))

    return merge_canonical_state(
        state,
        owner={"entity_type": "deal", "entity_id": bundle.get("deal_id") or deal.get("ID")},
        observed_at=observed_at,
        source_status=source_status,
        entities=entities,
    )


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def _document_fingerprints(entities: dict[str, dict[str, Any]]) -> tuple[str, str]:
    semantic = {key: entity["semantic_fingerprint"] for key, entity in sorted(entities.items())}
    raw = {key: entity["raw_fingerprint"] for key, entity in sorted(entities.items())}
    return _fingerprint(semantic), _fingerprint(raw)


def _communications_index(entities: dict[str, dict[str, Any]]) -> dict[str, str]:
    prefixes = {
        "activity": "crm_activity",
        "timeline_comment": "crm_timeline_comment",
        "im_message": "internal_im_chat",
    }
    return {
        f"{prefixes[entity['entity_type']]}:{entity['source_id']}": key
        for key, entity in sorted(entities.items())
        if entity["entity_type"] in prefixes
    }


def merge_canonical_state(
    state: dict[str, Any] | None,
    *,
    owner: dict[str, Any],
    observed_at: str,
    source_status: dict[str, str],
    entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if owner.get("entity_type") != "deal":
        raise ValueError("Canonical state v1 supports deal owners only")
    normalized_owner = {"entity_type": "deal", "entity_id": _id(owner.get("entity_id"))}
    invalid_sources = set(source_status) - set(SOURCE_ENTITY_TYPES)
    invalid_statuses = {status for status in source_status.values() if status not in {"ok", "failed"}}
    if invalid_sources or invalid_statuses:
        raise ValueError("Unsupported canonical source status")

    previous = copy.deepcopy(state) if state is not None else None
    if previous is not None and previous.get("owner") != normalized_owner:
        raise ValueError("Canonical state owner does not match merge owner")
    current_entities = copy.deepcopy(previous.get("entities", {})) if previous else {}
    current_status = copy.deepcopy(previous.get("source_status", {})) if previous else {}
    current_status.update(source_status)
    failed_types = {
        SOURCE_ENTITY_TYPES[source]
        for source, status in source_status.items()
        if status == "failed"
    }

    entries = []
    for entity in sorted(entities, key=lambda item: item["key"]):
        entity_type = entity.get("entity_type")
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"Unsupported canonical entity type: {entity_type}")
        if entity_type in failed_types:
            continue
        incoming = copy.deepcopy(entity)
        key = incoming["key"]
        before = current_entities.get(key)
        if before is None:
            change_type = "NEW"
            changed_semantic = sorted(incoming["semantic"])
            changed_metadata = sorted(incoming["metadata"])
        elif before["semantic_fingerprint"] != incoming["semantic_fingerprint"]:
            change_type = "UPDATED_MEANINGFUL"
            changed_semantic = _changed_fields(before["semantic"], incoming["semantic"])
            changed_metadata = _changed_fields(before["metadata"], incoming["metadata"])
        elif before["raw_fingerprint"] != incoming["raw_fingerprint"]:
            change_type = "UPDATED_TECHNICAL"
            changed_semantic = []
            changed_metadata = _changed_fields(before["metadata"], incoming["metadata"])
        else:
            continue

        current_entities[key] = incoming
        entries.append(
            {
                "key": key,
                "entity_type": incoming["entity_type"],
                "subtype": incoming["subtype"],
                "change_type": change_type,
                "changed_semantic_fields": changed_semantic,
                "changed_metadata_fields": changed_metadata,
                "before": None if before is None else before["semantic"],
                "after": incoming["semantic"],
                "before_metadata": None if before is None else before["metadata"],
                "after_metadata": incoming["metadata"],
            }
        )

    current_entities = dict(sorted(current_entities.items()))
    semantic_fingerprint, raw_fingerprint = _document_fingerprints(current_entities)
    document = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "owner": normalized_owner,
        "observed_at": observed_at,
        "source_status": current_status,
        "entities": current_entities,
        "semantic_fingerprint": semantic_fingerprint,
        "raw_fingerprint": raw_fingerprint,
        "derived": {"communications_index": _communications_index(current_entities)},
    }
    delta = {
        "from_semantic_fingerprint": None if previous is None else previous["semantic_fingerprint"],
        "to_semantic_fingerprint": semantic_fingerprint,
        "from_raw_fingerprint": None if previous is None else previous["raw_fingerprint"],
        "to_raw_fingerprint": raw_fingerprint,
        "entries": sorted(entries, key=lambda entry: entry["key"]),
    }
    return document, delta
