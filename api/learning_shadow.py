"""Admin-only Learning Shadow orchestration over already stored local facts."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from setup import MSK_TZ
from api.deal_call_transcript import find_call_transcript
from openai_api.llm.learning_shadow import (
    LEARNING_SHADOW_MODEL,
    LEARNING_SHADOW_REASONING_EFFORT,
    analyze_learning_shadow_case,
)
from storage.rop_db import (
    DEFAULT_DB_PATH,
    create_learning_shadow_run,
    get_deal_control_task,
    get_deal_manager_quick_help,
    get_learning_shadow_run,
    list_learning_shadow_runs,
    list_manager_trajectory_events,
    save_learning_shadow_case,
    update_learning_shadow_case,
    update_learning_shadow_run,
    utcish_now,
)


_THREADS: dict[int, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()


def _period_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    if to_date < from_date:
        raise ValueError("to_date не может быть раньше from_date")
    if (to_date - from_date).days > 31:
        raise ValueError("V1 поддерживает диапазон не более 32 календарных дней")
    return (
        datetime.combine(from_date, time.min, tzinfo=MSK_TZ),
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=MSK_TZ),
    )


def _safe_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _recommendation_projection(db_path: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("recommendation_kind") or "")
    recommendation_id = str(event.get("recommendation_id") or "")
    base: dict[str, Any] = {
        "recommendation_id": f"{kind}:{recommendation_id}",
        "source_id": recommendation_id,
        "kind": kind,
        "viewed_at": event.get("occurred_at"),
    }
    if kind == "quick_help" and recommendation_id.isdigit():
        item = get_deal_manager_quick_help(
            db_path, deal_id=str(event.get("entity_id") or ""), quick_help_id=int(recommendation_id),
        )
        if item:
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            base.update({
                "mode": item.get("mode"),
                "question": _safe_text(item.get("question"), 2000),
                "situation_summary": _safe_text(content.get("situation_summary")),
                "next_action": _safe_text(content.get("next_action")),
                "expected_result": _safe_text(content.get("expected_result")),
                "pressure_lever": content.get("pressure_lever"),
                "strategy_labels": content.get("strategy_labels") or [],
                "client_messages": content.get("client_messages") or [],
                "lifehacks": content.get("lifehacks") or [],
                "fallback_action": _safe_text(content.get("fallback_action")),
            })
    elif kind == "deal_task" and recommendation_id.isdigit():
        item = get_deal_control_task(db_path, task_id=int(recommendation_id))
        if item:
            guidance = item.get("guidance") if isinstance(item.get("guidance"), dict) else {}
            base.update({
                "mode": "deal_task",
                "question": "",
                "situation_summary": _safe_text(guidance.get("situation_summary")),
                "next_action": _safe_text(item.get("task_text")),
                "expected_result": _safe_text(item.get("expected_result")),
                "pressure_lever": _safe_text(guidance.get("pressure_lever")),
                "strategy_labels": guidance.get("strategy_labels") or [],
                "client_messages": guidance.get("client_messages") or [],
                "lifehacks": guidance.get("lifehacks") or [],
                "fallback_action": _safe_text(guidance.get("fallback_action")),
            })
    return base


def _event_projection(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = str(event.get("event_type") or "")
    direction = str(payload.get("direction") or "").lower()
    kind = str(payload.get("activity_kind") or "")
    actor = "system"
    if event_type == "manager_communication_completed":
        actor = "manager"
    elif event_type == "crm_activity_observed":
        actor = "client" if direction in {"1", "incoming", "inbound"} else "manager" if direction in {"2", "outgoing", "outbound"} else "system"
    elif event_type == "crm_timeline_comment_observed" and payload.get("is_messenger_mirror"):
        speaker = str(payload.get("speaker") or "").lower()
        actor = "client" if speaker in {"client", "клиент"} else "manager" if speaker in {"manager", "менеджер"} else "system"
    content = (
        payload.get("content") or payload.get("description") or payload.get("subject")
        or payload.get("comment") or payload.get("summary") or ""
    )
    transcript = payload.get("transcript_excerpt") or payload.get("transcript") or ""
    if not transcript and kind == "call" and payload.get("activity_id"):
        saved_transcript = find_call_transcript(
            "deal", str(event.get("entity_id") or ""), str(payload.get("activity_id")),
        )
        if saved_transcript:
            transcript = saved_transcript.get("text") or ""
    channel = payload.get("channel") or kind or payload.get("provider_id") or "crm"
    return {
        "event_id": str(event.get("id")),
        "timestamp": event.get("occurred_at"),
        "actor": actor,
        "event_type": event_type,
        "direction": direction or None,
        "channel": str(channel),
        "content": _safe_text(content),
        "transcript_excerpt": _safe_text(transcript, 1800),
        "changes": {
            key: payload.get(key) for key in (
                "from_stage_id", "to_stage_id", "stage_id", "field", "from_value",
                "to_value", "field_name", "result_status", "contact_status",
            ) if payload.get(key) is not None
        },
        "source_event_id": event.get("source_event_key"),
    }


def _is_manager_communication(item: dict[str, Any]) -> bool:
    if item.get("actor") != "manager":
        return False
    if item.get("event_type") == "manager_communication_completed":
        return True
    if item.get("event_type") == "crm_timeline_comment_observed":
        return str(item.get("channel") or "").lower() in {"whatsapp", "max", "telegram"}
    if item.get("event_type") != "crm_activity_observed":
        return False
    return str(item.get("channel") or "").lower() in {"call", "email", "message", "im", "crm_email", "voximplant_call"}


def collect_learning_shadow_cases(
    db_path: str | Path,
    *,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    start, end = _period_bounds(from_date, to_date)
    rows = list_manager_trajectory_events(
        db_path, from_at=start.isoformat(), to_at=end.isoformat(),
    )
    deal_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("entity_type") == "deal":
            deal_rows[str(row.get("entity_id"))].append(row)
    cases: list[dict[str, Any]] = []
    for deal_id, events in deal_rows.items():
        views = [
            event for event in events
            if event.get("event_type") == "recommendation_viewed"
            and event.get("recommendation_id")
            and isinstance(event.get("payload"), dict)
            and bool(event["payload"].get("actor_verified"))
        ]
        if not views:
            continue
        views.sort(key=lambda item: (str(item.get("occurred_at") or ""), int(item.get("id") or 0)))
        first_view_at = str(views[0]["occurred_at"])
        after = [event for event in events if str(event.get("occurred_at") or "") >= first_view_at]
        unique: dict[str, dict[str, Any]] = {}
        for view in views:
            key = f"{view.get('recommendation_kind')}:{view.get('recommendation_id')}"
            occurred_at = str(view.get("occurred_at") or "")
            if key not in unique:
                unique[key] = {
                    **_recommendation_projection(db_path, view),
                    "first_view_at": occurred_at,
                    "last_view_at": occurred_at,
                    "view_count": 1,
                }
            else:
                unique[key]["last_view_at"] = occurred_at
                unique[key]["view_count"] = int(unique[key].get("view_count") or 0) + 1
        timeline = [_event_projection(event) for event in after]
        action_ids = [item["event_id"] for item in timeline if _is_manager_communication(item)]
        client_ids = [item["event_id"] for item in timeline if item.get("actor") == "client"]
        cases.append({
            "deal_id": deal_id,
            "manager_id": views[0].get("manager_id"),
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "period_end_at": end.isoformat(),
            "first_view_at": first_view_at,
            "last_view_at": str(views[-1]["occurred_at"]),
            "unique_recommendation_ids": list(unique),
            "view_count": len(views),
            "action_event_ids": action_ids,
            "client_event_ids": client_ids,
            "recommendations": list(unique.values()),
            "timeline": timeline,
            "status": "pending" if action_ids else "no_action_observed",
        })
    return sorted(cases, key=lambda item: (item["first_view_at"], item["deal_id"]))


def _public_model_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key != "raw_output_text"}


def run_learning_shadow(run_id: int, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    run = get_learning_shadow_run(db_path, run_id)
    if run is None:
        return
    update_learning_shadow_run(db_path, run_id, status="running", error=None)
    try:
        cases = collect_learning_shadow_cases(
            db_path,
            from_date=date.fromisoformat(str(run["from_date"])),
            to_date=date.fromisoformat(str(run["to_date"])),
        )
        no_action = sum(item["status"] == "no_action_observed" for item in cases)
        llm_count = len(cases) - no_action
        update_learning_shadow_run(
            db_path, run_id, total_cases=len(cases), no_action_cases=no_action,
            llm_cases=llm_count,
        )
        successful = 0
        for case in cases:
            saved = save_learning_shadow_case(db_path, run_id=run_id, **{
                key: case[key] for key in (
                    "deal_id", "manager_id", "from_date", "to_date", "first_view_at",
                    "last_view_at", "unique_recommendation_ids", "view_count",
                    "action_event_ids", "client_event_ids", "recommendations", "timeline", "status",
                )
            })
            if case["status"] == "no_action_observed":
                continue
            update_learning_shadow_case(db_path, int(saved["id"]), status="analyzing")
            try:
                result, metadata = analyze_learning_shadow_case(case)
                update_learning_shadow_case(
                    db_path, int(saved["id"]), status="completed", llm_result=result,
                    model_meta=_public_model_meta(metadata),
                )
                successful += 1
                update_learning_shadow_run(db_path, run_id, completed_cases=successful)
            except Exception as error:
                update_learning_shadow_case(
                    db_path, int(saved["id"]), status="failed", error=str(error),
                )
        update_learning_shadow_run(
            db_path, run_id, status="completed", completed_at=utcish_now(),
        )
    except Exception as error:
        update_learning_shadow_run(
            db_path, run_id, status="failed", error=str(error), completed_at=utcish_now(),
        )


def start_learning_shadow_run(
    *,
    from_date: date,
    to_date: date,
    confirm_paid: bool,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    _period_bounds(from_date, to_date)
    if not confirm_paid:
        raise PermissionError("Подтвердите платный анализ Luna")
    run = create_learning_shadow_run(
        db_path, from_date=from_date.isoformat(), to_date=to_date.isoformat(),
        model=LEARNING_SHADOW_MODEL, reasoning_effort=LEARNING_SHADOW_REASONING_EFFORT,
    )
    run_id = int(run["id"])
    thread = threading.Thread(
        target=run_learning_shadow, args=(run_id, db_path), daemon=True,
        name=f"learning-shadow-{run_id}",
    )
    with _THREADS_LOCK:
        _THREADS[run_id] = thread
    thread.start()
    return get_learning_shadow_run(db_path, run_id) or run


__all__ = [
    "collect_learning_shadow_cases", "get_learning_shadow_run",
    "list_learning_shadow_runs", "run_learning_shadow", "start_learning_shadow_run",
]
