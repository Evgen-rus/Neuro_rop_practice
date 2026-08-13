"""Independent personal-sales-coach jobs for the manager deal screen.

Canonical storage expected by this module:

``save_deal_manager_quick_help(db_path, *, deal_id, source_report_id,
situation_review_id, question, answer_json, model_meta=None)`` -> dict

``list_deal_manager_quick_help(db_path, *, deal_id, limit=20,
before_id=None)`` -> list[dict]

The API never reads or writes a previous quick-help answer into a new prompt.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.customer_history import build_normalized_communications
from bitrix.workspace import DEFAULT_RAW_DIR, deal_workspace_dir
from api.deal_manager_situation import (
    DEFAULT_DB_PATH,
    StorageContractUnavailable,
    _safe_model_meta,
    load_manager_screen_context,
    _situation_id,
    _situation_status,
    _storage_call,
)
from openai_api.llm.deal_manager_quick_help import ASSISTANT_MODES, generate_deal_manager_quick_help
from setup import MSK_TZ


MAX_QUESTION_CHARS = 4000
COMMUNICATION_WINDOW_DAYS = 30
COMMUNICATION_HISTORY_DAYS = 60
MAX_COMMUNICATION_EVENTS = 10
AUTO_QUESTIONS = {
    "push": "Сформируй текущий дожим сделки",
    "reanimator": "Сформируй текущую рекомендацию по восстановлению коммуникации",
}


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


@dataclass
class DealManagerQuickHelpJob:
    job_id: str
    deal_id: str
    question: str
    situation_id: int | None
    mode: str | None = None
    origin: str = "manager"
    turn_id: str | None = None
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем ответ тренера"
    percent: int = 5
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    quick_help_id: int | None = None
    saved_by_mode: dict[str, int] = field(default_factory=dict)
    reused: bool = False
    error: str | None = None


_QUICK_HELP_JOBS: dict[str, DealManagerQuickHelpJob] = {}
_QUICK_HELP_LOCK = threading.Lock()


def _touch(job: DealManagerQuickHelpJob, *, stage: str, detail: str, percent: int) -> None:
    with _QUICK_HELP_LOCK:
        job.stage = stage
        job.detail = detail
        job.percent = percent
        job.updated_at = _now()


def get_quick_help_job(job_id: str) -> dict[str, Any] | None:
    with _QUICK_HELP_LOCK:
        job = _QUICK_HELP_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _current_for_mode(db_path: str | Path, context: dict[str, Any], mode: str) -> dict[str, Any] | None:
    situation_id = context.get("situation_id")
    if situation_id is None:
        return None
    saved = _storage_call(
        "get_current_deal_manager_quick_help",
        db_path,
        deal_id=str(context["deal"]["deal_id"]),
        source_report_id=int(context["source_report_id"]),
        situation_review_id=int(situation_id),
        mode=mode,
    )
    return saved if isinstance(saved, dict) else None


def _save_mode_answer(
    *,
    db_path: str | Path,
    job: DealManagerQuickHelpJob,
    context: dict[str, Any],
    situation_id: int,
    mode: str,
    question: str,
    origin: str,
    communication_pattern_context: dict[str, Any],
) -> dict[str, Any]:
    answer, metadata = generate_deal_manager_quick_help(
        question=question,
        analysis_projection=context["analysis_projection"],
        deal=context["deal"],
        current_bitrix_task=context["current_bitrix_task"],
        situation_projection=context["situation_projection"],
        communication_pattern_context=communication_pattern_context,
        mode=mode,
    )
    saved = _storage_call(
        "save_deal_manager_quick_help",
        db_path,
        deal_id=job.deal_id,
        source_report_id=context["source_report_id"],
        situation_review_id=situation_id,
        question=question,
        answer_json=answer,
        model_meta=_safe_model_meta(metadata),
        mode=mode,
        origin=origin,
        turn_id=job.turn_id,
    )
    saved_id = int(saved["id"]) if isinstance(saved, dict) and saved.get("id") is not None else None
    with _QUICK_HELP_LOCK:
        if saved_id is not None:
            job.saved_by_mode[mode] = saved_id
            job.quick_help_id = saved_id
    return saved if isinstance(saved, dict) else {}


def _run_quick_help_job(job_id: str, db_path: str | Path) -> None:
    with _QUICK_HELP_LOCK:
        job = _QUICK_HELP_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, stage="context", detail="Проверяем подтверждённую ситуацию сделки", percent=20)
        context = load_manager_screen_context(
            db_path,
            job.deal_id,
            require_confirmed_situation=True,
        )
        situation_id = context["situation_id"] or job.situation_id
        if situation_id is None:
            raise ValueError("У текущей ситуации отсутствует идентификатор")
        communication_pattern_context = build_communication_pattern_context(
            _load_local_communications(job.deal_id)
        )
        if job.origin == "auto" and not job.question.strip():
            for item_mode in ASSISTANT_MODES:
                current = _current_for_mode(db_path, context, item_mode)
                sibling_turn = str((current or {}).get("turn_id") or "").strip()
                if sibling_turn:
                    job.turn_id = sibling_turn
                    break
        if not job.turn_id:
            job.turn_id = uuid.uuid4().hex
        modes = list(ASSISTANT_MODES)
        generated = 0
        for index, mode in enumerate(modes):
            existing = _current_for_mode(db_path, context, mode) if job.origin == "auto" and not job.question.strip() else None
            if isinstance(existing, dict) and existing.get("id") is not None:
                with _QUICK_HELP_LOCK:
                    job.saved_by_mode[mode] = int(existing["id"])
                    job.quick_help_id = int(existing["id"])
                continue
            label = "дожим" if mode == "push" else "реаниматор"
            _touch(job, stage="llm", detail=f"AI готовит {label}", percent=40 + index * 25)
            question = job.question.strip() or AUTO_QUESTIONS[mode]
            _save_mode_answer(
                db_path=db_path,
                job=job,
                context=context,
                situation_id=int(situation_id),
                mode=mode,
                question=question,
                origin=job.origin,
                communication_pattern_context=communication_pattern_context,
            )
            generated += 1
        with _QUICK_HELP_LOCK:
            job.reused = generated == 0
            job.status = "done"
        _touch(job, stage="done", detail="Рекомендация готова", percent=100)
    except Exception as error:  # noqa: BLE001 - never return model content in a job error
        with _QUICK_HELP_LOCK:
            job.status = "error"
            job.error = f"{error.__class__.__name__}: операция не выполнена"
        _touch(job, stage="error", detail="Не удалось подготовить ответ", percent=100)


def start_quick_help_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    question: str | None = None,
    confirm_paid: bool,
    mode: str | None = None,
) -> dict[str, Any]:
    normalized_question = str(question or "").strip()
    if normalized_question and not 1 <= len(normalized_question) <= MAX_QUESTION_CHARS:
        raise ValueError("Вопрос должен содержать от 1 до 4000 знаков")
    if mode is not None and mode not in ASSISTANT_MODES:
        raise ValueError("mode должен быть push или reanimator")
    origin = "manager" if normalized_question else "auto"
    if origin == "manager" and mode is None:
        mode = "reanimator"
    # Уточнение в чате всегда платное. Автогенерация может переиспользовать
    # уже сохранённые режимы без LLM, поэтому confirm_paid проверяем после контекста.
    if origin == "manager" and not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для quick help")
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    if origin == "auto":
        saved_by_mode: dict[str, int] = {}
        missing: list[str] = []
        for item_mode in ASSISTANT_MODES:
            current = _current_for_mode(db_path, context, item_mode)
            if isinstance(current, dict) and current.get("id") is not None:
                saved_by_mode[item_mode] = int(current["id"])
            else:
                missing.append(item_mode)
        if not missing:
            job = DealManagerQuickHelpJob(
                job_id=uuid.uuid4().hex,
                deal_id=str(deal_id),
                question="",
                situation_id=context["situation_id"],
                mode=mode,
                origin="auto",
                status="done",
                stage="done",
                detail="Открываем сохранённую актуальную рекомендацию",
                percent=100,
                quick_help_id=saved_by_mode.get("push") or next(iter(saved_by_mode.values()), None),
                saved_by_mode=saved_by_mode,
                reused=True,
            )
            with _QUICK_HELP_LOCK:
                _QUICK_HELP_JOBS[job.job_id] = job
            return asdict(job)
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для quick help")
    with _QUICK_HELP_LOCK:
        existing = next(
            (
                item
                for item in _QUICK_HELP_JOBS.values()
                if item.deal_id == str(deal_id) and item.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return asdict(existing)
        job_id = uuid.uuid4().hex
        job = DealManagerQuickHelpJob(
            job_id=job_id,
            deal_id=str(deal_id),
            question=normalized_question,
            situation_id=context["situation_id"],
            mode=mode,
            origin=origin,
            turn_id=uuid.uuid4().hex,
        )
        _QUICK_HELP_JOBS[job_id] = job
    thread = threading.Thread(target=_run_quick_help_job, args=(job_id, db_path), daemon=True)
    thread.start()
    return asdict(job)


def list_quick_help_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    limit: int = 20,
    before_id: int | None = None,
) -> dict[str, Any]:
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit должен быть от 1 до 100")
    if before_id is not None and int(before_id) < 1:
        raise ValueError("before_id должен быть положительным")
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    items = _storage_call(
        "list_deal_manager_quick_help",
        db_path,
        deal_id=str(deal_id),
        limit=int(limit),
        before_id=int(before_id) if before_id is not None else None,
    )
    return {"items": items if isinstance(items, list) else []}


def _load_local_communications(deal_id: str) -> list[dict[str, Any]]:
    paths = (
        deal_workspace_dir(str(deal_id)) / "raw" / f"deal_{deal_id}_customer_history_bundle.json",
        DEFAULT_RAW_DIR / f"deal_{deal_id}_customer_history_bundle.json",
    )
    bundle: dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            bundle = value
            break
    if not bundle:
        return []
    events = bundle.get("normalized_communications")
    if not isinstance(events, list):
        events = build_normalized_communications(bundle)
    return [
        item for item in events
        if isinstance(item, dict)
        and str(item.get("contact_class") or "") != "internal_information"
    ]


def _communication_datetime(item: dict[str, Any]) -> datetime | None:
    raw_value = str(item.get("occurred_at") or "").strip()
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK_TZ)
    return value.astimezone(MSK_TZ)


def _project_communication(item: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "occurred_at": str(item.get("occurred_at") or ""),
        "channel": str(item.get("channel") or "unknown"),
        "direction": str(item.get("direction") or "unknown"),
        "contact_class": str(item.get("contact_class") or "unknown"),
    }
    duration = item.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        projected["duration_seconds"] = int(duration)
    return projected


def build_communication_pattern_context(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded fact-only context without message bodies or transcripts."""
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    current = current.astimezone(MSK_TZ)
    dated = [
        (occurred_at, item)
        for item in events
        if isinstance(item, dict)
        and str(item.get("contact_class") or "") != "internal_information"
        and (occurred_at := _communication_datetime(item)) is not None
        and occurred_at <= current
    ]
    dated.sort(key=lambda pair: pair[0], reverse=True)
    recent_cutoff = current - timedelta(days=COMMUNICATION_WINDOW_DAYS)
    history_cutoff = current - timedelta(days=COMMUNICATION_HISTORY_DAYS)
    recent = [(occurred_at, item) for occurred_at, item in dated if occurred_at >= recent_cutoff]
    attempts = [
        item for _, item in recent
        if str(item.get("direction") or "") == "outgoing"
        and str(item.get("contact_class") or "") == "attempt"
    ]
    confirmed = [
        item for _, item in recent
        if str(item.get("contact_class") or "") == "confirmed_contact"
    ]
    attempts_by_channel: dict[str, int] = {}
    for item in attempts:
        channel = str(item.get("channel") or "unknown")
        attempts_by_channel[channel] = attempts_by_channel.get(channel, 0) + 1
    attempts_since_contact = 0
    for _, item in recent:
        if str(item.get("contact_class") or "") == "confirmed_contact":
            break
        if (
            str(item.get("direction") or "") == "outgoing"
            and str(item.get("contact_class") or "") == "attempt"
        ):
            attempts_since_contact += 1
    last_confirmed = next(
        (
            item for occurred_at, item in dated
            if occurred_at >= history_cutoff
            and str(item.get("contact_class") or "") == "confirmed_contact"
        ),
        None,
    )
    return {
        "window_days": COMMUNICATION_WINDOW_DAYS,
        "max_recent_events": MAX_COMMUNICATION_EVENTS,
        "total_attempts": len(attempts),
        "confirmed_contacts": len(confirmed),
        "attempts_by_channel": dict(sorted(attempts_by_channel.items())),
        "consecutive_attempts_without_contact": attempts_since_contact,
        "last_confirmed_contact": _project_communication(last_confirmed) if last_confirmed else None,
        "recent_events": [
            _project_communication(item)
            for _, item in recent[:MAX_COMMUNICATION_EVENTS]
        ],
    }


def _communication_text(item: dict[str, Any]) -> str:
    channel = str(item.get("channel") or "").lower()
    direction = str(item.get("direction") or "").lower()
    subject = str(item.get("subject") or "").strip()
    preview = str(item.get("preview") or "").strip()
    labels = {
        "call": "Звонок",
        "email": "Email",
        "message": "Сообщение",
        "whatsapp": "WhatsApp",
        "telegram": "Telegram",
        "max": "Max",
    }
    label = labels.get(channel, str(item.get("source_label") or "Коммуникация"))
    direction_label = "клиента" if direction == "incoming" else "менеджера" if direction == "outgoing" else ""
    detail = subject or preview
    prefix = " ".join(value for value in (label, direction_label) if value)
    return f"{prefix}: {detail}" if detail else prefix


def _main_risk(analysis: dict[str, Any], situation: dict[str, Any]) -> str:
    risk = analysis.get("main_risk")
    if isinstance(risk, dict):
        for key in ("description", "summary", "risk", "title"):
            value = str(risk.get(key) or "").strip()
            if value:
                return value
    elif isinstance(risk, str) and risk.strip():
        return risk.strip()
    return str(situation.get("what_blocks_progress") or "").strip()


def get_manager_assistant_workspace(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
) -> dict[str, Any]:
    """Project the existing quick-help rows and local CRM history for one deal."""
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    items = _storage_call(
        "list_deal_manager_quick_help",
        db_path,
        deal_id=str(deal_id),
        limit=100,
        before_id=None,
    )
    entries = items if isinstance(items, list) else []
    assistant_events = _storage_call(
        "list_deal_manager_assistant_events",
        db_path,
        deal_id=str(deal_id),
        limit=100,
    )
    local_events = assistant_events if isinstance(assistant_events, list) else []
    communications = _load_local_communications(str(deal_id))
    timeline: list[dict[str, Any]] = []
    for entry in entries:
        origin = str(entry.get("origin") or "manager")
        mode = str(entry.get("mode") or "reanimator")
        mode_label = "Дожим" if mode == "push" else "Реаниматор"
        if origin == "auto":
            text = f"Система сформировала рекомендацию: {mode_label}"
        else:
            text = f"Менеджер уточнил {mode_label}: {str(entry.get('question') or '').strip()}"
        timeline.append({
            "id": f"assistant:{entry.get('id')}",
            "kind": "assistant_request",
            "occurred_at": entry.get("created_at"),
            "text": text,
        })
    for event in local_events:
        timeline.append({
            "id": f"assistant-event:{event.get('id')}",
            "kind": str(event.get("event_type") or "assistant_event"),
            "occurred_at": event.get("created_at"),
            "text": "Менеджер отметил коммуникацию выполненной.",
        })
    for item in communications:
        text = _communication_text(item)
        if text:
            timeline.append({
                "id": str(item.get("event_id") or f"communication:{len(timeline)}"),
                "kind": "communication",
                "occurred_at": item.get("occurred_at"),
                "text": text,
                "contact_class": item.get("contact_class"),
            })
    timeline.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    latest_communication = max(
        communications,
        key=lambda item: str(item.get("occurred_at") or ""),
        default=None,
    )
    task = context.get("current_bitrix_task") if isinstance(context.get("current_bitrix_task"), dict) else {}
    deal = context.get("deal") if isinstance(context.get("deal"), dict) else {}
    analysis = context.get("analysis_projection") if isinstance(context.get("analysis_projection"), dict) else {}
    situation = context.get("situation_projection") if isinstance(context.get("situation_projection"), dict) else {}
    current_by_mode: dict[str, Any] = {"push": None, "reanimator": None}
    source_report_id = context.get("source_report_id")
    situation_id = context.get("situation_id")
    for entry in entries:
        mode = str(entry.get("mode") or "reanimator")
        if mode not in current_by_mode:
            continue
        if current_by_mode[mode] is not None:
            continue
        if source_report_id is not None and int(entry.get("source_report_id") or 0) != int(source_report_id):
            continue
        if situation_id is not None and int(entry.get("situation_review_id") or 0) != int(situation_id):
            continue
        current_by_mode[mode] = entry
    return {
        "started": bool(entries),
        "entries": entries,
        "current_by_mode": current_by_mode,
        "source_report_id": source_report_id,
        "situation_review_id": situation_id,
        "timeline": timeline[:50],
        "context": {
            "stage": str(deal.get("stage_name") or ""),
            "current_task": str(task.get("subject") or task.get("description") or ""),
            "last_communication": {
                "occurred_at": latest_communication.get("occurred_at"),
                "text": _communication_text(latest_communication),
            } if latest_communication else None,
            "main_risk": _main_risk(analysis, situation),
        },
    }


def record_manager_communication_completed(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    quick_help_id: int,
) -> dict[str, Any]:
    load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    return _storage_call(
        "record_deal_manager_assistant_event",
        db_path,
        deal_id=str(deal_id),
        event_type="communication_completed",
        quick_help_id=int(quick_help_id),
        payload=None,
    )
