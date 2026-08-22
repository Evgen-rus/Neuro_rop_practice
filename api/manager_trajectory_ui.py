"""Lightweight, read-only UI projections for the manager trajectory."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from api.deal_call_transcript import find_call_transcript
from api.manager_trajectory import build_manager_trajectory_report
from setup import MSK_TZ
from storage.rop_db import DEFAULT_DB_PATH


BUCKET_MINUTES = {15, 30, 60}
UI_CATEGORIES = {"all", "deals", "leads", "communications", "tasks", "crm", "neurorop"}
NEUROROP_EVENT_LABELS = {
    "generated": "Рекомендация сформирована",
    "shown": "Рекомендация показана",
    "viewed": "Рекомендация просмотрена",
    "quick_help_opened": "Открыт Quick Help",
}


def day_bounds(value: date) -> tuple[datetime, datetime]:
    start = datetime.combine(value, time.min, tzinfo=MSK_TZ)
    return start, start + timedelta(days=1)


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


def _entity_meta(manager: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("entity_type") or ""), str(item.get("entity_id") or "")): item
        for item in manager.get("workday", {}).get("entities") or []
    }


def _action_category(action: dict[str, Any]) -> str:
    kind = str(action.get("activity_kind") or action.get("action_type") or "").lower()
    if kind in {"call", "email", "message", "meeting"}:
        return "communications"
    if kind in {"task", "task_history"}:
        return "tasks"
    return "crm"


def _activity_label(action: dict[str, Any]) -> str:
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
    description = action.get("description") or payload.get("comment")
    if action.get("action_type") == "stage_change" or action.get("activity_kind") == "stage_history":
        before = action.get("from_stage_name") or action.get("from_stage_id")
        after = action.get("to_stage_name") or action.get("to_stage_id")
        description = f"{before or '—'} → {after or '—'}"
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
            str(action.get("activity_kind") or "").lower() == "call"
            or str(action.get("action_type") or "").lower() == "timeline_comment"
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
        for field, kind in (("generated_at", "generated"), ("shown_at", "shown")):
            for index, occurred_at in enumerate(recommendation.get(field) or []):
                events.append({
                    **common,
                    "event_id": f"neurorop:{kind}:{common['recommendation_id']}:{index}",
                    "occurred_at": occurred_at,
                    "event_type": f"recommendation_{kind}",
                    "label": NEUROROP_EVENT_LABELS[kind],
                    "entity_title": entity.get("title"),
                    "pipeline_name": entity.get("pipeline_name"),
                    "stage_name": entity.get("stage_name"),
                    "subject": None,
                    "description": None,
                    "temporal_relation": None,
                })
        for occurrence in recommendation.get("view_occurrences") or []:
            events.append({
                **common,
                "event_id": occurrence.get("event_id"),
                "occurred_at": occurrence.get("occurred_at"),
                "event_type": "recommendation_viewed",
                "label": NEUROROP_EVENT_LABELS["viewed"],
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
            "label": NEUROROP_EVENT_LABELS["quick_help_opened"],
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


def _report(
    db_path: str | Path, value: date, manager_ids: list[str] | None = None,
) -> tuple[dict[str, Any], datetime, datetime]:
    start, end = day_bounds(value)
    return build_manager_trajectory_report(
        db_path=db_path, from_at=start, to_at=end, manager_ids=manager_ids,
    ), start, end


def build_day_projection(
    *,
    value: date,
    bucket_minutes: int = 30,
    manager_ids: list[str] | None = None,
    category: str = "all",
    query: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    if bucket_minutes not in BUCKET_MINUTES:
        raise ValueError("bucket_minutes должен быть 15, 30 или 60")
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
    axis_end = start.replace(hour=min(23, max(20, last_hour)))
    if axis_end <= axis_start:
        axis_end = min(end, axis_start + timedelta(hours=12))
    if axis_end < end and any(item >= axis_end for item in all_datetimes):
        axis_end = end
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
        maximum = max((item["count"] for item in buckets.values()), default=0)
        for item in buckets.values():
            ratio = item["count"] / maximum if maximum else 0
            item["density"] = "peak" if ratio >= .75 else "high" if ratio >= .45 else "moderate" if item["count"] else "none"
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
            "attention": _attention(events),
            "buckets": list(buckets.values()),
        })
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
                    "transcript_text": transcript.get("text") if transcript else None,
                    "transcript_truncated": bool(transcript and transcript.get("truncated")),
                }
    raise LookupError("Событие не найдено в траектории выбранного дня")
