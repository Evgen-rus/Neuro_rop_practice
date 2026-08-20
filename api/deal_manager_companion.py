"""Explicit post-call companion message jobs for the manager deal screen.

The button starts the existing change-aware analyze job with force_llm=False.
That job first reads Bitrix for this deal, then the decision engine chooses
FULL / MINI / skip. Last contact is taken from the refreshed workspace, not from
a stale local snapshot. A small companion LLM runs only after that, and only if
the last contact plus current report are not already cached.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import _communication_datetime, _load_local_communications
from api.deal_manager_situation import DEFAULT_DB_PATH, _safe_model_meta, _storage_call, load_manager_screen_context
from api.jobs import AnalyzeOptions, busy_analyze_entity_ids, list_jobs, start_analyze_job, wait_for_job
from bitrix.workspace import deal_workspace_dir
from openai_api.llm.deal_manager_companion import generate_deal_manager_companion
from setup import MSK_TZ


ANALYZE_TIMEOUT_SECONDS = 50 * 60
MISSING_CONTACT = "Нет данных"
INVENTED_CONTENT_KEYS = (
    "text", "body", "html", "transcript", "audio", "message", "description", "content", "preview",
)


@dataclass
class DealManagerCompanionJob:
    job_id: str
    deal_id: str
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Обновляем данные из Bitrix"
    percent: int = 5
    companion_id: int | None = None
    reused: bool = False
    analysis_started: bool = False
    analysis_decision: str | None = None
    missing_reason: str | None = None
    error: str | None = None


_COMPANION_JOBS: dict[str, DealManagerCompanionJob] = {}
_COMPANION_LOCK = threading.Lock()


def _touch(job: DealManagerCompanionJob, stage: str, detail: str, percent: int) -> None:
    with _COMPANION_LOCK:
        job.stage, job.detail, job.percent = stage, detail, percent


def _public_last_contact(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    return {
        "event_id": str(event.get("event_id") or ""),
        "channel": event.get("channel"),
        "direction": event.get("direction"),
        "occurred_at": event.get("occurred_at"),
        "duration_seconds": event.get("duration_seconds"),
        "subject": event.get("subject"),
        "contact_class": event.get("contact_class"),
        "content_available": bool(event.get("content_available")),
    }


def _activity_id(event: dict[str, Any]) -> str:
    source_ids = event.get("source_ids") if isinstance(event.get("source_ids"), list) else []
    if source_ids:
        return str(source_ids[0] or "").strip()
    event_id = str(event.get("event_id") or "")
    if event_id.startswith("crm_activity:"):
        return event_id.split(":", 1)[1].strip()
    return event_id.strip()


def _transcript_excerpt(deal_id: str, activity_id: str) -> str | None:
    if not activity_id:
        return None
    transcripts_dir = deal_workspace_dir(str(deal_id)) / "transcripts"
    if not transcripts_dir.is_dir():
        return None
    matches = sorted(transcripts_dir.glob(f"call_{activity_id}_*"))
    for path in matches:
        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                text = str(payload.get("text") or payload.get("transcript") or "").strip()
            else:
                text = path.read_text(encoding="utf-8").strip()
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if text:
            return text[:2500]
    return None


def _event_content(deal_id: str, event: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    channel = str(event.get("channel") or "")
    if channel == "call":
        excerpt = _transcript_excerpt(deal_id, _activity_id(event))
        if excerpt:
            return True, "transcript", excerpt
        return False, None, None
    raw = str(event.get("content") or event.get("preview") or "").strip()
    if raw:
        return True, "message_text", raw[:2500]
    return False, None, None


def find_last_contact(deal_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    current = current.astimezone(MSK_TZ)
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for item in _load_local_communications(str(deal_id)):
        occurred_at = _communication_datetime(item)
        if occurred_at is None or occurred_at > current:
            continue
        dated.append((occurred_at, item))
    if not dated:
        return None
    dated.sort(key=lambda pair: pair[0], reverse=True)
    event = dict(dated[0][1])
    available, kind, excerpt = _event_content(deal_id, event)
    public = _public_last_contact({
        **event,
        "content_available": available,
    }) or {}
    return {
        **public,
        "content_kind": kind,
        "content_excerpt": excerpt,
    }


def _prompt_last_contact(event: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "event_id": event.get("event_id"),
        "channel": event.get("channel"),
        "direction": event.get("direction"),
        "occurred_at": event.get("occurred_at"),
        "duration_seconds": event.get("duration_seconds"),
        "subject": event.get("subject"),
        "contact_class": event.get("contact_class"),
        "content_available": bool(event.get("content_available")),
        "content_kind": event.get("content_kind"),
    }
    if event.get("content_available") and event.get("content_excerpt"):
        payload["content_excerpt"] = event.get("content_excerpt")
    for key in INVENTED_CONTENT_KEYS:
        payload.pop(key, None)
    return payload


def _cached(db_path: str | Path, deal_id: str, source_report_id: int, last_event_id: str) -> dict[str, Any] | None:
    return _storage_call(
        "get_deal_manager_companion",
        db_path,
        deal_id=str(deal_id),
        source_report_id=int(source_report_id),
        last_event_id=str(last_event_id),
    )


def _load_context(db_path: str | Path, deal_id: str) -> dict[str, Any] | None:
    try:
        return load_manager_screen_context(db_path, str(deal_id), require_confirmed_situation=False)
    except ValueError:
        return None


def get_companion_workspace(*, db_path: str | Path = DEFAULT_DB_PATH, deal_id: str) -> dict[str, Any]:
    last_contact = find_last_contact(str(deal_id))
    context = _load_context(db_path, str(deal_id))
    companion = None
    source_report_id = context.get("source_report_id") if isinstance(context, dict) else None
    if last_contact and source_report_id:
        companion = _cached(db_path, str(deal_id), int(source_report_id), str(last_contact.get("event_id") or ""))
    return {
        "last_contact": _public_last_contact(last_contact),
        "companion": companion,
        "source_report_id": source_report_id,
    }


def get_companion_job(job_id: str) -> dict[str, Any] | None:
    with _COMPANION_LOCK:
        job = _COMPANION_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _find_busy_analyze_job(deal_id: str) -> str | None:
    if str(deal_id) not in busy_analyze_entity_ids("deal"):
        return None
    for item in list_jobs(10000):
        if item.get("status") not in {"queued", "running"}:
            continue
        ids = (item.get("options") or {}).get("ids") or []
        if str(deal_id) in {str(value) for value in ids}:
            return str(item.get("job_id") or "") or None
    return None


def _analysis_decision(analyze_job: dict[str, Any], deal_id: str) -> str:
    progress = analyze_job.get("entity_progress") if isinstance(analyze_job.get("entity_progress"), dict) else {}
    item: dict[str, Any] = {}
    for value in progress.values():
        if isinstance(value, dict) and str(value.get("entity_id") or "") == str(deal_id):
            item = value
            break
    stage = str(item.get("stage") or item.get("status") or "").lower()
    if stage in {"skipped", "skip"} or "skip" in stage:
        return "skip"
    if "mini" in stage:
        return "mini"
    return "full"


def _run_analyze(deal_id: str) -> tuple[dict[str, Any], bool]:
    existing_id = _find_busy_analyze_job(deal_id)
    if existing_id:
        return wait_for_job(existing_id, timeout_seconds=ANALYZE_TIMEOUT_SECONDS), False
    started = start_analyze_job(
        AnalyzeOptions(
            entity_type="deal",
            ids=[str(deal_id)],
            history_days=60,
            include_related=True,
            include_internal=True,
            download_audio=True,
            redownload_audio=False,
            transcribe_audio=True,
            analyze=True,
            force_llm=False,
            transcript_mode="all",
        )
    )
    return wait_for_job(str(started["job_id"]), timeout_seconds=ANALYZE_TIMEOUT_SECONDS), True


def _finish_missing(job: DealManagerCompanionJob, reason: str) -> None:
    with _COMPANION_LOCK:
        job.status = "done"
        job.missing_reason = reason
    _touch(job, "done", reason, 100)


def _run(job_id: str, db_path: str | Path, regenerate: bool, manager_note: str) -> None:
    with _COMPANION_LOCK:
        job = _COMPANION_JOBS[job_id]
        job.status = "running"
    note = str(manager_note or "").strip()[:4000]
    rewrite_only = bool(note)
    try:
        if rewrite_only:
            _touch(job, "llm", "Переписываем сообщение по уточнению менеджера", 28)
        else:
            _touch(job, "bitrix", "Обновляем данные из Bitrix", 12)
            analyze_job, started = _run_analyze(job.deal_id)
            with _COMPANION_LOCK:
                job.analysis_started = started
                job.analysis_decision = _analysis_decision(analyze_job, job.deal_id)
            if analyze_job.get("status") == "error":
                raise RuntimeError(analyze_job.get("error") or "Анализ сделки не завершился")
            _touch(job, "contact", "Ищем последнюю коммуникацию", 48)
        last_contact = find_last_contact(job.deal_id)
        if not last_contact or not last_contact.get("event_id"):
            _finish_missing(job, MISSING_CONTACT)
            return
        context = _load_context(db_path, job.deal_id)
        if not isinstance(context, dict) or not context.get("source_report_id"):
            _finish_missing(job, MISSING_CONTACT)
            return
        source_report_id = int(context["source_report_id"])
        last_event_id = str(last_contact.get("event_id") or "")
        cached = _cached(db_path, job.deal_id, source_report_id, last_event_id)
        existing = None if regenerate or rewrite_only else cached
        if isinstance(existing, dict):
            with _COMPANION_LOCK:
                job.companion_id, job.reused, job.status = int(existing["id"]), True, "done"
            _touch(job, "done", "Открываем сохранённый сопроводительный текст", 100)
            return
        previous_message = ""
        if isinstance(cached, dict):
            content = cached.get("content") if isinstance(cached.get("content"), dict) else {}
            previous_message = str(content.get("message_text") or "").strip()
        _touch(job, "llm", "Формируем сообщение клиенту" if not rewrite_only else "Переписываем сообщение клиенту", 72)
        companion, metadata = generate_deal_manager_companion(
            analysis_projection=context["analysis_projection"],
            situation_projection=context["situation_projection"],
            deal=context["deal"],
            current_bitrix_task=context["current_bitrix_task"],
            last_contact=_prompt_last_contact(last_contact),
            manager_note=note,
            previous_message=previous_message,
        )
        _touch(job, "saving", "Сохраняем проверенный текст", 90)
        saved = _storage_call(
            "save_deal_manager_companion",
            db_path,
            deal_id=job.deal_id,
            source_report_id=source_report_id,
            last_event_id=last_event_id,
            companion_json=companion,
            model_meta=_safe_model_meta(metadata),
        )
        with _COMPANION_LOCK:
            job.companion_id, job.status = int(saved["id"]), "done"
            if companion.get("insufficient_reason"):
                job.missing_reason = MISSING_CONTACT
        _touch(job, "done", "Сопроводительный текст готов", 100)
    except Exception as error:  # noqa: BLE001
        with _COMPANION_LOCK:
            job.status = "error"
            job.error = f"{error.__class__.__name__}: операция не выполнена"
        _touch(job, "error", "Не удалось подготовить сопроводительный текст", 100)


def start_companion_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    confirm_paid: bool,
    regenerate: bool = False,
    manager_note: str = "",
) -> dict[str, Any]:
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для сопроводительного текста")
    with _COMPANION_LOCK:
        active = next(
            (item for item in _COMPANION_JOBS.values() if item.deal_id == str(deal_id) and item.status in {"queued", "running"}),
            None,
        )
        if active:
            return asdict(active)
        job = DealManagerCompanionJob(uuid.uuid4().hex, str(deal_id))
        _COMPANION_JOBS[job.job_id] = job
    threading.Thread(
        target=_run,
        args=(job.job_id, db_path, regenerate, str(manager_note or "")),
        daemon=True,
    ).start()
    return asdict(job)
