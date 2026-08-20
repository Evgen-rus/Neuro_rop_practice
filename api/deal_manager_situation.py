"""Manager-screen situation confirmation and refinement jobs.

Storage is intentionally resolved at call time.  The storage thread can add
the functions below without making this checkout import a table or bypassing
``storage.rop_db``.

Canonical storage functions:

* ``save_deal_manager_situation_confirmation(db_path, *, deal_id,
  source_report_id, manager_context=None, model_meta=None)`` -> dict
* ``save_deal_manager_situation_refined_projection(db_path, *, deal_id,
  source_report_id, refined_coaching, manager_context=None,
  model_meta=None)`` -> dict
* ``get_deal_manager_situation_state(db_path, *, deal_id)`` -> dict | None
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from openai_api.llm.deal_manager_situation import (
    build_confirmed_manager_projection,
    compact_analysis_projection,
    generate_deal_manager_situation,
    project_bitrix_task,
    project_deal,
    project_manager_projection,
    unwrap_analysis,
)
from openai_api.llm.llm_client import ModelJsonParseError, ModelResponseIncompleteError
from setup import MSK_TZ
from storage import rop_db as storage


DEFAULT_DB_PATH = storage.DEFAULT_DB_PATH
logger = logging.getLogger(__name__)

INCOMPLETE_SITUATION_ERROR = "Ответ модели оборвался. Нажмите «Пересобрать ситуацию» ещё раз."
FORMAT_SITUATION_ERROR = "Модель вернула ответ в неверном формате. Можно повторить."
GENERIC_SITUATION_ERROR = "Не удалось уточнить ситуацию. Попробуйте ещё раз."
_KEEP_SITUATION_ERRORS = (
    "Контекст менеджера должен содержать от 1 до 4000 знаков",
)


class StorageContractUnavailable(RuntimeError):
    """Raised when a storage function is not yet present in this checkout."""


def _storage_call(name: str, db_path: str | Path, **kwargs: Any) -> Any:
    function = getattr(storage, name, None)
    if not callable(function):
        raise StorageContractUnavailable(f"Storage contract is missing: {name}")
    return function(db_path, **kwargs)


def _storage_call_alias(names: tuple[str, ...], db_path: str | Path, **kwargs: Any) -> Any:
    for name in names:
        function = getattr(storage, name, None)
        if callable(function):
            return _storage_call(name, db_path, **kwargs)
    raise StorageContractUnavailable(f"Storage contract is missing: {' or '.join(names)}")


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


def _safe_model_meta(metadata: dict[str, Any] | None) -> dict[str, Any]:
    allowed = {
        "model",
        "call_type",
        "requested_at",
        "latency_seconds",
        "prompt_cache",
        "reasoning_effort",
        "usage",
        "estimated_cost",
        "estimated_cost_usd",
        "estimated_cost_rub",
        "response_id",
        "schema_name",
        "response_status",
        "max_output_tokens",
        "semantic_attempt_count",
    }
    return {key: metadata[key] for key in allowed if metadata and key in metadata}


def public_situation_error(error: BaseException) -> str:
    """Human job/HTTP error without class names, traces, prompts or cost."""
    if isinstance(error, ModelResponseIncompleteError):
        return INCOMPLETE_SITUATION_ERROR
    if isinstance(error, ModelJsonParseError):
        return FORMAT_SITUATION_ERROR
    text = str(error or "").strip()
    lowered = text.casefold()
    for known in _KEEP_SITUATION_ERRORS:
        if known.casefold() in lowered:
            return known
    if "платный" in lowered or "confirm_paid" in lowered:
        return GENERIC_SITUATION_ERROR
    if "incomplete" in lowered:
        return INCOMPLETE_SITUATION_ERROR
    if "invalid json" in lowered or "json-объект" in lowered:
        return FORMAT_SITUATION_ERROR
    if any(marker in lowered for marker in ("timeout", "connection", "api key", "openai")):
        return "Сервис ответа сейчас недоступен. Попробуйте ещё раз."
    return GENERIC_SITUATION_ERROR


def public_disc_profile(analysis_projection: dict[str, Any] | None) -> dict[str, Any] | None:
    """Allow-listed DISC labels for the manager UI; never expose evidence or reasons."""
    profile = analysis_projection.get("client_communication_profile") if isinstance(analysis_projection, dict) else None
    if not isinstance(profile, dict) or profile.get("status") not in {"tentative", "supported"}:
        return None
    primary = profile.get("primary_style")
    secondary = profile.get("secondary_style")
    confidence = profile.get("profile_confidence")
    if primary not in {"D", "I", "S", "C"} or confidence not in {"low", "medium", "high"}:
        return None
    return {
        "primary_style": primary,
        "secondary_style": secondary if secondary in {"D", "I", "S", "C"} and secondary != primary else None,
        "profile_confidence": confidence,
    }


def _parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _latest_full_analysis(db_path: str | Path, deal_id: str) -> tuple[dict[str, Any], dict[str, Any], int]:
    report = _storage_call(
        "get_latest_ui_report",
        db_path,
        entity_type="deal",
        entity_id=str(deal_id),
    )
    if not isinstance(report, dict) or not isinstance(report.get("report_json"), dict):
        raise ValueError("Сначала проведите полный анализ сделки")
    report_meta = _parse_json_dict(report.get("report_meta"))
    if str(report_meta.get("status") or "").lower() in {"partial", "error", "incomplete"}:
        raise ValueError("Сначала проведите полный анализ сделки")
    report_json = report["report_json"]
    analysis = unwrap_analysis(report_json)
    if not analysis:
        raise ValueError("Сначала проведите полный анализ сделки")
    try:
        report_id = int(report["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("У полного анализа сделки отсутствует идентификатор") from error
    return report, compact_analysis_projection(report_json), report_id


def _load_deal(db_path: str | Path, deal_id: str) -> dict[str, Any]:
    deals = _storage_call("list_deal_control_deals", db_path, active_only=False)
    for item in deals if isinstance(deals, list) else []:
        if isinstance(item, dict) and str(item.get("deal_id") or "") == str(deal_id):
            return item
    raise ValueError("Сделка не найдена в локальном контуре контроля")


def _current_bitrix_task(deal: dict[str, Any]) -> dict[str, Any] | None:
    primary = deal.get("primary_bitrix_task")
    if isinstance(primary, dict):
        return project_bitrix_task(primary)
    tasks = deal.get("bitrix_tasks")
    if isinstance(tasks, list):
        for item in tasks:
            if isinstance(item, dict) and not bool(item.get("completed")):
                return project_bitrix_task(item)
        for item in tasks:
            if isinstance(item, dict):
                return project_bitrix_task(item)
    return None


def _situation_status(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("is_current") is False:
        return ""
    status = str(
        value.get("status")
        or value.get("state")
        or value.get("review_status")
        or value.get("situation_status")
        or ""
    ).strip().lower()
    if status in {"confirmed", "refined"}:
        return status
    if value.get("is_confirmed") is True:
        return "confirmed"
    return ""


def _situation_projection(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    for key in ("manager_projection", "situation_projection", "projection"):
        projection = project_manager_projection(value.get(key))
        if projection:
            return projection
    return project_manager_projection(value)


def _situation_id(value: dict[str, Any] | None) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ("id", "situation_id", "review_id"):
        try:
            if value.get(key) is not None:
                return int(value[key])
        except (TypeError, ValueError):
            continue
    return None


def load_manager_screen_context(
    db_path: str | Path,
    deal_id: str,
    *,
    require_confirmed_situation: bool = False,
) -> dict[str, Any]:
    """Load only the bounded context shared by situation and quick help."""
    deal = _load_deal(db_path, str(deal_id))
    report, analysis_projection, report_id = _latest_full_analysis(db_path, str(deal_id))
    situation = _storage_call_alias(
        ("get_deal_manager_situation_state", "get_current_deal_manager_situation", "get_latest_deal_manager_situation"),
        db_path,
        deal_id=str(deal_id),
    )
    current_status = _situation_status(situation if isinstance(situation, dict) else None)
    if require_confirmed_situation and current_status != "confirmed":
        raise ValueError("Сначала подтвердите текущую ситуацию сделки")
    situation_review = _storage_call_alias(
        ("get_latest_deal_manager_situation_review", "get_current_deal_manager_situation_review"),
        db_path,
        deal_id=str(deal_id),
        source_report_id=report_id,
    )
    situation_projection = project_manager_projection(
        situation_review.get("refined_coaching") if isinstance(situation_review, dict) else None
    )
    if not situation_projection:
        situation_projection = build_confirmed_manager_projection(report.get("report_json"))
    return {
        "deal": deal,
        "deal_projection": project_deal(deal),
        "report": report,
        "source_report_id": report_id,
        "analysis_projection": analysis_projection,
        "current_bitrix_task": _current_bitrix_task(deal),
        "situation": situation,
        "situation_review": situation_review,
        "situation_id": _situation_id(situation),
        "situation_status": _situation_status(situation),
        "situation_projection": situation_projection,
    }


def _public_situation(
    value: dict[str, Any] | None,
    *,
    deal_id: str,
    status: str,
    source_report_id: int,
    analysis_projection: dict[str, Any],
    deal_projection: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    previous_manager_projection: dict[str, Any],
    manager_projection: dict[str, Any],
    context: str | None,
) -> dict[str, Any]:
    saved = value if isinstance(value, dict) else {}
    result = {
        "id": _situation_id(saved),
        "deal_id": str(saved.get("deal_id") or deal_id),
        "status": _situation_status(saved) or status,
        "source_report_id": saved.get("source_report_id", source_report_id),
        "analysis_projection": saved.get("analysis_projection", analysis_projection),
        "deal_projection": saved.get("deal_projection", deal_projection),
        "current_bitrix_task": saved.get("current_bitrix_task", current_bitrix_task),
        "previous_manager_projection": saved.get(
            "previous_manager_projection", previous_manager_projection
        ),
        "manager_projection": saved.get("manager_projection", manager_projection),
        "context": saved.get("context", context),
        "business_date": saved.get("business_date"),
        "last_confirmation_business_date": saved.get("business_date"),
    }
    for key in ("created_at", "updated_at"):
        if saved.get(key) is not None:
            result[key] = saved[key]
    if isinstance(saved.get("model_meta"), dict):
        result["model_meta"] = _safe_model_meta(saved["model_meta"])
    return result


def confirm_deal_manager_situation(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
) -> dict[str, Any]:
    """Confirm the latest full analysis without an LLM call."""
    context = load_manager_screen_context(db_path, str(deal_id))
    projection = build_confirmed_manager_projection(context["report"].get("report_json"))
    saved = _storage_call(
        "save_deal_manager_situation_confirmation",
        db_path,
        deal_id=str(deal_id),
        source_report_id=context["source_report_id"],
        manager_context=None,
        model_meta=None,
    )
    return {
        "ok": True,
        "situation": _public_situation(
            saved,
            deal_id=str(deal_id),
            status="confirmed",
            source_report_id=context["source_report_id"],
            analysis_projection=context["analysis_projection"],
            deal_projection=context["deal_projection"],
            current_bitrix_task=context["current_bitrix_task"],
            previous_manager_projection={},
            manager_projection=projection,
            context=None,
        ),
    }


@dataclass
class DealManagerSituationJob:
    job_id: str
    deal_id: str
    context: str
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем уточнение ситуации"
    percent: int = 5
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    situation_id: int | None = None
    error: str | None = None


_SITUATION_JOBS: dict[str, DealManagerSituationJob] = {}
_SITUATION_LOCK = threading.Lock()


def _touch(job: DealManagerSituationJob, *, stage: str, detail: str, percent: int) -> None:
    with _SITUATION_LOCK:
        job.stage = stage
        job.detail = detail
        job.percent = percent
        job.updated_at = _now()


def get_situation_job(job_id: str) -> dict[str, Any] | None:
    with _SITUATION_LOCK:
        job = _SITUATION_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _run_situation_job(job_id: str, db_path: str | Path) -> None:
    with _SITUATION_LOCK:
        job = _SITUATION_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, stage="context", detail="Собираем анализ, сделку и текущую задачу", percent=20)
        context = load_manager_screen_context(
            db_path,
            job.deal_id,
            require_confirmed_situation=False,
        )
        previous = context["situation_projection"]
        _touch(job, stage="llm", detail="AI уточняет рабочую ситуацию менеджера", percent=55)
        projection, metadata = generate_deal_manager_situation(
            analysis_projection=context["analysis_projection"],
            deal=context["deal"],
            current_bitrix_task=context["current_bitrix_task"],
            previous_manager_projection=previous,
            manager_context=job.context,
        )
        _touch(job, stage="saving", detail="Сохраняем только полный проверенный результат", percent=88)
        saved = _storage_call(
            "save_deal_manager_situation_refined_projection",
            db_path,
            deal_id=job.deal_id,
            source_report_id=context["source_report_id"],
            refined_coaching=projection,
            manager_context=job.context,
            model_meta=_safe_model_meta(metadata),
        )
        with _SITUATION_LOCK:
            job.situation_id = _situation_id(saved)
            job.status = "done"
        _touch(job, stage="done", detail="Ситуация пересобрана. Проверьте текст и подтвердите", percent=100)
    except Exception as error:  # noqa: BLE001 - never return model content in a job error
        logger.exception("Situation refine job %s failed for deal %s", job_id, job.deal_id)
        with _SITUATION_LOCK:
            job.status = "error"
            job.error = public_situation_error(error)
        _touch(job, stage="error", detail="Не удалось уточнить ситуацию", percent=100)


def start_situation_refine_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    context: str,
    confirm_paid: bool,
) -> dict[str, Any]:
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для уточнения ситуации")
    normalized_context = str(context or "").strip()
    if not 1 <= len(normalized_context) <= 4000:
        raise ValueError("Контекст менеджера должен содержать от 1 до 4000 знаков")
    loaded = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=False,
    )
    with _SITUATION_LOCK:
        existing = next(
            (
                item
                for item in _SITUATION_JOBS.values()
                if item.deal_id == str(deal_id) and item.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return asdict(existing)
        job_id = uuid.uuid4().hex
        job = DealManagerSituationJob(
            job_id=job_id,
            deal_id=str(deal_id),
            context=normalized_context,
            situation_id=loaded["situation_id"],
        )
        _SITUATION_JOBS[job_id] = job
    thread = threading.Thread(target=_run_situation_job, args=(job_id, db_path), daemon=True)
    thread.start()
    return asdict(job)
