"""Independent personal-sales-coach jobs for the manager deal screen.

Canonical storage expected by this module:

``save_deal_manager_quick_help(db_path, *, deal_id, source_report_id,
situation_review_id, question, answer_json, model_meta=None)`` -> dict

``list_deal_manager_quick_help(db_path, *, deal_id, limit=20,
before_id=None)`` -> list[dict]

The API never reads or writes a previous quick-help answer into a new prompt.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from api.deal_manager_situation import (
    DEFAULT_DB_PATH,
    StorageContractUnavailable,
    _safe_model_meta,
    load_manager_screen_context,
    _situation_id,
    _situation_status,
    _storage_call,
)
from openai_api.llm.deal_manager_quick_help import generate_deal_manager_quick_help
from setup import MSK_TZ


MAX_QUESTION_CHARS = 4000


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


@dataclass
class DealManagerQuickHelpJob:
    job_id: str
    deal_id: str
    question: str
    situation_id: int | None
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем ответ тренера"
    percent: int = 5
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    quick_help_id: int | None = None
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
        _touch(job, stage="llm", detail="AI разбирает вопрос менеджера", percent=55)
        answer, metadata = generate_deal_manager_quick_help(
            question=job.question,
            analysis_projection=context["analysis_projection"],
            deal=context["deal"],
            current_bitrix_task=context["current_bitrix_task"],
            situation_projection=context["situation_projection"],
        )
        _touch(job, stage="saving", detail="Сохраняем полный проверенный ответ", percent=88)
        saved = _storage_call(
            "save_deal_manager_quick_help",
            db_path,
            deal_id=job.deal_id,
            source_report_id=context["source_report_id"],
            situation_review_id=situation_id,
            question=job.question,
            answer_json=answer,
            model_meta=_safe_model_meta(metadata),
        )
        with _QUICK_HELP_LOCK:
            try:
                job.quick_help_id = int(saved.get("id")) if isinstance(saved, dict) and saved.get("id") is not None else None
            except (TypeError, ValueError):
                job.quick_help_id = None
            job.status = "done"
        _touch(job, stage="done", detail="Ответ тренера готов", percent=100)
    except Exception as error:  # noqa: BLE001 - never return model content in a job error
        with _QUICK_HELP_LOCK:
            job.status = "error"
            job.error = f"{error.__class__.__name__}: операция не выполнена"
        _touch(job, stage="error", detail="Не удалось подготовить ответ", percent=100)


def start_quick_help_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    question: str,
    confirm_paid: bool,
) -> dict[str, Any]:
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для quick help")
    normalized_question = str(question or "").strip()
    if not 1 <= len(normalized_question) <= MAX_QUESTION_CHARS:
        raise ValueError("Вопрос должен содержать от 1 до 4000 знаков")
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
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
