"""Lightweight, read-only UI projections for the manager trajectory."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from api.deal_call_transcript import find_call_transcript
from api.manager_trajectory import (
    MESSENGER_CHANNEL_LABELS,
    build_manager_trajectory_report,
    neurorop_display_label,
    project_manager_trajectory_for_display,
)
from setup import MSK_TZ
from storage.rop_db import DEFAULT_DB_PATH


BUCKET_MINUTES = {30, 60}
UI_CATEGORIES = {"all", "deals", "leads", "communications", "tasks", "crm", "neurorop"}
ENTITY_EVENT_KEYS = (
    "crm_actions", "stage_changes", "task_history", "timeline_comments",
    "business_field_changes", "stage_history",
)


def day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=MSK_TZ)
    return start, start + timedelta(days=1)


def _now_moscow() -> datetime:
    return datetime.now(MSK_TZ)


def _effective_day_bounds(value: date) -> tuple[datetime, datetime]:
    start, end = day_bounds(value)
    now = _now_moscow()
    return start, min(end, now) if value == now.date() else end


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed.astimezone(MSK_TZ)


def _short(value: Any, limit: int = 220) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _detail_value(value: Any, limit: int = 20_000) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    elif isinstance(value, bool):
        text = "Да" if value else "Нет"
    else:
        text = str(value).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _event_detail_items(action: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, str]]:
    kind = str(action.get("activity_kind") or action.get("action_type") or "").lower()
    action_type = str(action.get("action_type") or "").lower()
    raw_items: list[tuple[str, Any]] = []
    if kind == "task":
        raw_items.extend([
            ("Состояние", "Завершена" if action.get("completed") else "Не завершена"),
            ("ID задачи", action.get("associated_entity_id")),
        ])
    if action_type == "task_history" or kind == "task_history":
        raw_items.extend([
            ("ID задачи", payload.get("task_id")),
            ("Изменённое поле", payload.get("field")),
            ("Было", payload.get("from_value")),
            ("Стало", payload.get("to_value")),
        ])
    if action_type == "business_field_change" or kind == "business_field_change":
        raw_items.extend([
            ("Поле", payload.get("field_label") or payload.get("field_name")),
            ("Было", payload.get("from_value")),
            ("Стало", payload.get("to_value")),
        ])
    if action_type == "stage_change" or kind in {"stage_change", "stage_history"}:
        raw_items.extend([
            ("Предыдущая стадия", action.get("from_stage_name") or action.get("from_stage_id")),
            ("Новая стадия", action.get("to_stage_name") or action.get("to_stage_id")),
        ])
    result: list[dict[str, str]] = []
    for label, raw_value in raw_items:
        value = _detail_value(raw_value)
        if value is not None:
            result.append({"label": label, "value": value})
    return result


def _entity_meta(manager: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): item
        for item in manager.get("workday", {}).get("entities") or []
    }


def _action_category(action: dict[str, Any]) -> str:
    kind = str(action.get("activity_kind") or action.get("action_type") or "").lower()
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    channel = str(payload.get("channel") or "").lower()
    if kind in {"call", "email", "message", "meeting"} or channel in MESSENGER_CHANNEL_LABELS:
        return "communications"
    if kind in {"task", "task_history"}:
        return "tasks"
    return "crm"


def _activity_label(action: dict[str, Any]) -> str:
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    channel = str(payload.get("channel") or "").lower()
    if channel in MESSENGER_CHANNEL_LABELS:
        return MESSENGER_CHANNEL_LABELS[channel]
    kind = str(action.get("activity_kind") or action.get("action_type") or "other").lower()
    labels = {
        "call": "Звонок",
        "email": "Письмо",
        "message": "Сообщение",
        "meeting": "Встреча",
        "task": "Задача",
        "task_history": "Изменение задачи",
        "timeline_comment": "Комментарий CRM",
        "business_field_change": "Изменение CRM",
        "stage_change": "Смена стадии",
        "stage_history": "История стадии",
        "other": "CRM-событие",
    }
    return labels.get(kind, labels.get(str(action.get("action_type") or "").lower(), kind.replace("_", " ")))


def _base_event(
    action: dict[str, Any],
    metadata: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    occurred_at = action.get("occurred_at")
    if _parse_at(occurred_at) is None:
        return None
    entity_type = str(action.get("entity_type") or "")
    entity_id = str(action.get("entity_id") or "")
    entity = metadata.get((entity_type, entity_id)) or {}
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    call = action.get("call") if isinstance(action.get("call"), dict) else {}
    subject = action.get("subject") or payload.get("field_label") or payload.get("field_name")
    description = action.get("description") or payload.get("content") or payload.get("comment")
    if action.get("action_type") == "stage_change" or action.get("activity_kind") == "stage_history":
        before = action.get("from_stage_name") or action.get("from_stage_id")
        after = action.get("to_stage_name") or action.get("to_stage_id")
        description = f"{before or '—'} → {after or '—'}"
    kind = str(action.get("activity_kind") or "").lower()
    action_type = str(action.get("action_type") or "").lower()
    has_saved_detail = bool(subject or description) or any(
        payload.get(key) not in (None, "")
        for key in ("task_id", "field", "field_name", "from_value", "to_value")
    )
    return {
        "event_id": action.get("event_id"),
        "occurred_at": occurred_at,
        "category": _action_category(action),
        "activity_id": action.get("activity_id"),
        "event_type": action.get("event_type") or action.get("action_type"),
        "label": _activity_label(action),
        "entity_type": entity_type or None,
        "entity_id": entity_id or None,
        "entity_title": entity.get("title"),
        "pipeline_name": entity.get("pipeline_name"),
        "stage_name": entity.get("stage_name"),
        "subject": _short(subject),
        "description": _short(description),
        "direction": action.get("direction"),
        "completed": action.get("completed"),
        "duration_seconds": call.get("duration_seconds"),
        "expandable": (
            kind in {"call", "email", "message", "meeting", "task", "task_history", "stage_history"}
            or action_type in {"timeline_comment", "task_history", "business_field_change", "stage_change"}
            or has_saved_detail
        ),
        "temporal_relation": None,
    }


def _manager_events(manager: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _entity_meta(manager)
    events: list[dict[str, Any]] = []
    for entity in manager.get("workday", {}).get("entities") or []:
        for key in (
            "crm_actions", "stage_changes", "task_history", "timeline_comments",
            "business_field_changes", "stage_history",
        ):
            for action in entity.get(key) or []:
                event = _base_event(action, metadata)
                if event is not None:
                    events.append(event)

    for recommendation in manager.get("product_usage", {}).get("recommendations") or []:
        common = {
            "category": "neurorop",
            "entity_type": "deal",
            "entity_id": str(recommendation.get("deal_id") or ""),
            "recommendation_kind": recommendation.get("recommendation_kind"),
            "recommendation_id": recommendation.get("recommendation_id"),
        }
        entity = metadata.get(("deal", common["entity_id"])) or {}
        for occurrence in recommendation.get("view_occurrences") or []:
            events.append({
                **common,
                "event_id": occurrence.get("event_id"),
                "occurred_at": occurrence.get("occurred_at"),
                "event_type": "recommendation_viewed",
                "label": neurorop_display_label("viewed", common.get("recommendation_kind")),
                "entity_title": entity.get("title"),
                "pipeline_name": entity.get("pipeline_name"),
                "stage_name": entity.get("stage_name"),
                "subject": None,
                "description": None,
                "temporal_relation": None,
            })
    for opening in manager.get("product_usage", {}).get("quick_help_openings") or []:
        entity_id = str(opening.get("deal_id") or "")
        entity = metadata.get(("deal", entity_id)) or {}
        events.append({
            "event_id": opening.get("event_id"),
            "occurred_at": opening.get("opened_at"),
            "category": "neurorop",
            "event_type": "quick_help_opened",
            "label": neurorop_display_label("quick_help_opened"),
            "entity_type": "deal",
            "entity_id": entity_id,
            "entity_title": entity.get("title"),
            "pipeline_name": entity.get("pipeline_name"),
            "stage_name": entity.get("stage_name"),
            "subject": _short(opening.get("entrypoint")),
            "description": None,
            "temporal_relation": None,
        })

    by_id = {str(item.get("event_id")): item for item in events if item.get("event_id") is not None}
    for correlation in manager.get("correlations") or []:
        for view in correlation.get("views") or []:
            first = view.get("first_same_deal_action_after")
            target = by_id.get(str((first or {}).get("event_id"))) if isinstance(first, dict) else None
            minutes = view.get("minutes_to_first_same_deal_action")
            if target is not None and minutes is not None:
                target["temporal_relation"] = {
                    "kind": "after_recommendation_view",
                    "minutes": minutes,
                    "text": f"через {minutes:g} мин после просмотра рекомендации",
                }

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in events:
        key = (
            str(item.get("event_id") or ""), str(item.get("event_type") or ""),
            str(item.get("occurred_at") or ""),
        )
        deduplicated[key] = item
    return sorted(deduplicated.values(), key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or "")))


def _matches(event: dict[str, Any], *, category: str, query: str) -> bool:
    if category == "deals" and event.get("entity_type") != "deal":
        return False
    if category == "leads" and event.get("entity_type") != "lead":
        return False
    if category not in {"all", "deals", "leads"} and event.get("category") != category:
        return False
    needle = query.strip().casefold()
    if not needle:
        return True
    haystack = " ".join(str(event.get(key) or "") for key in ("entity_id", "entity_title", "subject", "description"))
    return needle in haystack.casefold()


def _bucket_start(value: datetime, minutes: int) -> datetime:
    minute = (value.minute // minutes) * minutes
    return value.replace(minute=minute, second=0, microsecond=0)


def _attention(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    sequence = [str(item.get("entity_type")) for item in events if item.get("entity_type") in {"deal", "lead"}]
    counts = {"deal": sequence.count("deal"), "lead": sequence.count("lead")}
    other = max(0, len(list(events)) - counts["deal"] - counts["lead"]) if not isinstance(events, list) else max(0, len(events) - counts["deal"] - counts["lead"])
    total = counts["deal"] + counts["lead"] + other
    switches = {"deal_to_lead": 0, "lead_to_deal": 0, "deal_to_deal": 0, "lead_to_lead": 0}
    for before, after in zip(sequence, sequence[1:]):
        switches[f"{before}_to_{after}"] += 1
    return {
        "distribution": {
            "deals": round(counts["deal"] * 100 / total) if total else 0,
            "leads": round(counts["lead"] * 100 / total) if total else 0,
            "other": round(other * 100 / total) if total else 0,
        },
        "context_switches": {
            **switches,
            "deal_lead_total": switches["deal_to_lead"] + switches["lead_to_deal"],
        },
    }


def _call_summary(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result = {
        direction: {"count": 0, "duration_seconds": 0, "missing_duration": 0}
        for direction in ("incoming", "outgoing", "unknown")
    }
    for event in events:
        if event.get("label") != "Звонок":
            continue
        raw_direction = str(event.get("direction") or "").lower()
        direction = (
            "incoming" if raw_direction in {"1", "incoming"}
            else "outgoing" if raw_direction in {"2", "outgoing"}
            else "unknown"
        )
        result[direction]["count"] += 1
        duration = event.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            result[direction]["duration_seconds"] += round(duration)
        else:
            result[direction]["missing_duration"] += 1
    return result


def _report(
    db_path: str | Path, value: date, manager_ids: list[str] | None = None,
) -> tuple[dict[str, Any], datetime, datetime]:
    start, end = _effective_day_bounds(value)
    return build_manager_trajectory_report(
        db_path=db_path, from_at=start, to_at=end, manager_ids=manager_ids,
    ), start, end


def build_day_projection(
    *,
    value: date,
    bucket_minutes: int = 60,
    manager_ids: list[str] | None = None,
    category: str = "all",
    query: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if bucket_minutes not in BUCKET_MINUTES:
        raise ValueError("bucket_minutes должен быть 30 или 60")
    if category not in UI_CATEGORIES:
        raise ValueError("Неизвестный фильтр событий")
    report, start, end = _report(db_path, value, manager_ids)
    manager_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    all_datetimes: list[datetime] = []
    for manager in report.get("managers") or []:
        events = [item for item in _manager_events(manager) if _matches(item, category=category, query=query)]
        manager_rows.append((manager, events))
        all_datetimes.extend(item for item in (_parse_at(event.get("occurred_at")) for event in events) if item)
    first_hour = min((item.hour for item in all_datetimes), default=8)
    last_hour = max((item.hour + (1 if item.minute or item.second else 0) for item in all_datetimes), default=20)
    axis_start = start.replace(hour=max(0, min(8, first_hour)))
    day_end = day_bounds(value)[1]
    axis_end = start.replace(hour=min(23, max(20, last_hour)))
    axis_end = min(axis_end, end)
    if axis_end <= axis_start:
        axis_start = start
        axis_end = end
    if axis_end < end and any(item >= axis_end for item in all_datetimes):
        axis_end = min(day_end, end)
    slots: list[dict[str, Any]] = []
    cursor = axis_start
    while cursor < axis_end:
        slots.append({"from": cursor.isoformat(timespec="seconds"), "to": min(cursor + timedelta(minutes=bucket_minutes), axis_end).isoformat(timespec="seconds"), "label": cursor.strftime("%H:%M")})
        cursor += timedelta(minutes=bucket_minutes)

    projected_managers = []
    for manager, events in manager_rows:
        entity_types = {(item.get("entity_type"), item.get("entity_id")) for item in events}
        buckets: dict[str, dict[str, Any]] = {}
        for event in events:
            occurred = _parse_at(event.get("occurred_at"))
            if occurred is None:
                continue
            bucket = _bucket_start(occurred, bucket_minutes)
            key = bucket.isoformat(timespec="seconds")
            item = buckets.setdefault(key, {
                "from": key,
                "to": (bucket + timedelta(minutes=bucket_minutes)).isoformat(timespec="seconds"),
                "count": 0,
                "density": "none",
                "lanes": {name: 0 for name in ("deals", "leads", "communications", "tasks", "crm", "neurorop")},
            })
            item["count"] += 1
            if event.get("entity_type") == "deal":
                item["lanes"]["deals"] += 1
            elif event.get("entity_type") == "lead":
                item["lanes"]["leads"] += 1
            if event.get("category") in item["lanes"]:
                item["lanes"][event["category"]] += 1
        activity_kinds = [str(item.get("label") or "") for item in events]
        projected_managers.append({
            "manager_id": manager.get("manager_id"),
            "manager_name": manager.get("manager_name") or f"Менеджер #{manager.get('manager_id')}",
            "totals": {
                "events": len(events),
                "deals": sum(entity_type == "deal" for entity_type, _ in entity_types),
                "leads": sum(entity_type == "lead" for entity_type, _ in entity_types),
                "calls": activity_kinds.count("Звонок"),
                "tasks": sum(item.get("category") == "tasks" for item in events),
                "communications": sum(item.get("category") == "communications" for item in events),
                "crm": sum(item.get("category") == "crm" for item in events),
                "neurorop": sum(item.get("category") == "neurorop" for item in events),
            },
            "call_summary": _call_summary(events),
            "attention": _attention(events),
            "buckets": list(buckets.values()),
        })
    maximum = max(
        (bucket["count"] for manager in projected_managers for bucket in manager["buckets"]),
        default=0,
    )
    for manager in projected_managers:
        for bucket in manager["buckets"]:
            ratio = bucket["count"] / maximum if maximum else 0
            bucket["density"] = (
                "peak" if ratio >= .75
                else "high" if ratio >= .45
                else "moderate" if bucket["count"]
                else "none"
            )
    state = report.get("collection_status") or {}
    last_success = state.get("last_success_at")
    return {
        "date": value.isoformat(),
        "timezone": "Europe/Moscow",
        "bucket_minutes": bucket_minutes,
        "period": {"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds")},
        "axis": {"from": axis_start.isoformat(timespec="seconds"), "to": axis_end.isoformat(timespec="seconds"), "slots": slots},
        "collection": {
            "status": state.get("last_status") or "unknown",
            "last_success_at": last_success,
            "last_attempt_at": state.get("last_attempt_at"),
            "is_current_day": value == datetime.now(MSK_TZ).date(),
        },
        "totals": {
            "events": sum(item["totals"]["events"] for item in projected_managers),
            "deals": sum(item["totals"]["deals"] for item in projected_managers),
            "leads": sum(item["totals"]["leads"] for item in projected_managers),
            "calls": sum(item["totals"]["calls"] for item in projected_managers),
            "tasks": sum(item["totals"]["tasks"] for item in projected_managers),
            "communications": sum(item["totals"]["communications"] for item in projected_managers),
            "crm": sum(item["totals"]["crm"] for item in projected_managers),
            "neurorop": sum(item["totals"]["neurorop"] for item in projected_managers),
        },
        "managers": projected_managers,
        "warnings": report.get("warnings") or [],
    }


def build_window_projection(
    *,
    manager_id: str,
    from_at: datetime,
    to_at: datetime,
    category: str = "all",
    query: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    start = from_at.astimezone(MSK_TZ) if from_at.tzinfo else from_at.replace(tzinfo=MSK_TZ)
    end = to_at.astimezone(MSK_TZ) if to_at.tzinfo else to_at.replace(tzinfo=MSK_TZ)
    now = _now_moscow()
    if start.date() == now.date():
        end = min(end, now)
    if start >= end or end - start > timedelta(hours=24):
        raise ValueError("Интервал должен быть положительным и не длиннее 24 часов")
    if category not in UI_CATEGORIES:
        raise ValueError("Неизвестный фильтр событий")
    report = build_manager_trajectory_report(db_path=db_path, from_at=start, to_at=end, manager_ids=[manager_id])
    manager = next((item for item in report.get("managers") or [] if str(item.get("manager_id")) == str(manager_id)), None)
    events = [item for item in _manager_events(manager or {}) if _matches(item, category=category, query=query)]
    return {
        "manager_id": manager_id,
        "manager_name": (manager or {}).get("manager_name") or f"Менеджер #{manager_id}",
        "period": {"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds")},
        "events": events,
        "entities": len({(item.get("entity_type"), item.get("entity_id")) for item in events if item.get("entity_id")}),
    }


def build_entity_projection(
    *,
    entity_type: str,
    entity_id: str,
    value: date,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if entity_type not in {"deal", "lead"}:
        raise ValueError("entity_type должен быть deal или lead")
    report, start, end = _report(db_path, value)
    for manager in report.get("managers") or []:
        for entity in manager.get("workday", {}).get("entities") or []:
            if entity.get("entity_type") != entity_type or str(entity.get("entity_id")) != str(entity_id):
                continue
            events = [
                item for item in _manager_events(manager)
                if item.get("entity_type") == entity_type and str(item.get("entity_id")) == str(entity_id)
            ]
            fields = entity.get("current_business_fields") if isinstance(entity.get("current_business_fields"), dict) else {}
            return {
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "title": entity.get("title"),
                "pipeline_id": entity.get("pipeline_id"),
                "pipeline_name": entity.get("pipeline_name"),
                "stage_id": entity.get("stage_id"),
                "stage_name": entity.get("stage_name"),
                "manager_id": manager.get("manager_id"),
                "manager_name": manager.get("manager_name"),
                "period": {"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds")},
                "created_at": entity.get("created_at"),
                "relevant_fields": fields,
                "chronology": events,
            }
    raise LookupError("Сущность не найдена в траектории выбранного дня")


def build_event_detail_projection(
    *,
    manager_id: str,
    event_id: str,
    value: date,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    report, _start, _end = _report(db_path, value, [manager_id])
    manager = next(
        (item for item in report.get("managers") or [] if str(item.get("manager_id")) == str(manager_id)),
        None,
    )
    if manager is None:
        raise LookupError("Менеджер не найден в траектории выбранного дня")
    metadata = _entity_meta(manager)
    for entity in manager.get("workday", {}).get("entities") or []:
        for key in (
            "crm_actions", "stage_changes", "task_history", "timeline_comments",
            "business_field_changes", "stage_history",
        ):
            for action in entity.get(key) or []:
                if str(action.get("event_id") or "") != str(event_id):
                    continue
                event = _base_event(action, metadata)
                if event is None:
                    continue
                payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                full_description = (
                    action.get("description")
                    or payload.get("content")
                    or payload.get("comment")
                    or payload.get("description")
                )
                transcript = None
                if event.get("label") == "Звонок" and event.get("activity_id"):
                    transcript = find_call_transcript(
                        str(event.get("entity_type") or ""),
                        str(event.get("entity_id") or ""),
                        str(event.get("activity_id") or ""),
                    )
                return {
                    **event,
                    "subject": action.get("subject") or event.get("subject"),
                    "description": str(full_description).strip() if full_description is not None else None,
                    "details": _event_detail_items(action, payload),
                    "transcript_text": transcript.get("text") if transcript else None,
                    "transcript_truncated": bool(transcript and transcript.get("truncated")),
                }
    raise LookupError("Событие не найдено в траектории выбранного дня")


def day_export_filename(value: date, manager_id: str | None = None) -> str:
    if manager_id:
        return f"trajectory-{value.isoformat()}-manager-{manager_id}.json"
    return f"trajectory-{value.isoformat()}-all-managers.json"


def _event_like(
    item: dict[str, Any],
    *,
    entity_title: Any = None,
    category: str | None = None,
    entity_type: Any = None,
    entity_id: Any = None,
    subject: Any = None,
    description: Any = None,
) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return {
        "entity_type": entity_type if entity_type is not None else item.get("entity_type"),
        "entity_id": entity_id if entity_id is not None else item.get("entity_id"),
        "entity_title": entity_title,
        "category": category or _action_category(item),
        "subject": subject if subject is not None else item.get("subject") or payload.get("subject"),
        "description": (
            description if description is not None
            else item.get("description") or payload.get("content") or payload.get("description") or payload.get("comment")
        ),
    }


def _export_summary(managers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "managers": len(managers),
        "unique_crm_actions": sum(item.get("workday", {}).get("unique_crm_actions", 0) for item in managers),
        "recommendations_viewed": sum(item.get("product_usage", {}).get("viewed", 0) for item in managers),
        "quick_help_opened": sum(item.get("product_usage", {}).get("quick_help_opened", 0) for item in managers),
    }


def _recompute_manager_export_aggregates(manager: dict[str, Any]) -> dict[str, Any]:
    workday = dict(manager.get("workday") or {})
    entities = list(workday.get("entities") or [])
    crm_actions = [item for entity in entities for item in entity.get("crm_actions") or []]
    stage_changes = [item for entity in entities for item in entity.get("stage_changes") or []]
    task_history = [item for entity in entities for item in entity.get("task_history") or []]
    timeline_comments = [item for entity in entities for item in entity.get("timeline_comments") or []]
    business_field_changes = [
        item for entity in entities for item in entity.get("business_field_changes") or []
    ]
    stage_history = [item for entity in entities for item in entity.get("stage_history") or []]
    product_usage = dict(manager.get("product_usage") or {})
    recommendations = list(product_usage.get("recommendations") or [])
    openings = list(product_usage.get("quick_help_openings") or [])
    activity_counts: dict[str, int] = {}
    for action in crm_actions:
        kind = str(action.get("activity_kind") or "other")
        activity_counts[kind] = activity_counts.get(kind, 0) + 1
    counts: dict[str, int] = {}
    if crm_actions:
        counts["crm_activity_observed"] = len(crm_actions)
    for item in stage_changes:
        key = "deal_stage_changed" if item.get("entity_type") == "deal" else "lead_stage_changed"
        counts[key] = counts.get(key, 0) + 1
    if task_history:
        counts["crm_task_history_observed"] = len(task_history)
    if timeline_comments:
        counts["crm_timeline_comment_observed"] = len(timeline_comments)
    if business_field_changes:
        counts["crm_business_field_changed"] = len(business_field_changes)
    if stage_history:
        counts["crm_stage_history_observed"] = len(stage_history)
    viewed = sum(len(item.get("view_occurrences") or item.get("viewed_at") or []) for item in recommendations)
    if viewed:
        counts["recommendation_viewed"] = viewed
    if openings:
        counts["quick_help_opened"] = len(openings)
    entity_keys = {(item.get("entity_type"), str(item.get("entity_id") or "")) for item in entities}
    entity_keys.update(
        ("deal", str(item.get("deal_id") or ""))
        for item in recommendations
        if item.get("deal_id") and (item.get("view_occurrences") or item.get("viewed_at"))
    )
    entity_keys.update(("deal", str(item.get("deal_id") or "")) for item in openings if item.get("deal_id"))
    workday.update({
        "unique_crm_actions": len(crm_actions),
        "activity_counts": activity_counts,
        "stage_changes": len(stage_changes),
        "task_history_events": len(task_history),
        "timeline_comments": len(timeline_comments),
        "business_field_changes": len(business_field_changes),
        "stage_history_events": len(stage_history),
        "system_creation_events": 0,
        "presence_snapshots": [],
        "deals_touched": sum(item.get("entity_type") == "deal" for item in entities),
        "leads_touched": sum(item.get("entity_type") == "lead" for item in entities),
        "entities": entities,
    })
    product_usage.update({
        "recommendations": recommendations,
        "quick_help_openings": openings,
        "viewed": viewed,
        "quick_help_opened": len(openings),
    })
    return {
        **manager,
        "counts": counts,
        "entities": len(entity_keys),
        "viewed_windows_60m": [],
        "workday": workday,
        "product_usage": product_usage,
        "correlations": [],
    }


def _filter_manager_report(manager: dict[str, Any], *, category: str, query: str) -> dict[str, Any]:
    workday = dict(manager.get("workday") or {})
    original_entities = list(workday.get("entities") or [])
    titles = {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): item.get("title")
        for item in original_entities
    }
    filtered_entities: list[dict[str, Any]] = []
    for entity in original_entities:
        title = entity.get("title")
        kept = dict(entity)
        has_match = False
        for key in ENTITY_EVENT_KEYS:
            matched = [
                item for item in entity.get(key) or []
                if _matches(_event_like(item, entity_title=title), category=category, query=query)
            ]
            kept[key] = matched
            has_match = has_match or bool(matched)
        if has_match:
            filtered_entities.append(kept)
    recommendations = [
        item for item in (manager.get("product_usage") or {}).get("recommendations") or []
        if _matches(
            _event_like(
                item,
                entity_title=titles.get(("deal", str(item.get("deal_id") or ""))),
                category="neurorop",
                entity_type="deal",
                entity_id=str(item.get("deal_id") or ""),
                subject=item.get("recommendation_kind"),
            ),
            category=category,
            query=query,
        )
    ]
    openings = [
        item for item in (manager.get("product_usage") or {}).get("quick_help_openings") or []
        if _matches(
            _event_like(
                item,
                entity_title=titles.get(("deal", str(item.get("deal_id") or ""))),
                category="neurorop",
                entity_type="deal",
                entity_id=str(item.get("deal_id") or ""),
                subject=item.get("entrypoint"),
            ),
            category=category,
            query=query,
        )
    ]
    return _recompute_manager_export_aggregates({
        **manager,
        "workday": {**workday, "entities": filtered_entities},
        "product_usage": {
            **(manager.get("product_usage") or {}),
            "recommendations": recommendations,
            "quick_help_openings": openings,
        },
    })


def _filter_full_report(report: dict[str, Any], *, category: str, query: str) -> dict[str, Any]:
    managers = [
        _filter_manager_report(item, category=category, query=query)
        for item in report.get("managers") or []
    ]
    return {
        **report,
        "managers": managers,
        "summary": _export_summary(managers),
    }


def build_day_export(
    *,
    value: date,
    manager_ids: list[str] | None = None,
    category: str = "all",
    query: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if category not in UI_CATEGORIES:
        raise ValueError("Неизвестный фильтр событий")
    report, _start, _end = _report(db_path, value, manager_ids)
    if category != "all" or query.strip():
        report = _filter_full_report(report, category=category, query=query)
    report = project_manager_trajectory_for_display(report)
    manager_id = manager_ids[0] if manager_ids and len(manager_ids) == 1 else None
    return {
        "export": {
            "generated_at": _now_moscow().isoformat(timespec="seconds"),
            "date": value.isoformat(),
            "timezone": "Europe/Moscow",
            "filters": {
                "manager_id": manager_id,
                "category": category,
                "q": query.strip(),
            },
        },
        **report,
    }
