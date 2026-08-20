"""Immutable daily-control snapshots for the ROP planning meeting.

The builder reuses the existing deal-control dashboard. It does not create a
second checklist, a second analysis, or Bitrix write methods.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from setup import MSK_TZ, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    create_daily_control_report,
    dumps_json,
    get_daily_control_report,
    get_latest_automatic_analysis_run,
    get_latest_daily_control_report,
    get_latest_ui_report,
    list_daily_control_reports,
    list_deal_control_deals,
    list_deal_daily_checklist_summaries,
    utcish_now,
)

GENERIC_STATUS_QUESTION = "Что было сделано и какой сейчас статус?"
STATUS_LABELS = {
    "red": "Требует решения РОПа",
    "yellow": "Нужна проверка",
    "green": "В норме",
}
CHECKLIST_WHY = {
    "missing": "Нет подтверждённого факта в анализе",
    "focus": "Фокус проверки на сегодня",
    "crm": "Нужно зафиксировать в CRM",
}
COMMUNICATION_ITEM_KEYS = (
    "event_id",
    "channel",
    "direction",
    "occurred_at",
    "subject",
    "duration_seconds",
    "contact_class",
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

logger = get_logger(__file__)

_generation_lock = threading.Lock()
_generation_state: dict[str, Any] | None = None


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

    Read-only: does not seed checklists or call Bitrix/LLM.
    """
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    business_date = current.date().isoformat()
    parts: list[str] = [business_date]
    for deal in list_deal_control_deals(db_path, active_only=True):
        deal_id = str(deal.get("deal_id") or "")
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
                    "stage_id": deal.get("stage_id"),
                    "amount": deal.get("amount"),
                    "communications": {
                        "date": communications.get("date"),
                        "available": communications.get("available"),
                        "calls": communications.get("calls"),
                        "messages": communications.get("messages"),
                        "duration_seconds": communications.get("duration_seconds"),
                        "completed": communications.get("completed"),
                    },
                    "report_id": report.get("id") if report else None,
                    "report_created_at": report.get("created_at") if report else None,
                }
            )
        )
    for summary in list_deal_daily_checklist_summaries(db_path, business_date=business_date):
        parts.append(dumps_json(summary))
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


def classify_deal_status(deal: dict[str, Any]) -> tuple[str, str]:
    """Server-side red/yellow/green from existing structured deal-control facts."""
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    audit = (
        coaching.get("communication_quality_audit")
        if isinstance(coaching.get("communication_quality_audit"), dict)
        else {}
    )
    current_task = deal.get("current_task") if isinstance(deal.get("current_task"), dict) else {}
    bitrix_task = deal.get("primary_bitrix_task") if isinstance(deal.get("primary_bitrix_task"), dict) else {}
    communications = (
        deal.get("communications_today")
        if isinstance(deal.get("communications_today"), dict)
        else {}
    )
    checklist = deal.get("checklist") if isinstance(deal.get("checklist"), dict) else {}
    scores = _audit_scores(audit)
    zero_count = sum(1 for score in scores.values() if score == 0)
    next_action_zero = scores.get("next_action") == 0
    assessed = audit.get("status") == "assessed"
    insufficient = audit.get("status") == "insufficient_evidence"
    overdue_control = str(current_task.get("time_bucket") or "") == "overdue"
    overdue_bitrix = (
        str(bitrix_task.get("time_bucket") or "") == "overdue"
        and str(bitrix_task.get("completion_state") or "open") == "open"
    )
    no_analysis = not coaching.get("report_id")
    comms_available = communications.get("available") is True
    no_movement = comms_available and int(communications.get("completed") or 0) == 0
    open_checklist = int(checklist.get("total") or 0) > int(checklist.get("completed") or 0)

    if overdue_control or (assessed and zero_count >= 2) or (overdue_bitrix and next_action_zero):
        return "red", STATUS_LABELS["red"]
    if (
        no_analysis
        or insufficient
        or next_action_zero
        or zero_count >= 1
        or overdue_bitrix
        or (no_movement and open_checklist)
    ):
        return "yellow", STATUS_LABELS["yellow"]
    return "green", STATUS_LABELS["green"]


def _audit_scores(audit: dict[str, Any]) -> dict[str, int | None]:
    criteria = audit.get("criteria") if isinstance(audit.get("criteria"), dict) else {}
    scores: dict[str, int | None] = {}
    for name in ("next_action", "value_development", "data_collection"):
        item = criteria.get(name) if isinstance(criteria.get(name), dict) else {}
        score = item.get("score")
        scores[name] = int(score) if isinstance(score, int) and not isinstance(score, bool) else None
    return scores


def _sanitize_communications(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    items = []
    for raw in source.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = {key: raw.get(key) for key in COMMUNICATION_ITEM_KEYS}
        item["content_available"] = False
        for forbidden in INVENTED_CONTENT_KEYS:
            item.pop(forbidden, None)
        items.append(item)
    available = bool(source.get("available"))
    return {
        "date": source.get("date"),
        "available": available,
        "target": source.get("target"),
        "completed": int(source.get("completed") or 0) if available else 0,
        "progress_percent": source.get("progress_percent") if available else None,
        "calls": int(source.get("calls") or 0) if available else 0,
        "messages": int(source.get("messages") or 0) if available else 0,
        "duration_seconds": int(source.get("duration_seconds") or 0) if available else 0,
        "unavailable": not available,
        "items": items if available else [],
        "content_available": False,
    }


def _checklist_why(item: dict[str, Any]) -> str | None:
    if str(item.get("change_kind") or "") == "carried":
        return "Перенесено с предыдущего дня"
    source = str(item.get("source") or "")
    return CHECKLIST_WHY.get(source)


def _project_checklist(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    items = []
    for raw in source.get("items") or []:
        if not isinstance(raw, dict):
            continue
        items.append(
            {
                "id": str(raw.get("id") or ""),
                "text": str(raw.get("text") or ""),
                "completed": bool(raw.get("completed")),
                "completed_at": raw.get("completed_at"),
                "source": raw.get("source"),
                "change_kind": raw.get("change_kind"),
                "why": _checklist_why(raw),
            }
        )
    completed = sum(1 for item in items if item["completed"])
    return {
        "business_date": source.get("business_date"),
        "revision": source.get("revision"),
        "source_report_id": source.get("source_report_id"),
        "completed": completed,
        "total": len(items),
        "items": items,
    }


def _quality_block(coaching: dict[str, Any]) -> dict[str, Any]:
    audit = coaching.get("communication_quality_audit")
    if not isinstance(audit, dict) or not audit:
        return {
            "status": "missing",
            "criteria": {
                "next_action": {"score": None, "verdict": "Нет оценки"},
                "value_development": {"score": None, "verdict": "Нет оценки"},
                "data_collection": {"score": None, "verdict": "Нет оценки"},
            },
            "confirmed_count": None,
            "total": 3,
            "scope_summary": None,
            "zero_reasons": [],
            "summary_for_rop": None,
            "insufficient_reason": "В сохранённом анализе нет communication_quality_audit.",
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
            "insufficient_reason": audit.get("insufficient_reason") or "Недостаточно содержательной коммуникации.",
        }
    scores = _audit_scores(audit)
    labels = {
        "next_action": ("Следующий шаг зафиксирован", "Нет точного следующего шага"),
        "value_development": ("Касания дали ценность", "Ценность касаний не подтверждена"),
        "data_collection": ("Ключевые данные собраны", "Данных недостаточно"),
    }
    criteria = {}
    confirmed = 0
    for name, (ok_text, bad_text) in labels.items():
        score = scores.get(name)
        if score == 1:
            confirmed += 1
            verdict = ok_text
        elif score == 0:
            verdict = bad_text
        else:
            verdict = "Нет оценки"
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
    checklist = deal.get("checklist") if isinstance(deal.get("checklist"), dict) else {}
    for item in checklist.get("items") or []:
        if not isinstance(item, dict) or item.get("completed"):
            continue
        question = _action_to_you_question(str(item.get("text") or ""))
        if question and question not in fragments:
            fragments.append(question)
        if len(fragments) >= 3:
            break
    if len(fragments) < 2:
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
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    for value in (
        coaching.get("current_situation"),
        quality.get("summary_for_rop"),
        coaching.get("what_to_check_now"),
        coaching.get("rop_focus"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    if quality.get("status") == "insufficient_evidence":
        return str(quality.get("insufficient_reason") or "Нет содержательного звонка, письма или переписки, чтобы оценить качество ведения.")
    if not coaching.get("report_id"):
        return "Сохранённого анализа сделки ещё нет — проверьте факт работы менеджера."
    return "Нужно проверить текущий статус сделки."


def project_deal_review_card(deal: dict[str, Any]) -> dict[str, Any]:
    """Project a live deal-control deal into the shared ROP review card.

    Used by the immutable daily snapshot and the live ROP card. Does not call
    LLM, Bitrix, or a second checklist store.
    """
    coaching = deal.get("coaching") if isinstance(deal.get("coaching"), dict) else {}
    quality = _quality_block(coaching)
    status, status_label = classify_deal_status(deal)
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
        "checklist": _project_checklist(deal.get("checklist")),
        "has_analysis": bool(coaching.get("report_id")),
        "analysis_created_at": coaching.get("analysis_created_at"),
    }


def _sort_deals(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rank = {"red": 0, "yellow": 1, "green": 2}

    def key(deal: dict[str, Any]) -> tuple[Any, ...]:
        return (
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
) -> dict[str, Any]:
    deals = [project_deal_review_card(deal) for deal in dashboard.get("deals") or [] if isinstance(deal, dict)]
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
                "checklist_completed": 0,
                "checklist_total": 0,
                "calls": 0,
                "messages": 0,
                "talk_seconds": 0,
                "red": 0,
                "yellow": 0,
                "green": 0,
            },
        )
        bucket["deals_count"] += 1
        checklist = deal.get("checklist") or {}
        bucket["checklist_completed"] += int(checklist.get("completed") or 0)
        bucket["checklist_total"] += int(checklist.get("total") or 0)
        communications = deal.get("communications_today") or {}
        if communications.get("unavailable"):
            unavailable_comms += 1
        else:
            movement_scope += 1
            if int(communications.get("completed") or 0) == 0:
                no_movement += 1
            bucket["calls"] += int(communications.get("calls") or 0)
            bucket["messages"] += int(communications.get("messages") or 0)
            bucket["talk_seconds"] += int(communications.get("duration_seconds") or 0)
        bucket[str(deal.get("status") or "yellow")] = int(bucket.get(str(deal.get("status") or "yellow")) or 0) + 1
    managers = _sort_managers(list(managers_by_id.values()))
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
) -> dict[str, Any]:
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    started = started_at if isinstance(started_at, datetime) else datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    started = _aware(started).astimezone(MSK_TZ)
    warnings: list[str] = []
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
    snapshot = build_daily_control_snapshot(payload, warnings=warnings)
    cutoff = cutoff_at or current
    if isinstance(cutoff, str):
        cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    else:
        cutoff_dt = cutoff
    cutoff_dt = _aware(cutoff_dt).astimezone(MSK_TZ)
    watermark = compute_source_watermark(db_path, now=cutoff_dt)
    if automatic_analysis_run_id is None:
        latest_run = get_latest_automatic_analysis_run(db_path)
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
) -> dict[str, Any]:
    """Publish today's planning snapshot from the last prepared persisted state.

    Does not wait for in-flight LLM jobs and does not start a new analysis.
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
    logger.info("Публикация планировочного daily-control из persisted state на %s.", _iso(current))
    return publish_daily_control_report(
        db_path=db_path,
        creation_kind="automatic_planning",
        started_at=current,
        cutoff_at=current,
        now=current,
        refresh=False,
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


def freshness_for_report(
    report: dict[str, Any] | None,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    live_watermark: str | None = None,
) -> dict[str, Any]:
    latest = get_latest_daily_control_report(db_path, include_snapshot=False)
    live = live_watermark or compute_source_watermark(db_path, now=now)
    if report is None:
        return {
            "state": "missing",
            "label": "Отчёта ещё нет",
            "is_latest": False,
            "live_watermark": live,
            "report_watermark": None,
        }
    is_latest = latest is not None and int(latest["id"]) == int(report["id"])
    stored = str(report.get("source_watermark") or "")
    if not is_latest:
        state, label = "historical", "Исторический"
    elif stored == live:
        state, label = "current", "Актуальный"
    else:
        state, label = "stale", "Данные обновились"
    return {
        "state": state,
        "label": label,
        "is_latest": is_latest,
        "live_watermark": live,
        "report_watermark": stored,
    }


def project_daily_control_snapshot(snapshot: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Apply the same deal-access contract as live deal-control. Stored JSON stays intact."""
    from api.access import deal_access, rop_team_manager_ids

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
                and int((deal.get("communications_today") or {}).get("completed") or 0) == 0
            ),
            "total": sum(
                1 for deal in deals if not (deal.get("communications_today") or {}).get("unavailable")
            ),
        },
        "calls": sum(int(item.get("calls") or 0) for item in managers),
        "messages": sum(int(item.get("messages") or 0) for item in managers),
        "talk_seconds": sum(int(item.get("talk_seconds") or 0) for item in managers),
    }
    return projected


def history_payload(*, db_path: str | Path = DEFAULT_DB_PATH, now: datetime | None = None) -> dict[str, Any]:
    reports = list_daily_control_reports(db_path)
    live = compute_source_watermark(db_path, now=now)
    latest_id = int(reports[0]["id"]) if reports else None
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
                "freshness": freshness_for_report(report, db_path=db_path, now=now, live_watermark=live),
            }
        )
    return {
        "reports": items,
        "latest_id": latest_id,
        "total": len(reports),
        "live_watermark": live,
        "generation": generation_status(),
    }


def report_payload(
    report_id: int,
    user: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    report = get_daily_control_report(db_path, report_id, include_snapshot=True)
    if report is None:
        return None
    snapshot = project_daily_control_snapshot(report.get("snapshot") or {}, user)
    live = compute_source_watermark(db_path, now=now)
    history = list_daily_control_reports(db_path)
    ids = [int(item["id"]) for item in history]
    index = ids.index(int(report["id"])) if int(report["id"]) in ids else None
    position_from_oldest = (len(ids) - index) if index is not None else None
    return {
        "id": report.get("id"),
        "business_date": report.get("business_date"),
        "creation_kind": report.get("creation_kind"),
        "started_at": report.get("started_at"),
        "cutoff_at": report.get("cutoff_at"),
        "created_at": report.get("created_at"),
        "source_watermark": report.get("source_watermark"),
        "automatic_analysis_run_id": report.get("automatic_analysis_run_id"),
        "source_status": report.get("source_status"),
        "warnings": report.get("warnings") or [],
        "error": report.get("error"),
        "snapshot": snapshot,
        "freshness": freshness_for_report(report, db_path=db_path, now=now, live_watermark=live),
        "position": position_from_oldest,
        "total": len(ids),
        "previous_id": ids[index + 1] if index is not None and index + 1 < len(ids) else None,
        "next_id": ids[index - 1] if index is not None and index > 0 else None,
        "generation": generation_status(),
    }
