"""
FastAPI entrypoint for local ROP assistant UI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.candidates import (
    DEFAULT_DAYS,
    DEFAULT_LIMIT,
    custom_period_bounds,
    list_crm_pipelines,
    profile_period_bounds,
    profile_candidates_preview,
    search_candidates,
)
from api.jobs import (
    AnalyzeOptions,
    build_lead_report_meta,
    extract_summary_fields,
    get_job,
    list_jobs,
    parse_ids,
    start_analyze_job,
    unwrap_analysis_payload,
    workspace_dir,
)
from api.compact_shadow import get_compact_job, get_evidence, review_payload, start_compact_job
from openai_api.bitrix_links import bitrix_entity_url
from setup import BASE_DIR
from storage.rop_db import (
    DEFAULT_DB_PATH,
    attach_job_to_daily_summary,
    complete_daily_summary_item,
    create_daily_summary_run,
    create_analysis_profile,
    delete_analysis_profile,
    fail_orphaned_daily_summary_items,
    get_analysis_profile,
    get_daily_summary_run,
    get_last_analysis_profile,
    get_latest_ui_report,
    get_candidate_filter,
    get_candidate_review_states,
    get_lead_workflow_state,
    get_compact_shadow_run,
    get_ui_report,
    init_db,
    list_analysis_profiles,
    list_daily_summary_runs,
    list_outcomes,
    list_qualification_reviews,
    list_rop_decisions,
    list_entity_ui_reports,
    list_ui_reports,
    prepare_daily_summary_items,
    save_candidate_filter,
    save_compact_shadow_feedback,
    save_outcome,
    save_qualification_review,
    save_rop_decision,
    set_last_analysis_profile,
    update_analysis_profile,
    upsert_candidate_review_state,
    upsert_lead_workflow_state,
)


load_dotenv(BASE_DIR / ".env")
init_db(DEFAULT_DB_PATH)

app = FastAPI(title="Помощник РОПа API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    entity_type: Literal["lead", "deal", "auto"] = "auto"
    ids: str | list[str]
    history_days: int = 60
    include_related: bool = True
    include_internal: bool = True
    download_audio: bool = True
    redownload_audio: bool = False
    transcribe_audio: bool = True
    analyze: bool = True
    force_llm: bool = False
    confirm_paid: bool = False
    transcript_mode: Literal["all", "latest", "none"] = "all"


class DecisionRequest(BaseModel):
    decision: str
    comment: str | None = None
    next_control_date: str | None = None


class LeadWorkflowRequest(BaseModel):
    source_report_id: int | None = None
    manager_review_text: str | None = Field(default=None, max_length=12000)
    manager_task_text: str | None = Field(default=None, max_length=12000)
    review_completed: bool | None = None
    task_completed: bool | None = None
    control_mode: Literal["days", "date", "daily"] | None = None
    control_days: int | None = Field(default=None, ge=1, le=365)
    control_date: str | None = Field(default=None, max_length=40)
    control_completed: bool | None = None
    final_decision: Literal["continue", "no_attention"] | None = None


class LeadNoAttentionRequest(BaseModel):
    report_id: int = Field(gt=0)


class OutcomeRequest(BaseModel):
    outcome_type: str
    deal_stage_after: str | None = None
    payment_status: str | None = None
    manager_action_done: bool | None = None
    notes: str | None = None


class CompactFeedbackRequest(BaseModel):
    result: Literal["correct", "partly_correct", "error"]
    reason: str | None = Field(default=None, max_length=120)
    comment: str | None = Field(default=None, max_length=800)


class QualificationReviewRequest(BaseModel):
    is_correct: bool
    issue_fields: list[Literal["budget", "authority", "need", "timeframe", "category", "solution_fit", "commercial_fit"]] = Field(default_factory=list)
    corrected_statuses: dict[str, Literal["confirmed", "not_confirmed", "negative", "unknown"]] = Field(default_factory=dict)
    corrected_category: Literal["A", "B", "C", "D", "E", "unknown"] | None = None
    comment: str | None = Field(default=None, max_length=800)


class CandidatesSearchRequest(BaseModel):
    entity_type: Literal["lead", "deal"] = "lead"
    created_days: int = Field(default=DEFAULT_DAYS, ge=0)
    modified_days: int = Field(default=DEFAULT_DAYS, ge=0)
    days: int | None = Field(default=None, ge=0, description="Устаревший alias для created_days")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=100)
    priority: Literal["high", "medium", "low"] | None = None
    pipeline_ids: list[str] = Field(default_factory=list)
    stage_ids: list[str] = Field(default_factory=list)
    review_view: Literal["active", "reviewed", "all"] = "active"
    lead_categories: list[Literal["A", "B", "C", "D", "E", "unknown"]] = Field(default_factory=list)
    bant_filter: Literal["", "complete", "incomplete", "budget", "authority", "need", "timeframe", "negative", "unknown"] = ""
    save: bool = True


class CandidateFilterSaveRequest(BaseModel):
    entity_type: Literal["lead", "deal"] = "lead"
    created_days: int = Field(default=DEFAULT_DAYS, ge=0)
    modified_days: int = Field(default=DEFAULT_DAYS, ge=0)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=100)
    priority: Literal["high", "medium", "low"] | None = None
    pipeline_ids: list[str] = Field(default_factory=list)
    stage_ids: list[str] = Field(default_factory=list)
    review_view: Literal["active", "reviewed", "all"] = "active"
    lead_categories: list[Literal["A", "B", "C", "D", "E", "unknown"]] = Field(default_factory=list)
    bant_filter: Literal["", "complete", "incomplete", "budget", "authority", "need", "timeframe", "negative", "unknown"] = ""


class AnalysisProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    profile: dict[str, Any] = Field(default_factory=dict)


class AnalysisProfilePreviewRequest(BaseModel):
    period_preset: Literal["today_and_previous_workday", "today", "previous_workday", "custom"] | None = None
    date_from: date | None = None
    date_to: date | None = None


class DailySummaryCreateRequest(BaseModel):
    profile_id: int
    profile_version: int
    preview: dict[str, Any]
    selected_journey_keys: list[str] = Field(default_factory=list)


class DailySummaryStartRequest(BaseModel):
    confirm_paid: bool = False


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "rop-assistant-api",
        "db_path": str(DEFAULT_DB_PATH),
    }


@app.get("/api/pipelines")
def pipelines() -> dict[str, Any]:
    return list_crm_pipelines()


@app.get("/api/candidate-filters")
def candidate_filters_get() -> dict[str, Any]:
    return {"filter": get_candidate_filter(DEFAULT_DB_PATH)}


@app.put("/api/candidate-filters")
def candidate_filters_put(body: CandidateFilterSaveRequest) -> dict[str, Any]:
    saved = save_candidate_filter(
        DEFAULT_DB_PATH,
        {
            "entity_type": body.entity_type,
            "created_days": body.created_days,
            "modified_days": body.modified_days,
            "limit": body.limit,
            "priority": body.priority,
            "pipeline_ids": body.pipeline_ids,
            "stage_ids": body.stage_ids,
            "review_view": body.review_view,
            "lead_categories": body.lead_categories,
            "bant_filter": body.bant_filter,
        },
    )
    return {"ok": True, "filter": saved}


@app.get("/api/analysis-profiles")
def analysis_profiles() -> dict[str, Any]:
    return {
        "items": list_analysis_profiles(DEFAULT_DB_PATH),
        "selected": get_last_analysis_profile(DEFAULT_DB_PATH),
    }


@app.post("/api/analysis-profiles")
def analysis_profile_create(body: AnalysisProfileRequest) -> dict[str, Any]:
    try:
        profile = create_analysis_profile(
            DEFAULT_DB_PATH,
            name=body.name,
            profile=body.profile,
        )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="Профиль с таким названием уже существует") from error
    selected = set_last_analysis_profile(DEFAULT_DB_PATH, int(profile["id"]))
    return {"ok": True, "profile": selected}


@app.put("/api/analysis-profiles/{profile_id}")
def analysis_profile_update(profile_id: int, body: AnalysisProfileRequest) -> dict[str, Any]:
    if not get_analysis_profile(DEFAULT_DB_PATH, profile_id):
        raise HTTPException(status_code=404, detail="Профиль не найден")
    try:
        profile = update_analysis_profile(
            DEFAULT_DB_PATH,
            profile_id,
            name=body.name,
            profile=body.profile,
        )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="Профиль с таким названием уже существует") from error
    return {"ok": True, "profile": profile}


@app.delete("/api/analysis-profiles/{profile_id}")
def analysis_profile_delete(profile_id: int) -> dict[str, Any]:
    if not get_analysis_profile(DEFAULT_DB_PATH, profile_id):
        raise HTTPException(status_code=404, detail="Профиль не найден")
    try:
        selected_id = delete_analysis_profile(DEFAULT_DB_PATH, profile_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "ok": True,
        "selected": get_analysis_profile(DEFAULT_DB_PATH, selected_id),
        "items": list_analysis_profiles(DEFAULT_DB_PATH),
    }


@app.put("/api/analysis-profiles/{profile_id}/selected")
def analysis_profile_select(profile_id: int) -> dict[str, Any]:
    try:
        profile = set_last_analysis_profile(DEFAULT_DB_PATH, profile_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Профиль не найден") from error
    return {"ok": True, "selected": profile}


@app.post("/api/analysis-profiles/{profile_id}/preview")
def analysis_profile_preview(profile_id: int, body: AnalysisProfilePreviewRequest | None = None) -> dict[str, Any]:
    profile = get_analysis_profile(DEFAULT_DB_PATH, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    try:
        profile_settings = profile.get("profile") if isinstance(profile.get("profile"), dict) else {}
        timezone_name = str(profile_settings.get("timezone") or "Europe/Moscow")
        preset = body.period_preset if body and body.period_preset else str(profile_settings.get("period_preset") or "today_and_previous_workday")
        if preset == "custom":
            if not body or not body.date_from or not body.date_to:
                raise ValueError("Для произвольного периода укажите обе даты")
            period = custom_period_bounds(body.date_from, body.date_to, timezone_name=timezone_name)
        else:
            period = profile_period_bounds(preset, timezone_name=timezone_name)
        return profile_candidates_preview(profile, db_path=DEFAULT_DB_PATH, period_override=period)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/daily-summaries")
def daily_summary_create(body: DailySummaryCreateRequest) -> dict[str, Any]:
    profile = get_analysis_profile(DEFAULT_DB_PATH, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    if int(profile.get("version") or 0) != body.profile_version:
        raise HTTPException(status_code=409, detail="Профиль изменился после preview — обновите список кандидатов")
    candidates = body.preview.get("candidates")
    period = body.preview.get("period")
    scope = body.preview.get("scope")
    cost_preview = body.preview.get("cost_preview")
    if not isinstance(candidates, list) or not isinstance(period, dict) or not isinstance(scope, dict) or not isinstance(cost_preview, dict):
        raise HTTPException(status_code=400, detail="Некорректный snapshot preview")
    known_keys = {str(item.get("journey_key") or "") for item in candidates if isinstance(item, dict)}
    unknown = [key for key in body.selected_journey_keys if key not in known_keys]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Неизвестные кандидаты: {', '.join(unknown[:5])}")
    return create_daily_summary_run(
        DEFAULT_DB_PATH,
        profile=profile,
        period=period,
        scope=scope,
        candidates=[item for item in candidates if isinstance(item, dict)],
        selected_journey_keys=body.selected_journey_keys,
        cost_preview=cost_preview,
    )


@app.get("/api/daily-summaries")
def daily_summaries(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    items = list_daily_summary_runs(DEFAULT_DB_PATH, limit=limit)
    for item in items:
        if item.get("status") != "analyzing":
            continue
        job_ids = {value for value in str(item.get("job_id") or "").split(",") if value}
        active_job_ids = {job_id for job_id in job_ids if get_job(job_id)}
        if active_job_ids != job_ids:
            fail_orphaned_daily_summary_items(
                DEFAULT_DB_PATH,
                int(item["id"]),
                active_job_ids=active_job_ids,
            )
    return {"items": list_daily_summary_runs(DEFAULT_DB_PATH, limit=limit)}


@app.get("/api/daily-summaries/{run_id}")
def daily_summary(run_id: int) -> dict[str, Any]:
    value = get_daily_summary_run(DEFAULT_DB_PATH, run_id)
    if not value:
        raise HTTPException(status_code=404, detail="Сводка не найдена")
    job_ids = [item for item in str(value.get("job_id") or "").split(",") if item]
    job_states = [job for job_id in job_ids if (job := get_job(job_id))]
    if value.get("status") == "analyzing" and len(job_states) < len(job_ids):
        fail_orphaned_daily_summary_items(
            DEFAULT_DB_PATH,
            run_id,
            active_job_ids={str(job.get("job_id") or "") for job in job_states},
        )
        value = get_daily_summary_run(DEFAULT_DB_PATH, run_id) or value
    results = [result for job in job_states for result in job.get("results") or []]
    seen_results = {(str(item.get("entity_type")), str(item.get("entity_id"))) for item in results}
    for item in value.get("items") or []:
        report_id = item.get("report_id")
        result_key = (str(item.get("entity_type")), str(item.get("entity_id")))
        if not report_id or result_key in seen_results:
            continue
        report = get_ui_report(DEFAULT_DB_PATH, int(report_id))
        if not report:
            continue
        analysis = report.get("report_json") if isinstance(report.get("report_json"), dict) else None
        summary = extract_summary_fields(analysis or {}, result_key[0])
        results.append(
            {
                "entity_type": result_key[0],
                "entity_id": result_key[1],
                "report_id": int(report_id),
                "has_analysis": analysis is not None,
                "has_markdown": bool(report.get("report_path")),
                "risk_level": summary.get("risk_level"),
                "attention_reason": summary.get("attention_reason"),
                "recommended_action": summary.get("recommended_action"),
                "lead_category": summary.get("lead_category"),
                "lead_route_status": summary.get("lead_route_status"),
                "lead_qualification": summary.get("lead_qualification"),
                "bitrix_url": bitrix_entity_url(result_key[0], result_key[1]),
                "analysis": analysis,
            }
        )
        seen_results.add(result_key)
    if job_states and value.get("status") in {"draft", "analyzing"}:
        statuses = {str(job.get("status") or "") for job in job_states}
        if statuses <= {"done"}:
            value["status"] = "done"
        elif statuses <= {"error"}:
            has_partial_result = any(
                result.get("report_id") or result.get("has_analysis")
                for job in job_states
                for result in job.get("results") or []
            )
            value["status"] = "completed_with_errors" if has_partial_result else "error"
        elif "error" in statuses and statuses <= {"done", "error"}:
            value["status"] = "completed_with_errors"
        else:
            value["status"] = "analyzing"
    value["job_states"] = job_states
    value["results"] = results
    return value


@app.post("/api/daily-summaries/{run_id}/start")
def daily_summary_start(run_id: int, body: DailySummaryStartRequest) -> dict[str, Any]:
    run = get_daily_summary_run(DEFAULT_DB_PATH, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Сводка не найдена")
    if run.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Эта сводка уже была запущена")
    selected = [item for item in run.get("items") or [] if item.get("selected")]
    paid = [
        item for item in selected
        if str((item.get("candidate") or {}).get("analysis_freshness") or "missing") in {"missing", "changed", "failed"}
    ]
    if paid and not body.confirm_paid:
        raise HTTPException(
            status_code=409,
            detail=f"Требуется подтверждение платного анализа: до {run.get('llm_allowed_count', 0)} карточек",
        )
    paid_allowed = int(run.get("llm_allowed_count") or 0)
    paid_keys = {str(item.get("journey_key")) for item in paid[:paid_allowed]}
    eligible: list[dict[str, Any]] = []
    for item in selected:
        candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
        requires_paid = str(item.get("journey_key")) in paid_keys
        if str(candidate.get("analysis_freshness") or "missing") in {"missing", "changed", "failed"} and not requires_paid:
            continue
        eligible.append(item)
    prepare_daily_summary_items(
        DEFAULT_DB_PATH,
        run_id,
        [str(item.get("journey_key") or "") for item in eligible],
    )
    paid_eligible = [item for item in eligible if str(item.get("journey_key")) in paid_keys]
    reused_count = 0
    for item in eligible:
        if str(item.get("journey_key")) in paid_keys:
            continue
        entity_type = str(item.get("entity_type") or "")
        entity_id = str(item.get("entity_id") or "")
        report = get_latest_ui_report(
            DEFAULT_DB_PATH,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if report:
            complete_daily_summary_item(
                DEFAULT_DB_PATH,
                run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                report_id=int(report["id"]),
            )
            reused_count += 1
        else:
            complete_daily_summary_item(
                DEFAULT_DB_PATH,
                run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                report_id=None,
                error="Свежий сохранённый отчёт не найден; платный анализ не запускался.",
            )
    options_payload = run.get("profile_snapshot") if isinstance(run.get("profile_snapshot"), dict) else {}
    analysis = options_payload.get("analysis") if isinstance(options_payload.get("analysis"), dict) else {}
    jobs_started = []
    for entity_type in ("lead", "deal"):
        ids = [
            str(item.get("entity_id") or "")
            for item in paid_eligible
            if item.get("entity_type") == entity_type
        ]
        if not ids:
            continue
        options = AnalyzeOptions(
            entity_type=entity_type,
            ids=ids,
            history_days=int(analysis.get("history_days") or 60),
            include_related=bool(analysis.get("include_related", True)),
            include_internal=bool(analysis.get("include_internal", True)),
            download_audio=bool(analysis.get("download_audio", True)),
            redownload_audio=bool(analysis.get("redownload_audio", False)),
            transcribe_audio=bool(analysis.get("transcribe_audio", True)),
            analyze=True,
            force_llm=False,
            transcript_mode=str(analysis.get("transcript_mode") or "all"),
            daily_summary_run_id=run_id,
        )
        jobs_started.append(start_analyze_job(options))
    if not jobs_started and not eligible and selected:
        raise HTTPException(status_code=409, detail="Платный лимит равен нулю или все новые карточки ждут ёмкости")
    job_ids = [str(job.get("job_id") or "") for job in jobs_started]
    updated = (
        attach_job_to_daily_summary(DEFAULT_DB_PATH, run_id, ",".join(job_ids))
        if job_ids
        else get_daily_summary_run(DEFAULT_DB_PATH, run_id) or {}
    )
    return {
        "summary": updated,
        "jobs": jobs_started,
        "started_count": len(eligible),
        "reused_count": reused_count,
    }


@app.get("/api/candidates")
def candidates(
    entity_type: Literal["lead", "deal"] = "lead",
    created_days: int = Query(default=DEFAULT_DAYS, ge=0),
    modified_days: int = Query(default=DEFAULT_DAYS, ge=0),
    days: int | None = Query(default=None, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=100),
    priority: Literal["high", "medium", "low"] | None = None,
    pipeline_ids: list[str] = Query(default=[]),
    stage_ids: list[str] = Query(default=[]),
    review_view: Literal["active", "reviewed", "all"] = "active",
    lead_categories: list[Literal["A", "B", "C", "D", "E", "unknown"]] = Query(default=[]),
    bant_filter: Literal["", "complete", "incomplete", "budget", "authority", "need", "timeframe", "negative", "unknown"] = "",
) -> dict[str, Any]:
    try:
        return search_candidates(
            entity_type=entity_type,
            created_days=created_days,
            modified_days=modified_days,
            days=days,
            limit=limit,
            priority=priority,
            pipeline_ids=pipeline_ids,
            stage_ids=stage_ids,
            review_view=review_view,
            lead_categories=lead_categories,
            bant_filter=bant_filter,
        )
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/candidates/search")
def candidates_search(body: CandidatesSearchRequest) -> dict[str, Any]:
    try:
        if body.save:
            save_candidate_filter(
                DEFAULT_DB_PATH,
                {
                    "entity_type": body.entity_type,
                    "created_days": body.created_days,
                    "modified_days": body.modified_days,
                    "limit": body.limit,
                    "priority": body.priority,
                    "pipeline_ids": body.pipeline_ids,
                    "stage_ids": body.stage_ids,
                    "review_view": body.review_view,
                    "lead_categories": body.lead_categories,
                    "bant_filter": body.bant_filter,
                },
            )
        return search_candidates(
            entity_type=body.entity_type,
            created_days=body.created_days,
            modified_days=body.modified_days,
            days=body.days,
            limit=body.limit,
            priority=body.priority,
            pipeline_ids=body.pipeline_ids,
            stage_ids=body.stage_ids,
            review_view=body.review_view,
            lead_categories=body.lead_categories,
            bant_filter=body.bant_filter,
        )
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/analyze")
def analyze(body: AnalyzeRequest) -> dict[str, Any]:
    ids = parse_ids(body.ids)
    if not ids:
        raise HTTPException(status_code=400, detail="Укажите хотя бы один ID")
    if body.force_llm and not body.confirm_paid:
        raise HTTPException(status_code=409, detail="Для принудительного LLM-анализа подтвердите платный запуск")
    options = AnalyzeOptions(
        entity_type=body.entity_type,
        ids=ids,
        history_days=body.history_days,
        include_related=body.include_related,
        include_internal=body.include_internal,
        download_audio=body.download_audio,
        redownload_audio=body.redownload_audio,
        transcribe_audio=body.transcribe_audio,
        analyze=body.analyze,
        force_llm=body.force_llm,
        transcript_mode=body.transcript_mode,
    )
    return start_analyze_job(options)


@app.get("/api/jobs")
def jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"items": list_jobs(limit)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _enrich_report_row(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize stored envelope/unwrapped JSON and fill empty summary fields for old rows."""
    row = dict(item)
    entity_type = str(row.get("entity_type") or "")
    entity_id = str(row.get("entity_id") or "")
    if entity_type in {"lead", "deal"} and entity_id:
        row["bitrix_url"] = bitrix_entity_url(entity_type, entity_id)
    analysis = unwrap_analysis_payload(row.get("report_json") if isinstance(row.get("report_json"), dict) else {})
    if analysis:
        row["report_json"] = analysis
        summary = extract_summary_fields(analysis, entity_type or "deal")
        if not row.get("risk_level"):
            row["risk_level"] = summary.get("risk_level")
        if not row.get("attention_reason"):
            row["attention_reason"] = summary.get("attention_reason")
        if not row.get("recommended_action"):
            row["recommended_action"] = summary.get("recommended_action")
        if entity_type == "lead":
            row["lead_category"] = summary.get("lead_category")
            row["lead_route_status"] = summary.get("lead_route_status")
            row["lead_qualification"] = summary.get("lead_qualification")
    return row


def _candidate_review_values(report: dict[str, Any]) -> dict[str, str | None]:
    analysis = unwrap_analysis_payload(report.get("report_json") if isinstance(report.get("report_json"), dict) else {})
    deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
    lead_state = analysis.get("lead_state") if isinstance(analysis.get("lead_state"), dict) else {}
    stage_text = str(deal_state.get("stage") or lead_state.get("status") or "")
    stage_id = stage_text.split("/", 1)[0].strip() or None
    return {
        "reviewed_stage_id": stage_id,
        "reviewed_pipeline_id": None,
        "reviewed_amount": str(deal_state.get("amount") or lead_state.get("amount") or "") or None,
        "reviewed_date_modify": None,
    }


def _report_markdown_path(report: dict[str, Any]) -> Path:
    configured_value = str(report.get("report_path") or "").strip()
    configured = Path(configured_value) if configured_value else Path("__missing_report__.md")
    if configured.is_file():
        return configured
    entity_type = str(report.get("entity_type") or "")
    entity_id = str(report.get("entity_id") or "")
    return workspace_dir(entity_type, entity_id) / "analysis" / f"{entity_type}_{entity_id}_rop_report.md"


def _workflow_default_texts(report: dict[str, Any]) -> tuple[str, str]:
    analysis = unwrap_analysis_payload(report.get("report_json") if isinstance(report.get("report_json"), dict) else {})
    manager_quality = analysis.get("manager_quality") if isinstance(analysis.get("manager_quality"), dict) else {}
    rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
    action = analysis.get("manager_action_block") if isinstance(analysis.get("manager_action_block"), dict) else {}

    def lines(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        return [text] if text else []

    review_parts: list[str] = []
    good = lines(manager_quality.get("what_done_well"))
    missed = lines(manager_quality.get("missed_points"))
    critical = lines(manager_quality.get("critical_mistake"))
    if good:
        review_parts.append("Сильные стороны:\n" + "\n".join(f"• {item}" for item in good))
    if missed or critical:
        review_parts.append("Что нужно усилить:\n" + "\n".join(f"• {item}" for item in [*missed, *critical]))
    manager_focus = str(rop.get("message_to_manager") or rop.get("check_for_rop") or "").strip()
    if manager_focus:
        review_parts.append(f"Фокус следующего контакта:\n{manager_focus}")
    if not review_parts:
        fallback = str(rop.get("why_it_matters") or rop.get("check_for_rop") or "").strip()
        if fallback:
            review_parts.append(fallback)

    task_parts = lines(rop.get("message_to_manager"))
    checklist = lines(action.get("manager_checklist"))
    expected = str(rop.get("expected_crm_update") or "").strip()
    deadline = str(rop.get("deadline") or "").strip()
    success = str(rop.get("success_condition") or "").strip()
    if checklist:
        task_parts.append("Что сделать:\n" + "\n".join(f"{index}. {item}" for index, item in enumerate(checklist, 1)))
    if expected:
        task_parts.append(f"Зафиксировать в CRM: {expected}")
    if deadline:
        task_parts.append(f"Срок: {deadline}")
    if success:
        task_parts.append(f"Результат: {success}")
    return "\n\n".join(review_parts), "\n\n".join(task_parts)


def _workflow_status(workflow: dict[str, Any], candidate_review: dict[str, Any] | None) -> str:
    if workflow.get("final_decision") == "no_attention":
        return "Не требует внимания"
    if workflow.get("final_decision") == "continue":
        return "Продолжать работу"
    if workflow.get("control_mode") and not workflow.get("control_completed"):
        return "На контроле"
    if workflow.get("review_completed") or workflow.get("task_completed") or workflow.get("control_completed"):
        return "В работе"
    if candidate_review and candidate_review.get("state") == "reviewed":
        return "Не требует внимания"
    return "Готов к разбору"


def _lead_workflow_payload(lead_id: str, report: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = get_lead_workflow_state(DEFAULT_DB_PATH, lead_id)
    if saved is not None:
        workflow = saved
    else:
        review_text, task_text = _workflow_default_texts(report or {})
        workflow = {
            "lead_id": str(lead_id),
            "source_report_id": report.get("id") if report else None,
            "manager_review_text": review_text,
            "manager_task_text": task_text,
            "review_completed": False,
            "task_completed": False,
            "control_mode": None,
            "control_days": 2,
            "control_date": None,
            "control_completed": False,
            "final_decision": None,
            "created_at": None,
            "updated_at": None,
        }
    candidate_review = get_candidate_review_states(
        DEFAULT_DB_PATH, entity_type="lead", entity_ids=[str(lead_id)]
    ).get(str(lead_id))
    return {**workflow, "status_label": _workflow_status(workflow, candidate_review)}


@app.get("/api/leads/{lead_id}/workflow")
def lead_workflow(lead_id: str, report_id: int | None = None) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id) if report_id is not None else get_latest_ui_report(
        DEFAULT_DB_PATH, entity_type="lead", entity_id=str(lead_id)
    )
    if report and (str(report.get("entity_type")) != "lead" or str(report.get("entity_id")) != str(lead_id)):
        raise HTTPException(status_code=400, detail="Report does not belong to this lead")
    return _lead_workflow_payload(str(lead_id), report)


@app.put("/api/leads/{lead_id}/workflow")
def save_lead_workflow(lead_id: str, body: LeadWorkflowRequest) -> dict[str, Any]:
    lead_id = str(lead_id)
    changes = body.model_dump(exclude_unset=True)
    existing = get_lead_workflow_state(DEFAULT_DB_PATH, lead_id)
    source_report_id = changes.get("source_report_id") or (existing or {}).get("source_report_id")
    report = get_ui_report(DEFAULT_DB_PATH, int(source_report_id)) if source_report_id else get_latest_ui_report(
        DEFAULT_DB_PATH, entity_type="lead", entity_id=lead_id
    )
    if not report or str(report.get("entity_type")) != "lead" or str(report.get("entity_id")) != lead_id:
        raise HTTPException(status_code=400, detail="A lead report is required for workflow")
    defaults = _lead_workflow_payload(lead_id, report)
    merged = {**defaults, **changes, "source_report_id": int(source_report_id or report["id"])}
    control_mode = merged.get("control_mode")
    if control_mode == "days" and not merged.get("control_days"):
        raise HTTPException(status_code=422, detail="control_days is required for days mode")
    if control_mode == "date" and not merged.get("control_date"):
        raise HTTPException(status_code=422, detail="control_date is required for date mode")
    if merged.get("final_decision") and not merged.get("control_completed"):
        raise HTTPException(status_code=422, detail="Complete control before final decision")

    saved = upsert_lead_workflow_state(
        DEFAULT_DB_PATH,
        lead_id=lead_id,
        source_report_id=merged["source_report_id"],
        manager_review_text=merged.get("manager_review_text"),
        manager_task_text=merged.get("manager_task_text"),
        review_completed=bool(merged.get("review_completed")),
        task_completed=bool(merged.get("task_completed")),
        control_mode=control_mode,
        control_days=merged.get("control_days"),
        control_date=merged.get("control_date"),
        control_completed=bool(merged.get("control_completed")),
        final_decision=merged.get("final_decision"),
    )

    previous_final = (existing or {}).get("final_decision")
    previous_control = (existing or {}).get("control_mode")
    next_control_date: str | None = None
    state = "active"
    decision: str | None = None
    if saved.get("final_decision") == "no_attention":
        state, decision = "reviewed", "Не требует внимания"
    elif saved.get("final_decision") == "continue":
        state, decision = "active", "Продолжать работу"
    elif saved.get("control_mode"):
        state, decision = "snoozed", "Назначен контроль"
        if saved.get("control_mode") == "days":
            next_control_date = (datetime.now().date() + timedelta(days=int(saved.get("control_days") or 1))).isoformat()
        elif saved.get("control_mode") == "daily":
            next_control_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        else:
            next_control_date = str(saved.get("control_date") or "") or None
    upsert_candidate_review_state(
        DEFAULT_DB_PATH,
        entity_type="lead",
        entity_id=lead_id,
        state=state,
        report_id=int(report["id"]),
        decision=decision,
        next_control_date=next_control_date,
        **_candidate_review_values(report),
    )
    if decision and (saved.get("final_decision") != previous_final or saved.get("control_mode") != previous_control):
        save_rop_decision(
            DEFAULT_DB_PATH,
            report_id=int(report["id"]),
            decision=decision,
            next_control_date=next_control_date,
        )
    return {**saved, "status_label": _workflow_status(saved, {"state": state})}


@app.post("/api/leads/{lead_id}/no-attention")
def mark_lead_no_attention(lead_id: str, body: LeadNoAttentionRequest) -> dict[str, Any]:
    lead_id = str(lead_id)
    report = get_ui_report(DEFAULT_DB_PATH, body.report_id)
    if not report or str(report.get("entity_type")) != "lead" or str(report.get("entity_id")) != lead_id:
        raise HTTPException(status_code=400, detail="A lead report is required for this decision")

    existing = get_candidate_review_states(
        DEFAULT_DB_PATH, entity_type="lead", entity_ids=[lead_id]
    ).get(lead_id)
    review = upsert_candidate_review_state(
        DEFAULT_DB_PATH,
        entity_type="lead",
        entity_id=lead_id,
        state="reviewed",
        report_id=body.report_id,
        decision="Не требует внимания",
        next_control_date=None,
        **_candidate_review_values(report),
    )
    if not existing or existing.get("state") != "reviewed" or existing.get("decision") != "Не требует внимания":
        save_rop_decision(
            DEFAULT_DB_PATH,
            report_id=body.report_id,
            decision="Не требует внимания",
            next_control_date=None,
        )
    return {
        "workflow": _lead_workflow_payload(lead_id, report),
        "candidate_review": review,
    }


@app.get("/api/reports")
def reports(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    items = list_ui_reports(DEFAULT_DB_PATH, limit=limit)
    # Keep list payload light: drop full analysis JSON.
    light = []
    for item in items:
        row = _enrich_report_row(item)
        row.pop("report_json", None)
        row.pop("report_meta", None)
        row.pop("technical_log", None)
        row.pop("model_context", None)
        light.append(row)
    return {"items": light}


@app.get("/api/reports/{report_id}")
def report_detail(report_id: int, include_markdown: bool = False) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    payload = _enrich_report_row(report)
    if str(report.get("entity_type") or "") == "lead" and not payload.get("report_meta"):
        payload["report_meta"] = build_lead_report_meta(str(report.get("entity_id") or ""))
    payload["decisions"] = list_rop_decisions(DEFAULT_DB_PATH, report_id)
    payload["qualification_reviews"] = list_qualification_reviews(DEFAULT_DB_PATH, report_id)
    payload["outcomes"] = list_outcomes(DEFAULT_DB_PATH, report_id)
    payload["candidate_review"] = get_candidate_review_states(
        DEFAULT_DB_PATH,
        entity_type=str(report.get("entity_type") or ""),
        entity_ids=[str(report.get("entity_id") or "")],
    ).get(str(report.get("entity_id") or ""))
    if str(report.get("entity_type") or "") == "lead":
        payload["workflow"] = _lead_workflow_payload(str(report.get("entity_id") or ""), report)
    related_reports = list_entity_ui_reports(
        DEFAULT_DB_PATH,
        entity_type=str(report.get("entity_type") or ""),
        entity_id=str(report.get("entity_id") or ""),
        limit=20,
    )
    payload["entity_history"] = [
        {
            "id": item.get("id"),
            "created_at": item.get("created_at"),
            "risk_level": item.get("risk_level"),
            "attention_reason": item.get("attention_reason"),
        }
        for item in related_reports
    ]
    payload["markdown_available"] = _report_markdown_path(report).exists()
    payload["technical_log_available"] = bool(report.get("technical_log"))
    if include_markdown:
        md_path = Path(str(report.get("report_path") or ""))
        if md_path.exists():
            payload["report_markdown"] = md_path.read_text(encoding="utf-8")
        else:
            payload["report_markdown"] = None
    return payload


@app.get("/api/reports/{report_id}/markdown")
def report_markdown(report_id: int) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    md_path = _report_markdown_path(report)
    if not md_path.exists():
        raise HTTPException(status_code=404, detail="Markdown report not found")
    return {
        "report_id": report_id,
        "path": str(md_path),
        "markdown": md_path.read_text(encoding="utf-8"),
    }


@app.post("/api/reports/{report_id}/rop-decision")
def report_decision(report_id: int, body: DecisionRequest) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    decision_id = save_rop_decision(
        DEFAULT_DB_PATH,
        report_id=report_id,
        decision=body.decision,
        comment=body.comment,
        next_control_date=body.next_control_date,
    )
    entity_type = str(report.get("entity_type") or "")
    entity_id = str(report.get("entity_id") or "")
    review = None
    if body.decision == "Закрытие обосновано":
        review = upsert_candidate_review_state(
            DEFAULT_DB_PATH,
            entity_type=entity_type,
            entity_id=entity_id,
            state="reviewed",
            report_id=report_id,
            decision=body.decision,
            **_candidate_review_values(report),
        )
    elif body.decision == "Проверить через 2 дня":
        review = upsert_candidate_review_state(
            DEFAULT_DB_PATH,
            entity_type=entity_type,
            entity_id=entity_id,
            state="snoozed",
            report_id=report_id,
            decision=body.decision,
            next_control_date=(datetime.now().date() + timedelta(days=2)).isoformat(),
            **_candidate_review_values(report),
        )
    elif body.decision == "Вернуть в контроль":
        review = upsert_candidate_review_state(
            DEFAULT_DB_PATH,
            entity_type=entity_type,
            entity_id=entity_id,
            state="active",
            report_id=report_id,
            decision="Возвращено РОПом в кандидаты",
        )
    return {
        "ok": True,
        "decision_id": decision_id,
        "decisions": list_rop_decisions(DEFAULT_DB_PATH, report_id),
        "candidate_review": review,
    }


@app.post("/api/reports/{report_id}/outcome")
def report_outcome(report_id: int, body: OutcomeRequest) -> dict[str, Any]:
    if not get_ui_report(DEFAULT_DB_PATH, report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    outcome_id = save_outcome(
        DEFAULT_DB_PATH,
        report_id=report_id,
        outcome_type=body.outcome_type,
        deal_stage_after=body.deal_stage_after,
        payment_status=body.payment_status,
        manager_action_done=body.manager_action_done,
        notes=body.notes,
    )
    return {"ok": True, "outcome_id": outcome_id, "outcomes": list_outcomes(DEFAULT_DB_PATH, report_id)}


@app.post("/api/reports/{report_id}/qualification-review")
def report_qualification_review(report_id: int, body: QualificationReviewRequest) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if str(report.get("entity_type") or "") != "lead":
        raise HTTPException(status_code=400, detail="Qualification review is available only for leads")
    if body.is_correct and (body.issue_fields or body.corrected_statuses or body.corrected_category or body.comment):
        raise HTTPException(status_code=400, detail="Correct review must not contain corrections")
    if not body.is_correct and not body.issue_fields:
        raise HTTPException(status_code=400, detail="Incorrect review requires at least one issue field")
    bant_fields = {"budget", "authority", "need", "timeframe"}
    if set(body.corrected_statuses) - bant_fields:
        raise HTTPException(status_code=400, detail="Corrected statuses are allowed only for BANT fields")
    if set(body.corrected_statuses) - set(body.issue_fields):
        raise HTTPException(status_code=400, detail="Corrected BANT field must be selected as an issue")
    if body.corrected_category and "category" not in body.issue_fields:
        raise HTTPException(status_code=400, detail="Corrected category requires category issue field")
    review_id = save_qualification_review(
        DEFAULT_DB_PATH,
        report_id=report_id,
        is_correct=body.is_correct,
        issue_fields=list(body.issue_fields),
        corrected_statuses=dict(body.corrected_statuses),
        corrected_category=body.corrected_category,
        comment=body.comment,
    )
    return {
        "ok": True,
        "review_id": review_id,
        "qualification_reviews": list_qualification_reviews(DEFAULT_DB_PATH, report_id),
    }


@app.get("/api/entity/{entity_type}/{entity_id}/analysis")
def entity_analysis(entity_type: Literal["lead", "deal"], entity_id: str) -> dict[str, Any]:
    path = workspace_dir(entity_type, entity_id) / "analysis" / f"{entity_type}_{entity_id}_analysis.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Analysis JSON not found")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"Cannot read analysis: {error}") from error
    analysis = unwrap_analysis_payload(envelope if isinstance(envelope, dict) else {})
    md_path = workspace_dir(entity_type, entity_id) / "analysis" / f"{entity_type}_{entity_id}_rop_report.md"
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "analysis_path": str(path),
        "has_markdown": md_path.exists(),
        "analysis": analysis,
    }


@app.get("/api/entity/{entity_type}/{entity_id}/compact-review")
def compact_review(
    entity_type: Literal["lead", "deal"], entity_id: str, run_id: str | None = None
) -> dict[str, Any]:
    """Read only: load a saved full report and separate Compact runs."""
    return review_payload(entity_type, entity_id, selected_run_id=run_id)


@app.post("/api/entity/{entity_type}/{entity_id}/compact-runs")
def compact_run(entity_type: Literal["lead", "deal"], entity_id: str) -> dict[str, Any]:
    try:
        return start_compact_job(entity_type, entity_id)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as error:
        # Do not expose workspace paths or a raw source error in the browser.
        raise HTTPException(
            status_code=409,
            detail="Compact-анализ недоступен: нужен полный анализ с сохранённым контекстом и транскриптом.",
        ) from error


@app.get("/api/compact-jobs/{job_id}")
def compact_job_status(job_id: str) -> dict[str, Any]:
    job = get_compact_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Compact job not found")
    return job


@app.get("/api/entity/{entity_type}/{entity_id}/compact-evidence/{evidence_id}")
def compact_evidence(
    entity_type: Literal["lead", "deal"], entity_id: str, evidence_id: str
) -> dict[str, Any]:
    try:
        source = get_evidence(entity_type, entity_id, evidence_id)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=404, detail="Evidence source is unavailable") from error
    if source is None:
        raise HTTPException(status_code=404, detail="Исходный evidence не найден в переданном контексте")
    return source


@app.put("/api/entity/{entity_type}/{entity_id}/compact-runs/{run_id}/feedback")
def compact_feedback(
    entity_type: Literal["lead", "deal"], entity_id: str, run_id: str, body: CompactFeedbackRequest
) -> dict[str, Any]:
    run = get_compact_shadow_run(DEFAULT_DB_PATH, run_id)
    if not run or run.get("entity_type") != entity_type or str(run.get("entity_id")) != str(entity_id):
        raise HTTPException(status_code=404, detail="Compact run not found")
    analysis = run.get("analysis") if isinstance(run.get("analysis"), dict) else {}
    review_key = "lead_review" if entity_type == "lead" else "deal_review"
    review = analysis.get(review_key) if isinstance(analysis.get(review_key), dict) else {}
    ui_metadata = analysis.get("_ui") if isinstance(analysis.get("_ui"), dict) else {}
    feedback = save_compact_shadow_feedback(
        DEFAULT_DB_PATH,
        compact_run_id=run_id,
        entity_type=entity_type,
        entity_id=entity_id,
        snapshot_hash=str(run.get("snapshot_hash") or ""),
        model=str(run.get("model") or "") or None,
        raw_playbook=str(ui_metadata.get("raw_playbook") or review.get("action_playbook") or "") or None,
        final_playbook=str(review.get("action_playbook") or "") or None,
        feedback_result=body.result,
        reason=body.reason,
        comment=body.comment,
    )
    return {"ok": True, "feedback": feedback}
