"""Background jobs for task-specific manager guidance."""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openai_api.llm.deal_task_guidance import generate_deal_task_guidance
from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_deal_control_task,
    get_latest_ui_report,
    list_deal_control_deals,
    save_deal_control_task_guidance,
)


@dataclass
class DealTaskGuidanceJob:
    job_id: str
    task_id: int
    deal_id: str
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем задачу РОПа"
    percent: int = 5
    created_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    guidance_id: int | None = None
    error: str | None = None


_GUIDANCE_JOBS: dict[str, DealTaskGuidanceJob] = {}
_GUIDANCE_LOCK = threading.Lock()


def _touch(job: DealTaskGuidanceJob, *, stage: str, detail: str, percent: int) -> None:
    with _GUIDANCE_LOCK:
        job.stage = stage
        job.detail = detail
        job.percent = percent
        job.updated_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")


def get_task_guidance_job(job_id: str) -> dict[str, Any] | None:
    with _GUIDANCE_LOCK:
        job = _GUIDANCE_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _load_context(db_path: str | Path, task_id: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    task = get_deal_control_task(db_path, task_id=task_id)
    if task is None:
        raise ValueError("Поручение не найдено")
    if str(task.get("local_status") or "") != "active":
        raise ValueError("AI-подсказку можно подготовить только для активного поручения")
    deal = next(
        (item for item in list_deal_control_deals(db_path, active_only=False) if str(item["deal_id"]) == str(task["deal_id"])),
        None,
    )
    if deal is None:
        raise ValueError("Сделка не найдена в локальном контуре контроля")
    report = get_latest_ui_report(db_path, entity_type="deal", entity_id=str(task["deal_id"]))
    if report is None:
        raise ValueError("Сначала проведите полный анализ сделки, затем подготовьте менеджера")
    return task, deal, report


def _safe_model_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "model",
        "reasoning_effort",
        "usage",
        "estimated_cost",
        "estimated_cost_usd",
        "estimated_cost_rub",
        "response_id",
        "schema_name",
        "response_status",
        "max_output_tokens",
    )
    return {key: metadata[key] for key in allowed if key in metadata}


def _run_task_guidance_job(job_id: str, db_path: str | Path) -> None:
    with _GUIDANCE_LOCK:
        job = _GUIDANCE_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, stage="context", detail="Связываем анализ сделки с задачей РОПа", percent=20)
        task, deal, report = _load_context(db_path, job.task_id)
        task_revision = int(task.get("guidance_revision") or 1)
        source_report_id = int(report["id"])
        _touch(job, stage="llm", detail="AI готовит цель контакта, вопросы и готовый текст", percent=55)
        guidance, metadata = generate_deal_task_guidance(
            task=task,
            deal=deal,
            report_json=report.get("report_json"),
        )
        _touch(job, stage="saving", detail="Проверяем и сохраняем подсказку к задаче", percent=88)
        saved = save_deal_control_task_guidance(
            db_path,
            task_id=job.task_id,
            task_revision=task_revision,
            source_report_id=source_report_id,
            guidance=guidance,
            model_meta=_safe_model_meta(metadata),
        )
        with _GUIDANCE_LOCK:
            job.guidance_id = int(saved["id"])
            job.status = "done"
        _touch(job, stage="done", detail="Подсказка менеджеру готова", percent=100)
    except Exception as error:  # noqa: BLE001 - returned to the local UI
        with _GUIDANCE_LOCK:
            job.status = "error"
            job.error = str(error)
        _touch(job, stage="error", detail="Не удалось подготовить подсказку", percent=100)


def start_task_guidance_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    task_id: int,
    confirm_paid: bool,
) -> dict[str, Any]:
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для подготовки менеджера")
    task, _, _ = _load_context(db_path, task_id)
    with _GUIDANCE_LOCK:
        existing = next(
            (
                item
                for item in _GUIDANCE_JOBS.values()
                if item.task_id == int(task_id) and item.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return asdict(existing)
        job_id = uuid.uuid4().hex
        job = DealTaskGuidanceJob(job_id=job_id, task_id=int(task_id), deal_id=str(task["deal_id"]))
        _GUIDANCE_JOBS[job_id] = job
    thread = threading.Thread(target=_run_task_guidance_job, args=(job_id, db_path), daemon=True)
    thread.start()
    return asdict(job)
