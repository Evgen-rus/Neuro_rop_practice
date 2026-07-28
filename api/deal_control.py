"""Read-only Bitrix synchronisation and local control logic for active deals."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from api.candidates import fetch_candidate_activities_bulk, load_pipeline_stage_names, make_client, parse_bitrix_dt
from api.jobs import unwrap_analysis_payload
from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    create_deal_control_task,
    confirm_deal_control_task_crm_match,
    get_deal_control_scope,
    get_deal_control_metrics,
    get_latest_ui_report,
    list_deal_control_deals,
    list_deal_control_task_history,
    list_deal_control_tasks,
    record_deal_control_task_event,
    save_deal_control_scope,
    save_deal_control_task_crm_fact,
    save_deal_control_task_crm_sync,
    save_deal_control_task_outcome,
    set_deal_control_deal_active,
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


def _match_task_to_activity(task: dict[str, Any], activities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return only a score; weak matches deliberately require an ROP decision."""
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
        confidence = "high" if similarity >= 0.55 and date_score >= 0.25 else "medium" if similarity >= 0.35 and date_score else "low"
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
    pipeline_rows = crm.list_all(
        "crm.deal.list",
        {"order": {"DATE_CREATE": "DESC", "ID": "DESC"}, "filter": {"CATEGORY_ID": scope["pipeline_id"], "CLOSED": "N"}, "select": DEAL_SELECT},
    )
    pipeline_rows = [
        row for row in pipeline_rows
        if str(row.get("ASSIGNED_BY_ID") or "") in manager_ids and str(row.get("CLOSED") or "").upper() != "Y"
    ]
    initial_ids = {str(value) for value in scope["initial_deal_ids"]}
    seen_pipeline_ids = {str(row.get("ID") or "") for row in pipeline_rows}
    saved_before = list_deal_control_deals(db_path, active_only=False)
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
    deals = list_deal_control_deals(db_path, active_only=False)
    activities = fetch_candidate_activities_bulk(crm, [("deal", {"ID": item["deal_id"]}) for item in deals])
    deals_by_id = {str(item["deal_id"]): item for item in deals}
    for task in list_deal_control_tasks(db_path):
        task_activities, activity_error = activities.get(("deal", str(task["deal_id"])), ([], "activity unavailable"))
        if activity_error:
            errors.append(f"Сделка #{task['deal_id']}: {activity_error}")
            continue
        match = _match_task_to_activity(task, task_activities)
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
        for activity in task_activities:
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
    return {
        "report_id": report.get("id") if report else None,
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
        "script_channel": str(manager.get("recommended_channel") or ""),
        "rop_task_hint": str(rop.get("message_to_manager") or rop.get("check_for_rop") or ""),
        "expected_crm_update": str(rop.get("expected_crm_update") or money.get("next_required_fact") or ""),
    }


def build_deal_control_dashboard(*, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None,
                                 sync_message: str | None = None, sync_errors: list[str] | None = None) -> dict[str, Any]:
    current = now or datetime.now(MSK_TZ)
    deals = list_deal_control_deals(db_path)
    active_deal_ids = [str(deal["deal_id"]) for deal in deals]
    all_tasks = list_deal_control_tasks(db_path, deal_ids=active_deal_ids) if active_deal_ids else []
    active_tasks = [task for task in all_tasks if task.get("local_status") == "active"]
    tasks_by_deal: dict[str, list[dict[str, Any]]] = {}
    for task in all_tasks:
        task = dict(task)
        task["view_status"] = _task_view_status(task, current)
        task["time_bucket"] = _task_time_bucket(task, current)
        tasks_by_deal.setdefault(str(task["deal_id"]), []).append(task)
    items = []
    for deal in deals:
        deal = dict(deal)
        deal_tasks = tasks_by_deal.get(str(deal["deal_id"]), [])
        deal["tasks"] = deal_tasks
        deal["current_task"] = next((task for task in deal_tasks if task.get("local_status") == "active"), None)
        deal["coaching"] = _analysis_coaching(db_path, str(deal["deal_id"]))
        items.append(deal)
    task_buckets = [_task_time_bucket(task, current) for task in all_tasks]
    overdue = task_buckets.count("overdue")
    today = task_buckets.count("today")
    tomorrow = task_buckets.count("tomorrow")
    future = task_buckets.count("future")
    completed_today = task_buckets.count("completed_today")
    probability_values = [int(item["probability"]) for item in deals if item.get("probability") is not None]
    total_amount = sum(float(str(item.get("amount") or "0").replace(",", ".") or 0) for item in deals if str(item.get("amount") or "").replace(",", ".").replace(".", "", 1).isdigit())
    return {
        "scope": get_deal_control_scope(db_path),
        "generated_at": current.isoformat(timespec="seconds"),
        "sync_message": sync_message,
        "sync_errors": sync_errors or [],
        "summary": {
            "active_deals": len(deals), "portfolio_amount": total_amount,
            "tasks_total": len(active_tasks), "tasks_today": today, "tasks_tomorrow": tomorrow,
            "tasks_future": future, "tasks_overdue": overdue, "tasks_completed_today": completed_today,
            "average_probability": round(sum(probability_values) / len(probability_values)) if probability_values else None,
        },
        "outcome_metrics": get_deal_control_metrics(db_path),
        "deals": items,
    }


def save_scope(*, db_path: str | Path, initial_deal_ids: list[str], manager_ids: list[str], pipeline_id: str) -> dict[str, Any]:
    return save_deal_control_scope(db_path, initial_deal_ids=initial_deal_ids, manager_ids=manager_ids, pipeline_id=pipeline_id)


def save_deal_fields(*, db_path: str | Path, deal_id: str, probability: int | None,
                     expected_payment_period: str | None, next_control_at: str | None) -> dict[str, Any]:
    return update_deal_control_fields(
        db_path, deal_id=deal_id, probability=probability,
        expected_payment_period=expected_payment_period, next_control_at=next_control_at,
    )


def add_task(*, db_path: str | Path, deal_id: str, task_text: str, touch_type: str | None,
             expected_result: str | None, due_at: str) -> dict[str, Any]:
    return create_deal_control_task(
        db_path, deal_id=deal_id, task_text=task_text, touch_type=touch_type,
        expected_result=expected_result, due_at=due_at,
    )


def edit_task(*, db_path: str | Path, task_id: int, task_text: str | None, touch_type: str | None,
              expected_result: str | None, due_at: str | None, local_status: str | None,
              business_result_status: str | None, business_result_note: str | None) -> dict[str, Any]:
    return update_deal_control_task(
        db_path, task_id=task_id, task_text=task_text, touch_type=touch_type,
        expected_result=expected_result, due_at=due_at, local_status=local_status,
        business_result_status=business_result_status, business_result_note=business_result_note,
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
