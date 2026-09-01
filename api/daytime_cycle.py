"""Server-side 30-minute Bitrix cycle: sync, CRM facts, then existing FULL/MINI/skip."""

from __future__ import annotations

import os
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from openai_api.change_detection.decision_engine import (
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    SKIPPED_NO_CHANGES,
)
from openai_api.config import read_bool_env
from setup import MSK_TZ, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    active_automatic_analysis_entity_ids,
    attach_automatic_analysis_job_id,
    create_automatic_analysis_run,
    finish_automatic_analysis_run,
    get_deal_control_scope,
    interrupt_running_automatic_analysis_runs,
    list_analysis_runs,
    list_automatic_analysis_items,
    list_recoverable_automatic_analysis_runs,
    list_deal_control_deals,
    mark_automatic_analysis_diary_written,
    requeue_unfinished_automatic_analysis_items,
    update_automatic_analysis_item,
    utcish_now,
)


CYCLE_INTERVAL = timedelta(minutes=30)
WORKDAY_START = time(7, 0)
WORKDAY_END = time(18, 0)
PLANNING_REPORT_TIME = time(15, 45)
EVENING_CYCLE_TIME = time(22, 0)
DAY_END_REPORT_TIME = time(23, 0)
WEEKDAY_MONDAY = 0
WEEKDAY_FRIDAY = 4
FULL_STATUSES = {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS}
MINI_STATUSES = {MINI_RECOMMENDATION_NO_LLM}
SKIP_STATUSES = {SKIPPED_NO_CHANGES}

logger = get_logger(__file__)

_stop_event = threading.Event()
_run_lock = threading.Lock()
_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_cycle_thread: threading.Thread | None = None
_last_cycle: dict[str, Any] | None = None
_next_at: str | None = None


def _running_under_unittest() -> bool:
    return any("unittest" in str(item) for item in sys.argv)


def daytime_cycle_enabled() -> bool:
    """Production default is on; unit tests stay off unless the env flag is set."""
    value = os.getenv("DAYTIME_CYCLE_ENABLED")
    if value is not None and value.strip():
        return read_bool_env("DAYTIME_CYCLE_ENABLED", True)
    return not _running_under_unittest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=MSK_TZ)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(MSK_TZ).isoformat(timespec="seconds")


def _is_workday(day: date) -> bool:
    return WEEKDAY_MONDAY <= day.weekday() <= WEEKDAY_FRIDAY


def slot_times_for_day() -> list[time]:
    """Daytime cycles, an evening cycle, and two independent report slots."""
    slots: list[time] = []
    cursor = datetime.combine(date.min, WORKDAY_START)
    last = datetime.combine(date.min, WORKDAY_END)
    while cursor <= last:
        slots.append(cursor.time())
        cursor += CYCLE_INTERVAL
    slots.extend((PLANNING_REPORT_TIME, EVENING_CYCLE_TIME, DAY_END_REPORT_TIME))
    return sorted(set(slots))


def next_scheduled_at(now: datetime | None = None) -> datetime:
    current = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    slots = slot_times_for_day()
    # Friday after the final report must jump to Monday.
    for offset in range(0, 8):
        day = current.date() + timedelta(days=offset)
        if not _is_workday(day):
            continue
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=MSK_TZ)
            if candidate > current:
                return candidate
    raise RuntimeError("Не удалось вычислить следующий слот дневного цикла")


def daytime_cycle_status() -> dict[str, Any]:
    with _state_lock:
        last = dict(_last_cycle) if _last_cycle else None
        next_at = _next_at
    if last is not None:
        last = {
            "status": last.get("status"),
            "trigger": last.get("trigger"),
            "started_at": last.get("started_at"),
            "finished_at": last.get("finished_at"),
            "checked": (last.get("decisions") or {}).get("checked"),
            "changed": (last.get("decisions") or {}).get("changed"),
            "full": (last.get("decisions") or {}).get("full"),
            "mini": (last.get("decisions") or {}).get("mini"),
            "skip": (last.get("decisions") or {}).get("skip"),
            "error": (last.get("decisions") or {}).get("error"),
            "has_errors": bool(last.get("errors")),
        }
    return {
        "enabled": daytime_cycle_enabled(),
        "running": _thread is not None and _thread.is_alive(),
        "interval_minutes": int(CYCLE_INTERVAL.total_seconds() // 60),
        "workdays": "mon-fri",
        "work_hours": "07:00-18:00",
        "planning_report_at": PLANNING_REPORT_TIME.strftime("%H:%M"),
        "evening_cycle_at": EVENING_CYCLE_TIME.strftime("%H:%M"),
        "day_end_report_at": DAY_END_REPORT_TIME.strftime("%H:%M"),
        "timezone": "Europe/Moscow",
        "next_at": next_at,
        "last": last,
    }


def _write_spend_diary(
    payload: dict[str, Any],
    *,
    started: datetime,
    analysis_payload: dict[str, Any] | None,
) -> None:
    if str((analysis_payload or {}).get("status") or "") == "running" or (analysis_payload or {}).get("automatic_analysis_run_id"):
        return
    try:
        from openai_api.spend_diary import load_batch_events, write_cycle_block

        write_cycle_block(
            started=started,
            counts=(analysis_payload or {}).get("counts") or payload.get("decisions") or {},
            events=load_batch_events((analysis_payload or {}).get("spend_batch_path")),
            busy_ids=payload.get("busy_ids") or [],
            status=str(payload.get("status") or ""),
            message=payload.get("message"),
            now=started,
        )
    except Exception as error:  # noqa: BLE001 - diary must not break the cycle
        logger.warning("Дневник трат не записан: %s", type(error).__name__)


def _store_cycle_result(payload: dict[str, Any]) -> dict[str, Any]:
    global _last_cycle
    with _state_lock:
        _last_cycle = dict(payload)
    return payload


def _empty_decision_counts() -> dict[str, int]:
    return {"checked": 0, "changed": 0, "full": 0, "mini": 0, "skip": 0, "error": 0}


def _summarize_decisions(
    *,
    db_path: str | Path,
    deal_ids: list[str],
    created_at_from: str,
) -> dict[str, Any]:
    counts: dict[str, Any] = _empty_decision_counts()
    counts["checked"] = len(deal_ids)
    counts["full_ids"] = []
    counts["mini_ids"] = []
    if not deal_ids:
        return counts
    runs = list_analysis_runs(
        db_path,
        entity_type="deal",
        entity_ids=deal_ids,
        created_at_from=created_at_from,
    )
    latest_by_entity: dict[str, dict[str, Any]] = {}
    for run in runs:
        latest_by_entity[str(run.get("entity_id") or "")] = run
    seen_ids = set()
    for entity_id, run in latest_by_entity.items():
        if not entity_id:
            continue
        seen_ids.add(entity_id)
        status = str(run.get("status") or "")
        if status in FULL_STATUSES:
            counts["full"] += 1
            counts["full_ids"].append(entity_id)
        elif status in MINI_STATUSES:
            counts["mini"] += 1
            counts["mini_ids"].append(entity_id)
        elif status in SKIP_STATUSES:
            counts["skip"] += 1
        else:
            counts["error"] += 1
    missing = [entity_id for entity_id in deal_ids if entity_id not in seen_ids]
    counts["error"] += len(missing)
    counts["changed"] = counts["full"] + counts["mini"]
    return counts


def _persist_skipped_automatic_run(
    db_path: str | Path,
    *,
    trigger: str,
    status: str,
    started_at: str,
    entity_ids: list[str] | tuple[str, ...] = (),
) -> None:
    started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    business_date = started.astimezone(MSK_TZ).date().isoformat()
    create_automatic_analysis_run(
        db_path,
        trigger=trigger,
        entity_ids=list(entity_ids),
        status=status,
        current_stage=status,
        business_date=business_date,
    )


def _analyze_work_pool(
    *,
    db_path: str | Path,
    deal_ids: list[str],
    started_at: str,
    trigger: str = "scheduled",
    sources_ok: bool = True,
) -> dict[str, Any]:
    from api.jobs import AnalyzeOptions, busy_analyze_entity_ids

    if not deal_ids:
        _persist_skipped_automatic_run(
            db_path,
            trigger=trigger,
            status="skipped_empty",
            started_at=started_at,
        )
        return {
            "status": "skipped_empty",
            "job_id": None,
            "busy_ids": [],
            "counts": _empty_decision_counts() | {"full_ids": [], "mini_ids": []},
            "spend_batch_path": None,
        }

    busy = busy_analyze_entity_ids("deal") | active_automatic_analysis_entity_ids(db_path)
    ready_ids = [entity_id for entity_id in deal_ids if entity_id not in busy]
    skipped_busy = [entity_id for entity_id in deal_ids if entity_id in busy]
    if skipped_busy:
        logger.info(
            "Пропуск анализа для сделок с уже запущенным job: %s",
            ", ".join(skipped_busy),
        )
    if not ready_ids:
        counts = _empty_decision_counts()
        counts["checked"] = len(deal_ids)
        counts["full_ids"] = []
        counts["mini_ids"] = []
        return {
            "status": "skipped_busy",
            "job_id": None,
            "busy_ids": skipped_busy,
            "counts": counts,
            "spend_batch_path": None,
        }

    from api.crm_change_gate import plan_automatic_refresh
    plans = plan_automatic_refresh(db_path=db_path, deal_ids=ready_ids,
        now=datetime.fromisoformat(started_at), sources_ok=sources_ok)
    idle_ids = [entity_id for entity_id in ready_ids if plans[entity_id]["mode"] == "skip"]
    heavy_ids = [entity_id for entity_id in ready_ids if plans[entity_id]["mode"] in {"full", "incremental"}]
    active_ids = [entity_id for entity_id in ready_ids if entity_id not in idle_ids]

    from openai_api.spend_diary import new_cycle_batch_path

    started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    batch_path = new_cycle_batch_path(started)
    business_date = started.astimezone(MSK_TZ).date().isoformat()
    automatic_run = create_automatic_analysis_run(
        db_path,
        trigger=trigger,
        entity_ids=ready_ids,
        status="running",
        current_stage="queued",
        business_date=business_date,
        spend_batch_path=str(batch_path),
        item_sync_plans=plans,
    )
    automatic_run_id = int(automatic_run["id"])
    for entity_id in idle_ids:
        update_automatic_analysis_item(db_path, automatic_run_id, entity_type="deal", entity_id=entity_id,
            stage="skipped_before_fetch", decision_status="skip", processing_status="done",
            publication_status="reused", current_stage="skipped_before_fetch")
    job_ids = _launch_automatic_run_jobs(
        db_path=db_path,
        automatic_run_id=automatic_run_id,
        deal_ids=active_ids,
        refresh_plans=plans,
        batch_path=str(batch_path),
        started_at=str(automatic_run.get("started_at") or started_at),
    )
    counts = _empty_decision_counts() | {"checked": len(deal_ids), "skip": len(idle_ids), "full_ids": [], "mini_ids": [],
        "skipped_before_fetch": len(idle_ids), "heavy_context_fetch_count": len(heavy_ids),
        "audio_only_jobs": sum(plans[entity_id]["mode"] == "audio" for entity_id in active_ids)}
    return {
        "status": "running" if active_ids else "done",
        "job_id": job_ids[0] if job_ids else None,
        "job_ids": job_ids,
        "busy_ids": skipped_busy,
        "counts": counts,
        "spend_batch_path": str(batch_path),
        "automatic_analysis_run_id": automatic_run_id,
    }


def _automatic_counts(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Any] = _empty_decision_counts()
    counts["checked"] = len(items)
    counts["full_ids"] = []
    counts["mini_ids"] = []
    for item in items:
        entity_id = str(item.get("entity_id") or "")
        decision = str(item.get("decision_status") or "")
        if decision == "full":
            counts["full"] += 1
            counts["full_ids"].append(entity_id)
        elif decision == "mini":
            counts["mini"] += 1
            counts["mini_ids"].append(entity_id)
        elif decision == "skip":
            counts["skip"] += 1
        elif decision == "error" or str(item.get("processing_status") or "") == "error":
            counts["error"] += 1
    counts["changed"] = counts["full"] + counts["mini"]
    return counts


def _finish_automatic_run(
    db_path: str | Path,
    run_id: int,
    *,
    batch_path: str | None,
    started_at: str,
) -> None:
    items = list_automatic_analysis_items(db_path, run_id)
    counts = _automatic_counts(items)
    errors = int(counts.get("error") or 0)
    completed = sum(
        str(item.get("processing_status") or "") in {"done", "error"}
        for item in items
    )
    if completed < len(items):
        return
    status = "done" if errors == 0 else "partial" if errors < len(items) else "error"
    run_started = datetime.fromisoformat(str(started_at or utcish_now()).replace("Z", "+00:00"))
    try:
        from openai_api.spend_diary import load_batch_events, write_cycle_block

        write_cycle_block(
            started=run_started,
            counts=counts,
            events=load_batch_events(batch_path),
            status=status,
            now=run_started,
        )
        mark_automatic_analysis_diary_written(db_path, run_id)
    except Exception as error:  # noqa: BLE001 - events remain recoverable
        logger.warning("Итоговый блок дневника run=%s не записан: %s", run_id, type(error).__name__)
    finish_automatic_analysis_run(db_path, run_id, status=status, current_stage=status)


def _monitor_automatic_run(
    *,
    db_path: str | Path,
    run_id: int,
    job_ids: list[str],
    batch_path: str | None,
    started_at: str,
) -> None:
    from api.jobs import wait_for_job

    for job_id in job_ids:
        try:
            wait_for_job(job_id, timeout_seconds=None)
        except Exception as error:  # noqa: BLE001 - item is made retryable by the next API start/tick
            logger.warning("Не удалось дождаться automatic job %s: %s", job_id, type(error).__name__)
    try:
        _finish_automatic_run(db_path, run_id, batch_path=batch_path, started_at=started_at)
    except Exception as error:  # noqa: BLE001 - recovery is persisted for the next API start
        # The API may be shutting down while a daemon monitor is unwinding.
        logger.info(
            "Automatic run %s завершится при следующем старте API: %s",
            run_id,
            type(error).__name__,
        )


DIAGNOSTIC_JOB_ENV_KEYS = (
    "BITRIX_USAGE_DAILY_DIR",
    "BITRIX_TRACE_RUN_ID",
    "BITRIX_TRACE_COMPONENT",
    "BITRIX_TRACE_ALLOW_ENTITY_ID",
    "BITRIX_DENY_WRITE_METHODS",
    "OPENAI_USAGE_DAILY_DIR",
    "OPENAI_USAGE_TRACE_PATH",
    "SPEND_DIARY_DIR",
)


def _job_extra_env(entity_id: str, *, batch_path: str | None, automatic_run_id: int) -> dict[str, str]:
    """Copy diagnostic env into the analysis subprocess; production extra_env stays spend-diary only."""
    from openai_api.spend_diary import BATCH_ENV, RUN_ENV

    extra = {
        BATCH_ENV: str(batch_path or ""),
        RUN_ENV: str(automatic_run_id),
    }
    for key in DIAGNOSTIC_JOB_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            extra[key] = value
    allow_entity = extra.get("BITRIX_TRACE_ALLOW_ENTITY_ID", "").strip().lower()
    if allow_entity in {"1", "true", "yes", "on"}:
        extra["BITRIX_TRACE_ENTITY_ID"] = str(entity_id)
    if extra.get("BITRIX_USAGE_DAILY_DIR") and not extra.get("BITRIX_TRACE_COMPONENT"):
        extra["BITRIX_TRACE_COMPONENT"] = "per_deal_context"
    return extra


def _launch_automatic_run_jobs(
    *,
    db_path: str | Path,
    automatic_run_id: int,
    deal_ids: list[str],
    batch_path: str | None,
    started_at: str,
    refresh_plans: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    from api.jobs import AnalyzeOptions, start_analyze_job

    job_ids: list[str] = []
    for entity_id in deal_ids:
        plan = (refresh_plans or {}).get(entity_id)
        if plan is None:
            from storage.rop_db import get_crm_sync_state, crm_trajectory_signal_versions
            ack = get_crm_sync_state(db_path, f"deal_ack:{entity_id}")
            plan = {"mode": "full", "reasons": ["recovery"],
                    "ack_revision": (ack or {}).get("revision", 0),
                    "events": {f"deal:{entity_id}": crm_trajectory_signal_versions(db_path).get(f"deal:{entity_id}", 0)}}
        try:
            job = start_analyze_job(
                AnalyzeOptions(
                    entity_type="deal",
                    ids=[entity_id],
                    force_llm=False,
                    analyze=True,
                    download_audio=True,
                    transcribe_audio=True,
                    transcript_mode="all",
                    automatic_analysis_run_id=automatic_run_id,
                    storage_db_path=str(db_path),
                    context_refresh_mode=plan["mode"],
                    sync_plan=plan,
                    extra_env=_job_extra_env(
                        entity_id,
                        batch_path=batch_path,
                        automatic_run_id=automatic_run_id,
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001 - do not duplicate a manual job that won the race
            update_automatic_analysis_item(
                db_path,
                automatic_run_id,
                entity_type="deal",
                entity_id=entity_id,
                stage="error",
                decision_status="error",
                error=f"Не удалось поставить в очередь: {type(error).__name__}",
                publication_status="error",
                processing_status="error",
                current_stage="error",
            )
            continue
        job_id = str(job.get("job_id") or "")
        if job_id:
            job_ids.append(job_id)
            update_automatic_analysis_item(
                db_path,
                automatic_run_id,
                entity_type="deal",
                entity_id=entity_id,
                job_id=job_id,
                processing_status="queued",
            )
    if job_ids:
        attach_automatic_analysis_job_id(db_path, automatic_run_id, job_ids[0])
    monitor = threading.Thread(
        target=_monitor_automatic_run,
        kwargs={
            "db_path": db_path,
            "run_id": automatic_run_id,
            "job_ids": job_ids,
            "batch_path": batch_path,
            "started_at": started_at,
        },
        name=f"automatic-analysis-run-{automatic_run_id}",
        daemon=True,
    )
    monitor.start()
    return job_ids


def resume_automatic_analysis_runs(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    """Recover unfinished per-deal work after an API/container restart."""
    interrupt_running_automatic_analysis_runs(db_path)
    resumed = 0
    for run in list_recoverable_automatic_analysis_runs(db_path):
        run_id = int(run["id"])
        deal_ids = requeue_unfinished_automatic_analysis_items(db_path, run_id)
        if not deal_ids:
            _finish_automatic_run(
                db_path,
                run_id,
                batch_path=str(run.get("spend_batch_path") or "") or None,
                started_at=str(run.get("started_at") or utcish_now()),
            )
            continue
        _launch_automatic_run_jobs(
            db_path=db_path,
            automatic_run_id=run_id,
            deal_ids=deal_ids,
            batch_path=str(run.get("spend_batch_path") or "") or None,
            started_at=str(run.get("started_at") or utcish_now()),
        )
        resumed += len(deal_ids)
    return resumed


def run_daytime_cycle(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    now: datetime | None = None,
    trigger: str = "scheduled",
    refresh_fn: Callable[..., dict[str, Any]] | None = None,
    collect_fn: Callable[..., dict[str, Any]] | None = None,
    analyze_fn: Callable[..., dict[str, Any]] | None = None,
    make_client_fn: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """One cycle: dashboard sync → manager-trajectory facts → change-aware analysis.

    The clock tick itself is not an LLM trigger. FULL/MINI/skip stay in the
    existing decision engine. CRM facts are stored as facts, not interpretations.
    """
    if not _run_lock.acquire(blocking=False):
        logger.warning("Пропуск дневного цикла: предыдущий запуск ещё выполняется (%s).", trigger)
        return _store_cycle_result(
            {
                "status": "skipped_locked",
                "trigger": trigger,
                "started_at": utcish_now(),
                "finished_at": utcish_now(),
            }
        )

    started = _aware(now or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    started_at = _iso(started)
    errors: list[str] = []
    sync_payload: dict[str, Any] | None = None
    trajectory_payload: dict[str, Any] | None = None
    analysis_payload: dict[str, Any] | None = None
    deal_ids: list[str] = []
    phase_seconds: dict[str, float] = {}
    cycle_started_monotonic = monotonic()
    cycle_file_lock = None
    logger.info("Начало дневного цикла (%s) в %s МСК.", trigger, started_at)
    try:
        scope = get_deal_control_scope(db_path)
        if not scope.get("configured"):
            payload = {
                "status": "skipped_unconfigured",
                "trigger": trigger,
                "started_at": started_at,
                "finished_at": utcish_now(),
                "message": "Выборка сделок deal-control ещё не настроена.",
            }
            logger.info("Дневной цикл пропущен: %s", payload["message"])
            _persist_skipped_automatic_run(
                db_path,
                trigger=trigger,
                status="skipped_unconfigured",
                started_at=started_at,
            )
            _write_spend_diary(payload, started=started, analysis_payload=None)
            return _store_cycle_result(payload)

        from api.candidates import make_client
        from api.deal_control import refresh_deal_control
        from api.manager_trajectory import collect_manager_trajectory
        from bitrix.context_sync import local_sync_lock
        candidate_lock = local_sync_lock(Path(db_path).with_suffix(".daytime-cycle.lock"))
        try:
            candidate_lock.__enter__()
        except RuntimeError:
            return _store_cycle_result({"status": "skipped_locked", "trigger": trigger,
                "started_at": started_at, "finished_at": utcish_now()})
        cycle_file_lock = candidate_lock

        client_factory = make_client_fn or make_client
        refresh = refresh_fn or refresh_deal_control
        collect = collect_fn or collect_manager_trajectory
        analyze = analyze_fn or (
            lambda **kwargs: _analyze_work_pool(
                db_path=kwargs["db_path"],
                deal_ids=kwargs["deal_ids"],
                started_at=kwargs["started_at"],
                trigger=kwargs.get("trigger") or trigger,
                sources_ok=not errors,
            )
        )

        phase_started = monotonic()
        try:
            logger.info("Bitrix sync рабочего пула сделок.")
            sync_payload = refresh(db_path=db_path, client=client_factory(), now=started)
            sync_errors = list(sync_payload.get("sync_errors") or []) if isinstance(sync_payload, dict) else []
            errors.extend(str(item) for item in sync_errors if item)
            logger.info(
                "Bitrix sync завершён: %s",
                (sync_payload or {}).get("sync_message") or "CRM обновлена",
            )
        except Exception as error:  # noqa: BLE001 - keep later steps and the next tick alive
            message = f"Bitrix sync: {error}"
            errors.append(message)
            logger.error("%s", message)
        finally:
            phase_seconds["deal_control_sync"] = round(monotonic() - phase_started, 3)

        deal_ids = [
            str(item["deal_id"])
            for item in list_deal_control_deals(db_path, active_only=True)
            if str(item.get("deal_id") or "").strip()
        ]
        logger.info("Рабочий пул после sync: %s сделок.", len(deal_ids))

        phase_started = monotonic()
        try:
            logger.info("Сбор CRM-фактов manager trajectory.")
            trajectory_payload = collect(client_factory(), db_path=db_path)
            counts = (trajectory_payload or {}).get("counts") or {}
            logger.info(
                "Manager trajectory: status=%s, activities=%s, stage_changes=%s",
                (trajectory_payload or {}).get("status"),
                counts.get("activities"),
                counts.get("stage_changes"),
            )
            for source, error in ((trajectory_payload or {}).get("errors") or {}).items():
                errors.append(f"trajectory {source}: {error}")
        except Exception as error:  # noqa: BLE001 - analysis can still use already synced workspaces
            message = f"Manager trajectory: {error}"
            errors.append(message)
            logger.error("%s", message)
        finally:
            phase_seconds["manager_trajectory"] = round(monotonic() - phase_started, 3)

        phase_started = monotonic()
        try:
            logger.info("Change detection / decision engine для %s сделок.", len(deal_ids))
            analysis_payload = analyze(
                db_path=db_path,
                deal_ids=deal_ids,
                started_at=started_at,
                trigger=trigger,
            )
            counts = (analysis_payload or {}).get("counts") or _empty_decision_counts()
            logger.info(
                "Решения: проверено=%s, изменилось=%s, FULL=%s, MINI=%s, skip=%s, ошибки=%s",
                counts.get("checked"),
                counts.get("changed"),
                counts.get("full"),
                counts.get("mini"),
                counts.get("skip"),
                counts.get("error"),
            )
            if analysis_payload and analysis_payload.get("error"):
                errors.append(f"analysis: {analysis_payload['error']}")
        except Exception as error:  # noqa: BLE001 - the next scheduled tick must still run
            message = f"Analysis: {error}"
            errors.append(message)
            logger.error("%s", message)
            analysis_payload = {
                "status": "error",
                "error": str(error),
                "counts": _empty_decision_counts() | {"checked": len(deal_ids), "error": len(deal_ids)},
            }
        finally:
            phase_seconds["change_detection_and_enqueue"] = round(monotonic() - phase_started, 3)

        status = "success" if not errors else "partial" if (sync_payload or trajectory_payload or analysis_payload) else "error"
        counts = (analysis_payload or {}).get("counts") or _empty_decision_counts()
        payload = {
            "status": status,
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": utcish_now(),
            "deal_ids": deal_ids,
            "sync_message": (sync_payload or {}).get("sync_message") if isinstance(sync_payload, dict) else None,
            "trajectory": {
                "status": (trajectory_payload or {}).get("status"),
                "counts": (trajectory_payload or {}).get("counts") or {},
            },
            "decisions": counts,
            "analysis_job_id": (analysis_payload or {}).get("job_id"),
            "automatic_analysis_run_id": (analysis_payload or {}).get("automatic_analysis_run_id"),
            "busy_ids": (analysis_payload or {}).get("busy_ids") or [],
            "phase_seconds": {
                **phase_seconds,
                "tick_total": round(monotonic() - cycle_started_monotonic, 3),
            },
            "errors": errors,
        }
        if errors:
            logger.warning("Дневной цикл завершён с ошибками: %s", "; ".join(errors))
        else:
            logger.info("Дневной цикл завершён без ошибок.")
        _write_spend_diary(payload, started=started, analysis_payload=analysis_payload)
        return _store_cycle_result(payload)
    finally:
        if cycle_file_lock is not None:
            cycle_file_lock.__exit__(None, None, None)
        _run_lock.release()


def _set_next_at(value: datetime | None) -> None:
    global _next_at
    with _state_lock:
        _next_at = _iso(value) if value is not None else None


def _publish_planning_report(due: datetime) -> dict[str, Any]:
    """Read-only CRM sync then snapshot; do not wait for in-flight LLM jobs."""
    from api.daily_control import publish_planning_daily_control_report

    started_at = _iso(due)
    try:
        report = publish_planning_daily_control_report(now=due)
        payload = {
            "status": "success",
            "trigger": "planning_report",
            "started_at": started_at,
            "finished_at": utcish_now(),
            "daily_control_report_id": report.get("id"),
            "creation_kind": report.get("creation_kind"),
            "source_status": report.get("source_status"),
            "warnings": report.get("warnings") or [],
        }
        logger.info(
            "Планировочный daily-control готов: id=%s, cutoff=%s.",
            report.get("id"),
            report.get("cutoff_at"),
        )
        return _store_cycle_result(payload)
    except Exception as error:  # noqa: BLE001 - the next scheduled tick must still run
        logger.exception("Не удалось опубликовать планировочный daily-control.")
        return _store_cycle_result(
            {
                "status": "error",
                "trigger": "planning_report",
                "started_at": started_at,
                "finished_at": utcish_now(),
                "errors": [str(error)],
            }
        )


def _start_interval_cycle(due: datetime) -> None:
    """Keep slow CRM/analysis work off the publication clock; never overlap workers."""
    global _cycle_thread
    if _cycle_thread is not None and _cycle_thread.is_alive():
        logger.info("Предыдущий дневной цикл ещё выполняется; новый цикл не запускается.")
        return

    def run() -> None:
        try:
            run_daytime_cycle(trigger="evening_22" if due.time() == EVENING_CYCLE_TIME else "interval_30m", now=due)
        except Exception:  # noqa: BLE001 - a failed worker must not kill future ticks
            logger.exception("Необработанная ошибка дневного цикла; следующий слот остаётся в расписании.")

    _cycle_thread = threading.Thread(
        target=run,
        name="neuro-rop-daytime-worker",
        daemon=True,
    )
    _cycle_thread.start()


def _planning_report_is_due(now: datetime, published_on: date | None, report_time: time = PLANNING_REPORT_TIME) -> bool:
    current = _aware(now).astimezone(MSK_TZ)
    return (
        _is_workday(current.date())
        and current.time() >= report_time
        and current.date() != published_on
    )


def _publish_day_end_report(due: datetime) -> dict[str, Any]:
    from api.daily_control import publish_day_end_daily_control_report

    try:
        report = publish_day_end_daily_control_report(now=due)
        return _store_cycle_result({"status": "success", "trigger": "day_end_report", "started_at": _iso(due),
                                    "finished_at": utcish_now(), "daily_control_report_id": report.get("id")})
    except Exception as error:  # noqa: BLE001 - leave the next slot alive
        logger.exception("Не удалось опубликовать итоговый daily-control.")
        return _store_cycle_result({"status": "error", "trigger": "day_end_report", "started_at": _iso(due),
                                    "finished_at": utcish_now(), "errors": [str(error)]})


def _scheduler_loop() -> None:
    logger.info("Планировщик: будни 07:00–18:00 каждые 30 минут, цикл в 22:00, отчёты в 15:45 и 23:00 МСК.")
    published_on: date | None = None
    day_end_on: date | None = None
    while not _stop_event.is_set():
        current = datetime.now(MSK_TZ)
        # Catch up after a restart or delayed tick using the actual publication time.
        # The existing SQLite uniqueness constraint also protects across restarts.
        if _planning_report_is_due(current, published_on):
            result = _publish_planning_report(current)
            if result.get("status") == "success":
                published_on = current.date()
        if _planning_report_is_due(current, day_end_on, DAY_END_REPORT_TIME):
            result = _publish_day_end_report(current)
            if result.get("status") == "success":
                day_end_on = current.date()
        due = next_scheduled_at()
        _set_next_at(due)
        wait_seconds = max(0.0, (due - datetime.now(MSK_TZ)).total_seconds())
        logger.info("Следующий дневной цикл: %s МСК.", _iso(due))
        if _stop_event.wait(timeout=wait_seconds):
            break
        try:
            if due.time() not in {PLANNING_REPORT_TIME, DAY_END_REPORT_TIME}:
                _start_interval_cycle(due)
        except Exception:  # noqa: BLE001 - a failed tick must not kill the loop
            logger.exception("Необработанная ошибка дневного цикла; следующий слот остаётся в расписании.")
    _set_next_at(None)
    logger.info("Планировщик дневного цикла остановлен.")


def start_daytime_cycle() -> None:
    global _thread
    if not daytime_cycle_enabled():
        logger.info("Дневной цикл Bitrix отключён.")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_scheduler_loop, name="neuro-rop-daytime-cycle", daemon=True)
    _thread.start()
    logger.info("Дневной цикл Bitrix запущен в процессе API.")


def stop_daytime_cycle(*, timeout: float = 2.0) -> None:
    global _thread
    _stop_event.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    if thread is not None and not thread.is_alive():
        _thread = None
    _set_next_at(None)
