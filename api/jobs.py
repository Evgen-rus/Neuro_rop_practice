"""
Background analyze jobs that wrap existing CLI orchestration.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from openai_api.bitrix_links import bitrix_entity_url
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import DEFAULT_DB_PATH, save_ui_report


PROJECT_ROOT = BASE_DIR
PYTHON = sys.executable


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
    force_llm: bool = True
    transcript_mode: str = "all"


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


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


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


def extract_summary_fields(analysis: dict[str, Any], entity_type: str) -> dict[str, str | None]:
    analysis = unwrap_analysis_payload(analysis)
    risk = None
    attention = None
    action = None
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
        loss = analysis.get("loss_diagnosis") if isinstance(analysis.get("loss_diagnosis"), dict) else {}
        if loss and not attention:
            attention = str(loss.get("final_verdict") or "") or None
        lead_state = analysis.get("lead_state") if isinstance(analysis.get("lead_state"), dict) else {}
        if lead_state and not attention:
            attention = str(lead_state.get("summary") or "") or None
    else:
        deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
        if deal_state and not attention:
            attention = str(deal_state.get("summary") or "") or None
    priority = analysis.get("priority_recommendation") if isinstance(analysis.get("priority_recommendation"), dict) else {}
    if priority and not risk:
        risk = str(priority.get("level") or "") or None
    return {
        "risk_level": risk,
        "attention_reason": attention,
        "recommended_action": action,
    }


def run_command(command: list[str], on_line: Callable[[str], None] | None = None) -> None:
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def _collect_results(job: JobState, entity_type: str, ids: list[str]) -> None:
    for entity_id in ids:
        paths = analysis_paths(entity_type, entity_id)
        envelope: dict[str, Any] | None = None
        if paths["analysis_json"].exists():
            try:
                loaded = json.loads(paths["analysis_json"].read_text(encoding="utf-8"))
                envelope = loaded if isinstance(loaded, dict) else None
            except (OSError, json.JSONDecodeError):
                envelope = None
        analysis = unwrap_analysis_payload(envelope) if envelope is not None else None
        summary = extract_summary_fields(analysis or {}, entity_type)
        report_id = None
        if analysis is not None:
            report_id = save_ui_report(
                DEFAULT_DB_PATH,
                entity_type=entity_type,
                entity_id=entity_id,
                risk_level=summary.get("risk_level"),
                attention_reason=summary.get("attention_reason"),
                recommended_action=summary.get("recommended_action"),
                analysis_path=str(paths["analysis_json"]) if paths["analysis_json"].exists() else None,
                report_path=str(paths["report_md"]) if paths["report_md"].exists() else None,
                # Store unwrapped analysis so UI history works without extra mapping.
                report_json=analysis,
                job_id=job.job_id,
            )
            job.report_ids.append(report_id)
        job.results.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "report_id": report_id,
                "has_analysis": analysis is not None,
                "has_markdown": paths["report_md"].exists(),
                "risk_level": summary.get("risk_level"),
                "attention_reason": summary.get("attention_reason"),
                "recommended_action": summary.get("recommended_action"),
                "bitrix_url": bitrix_entity_url(entity_type, entity_id),
                "analysis": analysis,
            }
        )


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job.status = "running"
        _touch(job)
        options = AnalyzeOptions(**job.options)

    def log_line(text: str) -> None:
        with _LOCK:
            current = _JOBS[job_id]
            # Keep last detail on current stage.
            if current.stages:
                current.stages[-1]["detail"] = text[-300:]
            _touch(current)

    try:
        # Group IDs by resolved type for auto mode.
        groups: dict[str, list[str]] = {"lead": [], "deal": []}
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
            run_command(command, on_line=log_line)
            with _LOCK:
                _set_stage(_JOBS[job_id], stage_key, f"Pipeline {entity_type}", "done")
                _set_stage(_JOBS[job_id], f"collect_{entity_type}", f"Сбор результатов ({entity_type})", "running")
                _collect_results(_JOBS[job_id], entity_type, ids)
                _set_stage(_JOBS[job_id], f"collect_{entity_type}", f"Сбор результатов ({entity_type})", "done")

        with _LOCK:
            job = _JOBS[job_id]
            job.status = "done"
            _set_stage(job, "done", "Отчёт готов", "done")
            _touch(job)
    except Exception as error:  # noqa: BLE001 - surface to UI
        with _LOCK:
            job = _JOBS[job_id]
            job.status = "error"
            job.error = str(error)
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
    with _LOCK:
        _JOBS[job_id] = job
        _set_stage(job, "queued", "В очереди", "queued")
    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()
    return asdict(job)
