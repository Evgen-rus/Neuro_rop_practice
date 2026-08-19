"""
Background analyze jobs that wrap existing CLI orchestration.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bitrix.customer_history import build_normalized_communications
from openai_api.bitrix_links import bitrix_entity_url
from openai_api.llm.validation import validate_deal_recommendation_materialization
from progress_events import (
    DECISION_STATUS_ERROR,
    DECISION_STATUS_FULL,
    DECISION_STATUS_MINI,
    DECISION_STATUS_SKIP,
    PROGRESS_PREFIX,
    compact_decision_status,
    progress_key,
)
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    apply_deal_daily_checklist_update,
    apply_deal_recommendation_feedback,
    complete_daily_summary_item,
    get_automatic_analysis_item,
    get_latest_ui_report,
    get_or_create_ui_report_for_analysis_run,
    get_ui_report_by_analysis_run_id,
    materialize_deal_recommendation_from_report,
    register_daily_summary_job,
    record_daily_summary_actual_cost,
    save_ui_report,
    update_automatic_analysis_item,
    update_daily_summary_item_progress,
)


PROJECT_ROOT = BASE_DIR
PYTHON = sys.executable
MAX_JOB_LOG_LINES = 120
MAX_JOB_LOG_LINE_CHARS = 1200


@dataclass
class AnalyzeOptions:
    entity_type: str  # lead | deal | auto
    ids: list[str]
    history_days: int = 60
    include_related: bool = True
    include_internal: bool = True
    download_audio: bool = True
    redownload_audio: bool = False
    transcribe_audio: bool = True
    analyze: bool = True
    force_llm: bool = False
    transcript_mode: str = "all"
    daily_summary_run_id: int | None = None
    automatic_analysis_run_id: int | None = None
    extra_env: dict[str, str] | None = None


@dataclass
class JobState:
    job_id: str
    status: str = "queued"  # queued|running|done|error
    created_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    options: dict[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    current_stage: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    report_ids: list[int] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    entity_progress: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


_JOBS: dict[str, JobState] = {}
_LOCK = threading.Lock()


def _touch(job: JobState) -> None:
    job.updated_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")


def _set_stage(job: JobState, key: str, label: str, status: str, detail: str = "") -> None:
    now = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    existing = next((item for item in job.stages if item.get("key") == key), None)
    if existing:
        existing["status"] = status
        existing["label"] = label
        existing["detail"] = detail
        existing["updated_at"] = now
    else:
        job.stages.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "detail": detail,
                "updated_at": now,
            }
        )
    job.current_stage = key if status == "running" else job.current_stage
    _touch(job)


def parse_progress_event(text: str) -> dict[str, Any] | None:
    marker = text.find(PROGRESS_PREFIX)
    if marker < 0:
        return None
    raw = text[marker + len(PROGRESS_PREFIX):].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    entity_type = str(value.get("entity_type") or "")
    entity_id = str(value.get("entity_id") or "")
    stage = str(value.get("stage") or "")
    if entity_type not in {"lead", "deal"} or not entity_id or not stage:
        return None
    return value


def _apply_progress_event(job: JobState, event: dict[str, Any]) -> None:
    key = progress_key(str(event.get("entity_type")), str(event.get("entity_id")))
    previous = job.entity_progress.get(key) or {}
    started_at = previous.get("started_at") or event.get("updated_at") or datetime.now(MSK_TZ).isoformat(timespec="seconds")
    meaningful_event = {field: value for field, value in event.items() if value is not None}
    job.entity_progress[key] = {
        **previous,
        **meaningful_event,
        "key": key,
        "started_at": started_at,
    }
    _touch(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


def wait_for_job(job_id: str, *, timeout_seconds: float = 3000, poll_seconds: float = 1.0) -> dict[str, Any]:
    """Block until an in-memory analyze job finishes. Used by the daytime scheduler."""
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    pause = max(0.05, float(poll_seconds))
    while True:
        job = get_job(job_id)
        if job is None:
            raise RuntimeError(f"Задание {job_id} не найдено")
        if job.get("status") in {"done", "error"}:
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Задание {job_id} не завершилось за {timeout_seconds:.0f} с")
        time.sleep(pause)


def busy_analyze_entity_ids(entity_type: str) -> set[str]:
    """Entity IDs already queued or running, so a scheduler must not start a second pipeline."""
    with _LOCK:
        busy: set[str] = set()
        for job in _JOBS.values():
            if job.status not in {"queued", "running"}:
                continue
            options = job.options or {}
            job_type = str(options.get("entity_type") or "")
            if job_type not in {entity_type, "auto"}:
                continue
            busy.update(str(item) for item in options.get("ids") or [] if str(item).strip())
        return busy


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _LOCK:
        rows = sorted(_JOBS.values(), key=lambda item: item.created_at, reverse=True)
        return [asdict(item) for item in rows[:limit]]


def parse_ids(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        text = "\n".join(str(item) for item in raw)
    else:
        text = str(raw or "")
    parts = []
    for chunk in text.replace(";", ",").replace("\r", "\n").split("\n"):
        for token in chunk.split(","):
            value = token.strip()
            if value:
                parts.append(value)
    # unique preserve order
    seen: set[str] = set()
    result: list[str] = []
    for item in parts:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_entity_type(entity_type: str, entity_id: str) -> str:
    """Auto: try lead first, then deal."""
    if entity_type in {"lead", "deal"}:
        return entity_type
    from bitrix.client import BitrixReadOnlyClient, get_env_required

    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"))
    lead = client.safe_call("crm.lead.get", {"id": entity_id})
    if lead.get("ok") and isinstance((lead.get("response") or {}).get("result"), dict):
        return "lead"
    deal = client.safe_call("crm.deal.get", {"id": entity_id})
    if deal.get("ok") and isinstance((deal.get("response") or {}).get("result"), dict):
        return "deal"
    raise RuntimeError(f"Не удалось определить тип сущности для ID {entity_id}")


def workspace_dir(entity_type: str, entity_id: str) -> Path:
    folder = "deals" if entity_type == "deal" else "leads"
    return PROJECT_ROOT / "reports" / "rop_assistant" / folder / f"{entity_type}_{entity_id}"


def analysis_paths(entity_type: str, entity_id: str) -> dict[str, Path]:
    analysis_dir = workspace_dir(entity_type, entity_id) / "analysis"
    return {
        "analysis_json": analysis_dir / f"{entity_type}_{entity_id}_analysis.json",
        "report_md": analysis_dir / f"{entity_type}_{entity_id}_rop_report.md",
        "raw_output": analysis_dir / f"{entity_type}_{entity_id}_raw_model_output.txt",
        "error_json": analysis_dir / f"{entity_type}_{entity_id}_analysis_error.json",
    }


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _response_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    response = payload.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    return result if isinstance(result, dict) else {}


def _lead_stage_name(status_id: str) -> str | None:
    mapping = _load_json_object(PROJECT_ROOT / "crm_pipeline_map.json")
    pipeline = mapping.get("lead_pipeline") if isinstance(mapping.get("lead_pipeline"), dict) else {}
    for item in pipeline.get("stages") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("status_id") or item.get("STATUS_ID") or item.get("id") or "")
        if item_id == status_id:
            return str(item.get("name") or item.get("NAME") or status_id)
    return None


def _short_text(value: Any, limit: int = 600) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


_NO_CONTACT_TRANSCRIPT_MARKERS = (
    "абонент недоступен",
    "временно недоступен",
    "не может ответить",
    "оставьте сообщение",
    "после звукового сигнала",
    "голосовая почта",
    "автоответчик",
)


def _call_transcript(lead_id: str, call_id: str) -> str | None:
    if not call_id:
        return None
    transcript_dir = workspace_dir("lead", lead_id) / "transcripts"
    candidates = sorted(transcript_dir.glob(f"call_{call_id}_*_transcript.json"))
    candidates.extend(sorted(transcript_dir.glob(f"call_{call_id}_*_transcript.txt")))
    for path in candidates:
        try:
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                text = payload.get("text") if isinstance(payload, dict) else None
            else:
                text = path.read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            continue
        normalized = str(text or "").strip()
        if normalized:
            return normalized
    return None


def _transcript_confirms_contact(text: str | None, duration_seconds: float | None) -> bool:
    if not text or (duration_seconds is not None and duration_seconds < 20):
        return False
    normalized = " ".join(text.lower().split())
    if len(normalized) < 80:
        return False
    if any(marker in normalized for marker in _NO_CONTACT_TRANSCRIPT_MARKERS):
        return False
    return True


def _communication_snapshot(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    channel = str(event.get("channel") or "unknown")
    channel_label = {
        "call": "Звонок",
        "email": "Письмо",
        "whatsapp": "WhatsApp",
        "max": "Max",
        "telegram": "Telegram",
        "message": "Сообщение",
        "internal_comment": "Комментарий CRM",
        "internal_chat": "Внутренний чат",
        "unknown": "Коммуникация",
    }.get(channel, channel)
    direction_label = {
        "incoming": "входящий",
        "outgoing": "исходящий",
        "internal": "внутренний",
        "unknown": "направление не определено",
    }.get(str(event.get("direction") or "unknown"), "направление не определено")
    contact_label = {
        "attempt": "Попытка связи",
        "confirmed_contact": "Подтверждённый контакт",
        "internal_information": "Внутренняя информация",
        "unknown": "Результат не определён",
    }.get(str(event.get("contact_class") or "unknown"), "Результат не определён")
    duration = event.get("duration_seconds")
    return {
        "event_id": _short_text(event.get("event_id"), 160),
        "type": channel_label,
        "channel": channel,
        "direction": str(event.get("direction") or "unknown"),
        "direction_label": direction_label,
        "date": _short_text(event.get("occurred_at"), 80),
        "subject": _short_text(event.get("subject"), 240),
        "text": _short_text(event.get("content"), 4000),
        "participant_name": _short_text(event.get("participant_name"), 240),
        "source_label": _short_text(event.get("source_label"), 240),
        "contact_class": str(event.get("contact_class") or "unknown"),
        "contact_label": contact_label,
        "classification_reason": _short_text(event.get("classification_reason"), 500),
        "duration_seconds": round(float(duration), 1) if isinstance(duration, (int, float)) else None,
        "has_transcript": bool(event.get("has_transcript")),
        "transcript_text": _short_text(event.get("transcript_text"), 12000),
    }


def build_lead_communication_summary(lead_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    events = bundle.get("normalized_communications")
    if not isinstance(events, list):
        events = build_normalized_communications(bundle)
    normalized_events = [dict(item) for item in events if isinstance(item, dict)]
    for event in normalized_events:
        if event.get("channel") != "call":
            continue
        transcript = _call_transcript(lead_id, str(event.get("call_id") or ""))
        event["transcript_text"] = transcript
        event["has_transcript"] = bool(transcript)
        duration = event.get("duration_seconds")
        duration_value = float(duration) if isinstance(duration, (int, float)) else None
        if _transcript_confirms_contact(transcript, duration_value):
            event["contact_class"] = "confirmed_contact"
            event["evidence_level"] = "direct"
            event["classification_reason"] = "Есть содержательная расшифровка разговора с клиентом."
        elif duration_value is not None and duration_value < 20:
            event["contact_class"] = "attempt"
            event["classification_reason"] = "Звонок короче 20 секунд: подтверждённый разговор не установлен."
        elif transcript:
            event["contact_class"] = "attempt"
            event["classification_reason"] = "Расшифровка не подтверждает содержательный разговор с клиентом."
        else:
            event["contact_class"] = "attempt"
            event["classification_reason"] = "Нет доступной расшифровки, поэтому результат звонка не подтверждён."

    def event_time(item: dict[str, Any]) -> str:
        return str(item.get("occurred_at") or "")

    attempts = [
        item for item in normalized_events
        if item.get("contact_class") in {"attempt", "confirmed_contact"}
        and item.get("direction") in {"outgoing", "unknown"}
    ]
    confirmed = [item for item in normalized_events if item.get("contact_class") == "confirmed_contact"]
    internal = [item for item in normalized_events if item.get("contact_class") == "internal_information"]
    return {
        "last_attempt": _communication_snapshot(max(attempts, key=event_time, default=None)),
        "last_confirmed_contact": _communication_snapshot(max(confirmed, key=event_time, default=None)),
        "last_internal_information": _communication_snapshot(max(internal, key=event_time, default=None)),
    }


def build_lead_report_meta(lead_id: str) -> dict[str, Any] | None:
    bundle_path = workspace_dir("lead", lead_id) / "raw" / f"lead_{lead_id}_customer_history_bundle.json"
    bundle = _load_json_object(bundle_path)
    if not bundle:
        context_path = workspace_dir("lead", lead_id) / "raw" / f"lead_{lead_id}_context.json"
        context = _load_json_object(context_path)
        lead = _response_result(context.get("lead"))
        touchpoints: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
    else:
        lead = _response_result(bundle.get("lead"))
        touchpoints = [item for item in bundle.get("client_touchpoints") or [] if isinstance(item, dict)]
        tasks = [item for item in bundle.get("tasks_and_control") or [] if isinstance(item, dict)]
    if not lead and not bundle:
        return None

    def activity_time(item: dict[str, Any]) -> str:
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        return str(item.get("when") or raw.get("DEADLINE") or raw.get("START_TIME") or raw.get("LAST_UPDATED") or "")

    last_contact = max(touchpoints, key=activity_time, default=None)
    communication_summary = build_lead_communication_summary(lead_id, bundle) if bundle else {}
    open_tasks = [item for item in tasks if not bool(item.get("completed"))]
    current_task = max(open_tasks or tasks, key=activity_time, default=None)

    def activity_snapshot(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if not item:
            return None
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        activity_type = str(item.get("event_type") or item.get("category") or "").lower()
        type_label = {
            "call": "Звонок",
            "email": "Письмо",
            "message": "Сообщение",
            "task": "Задача",
            "comment": "Комментарий",
        }.get(activity_type, activity_type)
        return {
            "type": _short_text(type_label, 80),
            "date": _short_text(item.get("when") or raw.get("DEADLINE") or raw.get("START_TIME"), 80),
            "subject": _short_text(item.get("subject"), 240),
            "text": _short_text(item.get("text") or raw.get("DESCRIPTION"), 600),
            "completed": bool(item.get("completed")),
        }

    status_id = str(lead.get("STATUS_ID") or "")
    client_name = " ".join(str(lead.get(key) or "") for key in ("NAME", "LAST_NAME")).strip()
    return {
        "client_name": _short_text(client_name or lead.get("TITLE"), 240),
        "lead_title": _short_text(lead.get("TITLE"), 240),
        "lead_created_at": _short_text(lead.get("DATE_CREATE"), 80),
        "lead_modified_at": _short_text(lead.get("DATE_MODIFY"), 80),
        "manager_id": _short_text(lead.get("ASSIGNED_BY_ID"), 80),
        "stage_id": status_id or None,
        "stage_name": _lead_stage_name(status_id) or status_id or None,
        "last_contact": activity_snapshot(last_contact),
        "last_attempt": communication_summary.get("last_attempt"),
        "last_confirmed_contact": communication_summary.get("last_confirmed_contact"),
        "last_internal_information": communication_summary.get("last_internal_information"),
        "current_task": activity_snapshot(current_task),
        "snapshot_generated_at": _short_text(bundle.get("generated_at"), 80),
    }


_SENSITIVE_LOG_RE = re.compile(
    r"(?i)(https?://\S+|(?:webhook|token|secret|api[_-]?key|authorization)\s*[:=]\s*\S+)"
)


def build_technical_log_snapshot(job: JobState, entity_type: str, entity_id: str) -> dict[str, Any]:
    def clean(value: Any, limit: int) -> str:
        return _SENSITIVE_LOG_RE.sub("[скрыто]", str(value or ""))[:limit]

    key = progress_key(entity_type, entity_id)
    progress = job.entity_progress.get(key) or {}
    return {
        "job_id": job.job_id,
        "status": job.status,
        "current_stage": job.current_stage,
        "stages": [
            {
                "key": clean(item.get("key"), 80),
                "label": clean(item.get("label"), 160),
                "status": clean(item.get("status"), 40),
                "detail": clean(item.get("detail"), 400),
                "updated_at": clean(item.get("updated_at"), 80),
            }
            for item in job.stages[-20:]
        ],
        "entity_progress": {
            key: clean(value, 500) for key, value in progress.items()
            if key in {"stage", "status", "detail", "error", "updated_at", "attempt", "max_attempts"}
        },
        "log_tail": [clean(line, 500) for line in job.logs[-40:]],
    }


def build_model_context_snapshot(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the factual files used by one completed analysis, without prompts or OKF rules."""
    if not isinstance(envelope, dict):
        return None
    input_files = envelope.get("input_files")
    if not isinstance(input_files, dict):
        return None

    def read_source(key: str) -> str | None:
        raw_path = input_files.get(key)
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            return Path(raw_path).read_text(encoding="utf-8")
        except OSError:
            return None

    history_text = read_source("history")
    transcript_text = read_source("transcript")
    if history_text is None and transcript_text is None:
        return None
    return {
        "history_text": history_text,
        "transcript_text": transcript_text,
        "transcript_used": transcript_text is not None,
    }


def unwrap_analysis_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """
    LLM files are saved as envelope:
    {generated_at, input_files, model_metadata, analysis: {...real fields...}}.

    UI/API need the inner analysis object. If payload is already unwrapped, return as is.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("analysis")
    if isinstance(inner, dict) and (
        "rop_manager_message_block" in inner
        or "main_risk" in inner
        or "lead_state" in inner
        or "deal_state" in inner
        or "loss_diagnosis" in inner
        or "money_path_diagnosis" in inner
    ):
        return inner
    # Already a flat analysis object.
    if (
        "rop_manager_message_block" in payload
        or "main_risk" in payload
        or "lead_state" in payload
        or "deal_state" in payload
    ):
        return payload
    return payload


def extract_lead_qualification_summary(analysis: dict[str, Any]) -> dict[str, Any] | None:
    analysis = unwrap_analysis_payload(analysis)
    assessment = analysis.get("qualification_assessment")
    if not isinstance(assessment, dict):
        return None
    bant = assessment.get("bant") if isinstance(assessment.get("bant"), dict) else {}
    category = assessment.get("lead_category") if isinstance(assessment.get("lead_category"), dict) else {}
    route = assessment.get("lead_route") if isinstance(assessment.get("lead_route"), dict) else {}
    timeframe = bant.get("timeframe") if isinstance(bant.get("timeframe"), dict) else {}
    lead_state = analysis.get("lead_state") if isinstance(analysis.get("lead_state"), dict) else {}
    statuses = {
        key: str(value.get("status") or "unknown")
        for key in ("budget", "authority", "need", "timeframe")
        if isinstance((value := bant.get(key)), dict)
    }
    for key in ("budget", "authority", "need", "timeframe"):
        statuses.setdefault(key, "unknown")
    confirmed_count = sum(1 for status in statuses.values() if status == "confirmed")
    return {
        "category": str(category.get("value") or lead_state.get("qualification") or "unknown"),
        "overall_status": str(bant.get("overall_status") or "unknown"),
        "confirmed_count": confirmed_count,
        "total_count": 4,
        "statuses": statuses,
        "decision_timing": timeframe.get("decision_timing"),
        "need_or_launch_timing": timeframe.get("need_or_launch_timing"),
        "route_status": str(route.get("status") or "unknown"),
        "controlled_return_status": str(route.get("controlled_return_status") or "unknown"),
        "controlled_return_date": route.get("controlled_return_date"),
        "recommended_return_date": route.get("recommended_return_date"),
    }


def extract_summary_fields(analysis: dict[str, Any], entity_type: str) -> dict[str, Any]:
    analysis = unwrap_analysis_payload(analysis)
    risk = None
    attention = None
    action = None
    lead_category = None
    lead_route_status = None
    lead_qualification = None
    main_risk = analysis.get("main_risk") if isinstance(analysis.get("main_risk"), dict) else {}
    if main_risk:
        risk = str(main_risk.get("risk_level") or "") or None
        attention = str(main_risk.get("description") or main_risk.get("risk_type") or "") or None
    rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
    if rop:
        action = str(rop.get("check_for_rop") or rop.get("message_to_manager") or "") or None
        if not attention:
            attention = str(rop.get("why_it_matters") or "") or None
    if entity_type == "lead":
        assessment = analysis.get("qualification_assessment") if isinstance(analysis.get("qualification_assessment"), dict) else {}
        category = assessment.get("lead_category") if isinstance(assessment.get("lead_category"), dict) else {}
        route = assessment.get("lead_route") if isinstance(assessment.get("lead_route"), dict) else {}
        lead_state = analysis.get("lead_state") if isinstance(analysis.get("lead_state"), dict) else {}
        lead_category = str(category.get("value") or lead_state.get("qualification") or "") or None
        lead_route_status = str(route.get("status") or "") or None
        lead_qualification = extract_lead_qualification_summary(analysis)
        loss = analysis.get("loss_diagnosis") if isinstance(analysis.get("loss_diagnosis"), dict) else {}
        if loss and not attention:
            attention = str(loss.get("final_verdict") or "") or None
        if lead_state and not attention:
            attention = str(lead_state.get("summary") or "") or None
    else:
        deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
        if deal_state and not attention:
            attention = str(deal_state.get("summary") or "") or None
    return {
        "risk_level": risk,
        "attention_reason": attention,
        "recommended_action": action,
        "lead_category": lead_category,
        "lead_route_status": lead_route_status,
        "lead_qualification": lead_qualification,
    }


def run_command(
    command: list[str],
    on_line: Callable[[str], None] | None = None,
    extra_env: dict[str, str] | None = None,
) -> None:
    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        if on_line and text:
            on_line(text)
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"Команда завершилась с кодом {code}: {' '.join(command)}")


def build_cli_command(options: AnalyzeOptions, entity_type: str, ids: list[str]) -> list[str]:
    command = [
        PYTHON,
        str(PROJECT_ROOT / "run_rop_assistant.py"),
        "--entity",
        entity_type,
        "--ids",
        *ids,
        "--history-days",
        str(options.history_days),
        "--yes",
    ]
    if not options.include_related:
        command.append("--no-related")
    if not options.include_internal:
        command.append("--no-internal")
    if not options.download_audio:
        command.append("--skip-audio-download")
    if options.redownload_audio:
        command.append("--redownload-audio")
    if not options.transcribe_audio:
        command.append("--no-transcribe")
    if not options.analyze:
        command.append("--no-analyze")
    if not options.force_llm:
        command.append("--no-force-llm")
    command.extend(["--transcript", options.transcript_mode])
    return command


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_publish_error(error: BaseException) -> str:
    return f"Ошибка публикации: {type(error).__name__}"


def _upsert_job_result(job: JobState, result: dict[str, Any]) -> None:
    entity_type = str(result.get("entity_type") or "")
    entity_id = str(result.get("entity_id") or "")
    replaced = False
    for index, existing in enumerate(job.results):
        if str(existing.get("entity_type") or "") == entity_type and str(existing.get("entity_id") or "") == entity_id:
            job.results[index] = result
            replaced = True
            break
    if not replaced:
        job.results.append(result)
    report_id = result.get("report_id")
    if isinstance(report_id, int) and report_id not in job.report_ids:
        job.report_ids.append(report_id)
    _touch(job)


def _load_analysis_envelope(
    entity_type: str,
    entity_id: str,
) -> tuple[dict[str, Path], dict[str, Any] | None, dict[str, Any]]:
    paths = analysis_paths(entity_type, entity_id)
    envelope: dict[str, Any] | None = None
    if paths["analysis_json"].exists():
        try:
            loaded = json.loads(paths["analysis_json"].read_text(encoding="utf-8"))
            envelope = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            envelope = None
    model_metadata = (
        envelope.get("model_metadata")
        if isinstance(envelope, dict) and isinstance(envelope.get("model_metadata"), dict)
        else {}
    )
    return paths, envelope, model_metadata


def _apply_deal_control_projections(entity_id: str, report_id: int, analysis: dict[str, Any]) -> None:
    apply_deal_recommendation_feedback(
        DEFAULT_DB_PATH,
        entity_id,
        analysis.get("recommendation_feedback"),
        report_id,
        analysis,
    )
    manager_action = analysis.get("manager_action_block")
    fallback_checklist = manager_action.get("manager_checklist") if isinstance(manager_action, dict) else []
    apply_deal_daily_checklist_update(
        DEFAULT_DB_PATH,
        deal_id=entity_id,
        source_report_id=report_id,
        update=analysis.get("daily_checklist_update"),
        fallback_items=[
            {"text": str(text), "source": "crm"}
            for text in (fallback_checklist or [])
            if str(text).strip()
        ],
    )
    materialized_task = materialize_deal_recommendation_from_report(
        DEFAULT_DB_PATH,
        entity_id,
        report_id,
        analysis,
    )
    if materialized_task is None:
        raise RuntimeError("Deal recommendation was saved but could not be materialized")


def _sync_automatic_analysis_item(
    run_id: int | None,
    *,
    entity_id: str,
    stage: str | None,
    decision_status: str | None = None,
    analysis_run_id: int | None = None,
    report_id: int | None = None,
    error: str | None = None,
    publication_status: str | None = None,
) -> None:
    if run_id is None:
        return
    update_automatic_analysis_item(
        DEFAULT_DB_PATH,
        int(run_id),
        entity_type="deal",
        entity_id=str(entity_id),
        stage=stage,
        decision_status=decision_status,
        analysis_run_id=analysis_run_id,
        report_id=report_id,
        error=error,
        publication_status=publication_status,
        current_stage=stage,
    )


def _finish_daily_summary_item(
    job: JobState,
    *,
    entity_type: str,
    entity_id: str,
    report_id: int | None,
    progress: dict[str, Any],
    analysis: dict[str, Any] | None,
    model_metadata: dict[str, Any],
) -> dict[str, Any] | None:
    run_id = job.options.get("daily_summary_run_id")
    actual_cost = None
    if progress.get("attempt") and model_metadata:
        actual_cost = {
            "estimated_cost_usd": model_metadata.get("estimated_cost_usd"),
            "estimated_cost_rub": model_metadata.get("estimated_cost_rub"),
            "semantic_attempt_count": model_metadata.get("semantic_attempt_count", 1),
        }
        if run_id:
            record_daily_summary_actual_cost(
                DEFAULT_DB_PATH,
                int(run_id),
                entity_type=entity_type,
                entity_id=entity_id,
                cost=actual_cost,
            )
    if run_id:
        progress_error = progress.get("error") if progress.get("status") == "error" else None
        if analysis is None and not progress_error:
            progress_error = "Анализ не сформирован"
        complete_daily_summary_item(
            DEFAULT_DB_PATH,
            int(run_id),
            entity_type=entity_type,
            entity_id=entity_id,
            report_id=report_id,
            error=str(progress_error) if progress_error else None,
        )
    return actual_cost


def _publish_deal_result(job: JobState, entity_id: str, *, allow_raise: bool = True) -> None:
    """Publish one deal from a terminal progress event. A stale analysis file is not treated as FULL."""
    key = progress_key("deal", entity_id)
    progress = dict(job.entity_progress.get(key) or {})
    automatic_run_id = _int_or_none(job.options.get("automatic_analysis_run_id"))
    publish_ready = progress.get("publish_ready") is True
    decision = compact_decision_status(str(progress.get("decision_status") or ""))
    if not decision and str(progress.get("status") or "") == "error":
        decision = DECISION_STATUS_ERROR
    event_run_id = _int_or_none(progress.get("analysis_run_id"))
    paths, envelope, model_metadata = _load_analysis_envelope("deal", entity_id)
    if str(progress.get("status") or "") == "error" and paths["error_json"].exists():
        try:
            error_payload = json.loads(paths["error_json"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            error_payload = {}
        if isinstance(error_payload.get("model_metadata"), dict):
            model_metadata = error_payload["model_metadata"]

    analysis: dict[str, Any] | None = None
    report_id: int | None = None
    publication_status = "pending"
    item_error: str | None = None

    try:
        if not publish_ready:
            if decision == DECISION_STATUS_ERROR or str(progress.get("status") or "") == "error":
                publication_status = "error"
                item_error = str(progress.get("error") or "Анализ не сформирован")
            else:
                publication_status = "pending"
        elif decision == DECISION_STATUS_FULL:
            if event_run_id is None:
                raise RuntimeError("FULL publish_ready without analysis_run_id")
            analysis = unwrap_analysis_payload(envelope) if envelope is not None else None
            if analysis is None:
                raise RuntimeError("FULL analysis file is missing after publish_ready")
            validate_deal_recommendation_materialization(analysis)
            summary = extract_summary_fields(analysis, "deal")
            report, created = get_or_create_ui_report_for_analysis_run(
                DEFAULT_DB_PATH,
                entity_type="deal",
                entity_id=entity_id,
                analysis_run_id=event_run_id,
                risk_level=summary.get("risk_level"),
                attention_reason=summary.get("attention_reason"),
                recommended_action=summary.get("recommended_action"),
                analysis_path=str(paths["analysis_json"]) if paths["analysis_json"].exists() else None,
                report_path=str(paths["report_md"]) if paths["report_md"].exists() else None,
                report_json=analysis,
                technical_log=build_technical_log_snapshot(job, "deal", entity_id),
                model_context=build_model_context_snapshot(envelope),
                job_id=job.job_id,
            )
            report_id = int(report["id"])
            pending = False
            if automatic_run_id is not None:
                item = get_automatic_analysis_item(
                    DEFAULT_DB_PATH,
                    automatic_run_id,
                    entity_type="deal",
                    entity_id=entity_id,
                )
                pending = str((item or {}).get("publication_status") or "") == "pending"
            if created or pending:
                _apply_deal_control_projections(entity_id, report_id, analysis)
            publication_status = "published"
        elif decision in {DECISION_STATUS_MINI, DECISION_STATUS_SKIP}:
            existing_report = get_latest_ui_report(DEFAULT_DB_PATH, entity_type="deal", entity_id=entity_id)
            if existing_report is not None:
                report_id = int(existing_report["id"])
                stored = existing_report.get("report_json")
                analysis = unwrap_analysis_payload(stored if isinstance(stored, dict) else None) or None
            publication_status = "not_applicable"
        else:
            publication_status = "error"
            item_error = str(progress.get("error") or "Анализ не сформирован")
    except Exception as error:
        if allow_raise:
            raise
        publication_status = "pending" if decision == DECISION_STATUS_FULL else "error"
        item_error = _safe_publish_error(error)
        if analysis is None and envelope is not None:
            analysis = unwrap_analysis_payload(envelope) or None

    summary = extract_summary_fields(analysis or {}, "deal")
    result = {
        "entity_type": "deal",
        "entity_id": entity_id,
        "report_id": report_id,
        "has_analysis": analysis is not None,
        "has_markdown": paths["report_md"].exists(),
        "risk_level": summary.get("risk_level"),
        "attention_reason": summary.get("attention_reason"),
        "recommended_action": summary.get("recommended_action"),
        "lead_category": summary.get("lead_category"),
        "lead_route_status": summary.get("lead_route_status"),
        "lead_qualification": summary.get("lead_qualification"),
        "bitrix_url": bitrix_entity_url("deal", entity_id),
        "analysis": analysis,
    }
    result["actual_cost"] = _finish_daily_summary_item(
        job,
        entity_type="deal",
        entity_id=entity_id,
        report_id=report_id,
        progress=progress,
        analysis=analysis,
        model_metadata=model_metadata,
    )
    with _LOCK:
        _upsert_job_result(job, result)
    stored_error = item_error
    if stored_error and entity_id in stored_error:
        stored_error = _safe_publish_error(RuntimeError(stored_error))
    _sync_automatic_analysis_item(
        automatic_run_id,
        entity_id=entity_id,
        stage=str(progress.get("stage") or ("error" if publication_status == "error" else "done")),
        decision_status=decision or None,
        analysis_run_id=event_run_id,
        report_id=report_id,
        error=stored_error or "",
        publication_status=publication_status,
    )


def _collect_lead_results(job: JobState, ids: list[str]) -> None:
    for entity_id in ids:
        paths, envelope, model_metadata = _load_analysis_envelope("lead", entity_id)
        analysis = unwrap_analysis_payload(envelope) if envelope is not None else None
        analysis_run_id = _int_or_none(envelope.get("analysis_run_id") if isinstance(envelope, dict) else None)
        key = progress_key("lead", entity_id)
        progress = job.entity_progress.get(key) or {}
        if progress.get("status") == "error":
            analysis = None
            if paths["error_json"].exists():
                try:
                    error_payload = json.loads(paths["error_json"].read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    error_payload = {}
                if isinstance(error_payload.get("model_metadata"), dict):
                    model_metadata = error_payload["model_metadata"]
        summary = extract_summary_fields(analysis or {}, "lead")
        report_id = None
        if analysis is not None:
            existing_report = (
                get_ui_report_by_analysis_run_id(DEFAULT_DB_PATH, analysis_run_id)
                if analysis_run_id is not None
                else None
            )
            if existing_report is not None:
                report_id = int(existing_report["id"])
            else:
                report_id = save_ui_report(
                    DEFAULT_DB_PATH,
                    entity_type="lead",
                    entity_id=entity_id,
                    risk_level=summary.get("risk_level"),
                    attention_reason=summary.get("attention_reason"),
                    recommended_action=summary.get("recommended_action"),
                    analysis_path=str(paths["analysis_json"]) if paths["analysis_json"].exists() else None,
                    report_path=str(paths["report_md"]) if paths["report_md"].exists() else None,
                    report_json=analysis,
                    report_meta=build_lead_report_meta(entity_id),
                    technical_log=build_technical_log_snapshot(job, "lead", entity_id),
                    model_context=build_model_context_snapshot(envelope),
                    job_id=job.job_id,
                    analysis_run_id=analysis_run_id,
                )
        result = {
            "entity_type": "lead",
            "entity_id": entity_id,
            "report_id": report_id,
            "has_analysis": analysis is not None,
            "has_markdown": paths["report_md"].exists(),
            "risk_level": summary.get("risk_level"),
            "attention_reason": summary.get("attention_reason"),
            "recommended_action": summary.get("recommended_action"),
            "lead_category": summary.get("lead_category"),
            "lead_route_status": summary.get("lead_route_status"),
            "lead_qualification": summary.get("lead_qualification"),
            "bitrix_url": bitrix_entity_url("lead", entity_id),
            "analysis": analysis,
        }
        result["actual_cost"] = _finish_daily_summary_item(
            job,
            entity_type="lead",
            entity_id=entity_id,
            report_id=report_id,
            progress=progress,
            analysis=analysis,
            model_metadata=model_metadata,
        )
        with _LOCK:
            _upsert_job_result(job, result)


def _collect_results(job: JobState, entity_type: str, ids: list[str]) -> None:
    if entity_type == "deal":
        for entity_id in ids:
            _publish_deal_result(job, entity_id, allow_raise=True)
        return
    _collect_lead_results(job, ids)


def _converted_lead_handoffs(lead_ids: list[str]) -> dict[str, str]:
    """Read the just-built local lead bundles; never refetch CRM for UI routing."""
    from run_rop_assistant import converted_lead_deals

    return {
        lead_id: str(deal.get("id"))
        for lead_id, deal in converted_lead_deals(lead_ids).items()
        if deal.get("id")
    }


def _collect_group_results(job: JobState, entity_type: str, ids: list[str]) -> None:
    if entity_type == "lead":
        handoffs = _converted_lead_handoffs(ids)
        remaining_lead_ids = [entity_id for entity_id in ids if entity_id not in handoffs]
        _collect_results(job, "lead", remaining_lead_ids)
        _collect_results(job, "deal", list(handoffs.values()))
        return
    _collect_results(job, entity_type, ids)


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job.status = "running"
        _touch(job)
        options = AnalyzeOptions(**job.options)

    def log_line(text: str) -> None:
        publish_entity_id: str | None = None
        stage_event: dict[str, str] | None = None
        daily_progress: dict[str, Any] | None = None
        daily_run_id: int | None = None
        automatic_run_id: int | None = None
        with _LOCK:
            current = _JOBS[job_id]
            progress_event = parse_progress_event(text)
            if progress_event is not None:
                _apply_progress_event(current, progress_event)
                daily_run_id = _int_or_none(current.options.get("daily_summary_run_id"))
                automatic_run_id = _int_or_none(current.options.get("automatic_analysis_run_id"))
                merged_progress = current.entity_progress.get(
                    progress_key(str(progress_event.get("entity_type")), str(progress_event.get("entity_id")))
                ) or progress_event
                if daily_run_id:
                    daily_progress = dict(merged_progress)
                entity_type = str(progress_event.get("entity_type") or "")
                entity_id = str(progress_event.get("entity_id") or "")
                if progress_event.get("publish_ready") is True and entity_type == "deal" and entity_id:
                    publish_entity_id = entity_id
                elif automatic_run_id is not None and entity_type == "deal" and entity_id:
                    stage_event = {"entity_id": entity_id, "stage": str(progress_event.get("stage") or "")}
            else:
                current.logs.append(text[-MAX_JOB_LOG_LINE_CHARS:])
                if len(current.logs) > MAX_JOB_LOG_LINES:
                    del current.logs[:-MAX_JOB_LOG_LINES]
                if current.stages:
                    current.stages[-1]["detail"] = text[-300:]
                _touch(current)
                return
        if daily_run_id and daily_progress is not None:
            update_daily_summary_item_progress(DEFAULT_DB_PATH, int(daily_run_id), daily_progress)
        if publish_entity_id:
            try:
                _publish_deal_result(_JOBS[job_id], publish_entity_id, allow_raise=False)
            except Exception as publish_error:  # noqa: BLE001 - keep reading stdout
                _sync_automatic_analysis_item(
                    automatic_run_id,
                    entity_id=publish_entity_id,
                    stage="error",
                    error=_safe_publish_error(publish_error),
                    publication_status="pending",
                )
            return
        if automatic_run_id is not None and stage_event is not None:
            _sync_automatic_analysis_item(
                automatic_run_id,
                entity_id=stage_event["entity_id"],
                stage=stage_event["stage"] or None,
            )

    groups: dict[str, list[str]] = {"lead": [], "deal": []}
    collected_groups: set[str] = set()
    try:
        # Group IDs by resolved type for auto mode.
        with _LOCK:
            _set_stage(_JOBS[job_id], "resolve", "Определение типа сущностей", "running")
        for entity_id in options.ids:
            resolved = resolve_entity_type(options.entity_type, entity_id)
            groups[resolved].append(entity_id)
        with _LOCK:
            _set_stage(
                _JOBS[job_id],
                "resolve",
                "Определение типа сущностей",
                "done",
                f"leads={len(groups['lead'])}, deals={len(groups['deal'])}",
            )

        for entity_type, ids in groups.items():
            if not ids:
                continue
            stage_key = f"pipeline_{entity_type}"
            with _LOCK:
                _set_stage(
                    _JOBS[job_id],
                    stage_key,
                    f"Сбор CRM / аудио / транскрипты / анализ ({entity_type})",
                    "running",
                    f"ids={', '.join(ids)}",
                )
            command = build_cli_command(options, entity_type, ids)
            run_command(command, on_line=log_line, extra_env=options.extra_env)
            with _LOCK:
                _set_stage(_JOBS[job_id], stage_key, f"Pipeline {entity_type}", "done")
                _set_stage(_JOBS[job_id], f"collect_{entity_type}", f"Сбор результатов ({entity_type})", "running")
            _collect_group_results(_JOBS[job_id], entity_type, ids)
            with _LOCK:
                collected_groups.add(entity_type)
                _set_stage(_JOBS[job_id], f"collect_{entity_type}", f"Сбор результатов ({entity_type})", "done")

        with _LOCK:
            job = _JOBS[job_id]
            job.status = "done"
            _set_stage(job, "done", "Отчёт готов", "done")
            _touch(job)
    except Exception as error:  # noqa: BLE001 - surface to UI
        job = _JOBS[job_id]
        for entity_type, ids in groups.items():
            if not ids or entity_type in collected_groups:
                continue
            try:
                _collect_group_results(job, entity_type, ids)
            except Exception as collection_error:  # noqa: BLE001 - keep the original job failure visible
                with _LOCK:
                    _set_stage(
                        job,
                        f"collect_{entity_type}",
                        f"Частичный сбор результатов ({entity_type})",
                        "error",
                        f"Не удалось собрать частичные результаты: {collection_error}",
                    )
            else:
                with _LOCK:
                    _set_stage(
                        job,
                        f"collect_{entity_type}",
                        f"Частичный сбор результатов ({entity_type})",
                        "done",
                        "Собраны результаты, созданные до ошибки пакетного запуска.",
                    )
        with _LOCK:
            job = _JOBS[job_id]
            job.status = "error"
            job.error = str(error)
            run_id = job.options.get("daily_summary_run_id")
            if run_id:
                for entity_type, ids in groups.items():
                    for entity_id in ids:
                        key = progress_key(entity_type, entity_id)
                        progress = job.entity_progress.get(key) or {}
                        if progress.get("status") in {"done", "error"}:
                            continue
                        failed_progress = {
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                            "stage": "error",
                            "status": "error",
                            "detail": "Пайплайн завершился с ошибкой",
                            "error": str(error),
                            "updated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
                        }
                        _apply_progress_event(job, failed_progress)
                        complete_daily_summary_item(
                            DEFAULT_DB_PATH,
                            int(run_id),
                            entity_type=entity_type,
                            entity_id=entity_id,
                            report_id=None,
                            error=str(error),
                        )
            _set_stage(job, "error", "Ошибка", "error", str(error))
            job.stages.append(
                {
                    "key": "traceback",
                    "label": "traceback",
                    "status": "error",
                    "detail": traceback.format_exc()[-2000:],
                    "updated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
                }
            )
            _touch(job)


def start_analyze_job(options: AnalyzeOptions) -> dict[str, Any]:
    if not options.ids:
        raise ValueError("Нужен хотя бы один ID")
    if options.entity_type not in {"lead", "deal", "auto"}:
        raise ValueError("entity_type должен быть lead|deal|auto")
    if options.transcript_mode not in {"all", "latest", "none"}:
        raise ValueError("transcript_mode должен быть all|latest|none")

    job_id = uuid.uuid4().hex[:12]
    job = JobState(job_id=job_id, options=asdict(options))
    for entity_id in options.ids:
        key = progress_key(options.entity_type, entity_id)
        job.entity_progress[key] = {
            "key": key,
            "entity_type": options.entity_type,
            "entity_id": str(entity_id),
            "stage": "queued",
            "status": "queued",
            "detail": "Ожидает запуска",
            "current": None,
            "total": None,
            "attempt": None,
            "max_attempts": None,
            "error": None,
            "started_at": job.created_at,
            "updated_at": job.created_at,
        }
    with _LOCK:
        _JOBS[job_id] = job
        _set_stage(job, "queued", "В очереди", "queued")
    if options.daily_summary_run_id:
        register_daily_summary_job(
            DEFAULT_DB_PATH,
            int(options.daily_summary_run_id),
            job_id,
            options.entity_type,
            options.ids,
        )
    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()
    return asdict(job)
