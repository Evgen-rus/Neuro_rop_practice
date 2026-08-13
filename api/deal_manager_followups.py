"""Explicit paid jobs for current manager follow-up ideas."""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import _load_local_communications, build_communication_pattern_context
from api.deal_manager_situation import DEFAULT_DB_PATH, _safe_model_meta, _storage_call, load_manager_screen_context
from openai_api.llm.deal_manager_followups import generate_deal_manager_followups


@dataclass
class DealManagerFollowupsJob:
    job_id: str
    deal_id: str
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем идеи фоллоуапов"
    percent: int = 5
    followups_id: int | None = None
    reused: bool = False
    error: str | None = None


_FOLLOWUPS_JOBS: dict[str, DealManagerFollowupsJob] = {}
_FOLLOWUPS_LOCK = threading.Lock()


def _inputs(db_path: str | Path, deal_id: str) -> dict[str, Any]:
    context = load_manager_screen_context(db_path, str(deal_id), require_confirmed_situation=True)
    situation_id = context.get("situation_id")
    if situation_id is None:
        raise ValueError("У текущей ситуации отсутствует идентификатор")
    return {"context": context, "situation_id": int(situation_id)}


def _cached(db_path: str | Path, inputs: dict[str, Any]) -> dict[str, Any] | None:
    context = inputs["context"]
    return _storage_call(
        "get_deal_manager_followups", db_path, deal_id=str(context["deal"]["deal_id"]),
        source_report_id=int(context["source_report_id"]), situation_review_id=int(inputs["situation_id"]),
    )


def get_followups_job(job_id: str) -> dict[str, Any] | None:
    with _FOLLOWUPS_LOCK:
        job = _FOLLOWUPS_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _touch(job: DealManagerFollowupsJob, stage: str, detail: str, percent: int) -> None:
    with _FOLLOWUPS_LOCK:
        job.stage, job.detail, job.percent = stage, detail, percent


def _run(job_id: str, db_path: str | Path) -> None:
    with _FOLLOWUPS_LOCK:
        job = _FOLLOWUPS_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, "context", "Собираем актуальный контекст сделки", 20)
        inputs = _inputs(db_path, job.deal_id)
        existing = _cached(db_path, inputs)
        if isinstance(existing, dict):
            with _FOLLOWUPS_LOCK:
                job.followups_id, job.reused, job.status = int(existing["id"]), True, "done"
            _touch(job, "done", "Открываем актуальные фоллоуапы", 100)
            return
        context = inputs["context"]
        _touch(job, "llm", "AI подбирает полезные касания", 55)
        followups, metadata = generate_deal_manager_followups(
            analysis_projection=context["analysis_projection"], situation_projection=context["situation_projection"],
            deal=context["deal"], current_bitrix_task=context["current_bitrix_task"],
            communication_pattern_context=build_communication_pattern_context(_load_local_communications(job.deal_id)),
        )
        _touch(job, "saving", "Сохраняем проверенные идеи", 88)
        saved = _storage_call(
            "save_deal_manager_followups", db_path, deal_id=job.deal_id,
            source_report_id=context["source_report_id"], situation_review_id=inputs["situation_id"],
            followups_json=followups, model_meta=_safe_model_meta(metadata),
        )
        with _FOLLOWUPS_LOCK:
            job.followups_id, job.status = int(saved["id"]), "done"
        _touch(job, "done", "Фоллоуапы готовы", 100)
    except Exception as error:  # noqa: BLE001
        with _FOLLOWUPS_LOCK:
            job.status = "error"
            job.error = f"{error.__class__.__name__}: операция не выполнена"
        _touch(job, "error", "Не удалось подготовить фоллоуапы", 100)


def start_followups_job(*, db_path: str | Path = DEFAULT_DB_PATH, deal_id: str, confirm_paid: bool) -> dict[str, Any]:
    inputs = _inputs(db_path, str(deal_id))
    existing = _cached(db_path, inputs)
    if isinstance(existing, dict):
        job = DealManagerFollowupsJob(uuid.uuid4().hex, str(deal_id), status="done", stage="done", detail="Открываем актуальные фоллоуапы", percent=100, followups_id=int(existing["id"]), reused=True)
        with _FOLLOWUPS_LOCK:
            _FOLLOWUPS_JOBS[job.job_id] = job
        return asdict(job)
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для фоллоуапов")
    with _FOLLOWUPS_LOCK:
        active = next((item for item in _FOLLOWUPS_JOBS.values() if item.deal_id == str(deal_id) and item.status in {"queued", "running"}), None)
        if active:
            return asdict(active)
        job = DealManagerFollowupsJob(uuid.uuid4().hex, str(deal_id))
        _FOLLOWUPS_JOBS[job.job_id] = job
    threading.Thread(target=_run, args=(job.job_id, db_path), daemon=True).start()
    return asdict(job)


def get_followups_workspace(*, db_path: str | Path = DEFAULT_DB_PATH, deal_id: str) -> dict[str, Any]:
    inputs = _inputs(db_path, str(deal_id))
    return {"followups": _cached(db_path, inputs)}
