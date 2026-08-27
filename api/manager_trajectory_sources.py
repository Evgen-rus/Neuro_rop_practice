"""Read-only Bitrix sources used by the manager trajectory collector.

The functions in this module deliberately do not know about SQLite or the
trajectory report projection.  They turn Bitrix responses into small,
source-aware facts so the caller can persist or project them as needed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable

from api.candidates import DEAL_OWNER_TYPE_ID, LEAD_OWNER_TYPE_ID, parse_bitrix_dt
from bitrix.customer_history import activity_type, messenger_mirror_from_comment
from bitrix.usage_trace import bitrix_trace_context
from setup import MSK_TZ


ACTIVITY_SELECT_V3 = [
    "ID",
    "OWNER_ID",
    "OWNER_TYPE_ID",
    "TYPE_ID",
    "PROVIDER_ID",
    "PROVIDER_TYPE_ID",
    "PROVIDER_GROUP_ID",
    "ASSOCIATED_ENTITY_ID",
    "RESPONSIBLE_ID",
    "AUTHOR_ID",
    "EDITOR_ID",
    "DIRECTION",
    "COMPLETED",
    "STATUS",
    "SUBJECT",
    "DESCRIPTION",
    "DESCRIPTION_TYPE",
    "START_TIME",
    "END_TIME",
    "DEADLINE",
    "CREATED",
    "LAST_UPDATED",
    "FILES",
    "COMMUNICATIONS",
    "RESULT_MARK",
    "RESULT_VALUE",
    "RESULT_SUM",
    "RESULT_CURRENCY_ID",
    "RESULT_STATUS",
    "RESULT_STREAM",
    "RESULT_SOURCE_ID",
    "SETTINGS",
    "PROVIDER_PARAMS",
    "PROVIDER_DATA",
    "IS_INCOMING_CHANNEL",
]


# Keep this list intentionally small.  Custom UF_CRM_* fields are added only
# through ``business_snapshot(..., custom_allowlist=...)``.
BUSINESS_FIELD_SELECT = {
    "deal": [
        "ID",
        "TITLE",
        "TYPE_ID",
        "CATEGORY_ID",
        "STAGE_ID",
        "STAGE_SEMANTIC_ID",
        "CLOSED",
        "OPPORTUNITY",
        "CURRENCY_ID",
        "PROBABILITY",
        "BEGINDATE",
        "CLOSEDATE",
        "SOURCE_ID",
        "SOURCE_DESCRIPTION",
        "ASSIGNED_BY_ID",
        "CREATED_BY_ID",
        "MODIFY_BY_ID",
        "DATE_CREATE",
        "DATE_MODIFY",
        "COMMENTS",
    ],
    "lead": [
        "ID",
        "TITLE",
        "STATUS_ID",
        "STATUS_SEMANTIC_ID",
        "SOURCE_ID",
        "SOURCE_DESCRIPTION",
        "OPPORTUNITY",
        "CURRENCY_ID",
        "ASSIGNED_BY_ID",
        "CREATED_BY_ID",
        "MODIFY_BY_ID",
        "DATE_CREATE",
        "DATE_MODIFY",
        "COMMENTS",
    ],
}


_FIELD_LABELS = {
    "ID": "Идентификатор",
    "TITLE": "Название",
    "TYPE_ID": "Тип",
    "CATEGORY_ID": "Воронка",
    "STAGE_ID": "Этап",
    "STAGE_SEMANTIC_ID": "Семантика этапа",
    "STATUS_ID": "Статус",
    "STATUS_SEMANTIC_ID": "Семантика статуса",
    "CLOSED": "Закрыта",
    "OPPORTUNITY": "Сумма",
    "CURRENCY_ID": "Валюта",
    "PROBABILITY": "Вероятность",
    "BEGINDATE": "Дата начала",
    "CLOSEDATE": "Дата завершения",
    "SOURCE_ID": "Источник",
    "SOURCE_DESCRIPTION": "Описание источника",
    "ASSIGNED_BY_ID": "Ответственный",
    "CREATED_BY_ID": "Создал",
    "MODIFY_BY_ID": "Изменил",
    "DATE_CREATE": "Дата создания",
    "DATE_MODIFY": "Дата изменения",
    "COMMENTS": "Комментарий карточки",
}


def _string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iso(value: Any) -> str | None:
    parsed = parse_bitrix_dt(value)
    if parsed is None:
        return _string(value)
    return parsed.astimezone(MSK_TZ).isoformat(timespec="seconds")


def _entity_ref(value: Any) -> tuple[str, str, str | None] | None:
    if isinstance(value, dict):
        entity_type = _string(value.get("entity_type") or value.get("type"))
        entity_id = _string(value.get("entity_id") or value.get("id"))
        manager_id = _string(value.get("manager_id"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        entity_type = _string(value[0])
        entity_id = _string(value[1])
        manager_id = _string(value[2]) if len(value) > 2 else None
    else:
        return None
    if entity_type not in {"deal", "lead"} or not entity_id:
        return None
    return entity_type, entity_id, manager_id


def _result_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    if not response or not response.get("ok"):
        return []
    if isinstance(response.get("items"), list):
        return [row for row in response["items"] if isinstance(row, dict)]
    result = (response.get("response") or {}).get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        for key in ("items", "list", "result"):
            rows = result.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _first_datetime(row: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = _iso(row.get(key))
        if value:
            return value
    return None


def _activity_occurred_at(
    activity: dict[str, Any],
    *,
    kind: str,
    completed: bool,
    observed_at: datetime,
) -> tuple[str | None, str | None]:
    """Return a defensible performed-at timestamp, never a future CRM plan."""
    if not completed:
        return None, None

    candidates = (
        (("START_TIME", "start_time"), ("END_TIME", "end_time"),
         ("LAST_UPDATED", "last_updated"), ("CREATED", "created"))
        if kind in {"call", "email", "message", "meeting"}
        else (("LAST_UPDATED", "last_updated"), ("END_TIME", "end_time"),
              ("START_TIME", "start_time"), ("CREATED", "created"))
    )
    for field, source in candidates:
        parsed = parse_bitrix_dt(activity.get(field))
        if parsed is not None and parsed.astimezone(MSK_TZ) <= observed_at:
            return parsed.astimezone(MSK_TZ).isoformat(timespec="seconds"), source
    return None, None


def normalize_activity_payload(
    activity: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Project one CRM activity into a stable, text-preserving fact payload."""
    provider_id = str(activity.get("PROVIDER_ID") or "").upper()
    kind = "task" if provider_id.startswith("CRM_TASKS_") else activity_type(activity)
    completed = str(activity.get("COMPLETED") or "").upper() in {"Y", "1", "TRUE"}
    observation = (observed_at or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    occurred_at, occurred_at_source = _activity_occurred_at(
        activity,
        kind=kind,
        completed=completed,
        observed_at=observation,
    )
    start = parse_bitrix_dt(activity.get("START_TIME"))
    end = parse_bitrix_dt(activity.get("END_TIME"))
    duration_seconds = None
    if start is not None and end is not None and end >= start:
        duration_seconds = int((end - start).total_seconds())
    owner_type_id = str(activity.get("OWNER_TYPE_ID") or "")
    owner_type = (
        "deal" if owner_type_id == str(DEAL_OWNER_TYPE_ID)
        else "lead" if owner_type_id == str(LEAD_OWNER_TYPE_ID)
        else None
    )
    return {
        "payload_version": 3,
        "activity_id": _string(activity.get("ID")),
        "activity_kind": kind,
        "type_id": _string(activity.get("TYPE_ID")),
        "owner_type": owner_type,
        "owner_id": _string(activity.get("OWNER_ID")),
        "associated_entity_id": _string(activity.get("ASSOCIATED_ENTITY_ID")),
        "provider_id": _string(activity.get("PROVIDER_ID")),
        "provider_type_id": _string(activity.get("PROVIDER_TYPE_ID")),
        "responsible_id": _string(activity.get("RESPONSIBLE_ID")),
        "author_id": _string(activity.get("AUTHOR_ID")),
        "editor_id": _string(activity.get("EDITOR_ID")),
        "direction": _string(activity.get("DIRECTION")),
        "completed": completed,
        "status": _string(activity.get("STATUS")),
        "subject": activity.get("SUBJECT"),
        "description": activity.get("DESCRIPTION"),
        "description_type": _string(activity.get("DESCRIPTION_TYPE")),
        "occurred_at": occurred_at,
        "occurred_at_source": occurred_at_source,
        "is_observed_workday": occurred_at is not None,
        "scheduled_at": _iso(activity.get("START_TIME")),
        "start_time": _iso(activity.get("START_TIME")),
        "end_time": _iso(activity.get("END_TIME")),
        "deadline": _iso(activity.get("DEADLINE")),
        "created": _iso(activity.get("CREATED")),
        "last_updated": _iso(activity.get("LAST_UPDATED")),
        "files": activity.get("FILES") if isinstance(activity.get("FILES"), list) else [],
        "communications": activity.get("COMMUNICATIONS") if isinstance(activity.get("COMMUNICATIONS"), list) else [],
        "result": {
            "mark": activity.get("RESULT_MARK"),
            "value": activity.get("RESULT_VALUE"),
            "sum": activity.get("RESULT_SUM"),
            "currency_id": activity.get("RESULT_CURRENCY_ID"),
            "status": activity.get("RESULT_STATUS"),
            "stream": activity.get("RESULT_STREAM"),
            "source_id": activity.get("RESULT_SOURCE_ID"),
        },
        "call": {
            "duration_seconds": duration_seconds,
            "recording_file_ids": [
                _string(item.get("ID") or item.get("id"))
                for item in (activity.get("FILES") or [])
                if isinstance(item, dict) and _string(item.get("ID") or item.get("id"))
            ],
        },
        "settings": activity.get("SETTINGS") if isinstance(activity.get("SETTINGS"), dict) else {},
        "provider_params": activity.get("PROVIDER_PARAMS"),
        "provider_data": activity.get("PROVIDER_DATA"),
        "is_incoming_channel": _string(activity.get("IS_INCOMING_CHANNEL")),
    }


def _fact(*, source_event_key: str, entity_type: str, entity_id: str,
          manager_id: str | None, occurred_at: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_event_key": source_event_key,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "manager_id": manager_id,
        "occurred_at": occurred_at,
        "payload": payload,
    }


def _errors_add(errors: dict[str, Any], key: str, response: dict[str, Any]) -> None:
    if not response.get("ok"):
        errors[key] = response.get("error") or "Bitrix source unavailable"


def _safe_list_many(
    client: Any,
    requests_to_run: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Use Bitrix batch when available, preserving sequential test/custom clients."""
    batch = getattr(client, "safe_batch_list", None)
    if callable(batch):
        return batch(requests_to_run)
    return {
        key: client.safe_list_all(method, payload)
        for key, method, payload in requests_to_run
    }


def fetch_activity_facts(client: Any, manager_ids: list[str], start: datetime, end: datetime) -> dict[str, Any]:
    with bitrix_trace_context(component="manager_trajectory_core"):
        response = client.safe_list_all(
            "crm.activity.list",
            {
                "order": {"LAST_UPDATED": "ASC", "ID": "ASC"},
                "filter": {
                    "RESPONSIBLE_ID": manager_ids,
                    ">=LAST_UPDATED": _iso(start),
                    "<LAST_UPDATED": _iso(end),
                },
                "select": ACTIVITY_SELECT_V3,
            },
        )
    errors: dict[str, Any] = {}
    _errors_add(errors, "crm.activity.list", response)
    facts: list[dict[str, Any]] = []
    observed_at = min(end.astimezone(MSK_TZ), datetime.now(MSK_TZ))
    for activity in _result_items(response):
        payload = normalize_activity_payload(activity, observed_at=observed_at)
        entity_type = str(payload.get("owner_type") or "")
        if entity_type not in {"deal", "lead"}:
            continue
        entity_id = _string(activity.get("OWNER_ID")) or ""
        activity_id = payload.get("activity_id") or "unknown"
        version = payload.get("last_updated") or hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        facts.append(_fact(
            # v3 has a distinct namespace so a historical re-collection can
            # append the richer payload beside an existing compact v2 fact.
            source_event_key=f"crm_activity_v3:{activity_id}:{version}",
            entity_type=entity_type,
            entity_id=entity_id,
            manager_id=payload.get("responsible_id"),
            occurred_at=payload.get("occurred_at"),
            payload=payload,
        ))
    return {"facts": facts, "errors": errors}


def business_snapshot(entity: dict[str, Any], entity_type: str, custom_allowlist: Iterable[str] | None = None) -> dict[str, Any]:
    """Return significant fields and labels; UF fields require an explicit allowlist."""
    normalized_type = str(entity_type).lower().strip()
    fields = BUSINESS_FIELD_SELECT.get(normalized_type)
    if fields is None:
        raise ValueError("entity_type must be 'deal' or 'lead'")
    allowlist = {str(name).strip() for name in (custom_allowlist or []) if str(name).strip().startswith("UF_")}
    selected_names = [*fields, *sorted(allowlist)]
    values = {name: entity.get(name) for name in selected_names if name in entity}
    labels = {name: _FIELD_LABELS.get(name, name) for name in values}
    return {
        "entity_type": normalized_type,
        "entity_id": _string(entity.get("ID")),
        "fields": values,
        "field_labels": labels,
        "modified_at": _iso(entity.get("DATE_MODIFY")),
        "modified_by_id": _string(entity.get("MODIFY_BY_ID")),
    }


def collect_timeline_comment_facts(client: Any, entity_refs: Iterable[Any], manager_ids: Iterable[str]) -> dict[str, Any]:
    managers = {str(value) for value in manager_ids}
    facts: list[dict[str, Any]] = []
    errors: dict[str, Any] = {}
    seen: set[str] = set()
    refs: list[tuple[str, str, str | None]] = []
    ref_keys: set[str] = set()
    for raw_ref in entity_refs:
        ref = _entity_ref(raw_ref)
        if ref is None:
            continue
        entity_type, entity_id, _ref_manager = ref
        key = f"{entity_type}:{entity_id}"
        if key in ref_keys:
            continue
        ref_keys.add(key)
        refs.append(ref)
    requests_to_run = [
        (
            f"{entity_type}:{entity_id}",
            "crm.timeline.comment.list",
            {
                "order": {"CREATED": "ASC", "ID": "ASC"},
                "filter": {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id},
            },
        )
        for entity_type, entity_id, _ref_manager in refs
    ]
    with bitrix_trace_context(component="manager_trajectory_timeline"):
        responses = _safe_list_many(client, requests_to_run)
    for entity_type, entity_id, ref_manager in refs:
        key = f"{entity_type}:{entity_id}"
        response = responses[key]
        _errors_add(errors, key, response)
        responsible_id = ref_manager if ref_manager in managers else None
        for item in _result_items(response):
            comment_id = _string(item.get("ID"))
            if not comment_id:
                continue
            event_key = f"crm_timeline_comment:{entity_type}:{entity_id}:{comment_id}"
            if event_key in seen:
                continue
            seen.add(event_key)
            author_id = _string(item.get("AUTHOR_ID") or item.get("CREATED_BY"))
            comment = item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION")
            mirror = messenger_mirror_from_comment(str(comment or ""))
            if author_id in managers:
                manager_id = author_id
            elif mirror is not None and responsible_id:
                # Зеркало Max/WhatsApp/Telegram вешаем на ответственного карточки,
                # даже если запись в CRM создала интеграция, а не менеджер.
                manager_id = responsible_id
            else:
                continue
            payload = {
                "comment_id": comment_id,
                "author_id": author_id,
                "comment": comment,
                "files": item.get("FILES") if isinstance(item.get("FILES"), list) else [],
                "responsible_id": responsible_id,
                "author_is_manager": author_id in managers if author_id else False,
            }
            if mirror is not None:
                payload.update({
                    "is_messenger_mirror": True,
                    "channel": mirror["channel"],
                    "speaker": mirror["speaker"],
                    "content": mirror["content"],
                })
            facts.append(_fact(
                source_event_key=event_key,
                entity_type=entity_type,
                entity_id=entity_id,
                manager_id=manager_id,
                occurred_at=_first_datetime(item, ("CREATED", "DATE_CREATE")),
                payload=payload,
            ))
    return {"facts": facts, "errors": errors}


def _task_id_from_activity(activity: dict[str, Any]) -> str | None:
    payload = activity.get("payload") if isinstance(activity.get("payload"), dict) else activity
    provider = str(payload.get("provider_id") or payload.get("PROVIDER_ID") or "").upper()
    if provider != "CRM_TASKS_TASK":
        return None
    return _string(payload.get("associated_entity_id") or payload.get("ASSOCIATED_ENTITY_ID"))


def collect_task_history_facts(client: Any, activities: Iterable[dict[str, Any]], manager_ids: Iterable[str]) -> dict[str, Any]:
    managers = {str(value) for value in manager_ids}
    task_context: dict[str, tuple[str, str | None]] = {}
    for activity in activities:
        task_id = _task_id_from_activity(activity)
        if not task_id:
            continue
        payload = activity.get("payload") if isinstance(activity.get("payload"), dict) else activity
        entity_type = str(activity.get("entity_type") or "deal")
        entity_id = _string(activity.get("entity_id") or payload.get("owner_id") or payload.get("OWNER_ID")) or ""
        manager_id = _string(activity.get("manager_id") or payload.get("responsible_id") or payload.get("RESPONSIBLE_ID"))
        task_context.setdefault(task_id, (f"{entity_type}:{entity_id}", manager_id))

    facts: list[dict[str, Any]] = []
    errors: dict[str, Any] = {}
    requests_to_run = [
        (f"task:{task_id}", "task.ctasklogitem.list", {"TASKID": task_id})
        for task_id in sorted(task_context)
    ]
    with bitrix_trace_context(component="manager_trajectory_task_history"):
        responses = _safe_list_many(client, requests_to_run)
    for task_id, (entity_key, _fallback_manager) in sorted(task_context.items()):
        response = responses[f"task:{task_id}"]
        _errors_add(errors, f"task:{task_id}", response)
        if not response.get("ok"):
            continue
        entity_type, _, entity_id = entity_key.partition(":")
        for item in _result_items(response):
            field = _string(item.get("FIELD")) or "unknown"
            history_id = _string(item.get("ID"))
            created = _first_datetime(item, ("CREATED_DATE", "CREATED", "DATE_CREATE"))
            user_id = _string(item.get("USER_ID") or item.get("AUTHOR_ID"))
            manager_id = user_id if user_id in managers else None
            identity = history_id or hashlib.sha256(
                json.dumps(
                    {
                        "created": created,
                        "field": field,
                        "from": item.get("FROM_VALUE"),
                        "to": item.get("TO_VALUE"),
                        "user_id": user_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:20]
            facts.append(_fact(
                source_event_key=f"task_history:{task_id}:{identity}",
                entity_type=entity_type or "crm",
                entity_id=entity_id,
                manager_id=manager_id,
                occurred_at=created,
                payload={
                    "task_id": task_id,
                    "history_id": history_id,
                    "field": field,
                    "from_value": item.get("FROM_VALUE"),
                    "to_value": item.get("TO_VALUE"),
                    "user_id": user_id,
                },
            ))
    return {"facts": facts, "errors": errors}


def collect_stage_history_facts(client: Any, entity_refs: Iterable[Any]) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    errors: dict[str, Any] = {}
    seen: set[str] = set()
    refs: list[tuple[str, str, str | None]] = []
    ref_keys: set[str] = set()
    for raw_ref in entity_refs:
        ref = _entity_ref(raw_ref)
        if ref is None:
            continue
        entity_type, entity_id, manager_id = ref
        key = f"{entity_type}:{entity_id}"
        if key in ref_keys:
            continue
        ref_keys.add(key)
        refs.append((entity_type, entity_id, manager_id))
    requests_to_run = [
        (
            f"{entity_type}:{entity_id}",
            "crm.stagehistory.list",
            {
                "entityTypeId": LEAD_OWNER_TYPE_ID if entity_type == "lead" else DEAL_OWNER_TYPE_ID,
                "order": {"CREATED_TIME": "ASC", "ID": "ASC"},
                "filter": {"OWNER_ID": entity_id},
                "select": [
                    "ID", "TYPE_ID", "OWNER_ID", "CREATED_TIME", "CATEGORY_ID",
                    "STAGE_ID", "STATUS_ID", "STAGE_SEMANTIC_ID", "STATUS_SEMANTIC_ID",
                ],
            },
        )
        for entity_type, entity_id, _manager_id in refs
    ]
    with bitrix_trace_context(component="manager_trajectory_stage_history"):
        responses = _safe_list_many(client, requests_to_run)
    for entity_type, entity_id, manager_id in refs:
        response = responses[f"{entity_type}:{entity_id}"]
        key = f"{entity_type}:{entity_id}"
        _errors_add(errors, key, response)
        for item in _result_items(response):
            history_id = _string(item.get("ID"))
            if not history_id:
                continue
            event_key = f"crm_stage_history:{entity_type}:{entity_id}:{history_id}"
            if event_key in seen:
                continue
            seen.add(event_key)
            facts.append(_fact(
                source_event_key=event_key,
                entity_type=entity_type,
                entity_id=entity_id,
                manager_id=manager_id,
                occurred_at=_iso(item.get("CREATED_TIME")),
                payload={
                    "history_id": history_id,
                    "history_type_id": item.get("TYPE_ID"),
                    "category_id": item.get("CATEGORY_ID"),
                    "stage_id": item.get("STAGE_ID") or item.get("STATUS_ID"),
                    "stage_semantic_id": item.get("STAGE_SEMANTIC_ID") or item.get("STATUS_SEMANTIC_ID"),
                },
            ))
    return {"facts": facts, "errors": errors}


def collect_presence_snapshots(client: Any, manager_ids: Iterable[str]) -> dict[str, Any]:
    ids = sorted({str(value).strip() for value in manager_ids if str(value).strip()})
    with bitrix_trace_context(component="manager_presence"):
        response = client.safe_list_all(
            "user.get",
            {
                "filter": {"ID": ids},
                "select": ["ID", "NAME", "LAST_NAME", "IS_ONLINE", "LAST_ACTIVITY_DATE", "LAST_LOGIN"],
            },
        )
    errors: dict[str, Any] = {}
    _errors_add(errors, "user.get", response)
    observed_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    facts: list[dict[str, Any]] = []
    for item in _result_items(response):
        manager_id = _string(item.get("ID"))
        if not manager_id:
            continue
        facts.append(_fact(
            source_event_key=f"manager_presence:{manager_id}:{observed_at}",
            entity_type="manager",
            entity_id=manager_id,
            manager_id=manager_id,
            occurred_at=observed_at,
            payload={
                "name": item.get("NAME"),
                "last_name": item.get("LAST_NAME"),
                "is_online": str(item.get("IS_ONLINE") or "").upper() == "Y",
                "last_activity_date": _iso(item.get("LAST_ACTIVITY_DATE")),
                "last_login": _iso(item.get("LAST_LOGIN")),
            },
        ))
    return {"facts": facts, "errors": errors}
