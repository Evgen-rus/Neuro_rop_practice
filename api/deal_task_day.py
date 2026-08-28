"""Task facts for live cards and immutable daily snapshots; no CRM or AI calls."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from setup import MSK_TZ
from storage.rop_db import list_manager_trajectory_events


def stamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(str(value), pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK_TZ)).astimezone(MSK_TZ)


def day_events(db_path: Any, cutoff: datetime) -> list[dict[str, Any]]:
    current = stamp(cutoff)
    assert current is not None
    rows = list_manager_trajectory_events(
        db_path, from_at=current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        to_at=(current + timedelta(seconds=1)).isoformat(),
    )
    return [row for row in rows if row.get("entity_type") == "deal" and in_day(row.get("occurred_at"), current)
            and (not row.get("recorded_at") or (stamp(row["recorded_at"]) or datetime.max.replace(tzinfo=MSK_TZ)) <= current)]


def in_day(value: Any, cutoff: datetime) -> bool:
    parsed = stamp(value)
    return parsed is not None and parsed.date() == cutoff.date() and parsed <= cutoff


def is_reschedule(payload: dict[str, Any]) -> bool:
    before, after = stamp(payload.get("from_value")), stamp(payload.get("to_value"))
    # A first deadline assignment is not a reschedule. Removing an existing deadline is.
    return str(payload.get("field") or "").upper() == "DEADLINE" and before is not None and (
        (after is not None and before != after) or not payload.get("to_value")
    )


def task_results(tasks: list[dict[str, Any]], events: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    current = stamp(cutoff)
    assert current is not None
    rows: dict[str, dict[str, Any]] = {}
    activity_keys: dict[str, str] = {}
    for task in tasks:
        task_id, activity_id = str(task.get("task_id") or ""), str(task.get("activity_id") or "")
        if not task_id and not activity_id:
            continue
        key = f"task:{task_id}" if task_id else f"activity:{activity_id}"
        if activity_id:
            activity_keys[activity_id] = key
        completed_at = task.get("bitrix_completed_at") if task.get("completed") else task.get("local_completed_at")
        completed = bool(task.get("completed") or task.get("local_completed"))
        if stamp(completed_at) and stamp(completed_at) > current:
            completed = False
        deadline = stamp(task.get("deadline"))
        rows[key] = {
            "key": key, "task_id": task_id or None, "activity_id": activity_id or None,
            "subject": task.get("subject") or f"Задача #{task_id or activity_id}",
            "deadline": task.get("deadline"), "status": "completed" if completed else "open",
            "completion_source": "bitrix" if task.get("completed") else "local" if completed else None,
            "completed_at": completed_at, "completed_today": completed and in_day(completed_at, current),
            "overdue": not completed and deadline is not None and deadline < current,
            "was_due": deadline is not None and deadline.date() <= current.date(), "reschedules": [],
        }
    for event in sorted(events, key=lambda item: (str(item.get("occurred_at") or ""), int(item.get("id") or 0))):
        if not in_day(event.get("occurred_at"), current):
            continue
        recorded = stamp(event.get("recorded_at"))
        if recorded and recorded > current:
            continue
        payload = event.get("payload") or {}
        history = event.get("event_type") == "crm_task_history_observed"
        completed = history and (
            (str(payload.get("field")).upper() == "STATUS" and str(payload.get("to_value")) == "5")
            or (str(payload.get("field")).upper() == "COMPLETED" and str(payload.get("to_value")).upper() in {"Y", "1", "TRUE"})
        )
        moved = history and is_reschedule(payload)
        observed = event.get("event_type") == "crm_activity_observed" and payload.get("activity_kind") == "task" and payload.get("completed") is True
        if not (completed or moved or observed):
            continue
        task_id = str(payload.get("task_id") or (payload.get("associated_entity_id") if observed else "") or "")
        activity_id = str(payload.get("activity_id") or "")
        key = activity_keys.get(activity_id) or (f"task:{task_id}" if task_id else f"activity:{activity_id}")
        if not task_id and not activity_id:
            continue
        row = rows.setdefault(key, {
            "key": key, "task_id": task_id or None, "activity_id": activity_id or None,
            "subject": payload.get("subject") or f"Задача #{task_id or activity_id}",
            "deadline": None, "status": "unknown", "completion_source": None,
            "completed_at": None, "completed_today": False, "overdue": False,
            "was_due": False, "reschedules": [],
        })
        if completed or observed:
            row["completed_today"] = True
            # Current CRM state wins (a completed task may have been reopened).
            if row["status"] == "unknown":
                row.update(status="completed", completion_source="bitrix", completed_at=event.get("occurred_at"), overdue=False)
        if moved:
            change = {"from_deadline": payload.get("from_value"), "to_deadline": payload.get("to_value"), "occurred_at": event.get("occurred_at")}
            if change not in row["reschedules"]:
                row["reschedules"].append(change)
            before = stamp(payload.get("from_value"))
            row["was_due"] = row["was_due"] or bool(before and before.date() <= current.date())
    return list(rows.values())


def task_totals(deals: list[dict[str, Any]]) -> dict[str, int]:
    return {name: len({task["key"] for deal in deals for task in deal.get("task_results") or [] if task.get(flag)})
            for name, flag in (("tasks_completed", "completed_today"), ("tasks_rescheduled", "reschedules"))}
