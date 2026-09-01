"""Immutable daily-control snapshots for the ROP planning meeting.

The builder reuses the existing deal-control dashboard. It does not create a
second analysis or Bitrix write methods.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from api.deal_task_day import is_reschedule, stamp, task_results, task_totals
from openai_api.llm.deal_daily_quality import (
    CLIENT_CHANNELS,
    is_daily_quality_evidence,
    quality_event_signature,
    quality_time,
)

from setup import MSK_TZ, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    create_daily_control_report,
    dumps_json,
    get_daily_control_report,
    get_latest_automatic_analysis_run,
    get_latest_ui_report,
    list_daily_control_reports,
    list_deal_control_deals,
    list_manager_trajectory_events,
    utcish_now,
)

GENERIC_STATUS_QUESTION = "Что было сделано и какой сейчас статус?"
STATUS_LABELS = {
    "red": "Требует решения РОПа",
    "yellow": "Нужна проверка",
    "green": "В норме",
}
COMMUNICATION_ITEM_KEYS = (
    "event_id",
    "channel",
    "direction",
    "occurred_at",
    "subject",
    "duration_seconds",
    "contact_class",
    "call_outcome",
    "talk_duration_seconds",
    "content_available",
    "quality_evidence",
    "participant_name",
    "status_label",
    "source_type",
)
COMMUNICATION_REF_KEYS = (
    "event_id",
    "occurred_at",
    "channel",
    "direction",
    "kind",
    "label",
)
INVENTED_CONTENT_KEYS = (
    "text",
    "body",
    "html",
    "transcript",
    "audio",
    "message",
    "description",
    "content",
)
CLIENT_CONTACT_KINDS = frozenset({"call", "message"})
DAY_OBLIGATION_BUCKETS = frozenset({"today", "overdue"})

logger = get_logger(__file__)

_generation_lock = threading.Lock()
_generation_state: dict[str, Any] | None = None

DAILY_CONTROL_VISIBLE_FROM = "2026-08-31"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=MSK_TZ)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(MSK_TZ).isoformat(timespec="seconds")


def _business_date(value: datetime | str) -> str:
    if isinstance(value, str):
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        current = value
    return _aware(current).astimezone(MSK_TZ).date().isoformat()


def _at_or_before(value: Any, cutoff: datetime, *, same_day: bool = True) -> bool:
    try:
        stamp = _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00"))).astimezone(MSK_TZ)
    except (TypeError, ValueError):
        return False
    return stamp <= cutoff and (not same_day or stamp.date() == cutoff.date())


def _daily_trajectory_events(
    db_path: str | Path,
    cutoff: datetime,
    *,
    recorded_through: datetime | None = None,
) -> list[dict[str, Any]]:
    known_until = recorded_through if recorded_through is not None else cutoff
    rows = list_manager_trajectory_events(
        db_path,
        from_at=_iso(cutoff.replace(hour=0, minute=0, second=0, microsecond=0)),
        to_at=_iso(cutoff + timedelta(seconds=1)),
    )
    return [
        row for row in rows
        if row.get("entity_type") == "deal"
        and _at_or_before(row.get("occurred_at"), cutoff)
        and _at_or_before(row.get("recorded_at"), known_until, same_day=False)
    ]


def _day_activity_kind(event: dict[str, Any]) -> str | None:
    """Client contact or Bitrix-task outcome. Stage, comment and card edits are not day work."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    kind = event.get("event_type")
    if kind == "crm_activity_observed" and payload.get("completed") is True:
        return {"call": "call", "message": "message", "email": "message", "task": "bitrix_task_completed"}.get(payload.get("activity_kind"))
    if kind == "crm_timeline_comment_observed":
        return "message" if payload.get("is_messenger_mirror") else None
    if kind == "crm_task_history_observed":
        if is_reschedule(payload):
            return "bitrix_task_rescheduled"
        field = str(payload.get("field") or "").upper()
        value = str(payload.get("to_value") or "").upper()
        if (field == "STATUS" and value == "5") or (field == "COMPLETED" and value in {"Y", "1", "TRUE"}):
            return "bitrix_task_completed"
    return None


def report_heading(
    creation_kind: str | None,
    business_date: str | None,
    cutoff_at: datetime | str | None,
) -> str:
    """Human title: kind + date + cutoff time, so history does not need a tiny caption."""
    cutoff: datetime | None
    if isinstance(cutoff_at, datetime):
        cutoff = _aware(cutoff_at).astimezone(MSK_TZ)
    elif cutoff_at:
        try:
            cutoff = _aware(datetime.fromisoformat(str(cutoff_at).replace("Z", "+00:00"))).astimezone(MSK_TZ)
        except ValueError:
            cutoff = None
    else:
        cutoff = None
    day = None
    if business_date:
        try:
            day = datetime.fromisoformat(str(business_date)[:10]).date()
        except ValueError:
            day = None
    if day is None and cutoff is not None:
        day = cutoff.date()
    if day is None:
        return "Ежедневный контроль"
    weekdays = ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье")
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    stamp = f"{weekdays[day.weekday()]}, {day.day} {months[day.month - 1]} {day.year}"
    cutoff_label = f"{cutoff.strftime('%H:%M')} МСК" if cutoff is not None else "время не указано"
    kind = str(creation_kind or "")
    if kind == "automatic_planning":
        return f"Состояние команды на {stamp} — срез на {cutoff_label}"
    if kind == "automatic_day_end":
        return f"Итог команды за {stamp} — срез на {cutoff_label}"
    if kind == "manual":
        return f"Ручной слепок за {stamp} — на {cutoff_label}"
    return f"Ежедневный контроль за {stamp} — срез на {cutoff_label}"


def project_report_day_scope(
    deal: dict[str, Any], cutoff: datetime, *,
    source_deal: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    recorded_through: datetime | None = None,
) -> dict[str, Any]:
    """Freeze day membership; legacy reads use only the saved deal, never live CRM."""
    current = _aware(cutoff).astimezone(MSK_TZ)
    known_until = _aware(recorded_through).astimezone(MSK_TZ) if recorded_through is not None else current
    activity: set[str] = set()
    bucket = str(deal.get("bitrix_task_time_bucket") or "")
    buckets = {bucket} if bucket in {"overdue", "today", "tomorrow", "future", "unscheduled"} else set()
    if source_deal is not None and isinstance(source_deal.get("bitrix_tasks"), list):
        buckets = set()
        for task in source_deal["bitrix_tasks"]:
            state = str(task.get("completion_state") or "open")
            task_bucket = str(task.get("time_bucket") or "unscheduled")
            if state == "open" and not task.get("completed"):
                buckets.add(task_bucket)
            completed_today = bool(
                (task.get("completed") and _at_or_before(task.get("bitrix_completed_at"), current))
                or (task.get("local_completed") and _at_or_before(task.get("local_completed_at"), current))
            )
            if completed_today:
                activity.add("bitrix_task_completed")
                # Closed tasks may look missing on the live card; keep the day's obligation.
                if task_bucket in DAY_OBLIGATION_BUCKETS:
                    buckets.add(task_bucket)

    communications = deal.get("communications_today") or {}
    if communications.get("date") == current.date().isoformat() and communications.get("available"):
        items = communications.get("items") or []
        if items:
            for item in items:
                if not _at_or_before(item.get("occurred_at"), current):
                    continue
                channel = str(item.get("channel") or "")
                if channel == "call":
                    activity.add("call")
                elif channel in {"email", "message", "whatsapp", "telegram", "max", "sms"}:
                    activity.add("message")
        else:
            # Legacy snapshots can contain daily counters without the event list.
            if int(communications.get("calls_total") or communications.get("calls") or 0) > 0:
                activity.add("call")
            if int(communications.get("messages") or 0) > 0:
                activity.add("message")
    for event in events or []:
        if _at_or_before(event.get("occurred_at"), current) and (
            not event.get("recorded_at") or _at_or_before(event.get("recorded_at"), known_until, same_day=False)
        ):
            kind = _day_activity_kind(event)
            if kind:
                activity.add(kind)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if kind == "bitrix_task_rescheduled":
                before = stamp(payload.get("from_value"))
                if before is not None:
                    if before.date() < current.date():
                        buckets.add("overdue")
                    elif before.date() == current.date():
                        buckets.add("today")
    return {
        "business_date": current.date().isoformat(),
        "cutoff_at": _iso(current),
        "task_buckets": sorted(buckets),
        "activity_kinds": sorted(activity),
        "had_day_obligation": bool(buckets & DAY_OBLIGATION_BUCKETS),
        "untouched": False,
        "legacy": source_deal is None,
    }


def _has_client_contact(deal: dict[str, Any]) -> bool:
    scope = deal.get("day_scope")
    if isinstance(scope, dict):
        return bool(CLIENT_CONTACT_KINDS.intersection(scope.get("activity_kinds") or []))
    communications = deal.get("communications_today") or {}
    return int(communications.get("completed") or 0) > 0


def _has_report_day_work(deal: dict[str, Any]) -> bool:
    return _has_client_contact(deal)


def _has_day_obligation(deal: dict[str, Any]) -> bool:
    scope = deal.get("day_scope") if isinstance(deal.get("day_scope"), dict) else {}
    if set(scope.get("task_buckets") or []) & DAY_OBLIGATION_BUCKETS:
        return True
    return any(
        task.get("was_due") or task.get("overdue")
        for task in deal.get("task_results") or []
        if isinstance(task, dict)
    )


def _mark_day_membership(deal: dict[str, Any], *, carried: bool = False) -> None:
    scope = deal.get("day_scope")
    if not isinstance(scope, dict):
        return
    buckets = set(scope.get("task_buckets") or [])
    obligation = _has_day_obligation(deal) or carried
    if carried and not (buckets & DAY_OBLIGATION_BUCKETS):
        buckets.add("today")
        scope["task_buckets"] = sorted(buckets)
    unavailable = bool((deal.get("communications_today") or {}).get("unavailable"))
    scope["had_day_obligation"] = obligation
    scope["untouched"] = obligation and not _has_client_contact(deal) and not unavailable


def _belongs_to_daily_snapshot(deal: dict[str, Any]) -> bool:
    scope = deal.get("day_scope") if isinstance(deal.get("day_scope"), dict) else {}
    return bool(scope.get("had_day_obligation")) or _has_client_contact(deal)


def _planning_obligation_ids(db_path: str | Path, business_date: str) -> set[str]:
    """Deals that entered today's 15:45 snapshot because of a same-day Bitrix obligation."""
    ids: set[str] = set()
    for item in list_daily_control_reports(db_path):
        if item.get("creation_kind") != "automatic_planning" or item.get("business_date") != business_date:
            continue
        full = get_daily_control_report(db_path, int(item["id"]), include_snapshot=True)
        for deal in ((full or {}).get("snapshot") or {}).get("deals") or []:
            if not isinstance(deal, dict):
                continue
            scope = deal.get("day_scope") if isinstance(deal.get("day_scope"), dict) else {}
            if scope.get("had_day_obligation") or set(scope.get("task_buckets") or []) & DAY_OBLIGATION_BUCKETS:
                deal_id = str(deal.get("deal_id") or "")
                if deal_id:
                    ids.add(deal_id)
        break
    return ids


def generation_status() -> dict[str, Any] | None:
    with _generation_lock:
        return dict(_generation_state) if _generation_state else None


def _set_generation_state(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    global _generation_state
    with _generation_lock:
        _generation_state = dict(payload) if payload else None
        return dict(_generation_state) if _generation_state else None


def compute_source_watermark(
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> str:
    """Fingerprint persisted sources that affect a daily-control snapshot.

    Read-only: does not call Bitrix/LLM.
    """
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    business_date = current.date().isoformat()
    parts: list[str] = [business_date]
    active_ids: set[str] = set()
    for deal in list_deal_control_deals(db_path, active_only=True):
        deal_id = str(deal.get("deal_id") or "")
        active_ids.add(deal_id)
        communications = deal.get("communications_today")
        if not isinstance(communications, dict):
            communications = {}
        report = get_latest_ui_report(db_path, entity_type="deal", entity_id=deal_id)
        parts.append(
            dumps_json(
                {
                    "deal_id": deal_id,
                    "modified_at_crm": deal.get("modified_at_crm"),
                    "updated_at": deal.get("updated_at"),
                    "bitrix_tasks": deal.get("bitrix_tasks"),
                    "stage_id": deal.get("stage_id"),
                    "amount": deal.get("amount"),
                    "communications": {
                        "date": communications.get("date"),
                        "available": communications.get("available"),
                        "calls": communications.get("calls"),
                        "messages": communications.get("messages"),
                        "duration_seconds": communications.get("duration_seconds"),
                        "completed": communications.get("completed"),
                        "calls_total": communications.get("calls_total"),
                        "calls_connected": communications.get("calls_connected"),
                        "calls_no_answer": communications.get("calls_no_answer"),
                        "calls_unknown": communications.get("calls_unknown"),
                        "emails": communications.get("emails"),
                        "messenger_messages": communications.get("messenger_messages"),
                        "conversation_duration_seconds": communications.get("conversation_duration_seconds"),
                        "last_activity": (communications.get("last_activity") or {}).get("event_id")
                        if isinstance(communications.get("last_activity"), dict)
                        else communications.get("last_activity"),
                    },
                    "report_id": report.get("id") if report else None,
                    "report_created_at": report.get("created_at") if report else None,
                }
            )
        )
    for event in _daily_trajectory_events(db_path, current):
        if str(event.get("entity_id")) in active_ids and _day_activity_kind(event):
            parts.append(f"day_activity:{event['id']}")
    latest_run = get_latest_automatic_analysis_run(db_path)
    if latest_run:
        parts.append(
            dumps_json(
                {
                    "automatic_run_id": latest_run.get("id"),
                    "status": latest_run.get("status"),
                    "updated_at": latest_run.get("updated_at"),
                    "finished_at": latest_run.get("finished_at"),
                }
            )
        )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest


def classify_deal_status(
    deal: dict[str, Any], *, now: datetime | None = None, quality: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Server-side red/yellow/green from existing structured deal-control facts."""
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    audit = quality if quality is not None else _daily_quality_block(deal, now or datetime.now(MSK_TZ))
    bitrix_task = deal.get("primary_bitrix_task") if isinstance(deal.get("primary_bitrix_task"), dict) else {}
    scores = _audit_scores(audit)
    zero_count = sum(1 for score in scores.values() if score == 0)
    next_action_zero = scores.get("next_action") == 0
    assessed = audit.get("status") in {"assessed", "no_work"}
    insufficient = audit.get("status") in {"insufficient_evidence", "missing"}
    pending_without_baseline = (
        audit.get("status") == "pending_analysis"
        and all(score is None for score in scores.values())
    )
    next_action_warning = audit.get("next_action_warning")
    overdue_bitrix = (
        str(bitrix_task.get("time_bucket") or "") == "overdue"
        and str(bitrix_task.get("completion_state") or "open") == "open"
    )
    no_analysis = not coaching.get("report_id")

    # Локальное поручение РОПа не красит светофор: в UI рабочий объект — открытая
    # задача Bitrix. Скрытый AI-срок не должен зажигать красный.
    if (assessed and zero_count >= 2) or (overdue_bitrix and next_action_zero):
        return "red", STATUS_LABELS["red"]
    if (
        no_analysis
        or insufficient
        or pending_without_baseline
        or next_action_zero
        or zero_count >= 1
        or overdue_bitrix
        or bool(next_action_warning)
    ):
        return "yellow", STATUS_LABELS["yellow"]
    return "green", STATUS_LABELS["green"]


_OPEN_BITRIX_TIME_BUCKETS = {"overdue", "today", "tomorrow", "future", "unscheduled"}


def _bitrix_task_time_bucket(deal: dict[str, Any]) -> str:
    """Frozen open-Bitrix-task deadline bucket for daily-control filtering.

    Closed or missing Bitrix tasks are `missing`; that alone does not establish
    a task due on the report day. Local Neuro ROP due dates are ignored.
    """
    bitrix_task = deal.get("primary_bitrix_task") if isinstance(deal.get("primary_bitrix_task"), dict) else {}
    if not bitrix_task:
        return "missing"
    if str(bitrix_task.get("completion_state") or "open") != "open":
        return "missing"
    bucket = str(bitrix_task.get("time_bucket") or "")
    if bucket in _OPEN_BITRIX_TIME_BUCKETS:
        return bucket
    return "unscheduled"


def _audit_scores(audit: dict[str, Any]) -> dict[str, int | None]:
    criteria = audit.get("criteria") if isinstance(audit.get("criteria"), dict) else {}
    scores: dict[str, int | None] = {}
    for name in ("next_action", "value_development", "data_collection"):
        item = criteria.get(name) if isinstance(criteria.get(name), dict) else {}
        score = item.get("score")
        scores[name] = int(score) if isinstance(score, int) and not isinstance(score, bool) else None
    return scores


def _sanitize_communication_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in COMMUNICATION_REF_KEYS}


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_communications(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    items = []
    for raw in source.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = {key: raw.get(key) for key in COMMUNICATION_ITEM_KEYS}
        for forbidden in INVENTED_CONTENT_KEYS:
            item.pop(forbidden, None)
        if "content_available" not in raw:
            item["content_available"] = False
        else:
            item["content_available"] = bool(raw.get("content_available"))
        items.append(item)
    available = bool(source.get("available"))
    def _count(key: str, fallback: Any = 0) -> int:
        if not available:
            return 0
        if source.get(key) is None:
            return int(fallback or 0)
        return int(source.get(key) or 0)

    payload = {
        "date": source.get("date"),
        "available": available,
        "target": source.get("target"),
        "completed": int(source.get("completed") or 0) if available else 0,
        "progress_percent": source.get("progress_percent") if available else None,
        "calls": int(source.get("calls") or 0) if available else 0,
        "messages": int(source.get("messages") or 0) if available else 0,
        "duration_seconds": int(source.get("duration_seconds") or 0) if available else 0,
        "conversation_duration_seconds": _optional_int(source.get("conversation_duration_seconds")) if available else None,
        "last_activity": _sanitize_communication_ref(source.get("last_activity")) if available else None,
        "last_confirmed_contact": _sanitize_communication_ref(source.get("last_confirmed_contact")) if available else None,
        "calls_total": _count("calls_total", source.get("calls")),
        "calls_connected": _count("calls_connected"),
        "calls_no_answer": _count("calls_no_answer"),
        "calls_unknown": _count("calls_unknown"),
        "emails": _count("emails"),
        "messenger_messages": _count("messenger_messages"),
        "email_suffix": source.get("email_suffix") if available else None,
        "message_suffix": source.get("message_suffix") if available else None,
        "unavailable": not available,
        "items": items if available else [],
        "content_available": False,
    }
    return payload


def _has_current_quality_obligation(deal: dict[str, Any], current: datetime) -> bool:
    # Current Bitrix deadlines win over a historical "was_due" / planning membership.
    # Local Neuro ROP recommendations are advisory and must not create a daily
    # quality obligation or turn the traffic light red.
    tasks = list(deal.get("bitrix_tasks") or [])
    if not tasks and isinstance(deal.get("primary_bitrix_task"), dict):
        tasks.append(deal["primary_bitrix_task"])
    for task in tasks:
        if task.get("local_status") in {"cancelled", "canceled"}:
            continue
        deadline = quality_time(task.get("deadline") or task.get("due_at"))
        due = deadline.date() <= current.date() if deadline else task.get("time_bucket") in DAY_OBLIGATION_BUCKETS
        if not due:
            continue
        completed = bool(task.get("completed") or task.get("local_completed") or task.get("completion_state") in {"bitrix", "local"}
                         or task.get("local_status") == "completed")
        completed_at = quality_time(task.get("bitrix_completed_at") or task.get("local_completed_at") or task.get("completed_at"))
        if not completed or (completed_at and completed_at.date() == current.date()):
            return True
    return False


def _daily_quality_block(deal: dict[str, Any], now: datetime) -> dict[str, Any]:
    current = _aware(now).astimezone(MSK_TZ)
    day = current.date().isoformat()
    coaching = deal.get("coaching") or {}
    communications = deal.get("communications_today") or {}

    def state(status: str, reason: str, *, zero: bool = False) -> dict[str, Any]:
        return {
            "status": status, "business_date": day, "cutoff_at": _iso(current),
            "source": "system", "criteria": {
                name: {"score": 0 if zero else None, "verdict": "Сегодня не подтверждено" if zero else reason}
                for name in ("next_action", "value_development", "data_collection")
            }, "confirmed_count": 0 if zero else None, "total": 3,
            "scope_summary": reason, "zero_reasons": [], "summary_for_rop": None,
            "insufficient_reason": reason, "evaluated_through": None,
            "pending_message": reason if status == "pending_analysis" else None,
            "pending_events_count": 0, "next_action_warning": None,
        }

    if communications.get("date") != day or not communications.get("available") or communications.get("quality_sources_available") is False:
        return state("missing", "Нет данных: сегодняшние клиентские коммуникации получены не полностью.")
    items = [item for item in communications.get("items") or [] if isinstance(item, dict)
             and item.get("channel") in CLIENT_CHANNELS and _at_or_before(item.get("occurred_at"), current)]
    if not communications.get("items") and int(communications.get("completed") or 0) > 0:
        return state("missing", "Нет данных: есть счётчики коммуникаций, но нет событий для проверки качества.")
    candidates = [
        item for item in items
        if item.get("quality_evidence", is_daily_quality_evidence(item))
    ]
    if not candidates and any(item.get("channel") == "call" and item.get("call_outcome") not in {"connected", "no_answer"} for item in items):
        return state("missing", "Нет данных: результат сегодняшнего звонка ещё не подтверждён.")
    obligation = _has_current_quality_obligation(deal, current)
    current_audit = coaching.get("communication_quality_audit") or {}
    current_scope = current_audit.get("daily_scope") or {}
    saved_state = deal.get("daily_quality_state") if isinstance(deal.get("daily_quality_state"), dict) else {}
    saved_audit = saved_state.get("audit") if isinstance(saved_state.get("audit"), dict) else {}
    saved_scope = saved_state.get("daily_scope") if isinstance(saved_state.get("daily_scope"), dict) else {}

    def valid_scope(scope: dict[str, Any]) -> bool:
        return (
            scope.get("version") in {1, 2}
            and scope.get("business_date") == day
            and quality_time(scope.get("evaluated_through")) is not None
        )

    # Before the first post-migration write, a strict same-day audit is a read-only fallback.
    if not saved_audit and valid_scope(current_scope):
        signatures = current_scope.get("event_signatures") or {}
        candidate_map = {str(item.get("event_id")): item for item in candidates}
        strict_scope = (
            current_scope.get("version") == 2
            and bool(signatures)
            and set(signatures).issubset(candidate_map)
        )
        compatible_legacy_scope = signatures and all(
            event_id in candidate_map
            and signature == (
                candidate_map[event_id].get("quality_source_signature")
                or quality_event_signature(candidate_map[event_id])
            )
            for event_id, signature in signatures.items()
        )
        if strict_scope or compatible_legacy_scope:
            saved_audit, saved_scope = current_audit, current_scope

    if not saved_audit:
        if not candidates:
            if obligation:
                return state("no_work", "Сегодня по актуальной задаче ещё нет содержательной клиентской коммуникации. Попытки дозвона не дают единицы.", zero=True)
            return state("not_required", "На сегодня нет актуальной задачи или контрольной точки; содержательной коммуникации пока нет.")
        return state("pending_analysis", "Сегодня есть подтверждённая клиентская коммуникация. Ожидает AI-оценки.")

    scope = saved_scope or saved_audit.get("daily_scope") or {}
    through = quality_time(scope.get("evaluated_through"))
    signatures = scope.get("event_signatures") or {}
    candidate_signatures = {
        str(item.get("event_id")): (
            item.get("quality_source_signature") or quality_event_signature(item)
        )
        for item in candidates
    }
    covered = (
        scope.get("version") in {1, 2} and scope.get("business_date") == day
        and through is not None and through.date() == current.date() and through <= current
        and all(signatures.get(event_id) == signature
                for event_id, signature in candidate_signatures.items())
        and all(quality_time(item.get("occurred_at")) <= through for item in candidates)
    )
    if not covered:
        result = _quality_block({"communication_quality_audit": saved_audit})
        result.update(
            status="pending_analysis",
            business_date=day,
            cutoff_at=_iso(current),
            source="ai",
            evaluated_through=scope.get("evaluated_through"),
            pending_message=(
                f"Оценено до {through.strftime('%H:%M') if through else '—'}, "
                "новая коммуникация ожидает анализа."
            ),
            pending_events_count=sum(
                1 for event_id, signature in candidate_signatures.items()
                if signatures.get(event_id) != signature
            ),
        )
        return result
    if saved_audit.get("status") == "insufficient_evidence":
        if obligation:
            return state("no_work", "AI не подтвердил содержательную работу в сегодняшних коммуникациях.", zero=True)
        return state("not_required", "На сегодня нет актуальной задачи; AI не подтвердил содержательную коммуникацию.")
    if saved_audit.get("status") != "assessed" or any(value not in {0, 1} for value in _audit_scores(saved_audit).values()):
        return state("pending_analysis", "Ожидает корректной AI-оценки сегодняшней работы.")
    result = _quality_block({"communication_quality_audit": saved_audit})
    result.update(
        business_date=day,
        cutoff_at=_iso(current),
        source="ai",
        evaluated_through=scope.get("evaluated_through"),
        pending_message=None,
        pending_events_count=0,
    )
    return result


def _quality_block(coaching: dict[str, Any]) -> dict[str, Any]:
    audit = coaching.get("communication_quality_audit")
    if not isinstance(audit, dict) or not audit:
        return {
            "status": "missing",
            "criteria": {
                "next_action": {"score": None, "verdict": "Нет данных"},
                "value_development": {"score": None, "verdict": "Нет данных"},
                "data_collection": {"score": None, "verdict": "Нет данных"},
            },
            "confirmed_count": None,
            "total": 3,
            "scope_summary": None,
            "zero_reasons": [],
            "summary_for_rop": None,
            "insufficient_reason": "Нет данных",
            "evaluated_through": None,
            "pending_message": None,
            "pending_events_count": 0,
            "next_action_warning": None,
        }
    if audit.get("status") == "insufficient_evidence":
        return {
            "status": "insufficient_evidence",
            "criteria": {
                "next_action": {"score": None, "verdict": "Нет данных для оценки"},
                "value_development": {"score": None, "verdict": "Нет данных для оценки"},
                "data_collection": {"score": None, "verdict": "Нет данных для оценки"},
            },
            "confirmed_count": None,
            "total": 3,
            "scope_summary": audit.get("scope_summary"),
            "zero_reasons": [],
            "summary_for_rop": None,
            "insufficient_reason": audit.get("insufficient_reason") or "Нет данных",
            "evaluated_through": (audit.get("daily_scope") or {}).get("evaluated_through"),
            "pending_message": None,
            "pending_events_count": 0,
            "next_action_warning": audit.get("next_action_warning"),
        }
    scores = _audit_scores(audit)
    criteria = {}
    confirmed = 0
    for name in ("next_action", "value_development", "data_collection"):
        score = scores.get(name)
        if score == 1:
            confirmed += 1
            verdict = "Выполнено"
        elif score == 0:
            verdict = "Не выполнено"
        else:
            verdict = "Нет данных"
        criteria[name] = {"score": score, "verdict": verdict}
    reasons = []
    for raw in audit.get("zero_reasons") or []:
        if not isinstance(raw, dict):
            continue
        reasons.append(
            {
                "criterion": raw.get("criterion"),
                "explanation": raw.get("explanation"),
                "quote": raw.get("quote"),
            }
        )
    return {
        "status": "assessed",
        "criteria": criteria,
        "confirmed_count": confirmed,
        "total": 3,
        "scope_summary": audit.get("scope_summary"),
        "zero_reasons": reasons,
        "summary_for_rop": audit.get("summary_for_rop"),
        "insufficient_reason": None,
        "evaluated_through": (audit.get("daily_scope") or {}).get("evaluated_through"),
        "pending_message": None,
        "pending_events_count": 0,
        "next_action_warning": audit.get("next_action_warning"),
    }


def _action_to_you_question(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" \t\r\n.;:")
    if not cleaned:
        return ""
    if cleaned.endswith("?"):
        if cleaned.lower().startswith("ты "):
            return cleaned
        return f"Ты {cleaned[0].lower() + cleaned[1:]}" if cleaned[0].isupper() else f"Ты {cleaned}"
    lowered = cleaned.lower()
    replacements = (
        (r"^подтвердить\s+", "Ты подтвердил "),
        (r"^уточнить,\s+", "Ты уточнил, "),
        (r"^уточнить\s+", "Ты уточнил "),
        (r"^получить\s+", "Ты получил "),
        (r"^зафиксировать в crm\s+", "Ты зафиксировал в CRM "),
        (r"^согласовать\s+", "Ты согласовал "),
        (r"^назначить\s+", "Ты назначил "),
        (r"^отправить\s+", "Ты отправил "),
        (r"^проверить\s+", "Ты проверил "),
        (r"^определить\s+", "Ты определил "),
    )
    for pattern, prefix in replacements:
        match = re.match(pattern, lowered)
        if match:
            rest = cleaned[match.end():]
            return f"{prefix}{rest}?"
    return f"Ты сделал это: {cleaned}?"


def build_direct_manager_question(deal: dict[str, Any]) -> str:
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    stored = str(coaching.get("direct_manager_question") or "").strip()
    if stored:
        return stored
    fragments: list[str] = []
    for unknown in coaching.get("unknowns") or []:
        question = _action_to_you_question(str(unknown))
        if question and question not in fragments:
            fragments.append(question)
        if len(fragments) >= 3:
            break
    if not fragments:
        focus = str(coaching.get("what_to_check_now") or "").strip()
        if focus:
            fragments.append(_action_to_you_question(focus))
    if not fragments:
        return "Какой следующий согласованный шаг по сделке и когда клиент даст ответ?"
    return " ".join(fragments[:3])


def _attention_reason(deal: dict[str, Any], quality: dict[str, Any]) -> str:
    if quality.get("status") in {"no_work", "missing", "pending_analysis", "not_required"}:
        return str(quality.get("scope_summary") or "Нет данных")
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    for value in (
        coaching.get("main_risk_description"),
        coaching.get("current_situation"),
        quality.get("summary_for_rop"),
        coaching.get("what_to_check_now"),
        coaching.get("rop_focus"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    if quality.get("status") == "insufficient_evidence":
        return str(quality.get("insufficient_reason") or "Нет данных")
    return "Нет данных"


def project_deal_review_card(deal: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Project a live deal-control deal into the shared ROP review card.

    Used by the immutable daily snapshot and the live ROP card. Does not call
    LLM or Bitrix.
    """
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    quality = _daily_quality_block(deal, now or datetime.now(MSK_TZ))
    status, status_label = classify_deal_status(deal, quality=quality)
    communications = _sanitize_communications(deal.get("communications_today"))
    script = str(coaching.get("manager_coaching") or "").strip()
    variants = [
        str(item).strip()
        for item in (coaching.get("script_variants") or [])
        if str(item).strip()
    ]
    return {
        "deal_id": str(deal.get("deal_id") or ""),
        "title": deal.get("title"),
        "manager_id": deal.get("manager_id"),
        "manager_name": deal.get("manager_name"),
        "stage_id": deal.get("stage_id"),
        "stage_name": deal.get("stage_name"),
        "pipeline_id": deal.get("pipeline_id"),
        "pipeline_name": deal.get("pipeline_name"),
        "amount": deal.get("amount"),
        "currency_id": deal.get("currency_id") or "RUB",
        "status": status,
        "status_label": status_label,
        "attention_reason": _attention_reason(deal, quality),
        "quality": quality,
        "summary_for_rop": quality.get("summary_for_rop"),
        "direct_question": build_direct_manager_question(deal),
        "generic_question": GENERIC_STATUS_QUESTION,
        "ai_context": {
            "current_situation": coaching.get("current_situation") or "",
            "rop_focus": coaching.get("rop_focus") or "",
            "what_to_check_now": coaching.get("what_to_check_now") or "",
            "manager_coaching": coaching.get("manager_coaching") or "",
            "known": list(coaching.get("known") or []),
            "unknowns": list(coaching.get("unknowns") or []),
            "strengths": list(coaching.get("strengths") or []),
            "weaknesses": list(coaching.get("weaknesses") or []),
        },
        "script": script,
        "script_variants": variants,
        "communications_today": communications,
        "has_analysis": bool(coaching.get("report_id")),
        "analysis_created_at": coaching.get("analysis_created_at"),
        "analysis_checked_at": coaching.get("analysis_checked_at"),
        "analysis_check_status": coaching.get("analysis_check_status"),
        "bitrix_task_time_bucket": _bitrix_task_time_bucket(deal),
    }


def _sort_deals(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"red": 0, "yellow": 1, "green": 2}

    def key(deal: dict[str, Any]) -> tuple[Any, ...]:
        untouched = 0 if (deal.get("day_scope") or {}).get("untouched") else 1
        return (
            untouched,
            rank.get(str(deal.get("status")), 9),
            str(deal.get("title") or ""),
            str(deal.get("deal_id") or ""),
        )

    return sorted(deals, key=key)


def _sort_managers(managers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        managers,
        key=lambda item: (
            -int(item.get("red") or 0),
            -int(item.get("yellow") or 0),
            str(item.get("manager_name") or ""),
            str(item.get("manager_id") or ""),
        ),
    )


def build_daily_control_snapshot(
    dashboard: dict[str, Any],
    *,
    warnings: list[str] | None = None,
    cutoff_at: datetime | None = None,
    activity_events: list[dict[str, Any]] | None = None,
    carried_obligation_ids: set[str] | None = None,
    recorded_through: datetime | None = None,
) -> dict[str, Any]:
    quality_now = cutoff_at or quality_time(dashboard.get("generated_at")) or datetime.now(MSK_TZ)
    deals = [project_deal_review_card(deal, now=quality_now) for deal in dashboard.get("deals") or [] if isinstance(deal, dict)]
    if cutoff_at is not None:
        sources = {str(deal.get("deal_id")): deal for deal in dashboard.get("deals") or [] if isinstance(deal, dict)}
        events_by_deal: dict[str, list[dict[str, Any]]] = {}
        for event in activity_events or []:
            if event.get("entity_type") == "deal":
                events_by_deal.setdefault(str(event.get("entity_id")), []).append(event)
        carried = {str(item) for item in (carried_obligation_ids or set())}
        selected: list[dict[str, Any]] = []
        for deal in deals:
            deal["task_results"] = task_results(
                sources[deal["deal_id"]].get("bitrix_tasks") or [],
                events_by_deal.get(deal["deal_id"], []), cutoff_at,
                recorded_through=recorded_through,
            )
            deal["day_scope"] = project_report_day_scope(
                deal, cutoff_at, source_deal=sources[deal["deal_id"]],
                events=events_by_deal.get(deal["deal_id"]),
                recorded_through=recorded_through,
            )
            _mark_day_membership(deal, carried=deal["deal_id"] in carried)
            if _belongs_to_daily_snapshot(deal):
                selected.append(deal)
        deals = selected
    deals = _sort_deals(deals)
    managers_by_id: dict[str, dict[str, Any]] = {}
    unavailable_comms = 0
    no_movement = 0
    movement_scope = 0
    for deal in deals:
        manager_id = str(deal.get("manager_id") or "") or "unassigned"
        bucket = managers_by_id.setdefault(
            manager_id,
            {
                "manager_id": deal.get("manager_id"),
                "manager_name": deal.get("manager_name") or "Без ответственного",
                "deals_count": 0,
                "calls": 0,
                "messages": 0,
                "talk_seconds": 0,
                "red": 0,
                "yellow": 0,
                "green": 0,
            },
        )
        bucket["deals_count"] += 1
        communications = deal.get("communications_today") or {}
        has_work = _has_report_day_work(deal)
        if has_work or not communications.get("unavailable"):
            movement_scope += 1
            if not has_work:
                no_movement += 1
        if communications.get("unavailable"):
            unavailable_comms += 1
        else:
            bucket["calls"] += int(communications.get("calls") or 0)
            bucket["messages"] += int(communications.get("messages") or 0)
            bucket["talk_seconds"] += int(communications.get("duration_seconds") or 0)
        bucket[str(deal.get("status") or "yellow")] = int(bucket.get(str(deal.get("status") or "yellow")) or 0) + 1
    managers = _sort_managers(list(managers_by_id.values()))
    for manager in managers:
        manager.update(task_totals([deal for deal in deals if deal.get("manager_id") == manager.get("manager_id")]))
    team_calls = sum(item["calls"] for item in managers)
    team_messages = sum(item["messages"] for item in managers)
    team_talk = sum(item["talk_seconds"] for item in managers)
    notes = list(warnings or [])
    if unavailable_comms:
        notes.append(
            f"Коммуникации за сегодня недоступны для {unavailable_comms} "
            f"{'сделки' if unavailable_comms == 1 else 'сделок'} — это не нулевая активность."
        )
    sync_errors = [str(item) for item in (dashboard.get("sync_errors") or []) if item]
    notes.extend(sync_errors)
    return {
        "team": {
            **task_totals(deals),
            "traffic_light": {
                "red": sum(1 for deal in deals if deal["status"] == "red"),
                "yellow": sum(1 for deal in deals if deal["status"] == "yellow"),
                "green": sum(1 for deal in deals if deal["status"] == "green"),
            },
            "deals_total": len(deals),
            "no_movement": {"count": no_movement, "total": movement_scope},
            "calls": team_calls,
            "messages": team_messages,
            "talk_seconds": team_talk,
        },
        "managers": managers,
        "deals": deals,
        "source_warnings": notes,
        "communications_unavailable_count": unavailable_comms,
    }


def _source_status(snapshot: dict[str, Any], extra_warnings: list[str]) -> str:
    if extra_warnings or snapshot.get("source_warnings") or snapshot.get("communications_unavailable_count"):
        return "partial"
    return "ok"


def publish_daily_control_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    creation_kind: str,
    started_at: datetime | str,
    cutoff_at: datetime | str | None = None,
    now: datetime | None = None,
    refresh: bool = False,
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
    dashboard: dict[str, Any] | None = None,
    automatic_analysis_run_id: int | None = None,
    recorded_through: datetime | None = None,
) -> dict[str, Any]:
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    started = started_at if isinstance(started_at, datetime) else datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    started = _aware(started).astimezone(MSK_TZ)
    warnings: list[str] = []
    latest_run = get_latest_automatic_analysis_run(db_path)
    preparation = {
        key: latest_run.get(key) if latest_run else None
        for key in ("business_date", "status", "started_at", "finished_at")
    }
    if latest_run is None or latest_run.get("business_date") != current.date().isoformat():
        warnings.append("Завершение автоматического пакета за сегодня не подтверждено. Использованы последние сохранённые данные.")
    elif latest_run.get("status") != "done":
        warnings.append(
            "На момент создания отчёта автоматический пакет ещё выполнялся. После его завершения можно сформировать новый отчёт."
            if latest_run.get("status") == "running"
            else "На момент создания отчёта автоматический пакет не завершился успешно. Часть данных может быть неактуальна."
        )
    from api.deal_control import build_deal_control_dashboard, refresh_deal_control

    payload = dashboard
    if payload is None and refresh:
        try:
            refresher = refresh_fn or refresh_deal_control
            payload = refresher(db_path=db_path, now=current)
            for item in payload.get("sync_errors") or []:
                if item:
                    warnings.append(str(item))
        except Exception as error:  # noqa: BLE001 - fail-soft snapshot from persisted state
            warnings.append(f"Bitrix sync: {error}")
            logger.warning("Ежедневный контроль: Bitrix sync не удался, публикуем persisted state.")
            payload = None
    if payload is None:
        payload = build_deal_control_dashboard(db_path=db_path, now=current)
    cutoff = cutoff_at or current
    if isinstance(cutoff, str):
        cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    else:
        cutoff_dt = cutoff
    cutoff_dt = _aware(cutoff_dt).astimezone(MSK_TZ)
    if recorded_through is not None:
        recorded_through = _aware(recorded_through).astimezone(MSK_TZ)
    elif refresh:
        # CRM collect for this snapshot may finish a minute after the frozen cutoff.
        recorded_through = datetime.now(MSK_TZ)
    carried_ids = None
    if creation_kind == "automatic_day_end":
        carried_ids = _planning_obligation_ids(db_path, _business_date(cutoff_dt))
    snapshot = build_daily_control_snapshot(
        payload, warnings=warnings, cutoff_at=cutoff_dt,
        activity_events=_daily_trajectory_events(db_path, cutoff_dt, recorded_through=recorded_through),
        carried_obligation_ids=carried_ids,
        recorded_through=recorded_through,
    )
    snapshot["source_preparation"] = preparation
    watermark = compute_source_watermark(db_path, now=cutoff_dt)
    if automatic_analysis_run_id is None:
        if latest_run and str(latest_run.get("status") or "") in {"done", "running", "error", "interrupted"}:
            automatic_analysis_run_id = int(latest_run["id"])
    report = create_daily_control_report(
        db_path,
        business_date=_business_date(cutoff_dt),
        creation_kind=creation_kind,
        started_at=_iso(started),
        cutoff_at=_iso(cutoff_dt),
        snapshot=snapshot,
        source_watermark=watermark,
        automatic_analysis_run_id=automatic_analysis_run_id,
        source_status=_source_status(snapshot, warnings),
        warnings=snapshot.get("source_warnings") or warnings,
    )
    return report


def publish_planning_daily_control_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Publish today's planning snapshot after a read-only CRM sync, without waiting for LLM.

    Does not start a new analysis job. Uses the last automatic-batch status as a visible warning.
    """
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    existing = [
        item
        for item in list_daily_control_reports(db_path)
        if item.get("creation_kind") == "automatic_planning"
        and item.get("business_date") == current.date().isoformat()
    ]
    if existing:
        latest = get_daily_control_report(db_path, int(existing[0]["id"]), include_snapshot=True)
        if latest is not None:
            logger.info("Планировочный daily-control за %s уже опубликован (id=%s).", current.date().isoformat(), latest.get("id"))
            return latest
    logger.info("Публикация планировочного daily-control после CRM sync без AI на %s.", _iso(current))
    from api.deal_control import build_deal_control_dashboard

    try:
        dashboard = (refresh_fn or _refresh_final_sources)(db_path=db_path, now=current)
    except Exception as error:  # noqa: BLE001 - fail-soft snapshot from persisted state
        logger.warning("Ежедневный контроль 15:45: Bitrix sync не удался, публикуем persisted state.")
        dashboard = build_deal_control_dashboard(
            db_path=db_path, now=current, sync_errors=[f"Bitrix sync: {error}"],
        )
    return publish_daily_control_report(
        db_path=db_path,
        creation_kind="automatic_planning",
        started_at=current,
        cutoff_at=current,
        now=current,
        recorded_through=_aware((clock or (lambda: datetime.now(MSK_TZ)))()).astimezone(MSK_TZ),
        dashboard=dashboard,
    )


def _refresh_final_sources(*, db_path: str | Path, now: datetime) -> dict[str, Any]:
    """Read CRM and task history after the evening analysis, without launching AI."""
    from api.candidates import make_client
    from api.deal_control import build_deal_control_dashboard, refresh_deal_control
    from api.manager_trajectory import collect_manager_trajectory

    payload = refresh_deal_control(db_path=db_path, now=now)
    errors = list(payload.get("sync_errors") or [])
    try:
        facts = collect_manager_trajectory(make_client(), db_path=db_path, to_at=datetime.now(MSK_TZ))
        errors.extend(f"История CRM: {key}: {value}" for key, value in (facts.get("errors") or {}).items())
    except Exception as error:  # noqa: BLE001 - preserve a transparent partial report
        errors.append(f"История CRM недоступна: {error}")
    return build_deal_control_dashboard(db_path=db_path, now=datetime.now(MSK_TZ), sync_errors=errors)


def publish_day_end_daily_control_report(
    *, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None,
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    for item in list_daily_control_reports(db_path):
        if item.get("creation_kind") == "automatic_day_end" and item.get("business_date") == current.date().isoformat():
            return get_daily_control_report(db_path, int(item["id"]), include_snapshot=True) or {}
    from api.deal_control import build_deal_control_dashboard

    try:
        dashboard = (refresh_fn or _refresh_final_sources)(db_path=db_path, now=current)
    except Exception as error:  # noqa: BLE001 - publish saved state with an explicit warning
        dashboard = build_deal_control_dashboard(db_path=db_path, now=current, sync_errors=[f"Вечернее обновление CRM не завершено: {error}"])
    finished = _aware((clock or (lambda: datetime.now(MSK_TZ)))()).astimezone(MSK_TZ)
    if finished.date() != current.date():
        raise RuntimeError("Вечернее обновление пересекло полночь: нельзя выдать текущее состояние за вчерашний срез")
    latest_run = get_latest_automatic_analysis_run(db_path)
    run_started = stamp((latest_run or {}).get("started_at"))
    if not run_started or not current.replace(hour=22, minute=0, second=0, microsecond=0) <= run_started <= finished:
        dashboard.setdefault("sync_errors", []).append("Запуск дополнительного анализа в 22:00 МСК не подтверждён. Использован последний сохранённый анализ.")
    return publish_daily_control_report(
        db_path=db_path, creation_kind="automatic_day_end", started_at=current,
        cutoff_at=finished, now=finished, dashboard=dashboard,
    )


def _run_manual_generation(db_path: str | Path, started: datetime) -> None:
    try:
        report = publish_daily_control_report(
            db_path=db_path,
            creation_kind="manual",
            started_at=started,
            cutoff_at=datetime.now(MSK_TZ),
            now=datetime.now(MSK_TZ),
            refresh=True,
        )
        _set_generation_state(
            {
                "status": "done",
                "started_at": _iso(started),
                "finished_at": utcish_now(),
                "report_id": report.get("id"),
                "error": None,
            }
        )
    except Exception as error:  # noqa: BLE001 - surface generation failure to the UI
        logger.exception("Ручное формирование ежедневного контроля завершилось ошибкой.")
        _set_generation_state(
            {
                "status": "error",
                "started_at": _iso(started),
                "finished_at": utcish_now(),
                "report_id": None,
                "error": str(error),
            }
        )


def start_manual_daily_control_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    current = generation_status()
    if current and current.get("status") in {"queued", "running"}:
        return current
    started = datetime.now(MSK_TZ)
    state = _set_generation_state(
        {
            "status": "running",
            "started_at": _iso(started),
            "finished_at": None,
            "report_id": None,
            "error": None,
        }
    ) or {}
    thread = threading.Thread(
        target=_run_manual_generation,
        args=(db_path, started),
        name="neuro-rop-daily-control-manual",
        daemon=True,
    )
    thread.start()
    return state


def project_daily_control_snapshot(snapshot: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Apply the same deal-access contract as live deal-control. Stored JSON stays intact."""
    from api.access import deal_access, rop_team_manager_ids

    # Retired fields in historical snapshots stay on disk, not in the API contract.
    snapshot = {
        **snapshot,
        "deals": [
            {key: value for key, value in deal.items() if key != "checklist"}
            for deal in snapshot.get("deals") or [] if isinstance(deal, dict)
        ],
        "managers": [
            {key: value for key, value in manager.items() if key not in {"checklist_completed", "checklist_total"}}
            for manager in snapshot.get("managers") or [] if isinstance(manager, dict)
        ],
    }
    role = str(user.get("role") or "")
    if role == "admin":
        return snapshot
    if role != "rop":
        raise PermissionError("Ежедневный контроль доступен только admin и rop")
    allowed = rop_team_manager_ids()
    deals = []
    for deal in snapshot.get("deals") or []:
        if not isinstance(deal, dict):
            continue
        access = deal_access(user, deal)
        if str(deal.get("manager_id") or "") in allowed and access.can_open:
            deals.append(deal)
    managers = [
        item
        for item in snapshot.get("managers") or []
        if isinstance(item, dict) and str(item.get("manager_id") or "") in allowed
    ]
    projected = dict(snapshot)
    projected["deals"] = deals
    projected["managers"] = managers
    projected["team"] = {
        **task_totals(deals),
        "traffic_light": {
            "red": sum(1 for deal in deals if deal.get("status") == "red"),
            "yellow": sum(1 for deal in deals if deal.get("status") == "yellow"),
            "green": sum(1 for deal in deals if deal.get("status") == "green"),
        },
        "deals_total": len(deals),
        "no_movement": {
            "count": sum(
                1
                for deal in deals
                if not (deal.get("communications_today") or {}).get("unavailable")
                and not _has_report_day_work(deal)
            ),
            "total": sum(
                1 for deal in deals if _has_report_day_work(deal) or not (deal.get("communications_today") or {}).get("unavailable")
            ),
        },
        "calls": sum(int(item.get("calls") or 0) for item in managers),
        "messages": sum(int(item.get("messages") or 0) for item in managers),
        "talk_seconds": sum(int(item.get("talk_seconds") or 0) for item in managers),
    }
    return projected


def _visible_history_reports(db_path: str | Path) -> list[dict[str, Any]]:
    """User-facing history only; automation keeps using the complete storage list."""
    return [
        report
        for report in list_daily_control_reports(db_path)
        if str(report.get("business_date") or "") >= DAILY_CONTROL_VISIBLE_FROM
    ]


def history_payload(*, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    reports = _visible_history_reports(db_path)
    latest_id = int(reports[0]["id"]) if reports else None
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    default_id = latest_id
    expected_final_date = current.date() - timedelta(days=1)
    while expected_final_date.weekday() > 4:
        expected_final_date -= timedelta(days=1)
    morning = (current.hour, current.minute) < (15, 45)
    morning_final = next((item for item in reports if item.get("creation_kind") == "automatic_day_end"
                          and item.get("business_date") == expected_final_date.isoformat()), None)
    if morning and morning_final:
        default_id = int(morning_final["id"])
    items = []
    for index, report in enumerate(reports):
        items.append(
            {
                **{key: report.get(key) for key in (
                    "id",
                    "business_date",
                    "creation_kind",
                    "started_at",
                    "cutoff_at",
                    "created_at",
                    "source_watermark",
                    "automatic_analysis_run_id",
                    "source_status",
                    "warnings",
                    "error",
                )},
                "position": len(reports) - index,
                "total": len(reports),
                "heading": report_heading(
                    str(report.get("creation_kind") or ""),
                    str(report.get("business_date") or ""),
                    report.get("cutoff_at"),
                ),
            }
        )
    return {
        "reports": items,
        "latest_id": latest_id,
        "default_id": default_id,
        "missing_morning_final": morning and morning_final is None,
        "total": len(reports),
        "generation": generation_status(),
    }


def report_payload(
    report_id: int,
    user: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    metadata = get_daily_control_report(db_path, report_id, include_snapshot=False)
    if metadata is None or str(metadata.get("business_date") or "") < DAILY_CONTROL_VISIBLE_FROM:
        return None
    report = get_daily_control_report(db_path, report_id, include_snapshot=True)
    if report is None:
        return None
    snapshot = project_daily_control_snapshot(report.get("snapshot") or {}, user)
    cutoff = _aware(datetime.fromisoformat(str(report["cutoff_at"]).replace("Z", "+00:00"))).astimezone(MSK_TZ)
    deals = []
    for deal in snapshot.get("deals") or []:
        if not isinstance(deal, dict):
            continue
        row = {**deal, "day_scope": deal.get("day_scope") or project_report_day_scope(deal, cutoff)}
        _mark_day_membership(row, carried=bool((row.get("day_scope") or {}).get("had_day_obligation")))
        deals.append(row)
    snapshot = {
        **snapshot,
        "deals": deals,
    }
    history = _visible_history_reports(db_path)
    ids = [int(item["id"]) for item in history]
    index = ids.index(int(report["id"])) if int(report["id"]) in ids else None
    position_from_oldest = (len(ids) - index) if index is not None else None
    return {
        "id": report.get("id"),
        "business_date": report.get("business_date"),
        "creation_kind": report.get("creation_kind"),
        "heading": report_heading(
            str(report.get("creation_kind") or ""),
            str(report.get("business_date") or ""),
            report.get("cutoff_at"),
        ),
        "started_at": report.get("started_at"),
        "cutoff_at": report.get("cutoff_at"),
        "created_at": report.get("created_at"),
        "source_watermark": report.get("source_watermark"),
        "automatic_analysis_run_id": report.get("automatic_analysis_run_id"),
        "source_status": report.get("source_status"),
        "warnings": report.get("warnings") or [],
        "error": report.get("error"),
        "snapshot": snapshot,
        "position": position_from_oldest,
        "total": len(ids),
        "previous_id": ids[index + 1] if index is not None and index + 1 < len(ids) else None,
        "next_id": ids[index - 1] if index is not None and index > 0 else None,
        "generation": generation_status(),
    }
