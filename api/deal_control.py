"""Read-only Bitrix synchronisation and local control logic for active deals."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from api.candidates import fetch_candidate_activities_bulk, load_pipeline_stage_names, make_client, parse_bitrix_dt
from api.jobs import unwrap_analysis_payload
from bitrix.customer_history import activity_type, build_normalized_communications
from openai_api.config import COMMUNICATION_QUALITY_AUDIT_ENABLED
from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    create_deal_control_task,
    confirm_deal_control_task_crm_match,
    get_deal_manager_situation_state,
    get_deal_control_scope,
    get_deal_control_metrics,
    get_latest_deal_manager_situation_review,
    get_latest_ui_report,
    get_or_create_deal_daily_checklist,
    list_deal_control_bitrix_task_states,
    list_deal_control_deals,
    list_deal_control_task_history,
    list_deal_control_tasks,
    record_deal_control_task_event,
    save_deal_control_scope,
    save_deal_control_bitrix_tasks,
    save_deal_control_communications_today,
    save_deal_daily_checklist_item_completion,
    save_deal_control_task_crm_fact,
    save_deal_control_task_crm_sync,
    save_deal_control_task_outcome,
    set_deal_control_deal_active,
    set_deal_control_bitrix_task_completion,
    update_deal_control_fields,
    update_deal_control_task,
    upsert_deal_control_deal,
    review_deal_control_task_crm_fact,
)


DEAL_SELECT = [
    "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "CLOSED", "OPPORTUNITY",
    "CURRENCY_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE",
]
TOKEN_RE = re.compile(r"[\wа-яё-]{3,}", re.IGNORECASE)
MANAGER_SITUATION_REFINED_FIELDS = frozenset({
    "current_situation",
    "what_to_check_now",
    "rop_focus",
    "manager_coaching",
    "known",
    "unknowns",
    "contact_goal",
    "questions",
    "script",
    "script_variants",
    "crm_checklist",
    "script_channel",
})
DAILY_COMMUNICATION_TARGET = 3
CHECKLIST_LIMIT = 5

# Рабочий срез контроля сделок. Воронка 15 без отсечки по этапам — как сейчас.
# 17: с «Потребность выявлена», без закрытых. 47: с «Вышли на ЛПР», без закрытых.
# None = все открытые этапы этой воронки.
DEAL_CONTROL_PIPELINE_STAGE_IDS: dict[str, frozenset[str] | None] = {
    "15": None,
    "17": frozenset(
        {
            "C17:UC_3KRF2B",
            "C17:UC_QXDZOT",
            "C17:UC_U9TY5N",
            "C17:PREPAYMENT_INVOIC",
            "C17:EXECUTING",
            "C17:FINAL_INVOICE",
            "C17:UC_2QLXKE",
            "C17:UC_E16VYL",
            "C17:UC_9AXBMJ",
            "C17:UC_21L51H",
            "C17:UC_7KDUQ6",
            "C17:UC_ER3LNF",
            "C17:UC_9YB4R5",
        }
    ),
    "47": frozenset(
        {
            "C47:EXECUTING",
            "C47:UC_W9WXD3",
            "C47:FINAL_INVOICE",
        }
    ),
}


def _tokens(value: Any) -> set[str]:
    return set(TOKEN_RE.findall(str(value or "").lower()))


def _activity_time(activity: dict[str, Any]) -> datetime | None:
    for key in ("DEADLINE", "START_TIME", "END_TIME", "LAST_UPDATED", "CREATED"):
        parsed = parse_bitrix_dt(activity.get(key))
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK_TZ)
    return None


def _is_completed(activity: dict[str, Any]) -> bool:
    return str(activity.get("COMPLETED") or "").upper() in {"Y", "1", "TRUE"}


def _is_bitrix_task(activity: dict[str, Any]) -> bool:
    return str(activity.get("PROVIDER_ID") or "").upper().startswith("CRM_TASKS_")


def _deadline_time(activity: dict[str, Any]) -> datetime | None:
    parsed = parse_bitrix_dt(activity.get("DEADLINE"))
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK_TZ)


def _deadline_bucket(deadline: datetime | None, now: datetime) -> str:
    if deadline is None:
        return "unscheduled"
    if deadline < now:
        return "overdue"
    if deadline.date() == now.date():
        return "today"
    if (deadline.date() - now.date()).days == 1:
        return "tomorrow"
    return "future"


def _completed_at(activity: dict[str, Any]) -> datetime | None:
    for key in ("LAST_UPDATED", "END_TIME"):
        parsed = parse_bitrix_dt(activity.get(key))
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=MSK_TZ)
    return None


def _open_bitrix_tasks(activities: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for activity in activities:
        if not _is_bitrix_task(activity):
            continue
        completed = _is_completed(activity)
        completed_at = _completed_at(activity) if completed else None
        if completed and (
            completed_at is None
            or completed_at.astimezone(MSK_TZ).date() != now.astimezone(MSK_TZ).date()
        ):
            continue
        deadline = _deadline_time(activity)
        tasks.append({
            "activity_id": str(activity.get("ID") or ""),
            "task_id": str(activity.get("ASSOCIATED_ENTITY_ID") or ""),
            "responsible_id": str(activity.get("RESPONSIBLE_ID") or ""),
            "subject": str(activity.get("SUBJECT") or "Задача Bitrix"),
            "description": str(activity.get("DESCRIPTION") or ""),
            "deadline": deadline.isoformat() if deadline else None,
            "time_bucket": _deadline_bucket(deadline, now),
            "completed": completed,
            "bitrix_completed_at": completed_at.isoformat() if completed_at else None,
            "provider_id": str(activity.get("PROVIDER_ID") or ""),
        })
    rank = {"overdue": 0, "today": 1, "tomorrow": 2, "future": 3, "unscheduled": 4}
    return sorted(
        tasks,
        key=lambda item: (
            rank.get(str(item.get("time_bucket")), 5),
            int(bool(item.get("completed"))),
            str(item.get("deadline") or "9999-12-31"),
            str(item.get("activity_id") or ""),
        ),
    )


def _empty_daily_communications(now: datetime, *, available: bool) -> dict[str, Any]:
    current = now if now.tzinfo else now.replace(tzinfo=MSK_TZ)
    return {
        "date": current.astimezone(MSK_TZ).date().isoformat(),
        "available": available,
        "target": DAILY_COMMUNICATION_TARGET,
        "completed": 0,
        "progress_percent": 0,
        "calls": 0,
        "messages": 0,
        "duration_seconds": 0,
        "items": [],
    }


def _today_communications(activities: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    current = now if now.tzinfo else now.replace(tzinfo=MSK_TZ)
    current_date = current.astimezone(MSK_TZ).date()
    touchpoints: list[dict[str, Any]] = []
    for activity in activities:
        kind = activity_type(activity)
        if kind not in {"call", "email", "message"} or not _is_completed(activity):
            continue
        touchpoints.append({
            "when": activity.get("START_TIME") or activity.get("CREATED") or activity.get("END_TIME"),
            "event_type": kind,
            "entity_type": "deal",
            "entity_id": str(activity.get("OWNER_ID") or ""),
            "entity_key": f"deal:{activity.get('OWNER_ID') or ''}",
            "id": str(activity.get("ID") or ""),
            "subject": str(activity.get("SUBJECT") or ""),
            "text": str(activity.get("DESCRIPTION") or ""),
            "direction": activity.get("DIRECTION"),
            "raw": activity,
        })
    events = build_normalized_communications({"client_touchpoints": touchpoints, "internal_context": []})
    today_events: list[dict[str, Any]] = []
    for event in events:
        occurred = parse_bitrix_dt(event.get("occurred_at"))
        if occurred is None:
            continue
        localized = (occurred if occurred.tzinfo else occurred.replace(tzinfo=MSK_TZ)).astimezone(MSK_TZ)
        if localized.date() != current_date:
            continue
        today_events.append({
            "event_id": str(event.get("event_id") or ""),
            "channel": str(event.get("channel") or "unknown"),
            "direction": str(event.get("direction") or "unknown"),
            "occurred_at": localized.isoformat(timespec="seconds"),
            "subject": str(event.get("subject") or ""),
            "duration_seconds": event.get("duration_seconds"),
            "contact_class": str(event.get("contact_class") or "attempt"),
        })
    today_events.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("event_id") or "")))
    calls = sum(1 for event in today_events if event["channel"] == "call")
    messages = len(today_events) - calls
    duration = round(sum(float(event.get("duration_seconds") or 0) for event in today_events))
    completed = len(today_events)
    return {
        "date": current_date.isoformat(),
        "available": True,
        "target": DAILY_COMMUNICATION_TARGET,
        "completed": completed,
        "progress_percent": min(100, round(completed / DAILY_COMMUNICATION_TARGET * 100)),
        "calls": calls,
        "messages": messages,
        "duration_seconds": duration,
        "items": today_events,
    }


def _match_task_to_activity(task: dict[str, Any], activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return only a score; weak matches deliberately require an ROP decision."""
    confirmed_activity_id = str(task.get("crm_match_activity_id") or "")
    if confirmed_activity_id:
        for activity in activities:
            if str(activity.get("ID") or "") == confirmed_activity_id:
                return {"activity": activity, "confidence": "high", "score": 1.0, "days_delta": 0}
    task_tokens = _tokens(task.get("task_text"))
    due_at = parse_bitrix_dt(task.get("due_at"))
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=MSK_TZ)
    best: dict[str, Any] | None = None
    for activity in activities:
        activity_tokens = _tokens(f"{activity.get('SUBJECT') or ''} {activity.get('DESCRIPTION') or ''}")
        overlap = len(task_tokens & activity_tokens)
        similarity = overlap / max(len(task_tokens), 1)
        activity_at = _activity_time(activity)
        days_delta = None
        if due_at is not None and activity_at is not None:
            days_delta = abs((activity_at.date() - due_at.date()).days)
        date_score = 0.25 if days_delta is not None and days_delta <= 3 else 0.1 if days_delta is not None and days_delta <= 7 else 0
        score = similarity + date_score
        confidence = (
            "high"
            if similarity >= 0.55 and date_score >= 0.25
            else "medium"
            if (similarity >= 0.35 and date_score) or (_is_bitrix_task(activity) and days_delta == 0)
            else "low"
        )
        candidate = {"activity": activity, "confidence": confidence, "score": score, "days_delta": days_delta}
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def _execution_status(task: dict[str, Any], match: dict[str, Any] | None, now: datetime) -> tuple[str, str | None, str | None]:
    due_at = parse_bitrix_dt(task.get("due_at"))
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=MSK_TZ)
    if match is None or match["confidence"] == "low":
        return ("not_reflected", None, None)
    activity = match["activity"]
    activity_id = str(activity.get("ID") or "") or None
    if match["confidence"] == "medium":
        return ("match_review", activity_id, "medium")
    if _is_completed(activity):
        return ("crm_closed", activity_id, "high")
    return ("crm_open", activity_id, "high")


def _task_view_status(task: dict[str, Any], now: datetime) -> str:
    if task.get("local_status") != "active":
        return str(task.get("local_status"))
    due_at = parse_bitrix_dt(task.get("due_at"))
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=MSK_TZ)
    if due_at is not None and due_at < now:
        return "overdue"
    if due_at is not None and due_at.date() == now.date():
        return "today"
    return str(task.get("crm_execution_status") or "not_reflected")


def _task_time_bucket(task: dict[str, Any], now: datetime) -> str:
    if task.get("local_status") == "completed":
        completed_at = parse_bitrix_dt(task.get("completed_at"))
        if completed_at is not None and completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=MSK_TZ)
        return "completed_today" if completed_at is not None and completed_at.date() == now.date() else "completed"
    if task.get("local_status") == "cancelled":
        return "cancelled"
    due_at = parse_bitrix_dt(task.get("due_at"))
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=MSK_TZ)
    if due_at is None:
        return "future"
    if due_at < now:
        return "overdue"
    if due_at.date() == now.date():
        return "today"
    if (due_at.date() - now.date()).days == 1:
        return "tomorrow"
    return "future"


def _manager_names(client: Any, manager_ids: set[str]) -> dict[str, str]:
    if not manager_ids:
        return {}
    try:
        rows = client.list_all("user.get", {"filter": {"ID": sorted(manager_ids)}})
    except Exception:  # noqa: BLE001 - the dashboard still works if the CRM user list is unavailable
        return {}
    names: dict[str, str] = {}
    for row in rows:
        user_id = str(row.get("ID") or "")
        name = " ".join(str(row.get(key) or "").strip() for key in ("LAST_NAME", "NAME")).strip()
        if user_id and name:
            names[user_id] = name
    return names


def _deal_row(deal: dict[str, Any], *, source: str, stage_names: dict[str, str], manager_names: dict[str, str]) -> dict[str, Any]:
    deal_id = str(deal.get("ID") or "")
    manager_id = str(deal.get("ASSIGNED_BY_ID") or "") or None
    stage_id = str(deal.get("STAGE_ID") or "") or None
    return {
        "deal_id": deal_id,
        "source": source,
        "title": str(deal.get("TITLE") or f"Сделка {deal_id}"),
        "manager_id": manager_id,
        "manager_name": manager_names.get(manager_id or "") or (f"Ответственный #{manager_id}" if manager_id else "Не назначен"),
        "stage_id": stage_id,
        "stage_name": stage_names.get(stage_id or "") or stage_id or "Не указана",
        "pipeline_id": str(deal.get("CATEGORY_ID") or "") or None,
        "amount": str(deal.get("OPPORTUNITY") or "") or None,
        "currency_id": str(deal.get("CURRENCY_ID") or "") or None,
        "created_at_crm": str(deal.get("DATE_CREATE") or "") or None,
        "modified_at_crm": str(deal.get("DATE_MODIFY") or "") or None,
        "is_active": str(deal.get("CLOSED") or "").upper() != "Y",
    }


def _stage_allowed(pipeline_id: str, stage_id: str) -> bool:
    """Воронка 15 и неизвестные воронки — все открытые этапы. 17/47 — только рабочий срез."""
    allowed = DEAL_CONTROL_PIPELINE_STAGE_IDS.get(str(pipeline_id))
    if allowed is None:
        return True
    return str(stage_id) in allowed


def _fetch_pipeline_deals(client: Any, *, pipeline_ids: list[str], manager_ids: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pipeline_id in pipeline_ids:
        batch = client.list_all(
            "crm.deal.list",
            {
                "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
                "filter": {"CATEGORY_ID": pipeline_id, "CLOSED": "N"},
                "select": DEAL_SELECT,
            },
        )
        for row in batch:
            deal_id = str(row.get("ID") or "")
            if not deal_id or deal_id in seen:
                continue
            if str(row.get("ASSIGNED_BY_ID") or "") not in manager_ids:
                continue
            if str(row.get("CLOSED") or "").upper() == "Y":
                continue
            row_pipeline = str(row.get("CATEGORY_ID") or pipeline_id)
            if not _stage_allowed(row_pipeline, str(row.get("STAGE_ID") or "")):
                continue
            seen.add(deal_id)
            rows.append(row)
    return rows


def _fetch_initial_deals(client: Any, deal_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    ids = list(dict.fromkeys(str(item).strip() for item in deal_ids if str(item).strip()))
    if not ids:
        return [], []
    try:
        rows = client.list_all(
            "crm.deal.list",
            {
                "order": {"ID": "ASC"},
                "filter": {"ID": ids},
                "select": DEAL_SELECT,
            },
        )
    except Exception as error:  # noqa: BLE001 - one failure must not turn into 36 slow retries
        return [], [f"Стартовая выборка: {error}"]
    found_ids = {str(row.get("ID") or "") for row in rows}
    errors = [f"Сделка #{deal_id}: CRM не вернула карточку" for deal_id in ids if deal_id not in found_ids]
    return rows, errors


def refresh_deal_control(*, db_path: str | Path = DEFAULT_DB_PATH, client: Any | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Synchronise the configured deal portfolio using only Bitrix read calls."""
    scope = get_deal_control_scope(db_path)
    if not scope["configured"]:
        return build_deal_control_dashboard(db_path=db_path, now=now, sync_message="Сначала настройте локальную выборку сделок.")
    crm = client or make_client()
    current = now or datetime.now(MSK_TZ)
    stage_names = load_pipeline_stage_names()
    initial, errors = _fetch_initial_deals(crm, scope["initial_deal_ids"])
    manager_ids = {str(value) for value in scope["manager_ids"]}
    pipeline_ids = [str(item) for item in (scope.get("pipeline_ids") or [scope["pipeline_id"]]) if str(item).strip()]
    pipeline_rows = _fetch_pipeline_deals(crm, pipeline_ids=pipeline_ids, manager_ids=manager_ids)
    initial_ids = {str(value) for value in scope["initial_deal_ids"]}
    seen_pipeline_ids = {str(row.get("ID") or "") for row in pipeline_rows}
    saved_before = list_deal_control_deals(db_path, active_only=False)
    previously_active_ids = {
        str(item["deal_id"])
        for item in saved_before
        if item.get("is_active")
    }
    missing_pipeline_ids = [
        str(item["deal_id"])
        for item in saved_before
        if item.get("source") == "pipeline" and str(item.get("deal_id")) not in seen_pipeline_ids
    ]
    missing_pipeline_rows, missing_errors = _fetch_initial_deals(crm, missing_pipeline_ids)
    errors.extend(missing_errors)
    source_by_id = {str(item["deal_id"]): str(item.get("source") or "pipeline") for item in saved_before}
    rows_by_id = {
        str(row.get("ID") or ""): row
        for row in [*initial, *pipeline_rows, *missing_pipeline_rows]
        if str(row.get("ID") or "")
    }
    manager_names = _manager_names(
        crm,
        {str(row.get("ASSIGNED_BY_ID") or "") for row in rows_by_id.values()},
    )
    for row in rows_by_id.values():
        deal_id = str(row.get("ID") or "")
        if not deal_id:
            continue
        source = "initial" if deal_id in initial_ids else source_by_id.get(deal_id, "pipeline")
        row_data = _deal_row(row, source=source, stage_names=stage_names, manager_names=manager_names)
        upsert_deal_control_deal(db_path, **row_data)
    # A pipeline-sourced deal that left the active filtered scope is no longer active in this dashboard.
    for saved in list_deal_control_deals(db_path, active_only=False):
        if saved.get("source") == "pipeline" and str(saved.get("deal_id")) not in seen_pipeline_ids:
            set_deal_control_deal_active(db_path, deal_id=str(saved["deal_id"]), is_active=False)
    saved_after = list_deal_control_deals(db_path, active_only=False)
    deals = [
        item
        for item in saved_after
        if item.get("is_active") or str(item["deal_id"]) in previously_active_ids
    ]
    sync_deal_ids = [str(item["deal_id"]) for item in deals]
    sync_entities = [("deal", {"ID": deal_id}) for deal_id in sync_deal_ids]
    day_start = current.astimezone(MSK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    open_task_activities = fetch_candidate_activities_bulk(
        crm,
        sync_entities,
        additional_filter={"PROVIDER_ID": "CRM_TASKS_TASK", "COMPLETED": "N"},
    )
    completed_today_activities = fetch_candidate_activities_bulk(
        crm,
        sync_entities,
        additional_filter={"COMPLETED": "Y", ">LAST_UPDATED": day_start.isoformat(timespec="seconds")},
    )
    deals_by_id = {str(item["deal_id"]): item for item in deals}
    sync_tasks = list_deal_control_tasks(db_path, deal_ids=sync_deal_ids) if sync_deal_ids else []
    task_deal_ids = list(dict.fromkeys(str(task["deal_id"]) for task in sync_tasks))
    task_activities = fetch_candidate_activities_bulk(
        crm,
        [("deal", {"ID": deal_id}) for deal_id in task_deal_ids],
    )
    for deal_item in deals:
        deal_id = str(deal_item["deal_id"])
        open_items, open_error = open_task_activities.get(("deal", deal_id), ([], "open tasks unavailable"))
        completed_items, completed_error = completed_today_activities.get(
            ("deal", deal_id),
            ([], "completed activities unavailable"),
        )
        activity_errors = [str(value) for value in (open_error, completed_error) if value]
        if activity_errors:
            errors.append(f"Сделка #{deal_id}: {'; '.join(activity_errors)}")
            save_deal_control_communications_today(
                db_path,
                deal_id=deal_id,
                summary=_empty_daily_communications(current, available=False),
            )
            continue
        deal_activities = {
            str(activity.get("ID") or ""): activity
            for activity in [*open_items, *completed_items]
            if str(activity.get("ID") or "")
        }
        save_deal_control_bitrix_tasks(
            db_path,
            deal_id=deal_id,
            tasks=_open_bitrix_tasks(list(deal_activities.values()), current),
        )
        save_deal_control_communications_today(
            db_path,
            deal_id=deal_id,
            summary=_today_communications(completed_items, current),
        )
    reported_task_activity_errors: set[str] = set()
    for task in sync_tasks:
        task_deal_id = str(task["deal_id"])
        task_activity_items, activity_error = task_activities.get(
            ("deal", task_deal_id),
            ([], "task activity unavailable"),
        )
        if activity_error:
            if task_deal_id not in reported_task_activity_errors:
                errors.append(f"Сделка #{task_deal_id}: {activity_error}")
                reported_task_activity_errors.add(task_deal_id)
            continue
        match = _match_task_to_activity(task, task_activity_items)
        status, activity_id, confidence = _execution_status(task, match, current)
        fact_kind = None
        fact_summary = None
        fact_at = None
        if status == "crm_closed" and match is not None:
            activity = match["activity"]
            fact_kind = "crm_activity_completed"
            fact_summary = str(activity.get("SUBJECT") or "CRM-задача закрыта")
            occurred = _activity_time(activity)
            fact_at = occurred.isoformat() if occurred else None
        save_deal_control_task_crm_sync(
            db_path, task_id=int(task["id"]), crm_execution_status=status, crm_match_activity_id=activity_id,
            crm_match_confidence=confidence,
            crm_match_candidate_completed=bool(match and _is_completed(match["activity"])),
            result_activity_id=activity_id if fact_kind else None,
            fact_kind=fact_kind, fact_summary=fact_summary, fact_occurred_at=fact_at,
        )
        task_created = parse_bitrix_dt(task.get("created_at"))
        if task_created is not None and task_created.tzinfo is None:
            task_created = task_created.replace(tzinfo=MSK_TZ)
        for activity in task_activity_items:
            activity_id_value = str(activity.get("ID") or "")
            occurred = _activity_time(activity)
            if not activity_id_value or occurred is None or (task_created is not None and occurred < task_created):
                continue
            save_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_key=f"activity:{activity_id_value}",
                activity_id=activity_id_value,
                fact_kind="crm_activity_completed" if _is_completed(activity) else "crm_activity_created",
                summary=str(activity.get("SUBJECT") or "CRM-активность"),
                occurred_at=occurred.isoformat(),
                contact_class="attempt",
                payload={"completed": _is_completed(activity)},
            )
        baseline = task.get("baseline") if isinstance(task.get("baseline"), dict) else {}
        snapshot = baseline.get("deal_snapshot") if isinstance(baseline.get("deal_snapshot"), dict) else {}
        baseline_stage = str(snapshot.get("stage_id") or "")
        deal = deals_by_id.get(str(task["deal_id"])) or {}
        current_stage = str(deal.get("stage_id") or "")
        raw_deal = rows_by_id.get(str(task["deal_id"])) or {}
        semantic = str(raw_deal.get("STAGE_SEMANTIC_ID") or "").upper()
        if baseline_stage and current_stage and baseline_stage != current_stage:
            stage_kind = "deal_won" if semantic == "S" else "deal_lost" if semantic == "F" else "stage_changed"
            save_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_key=f"stage:{baseline_stage}:{current_stage}:{semantic}",
                activity_id=None,
                fact_kind=stage_kind,
                summary=f"Стадия изменилась: {snapshot.get('stage_name') or baseline_stage} → {deal.get('stage_name') or current_stage}",
                occurred_at=str(deal.get("modified_at_crm") or current.isoformat()),
                contact_class="deal_progress",
                payload={"from_stage": baseline_stage, "to_stage": current_stage, "semantic": semantic},
            )
    message = "CRM обновлена" if not errors else "CRM обновлена с ограничениями"
    return build_deal_control_dashboard(db_path=db_path, now=current, sync_message=message, sync_errors=errors)


def _analysis_coaching(db_path: str | Path, deal_id: str) -> dict[str, Any]:
    report = get_latest_ui_report(db_path, entity_type="deal", entity_id=deal_id)
    analysis = unwrap_analysis_payload(report.get("report_json") if report else None)
    rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
    money = analysis.get("money_path_diagnosis") if isinstance(analysis.get("money_path_diagnosis"), dict) else {}
    price = analysis.get("price_comparability_check") if isinstance(analysis.get("price_comparability_check"), dict) else {}
    payment = analysis.get("payment_blocker") if isinstance(analysis.get("payment_blocker"), dict) else {}
    manager = analysis.get("manager_action_block") if isinstance(analysis.get("manager_action_block"), dict) else {}
    brief = analysis.get("deal_control_brief") if isinstance(analysis.get("deal_control_brief"), dict) else {}
    deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
    mode = analysis.get("deal_mode") if isinstance(analysis.get("deal_mode"), dict) else {}
    risk = analysis.get("main_risk") if isinstance(analysis.get("main_risk"), dict) else {}
    quality = analysis.get("manager_quality") if isinstance(analysis.get("manager_quality"), dict) else {}
    communication_audit = (
        analysis.get("communication_quality_audit")
        if COMMUNICATION_QUALITY_AUDIT_ENABLED and isinstance(analysis.get("communication_quality_audit"), dict)
        else None
    )
    shaker = analysis.get("shaker_question") if isinstance(analysis.get("shaker_question"), dict) else {}
    primary = manager.get("primary_text") if isinstance(manager.get("primary_text"), dict) else {}
    backups = manager.get("backup_texts") if isinstance(manager.get("backup_texts"), list) else []
    backup_script = next(
        (
            str(item.get("text") or "")
            for item in backups
            if isinstance(item, dict) and str(item.get("type") or "") == "call_script" and str(item.get("text") or "")
        ),
        "",
    )

    def texts(value: Any, limit: int = 5) -> list[str]:
        return [
            str(item).strip()
            for item in (value if isinstance(value, list) else [])
            if item is not None and str(item).strip()
        ][:limit]

    known = texts(brief.get("known_facts")) or texts([*(rop.get("evidence") or []), *(money.get("evidence") or [])])
    unknowns = texts(brief.get("missing_facts")) or texts(
        [*(price.get("what_is_unclear") or []), *(payment.get("missing_confirmation") or [])]
    )
    strengths = texts(brief.get("strengths")) or texts(quality.get("what_done_well"))
    weaknesses = texts(brief.get("weaknesses")) or texts(
        [*(quality.get("missed_points") or []), risk.get("description")]
    )
    questions = texts(brief.get("contact_questions"))
    if not questions:
        questions = texts(
            [
                *(price.get("what_is_unclear") or []),
                *(payment.get("missing_confirmation") or []),
                shaker.get("question"),
            ]
        )
    script_variants = texts(brief.get("call_opening_variants"), 2)
    if not script_variants:
        script_variants = texts(
            [
                item.get("text")
                for item in backups
                if isinstance(item, dict)
            ],
            2,
        )
    coaching = {
        "report_id": report.get("id") if report else None,
        "analysis_created_at": report.get("created_at") if report else None,
        "current_situation": str(brief.get("current_situation") or deal_state.get("summary") or ""),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "rop_focus": str(brief.get("rop_focus") or mode.get("rop_focus") or rop.get("check_for_rop") or ""),
        "what_to_check_now": str(
            brief.get("what_to_check_now")
            or rop.get("check_for_rop")
            or money.get("next_required_fact")
            or ""
        ),
        "manager_coaching": str(brief.get("manager_coaching") or rop.get("message_to_manager") or ""),
        "known": known,
        "unknowns": unknowns,
        "contact_goal": str(brief.get("contact_goal") or manager.get("goal") or ""),
        "questions": questions,
        "script": str(brief.get("call_script") or primary.get("text") or primary.get("call_script") or backup_script),
        "script_variants": script_variants,
        "crm_checklist": texts(manager.get("manager_checklist")),
        "script_channel": str(manager.get("recommended_channel") or ""),
        "rop_task_hint": str(rop.get("message_to_manager") or rop.get("check_for_rop") or ""),
        "expected_crm_update": str(rop.get("expected_crm_update") or money.get("next_required_fact") or ""),
        "communication_quality_audit": communication_audit,
        "direct_manager_question": str(brief.get("direct_manager_question") or ""),
    }
    if report is None or report.get("id") is None:
        return coaching
    review = get_latest_deal_manager_situation_review(
        db_path,
        deal_id=str(deal_id),
        source_report_id=int(report["id"]),
    )
    refined = review.get("refined_coaching") if review is not None else None
    if not isinstance(refined, dict):
        return coaching
    for field in MANAGER_SITUATION_REFINED_FIELDS:
        value = refined.get(field)
        if _has_refined_value(value):
            coaching[field] = value
    return coaching


def _has_refined_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_refined_value(item) for item in value)
    if isinstance(value, dict):
        return bool(value)
    return True


def _checklist_action(value: Any, *, source: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n.;:")
    if not text:
        return ""
    lowered = text.lower()
    negative_match = re.match(
        r"^не\s+(?:подтвержд[её]н(?:а|о|ы)?|зафиксирован(?:а|о|ы)?|согласован(?:а|о|ы)?)\s+(.+)$",
        lowered,
    )
    if negative_match:
        action = f"Подтвердить {negative_match.group(1)}"
    elif lowered.startswith("нет данных о "):
        action = f"Уточнить {lowered.removeprefix('нет данных о ')}"
    elif source == "crm":
        action = f"Зафиксировать в CRM {lowered}"
    elif re.match(r"^(кто|когда|где|как|какой|какая|какие|актуален ли|актуальна ли)\b", lowered):
        action = f"Уточнить, {lowered}"
    elif lowered.startswith("дата "):
        action = f"Подтвердить дату {lowered.removeprefix('дата ')}"
    elif re.match(r"^(бюджет|срок|ЛПР)\b", text, flags=re.IGNORECASE):
        action = f"Подтвердить {lowered}"
    elif lowered.startswith("точный состав "):
        action = f"Получить {lowered}"
    elif re.match(r"^(уточнить|подтвердить|получить|зафиксировать|согласовать|обновить|назначить|проверить|определить)\b", lowered):
        action = text[0].upper() + text[1:]
    else:
        action = f"Уточнить {lowered}"
    action = re.sub(r"\bлпр\b", "ЛПР", action, flags=re.IGNORECASE)
    action = re.sub(r"\bчз\b", "ЧЗ", action, flags=re.IGNORECASE)
    action = re.sub(r"\b1с\b", "1С", action, flags=re.IGNORECASE)
    return action.rstrip(".") + "."


def _deal_checklist_candidates(
    coaching: dict[str, Any],
    legacy_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    report_id = int(coaching.get("report_id") or 0)
    candidates: list[tuple[str, str]] = []
    for value in coaching.get("unknowns") or []:
        candidates.append(("missing", _checklist_action(value, source="missing")))
    candidates.append(("focus", _checklist_action(coaching.get("what_to_check_now"), source="focus")))
    for value in coaching.get("crm_checklist") or []:
        candidates.append(("crm", _checklist_action(value, source="crm")))

    seen: set[str] = set()
    legacy_items = (
        legacy_state.get("items")
        if isinstance(legacy_state, dict) and int(legacy_state.get("source_report_id") or 0) == report_id
        else {}
    )
    if not isinstance(legacy_items, dict):
        legacy_items = {}
    items: list[dict[str, Any]] = []
    for source, text in candidates:
        normalized = re.sub(r"\W+", " ", text.lower(), flags=re.UNICODE).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        legacy_item_id = hashlib.sha256(f"{report_id}:{source}:{normalized}".encode("utf-8")).hexdigest()[:16]
        legacy_item = legacy_items.get(legacy_item_id) if isinstance(legacy_items.get(legacy_item_id), dict) else {}
        items.append({
            "text": text,
            "source": source,
            "completed": bool(legacy_item.get("completed")),
            "completed_at": legacy_item.get("completed_at"),
            "completed_by": legacy_item.get("completed_by"),
        })
        if len(items) >= CHECKLIST_LIMIT:
            break
    return items


def _deal_checklist(
    db_path: str | Path,
    deal_id: str,
    coaching: dict[str, Any],
    *,
    business_date: datetime | str | None = None,
    legacy_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report_id = int(coaching.get("report_id") or 0)
    return get_or_create_deal_daily_checklist(
        db_path,
        deal_id=str(deal_id),
        business_date=business_date,
        seed_items=_deal_checklist_candidates(coaching, legacy_state) if report_id else [],
        source_report_id=report_id or None,
    )


def build_deal_control_dashboard(*, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None,
                                 sync_message: str | None = None, sync_errors: list[str] | None = None) -> dict[str, Any]:
    from api.daily_control import project_deal_review_card

    current = now or datetime.now(MSK_TZ)
    deals = list_deal_control_deals(db_path)
    active_deal_ids = [str(deal["deal_id"]) for deal in deals]
    all_tasks = list_deal_control_tasks(db_path, deal_ids=active_deal_ids) if active_deal_ids else []
    bitrix_states = {
        str(item["activity_id"]): item
        for item in list_deal_control_bitrix_task_states(db_path, deal_ids=active_deal_ids)
    } if active_deal_ids else {}
    tasks_by_deal: dict[str, list[dict[str, Any]]] = {}
    for task in all_tasks:
        task = dict(task)
        task["view_status"] = _task_view_status(task, current)
        task["time_bucket"] = _task_time_bucket(task, current)
        tasks_by_deal.setdefault(str(task["deal_id"]), []).append(task)
    items = []
    projected_primary_tasks: list[dict[str, Any]] = []
    missing = 0
    for deal in deals:
        deal = dict(deal)
        communications_today = deal.get("communications_today")
        current_date = current.astimezone(MSK_TZ).date().isoformat()
        if not isinstance(communications_today, dict) or communications_today.get("date") != current_date:
            communications_today = _empty_daily_communications(current, available=False)
        deal["communications_today"] = communications_today
        deal_tasks = tasks_by_deal.get(str(deal["deal_id"]), [])
        deal_tasks.sort(
            key=lambda task: (
                -int(task.get("attention_priority") or 0),
                str(task.get("due_at") or "9999-12-31"),
                int(task.get("id") or 0),
            )
        )
        deal["tasks"] = deal_tasks
        neuro_tasks = [
            task for task in deal_tasks
            if task.get("source_kind") == "neuro_rop" and task.get("local_status") == "active"
        ]
        deal["current_task"] = max(
            neuro_tasks,
            key=lambda task: (int(task.get("source_report_id") or 0), int(task.get("id") or 0)),
            default=None,
        )
        bitrix_tasks = []
        for bitrix_task in deal.get("bitrix_tasks") or []:
            projected = dict(bitrix_task)
            deadline = parse_bitrix_dt(projected.get("deadline"))
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=MSK_TZ)
            projected["time_bucket"] = _deadline_bucket(deadline, current)
            local_state = bitrix_states.get(str(projected.get("activity_id") or ""), {})
            projected["local_completed"] = bool(local_state.get("local_completed"))
            projected["local_completed_at"] = local_state.get("local_completed_at")
            projected["local_completed_by"] = local_state.get("local_completed_by")
            projected["completion_state"] = (
                "bitrix"
                if projected.get("completed")
                else "local"
                if projected["local_completed"]
                else "open"
            )
            bitrix_tasks.append(projected)
        rank = {"overdue": 0, "today": 1, "tomorrow": 2, "future": 3, "unscheduled": 4}
        bitrix_tasks.sort(key=lambda item: (
            rank.get(str(item.get("time_bucket")), 5),
            int(str(item.get("completion_state")) != "open"),
            str(item.get("deadline") or "9999-12-31"),
            str(item.get("activity_id") or ""),
        ))
        deal["bitrix_tasks"] = bitrix_tasks
        deal["primary_bitrix_task"] = bitrix_tasks[0] if bitrix_tasks else None
        if deal["primary_bitrix_task"] is not None:
            projected_primary_tasks.append(deal["primary_bitrix_task"])
        else:
            missing += 1
        deal["coaching"] = _analysis_coaching(db_path, str(deal["deal_id"]))
        deal["checklist"] = _deal_checklist(
            db_path,
            str(deal["deal_id"]),
            deal["coaching"],
            business_date=current,
            legacy_state=deal.get("checklist_state") if isinstance(deal.get("checklist_state"), dict) else None,
        )
        deal["manager_situation"] = get_deal_manager_situation_state(
            db_path,
            deal_id=str(deal["deal_id"]),
        )
        deal["review"] = project_deal_review_card(deal)
        items.append(deal)
    items = [
        deal for _, deal in sorted(
            enumerate(items),
            key=lambda pair: (
                0 if pair[1].get("current_task") is not None else 1,
                -int((pair[1].get("current_task") or {}).get("attention_priority") or 0),
                str(((pair[1].get("current_task") or {}).get("due_at") or "9999-12-31")),
                pair[0],
            ),
        )
    ]
    task_buckets = [str(task.get("time_bucket") or "unscheduled") for task in projected_primary_tasks]
    overdue = task_buckets.count("overdue")
    today = task_buckets.count("today")
    tomorrow = task_buckets.count("tomorrow")
    future = task_buckets.count("future")
    completed_today = sum(
        1
        for task in projected_primary_tasks
        if str(task.get("completion_state") or "open") in {"local", "bitrix"}
        and str(task.get("time_bucket") or "") in {"overdue", "today"}
    )
    plan_today = missing + overdue + today
    probability_values = [int(item["probability"]) for item in deals if item.get("probability") is not None]
    total_amount = sum(float(str(item.get("amount") or "0").replace(",", ".") or 0) for item in deals if str(item.get("amount") or "").replace(",", ".").replace(".", "", 1).isdigit())
    return {
        "scope": get_deal_control_scope(db_path),
        "generated_at": current.isoformat(timespec="seconds"),
        "sync_message": sync_message,
        "sync_errors": sync_errors or [],
        "summary": {
            "active_deals": len(deals), "portfolio_amount": total_amount,
            "tasks_total": len(projected_primary_tasks), "tasks_today": today, "tasks_tomorrow": tomorrow,
            "tasks_future": future, "tasks_overdue": overdue, "tasks_completed_today": completed_today,
            "tasks_missing": missing, "tasks_plan_today": plan_today,
            "average_probability": round(sum(probability_values) / len(probability_values)) if probability_values else None,
        },
        "outcome_metrics": get_deal_control_metrics(db_path),
        "deals": items,
    }


def save_scope(
    *,
    db_path: str | Path,
    initial_deal_ids: list[str],
    manager_ids: list[str],
    pipeline_id: str,
    pipeline_ids: list[str] | None = None,
) -> dict[str, Any]:
    return save_deal_control_scope(
        db_path,
        initial_deal_ids=initial_deal_ids,
        manager_ids=manager_ids,
        pipeline_id=pipeline_id,
        pipeline_ids=pipeline_ids,
    )


def save_deal_fields(*, db_path: str | Path, deal_id: str, probability: int | None,
                     expected_payment_period: str | None, next_control_at: str | None) -> dict[str, Any]:
    return update_deal_control_fields(
        db_path, deal_id=deal_id, probability=probability,
        expected_payment_period=expected_payment_period, next_control_at=next_control_at,
    )


def save_bitrix_task_completion(
    *,
    db_path: str | Path,
    deal_id: str,
    activity_id: str,
    completed: bool,
    source_role: str,
) -> dict[str, Any]:
    return set_deal_control_bitrix_task_completion(
        db_path,
        deal_id=deal_id,
        activity_id=activity_id,
        completed=completed,
        source_role=source_role,
    )


def save_checklist_item_completion(
    *,
    db_path: str | Path,
    deal_id: str,
    item_id: str,
    completed: bool,
) -> dict[str, Any]:
    deal = next(
        (item for item in list_deal_control_deals(db_path) if str(item.get("deal_id")) == str(deal_id)),
        None,
    )
    if deal is None:
        raise ValueError("Сделка ещё не добавлена в контур контроля")
    coaching = _analysis_coaching(db_path, str(deal_id))
    checklist = _deal_checklist(
        db_path,
        str(deal_id),
        coaching,
        legacy_state=deal.get("checklist_state") if isinstance(deal.get("checklist_state"), dict) else None,
    )
    report_id = checklist.get("source_report_id")
    if not report_id:
        raise ValueError("Чек-лист появится после успешного анализа сделки")
    if not any(str(item.get("id")) == str(item_id) for item in checklist["items"]):
        raise ValueError("Пункт не найден в актуальном чек-листе")
    return save_deal_daily_checklist_item_completion(
        db_path,
        deal_id=str(deal_id),
        item_id=str(item_id),
        completed=completed,
    )


def add_task(*, db_path: str | Path, deal_id: str, task_text: str, touch_type: str | None,
             expected_result: str | None, due_at: str) -> dict[str, Any]:
    return create_deal_control_task(
        db_path, deal_id=deal_id, task_text=task_text, touch_type=touch_type,
        expected_result=expected_result, due_at=due_at,
    )


def edit_task(*, db_path: str | Path, task_id: int, task_text: str | None, touch_type: str | None,
              expected_result: str | None, due_at: str | None, local_status: str | None,
              business_result_status: str | None, business_result_note: str | None,
              reschedule_reason: str | None = None, source_role: str | None = None) -> dict[str, Any]:
    return update_deal_control_task(
        db_path, task_id=task_id, task_text=task_text, touch_type=touch_type,
        expected_result=expected_result, due_at=due_at, local_status=local_status,
        business_result_status=business_result_status, business_result_note=business_result_note,
        reschedule_reason=reschedule_reason, source_role=source_role,
    )


def task_history(*, db_path: str | Path, task_id: int) -> dict[str, list[dict[str, Any]]]:
    return list_deal_control_task_history(db_path, task_id=task_id)


def confirm_task_crm_match(*, db_path: str | Path, task_id: int) -> dict[str, Any]:
    return confirm_deal_control_task_crm_match(db_path, task_id=task_id)


def record_task_outcome(
    *,
    db_path: str | Path,
    task_id: int,
    contact_status: str,
    result_status: str,
    result_note: str | None,
    next_step_text: str | None,
    next_step_at: str | None,
    evidence_kind: str | None,
    evidence_id: str | None,
    source_role: str,
) -> dict[str, Any]:
    return save_deal_control_task_outcome(
        db_path,
        task_id=task_id,
        contact_status=contact_status,
        result_status=result_status,
        result_note=result_note,
        next_step_text=next_step_text,
        next_step_at=next_step_at,
        evidence_kind=evidence_kind,
        evidence_id=evidence_id,
        source_role=source_role,
    )


def review_task_crm_fact(
    *,
    db_path: str | Path,
    task_id: int,
    fact_id: int,
    review_status: str,
    contact_class: str | None,
) -> dict[str, Any]:
    return review_deal_control_task_crm_fact(
        db_path,
        task_id=task_id,
        fact_id=fact_id,
        review_status=review_status,
        contact_class=contact_class,
    )


def deal_control_metrics(*, db_path: str | Path, manager_id: str | None = None) -> dict[str, Any]:
    return get_deal_control_metrics(db_path, manager_id=manager_id)


def record_task_event(
    *,
    db_path: str | Path,
    task_id: int,
    event_type: str,
    event_key: str | None,
) -> dict[str, bool]:
    record_deal_control_task_event(
        db_path,
        task_id=task_id,
        event_type=event_type,
        event_key=event_key,
    )
    return {"ok": True}
