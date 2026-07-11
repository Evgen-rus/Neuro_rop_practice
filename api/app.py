"""
FastAPI entrypoint for local ROP assistant UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.candidates import DEFAULT_DAYS, DEFAULT_LIMIT, list_crm_pipelines, search_candidates
from api.jobs import (
    AnalyzeOptions,
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
    get_candidate_filter,
    get_candidate_review_states,
    get_compact_shadow_run,
    get_ui_report,
    init_db,
    list_outcomes,
    list_rop_decisions,
    list_ui_reports,
    save_candidate_filter,
    save_compact_shadow_feedback,
    save_outcome,
    save_rop_decision,
    upsert_candidate_review_state,
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
    force_llm: bool = True
    transcript_mode: Literal["all", "latest", "none"] = "all"


class DecisionRequest(BaseModel):
    decision: str
    comment: str | None = None
    next_control_date: str | None = None


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
        },
    )
    return {"ok": True, "filter": saved}


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
        )
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/analyze")
def analyze(body: AnalyzeRequest) -> dict[str, Any]:
    ids = parse_ids(body.ids)
    if not ids:
        raise HTTPException(status_code=400, detail="Укажите хотя бы один ID")
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


@app.get("/api/reports")
def reports(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    items = list_ui_reports(DEFAULT_DB_PATH, limit=limit)
    # Keep list payload light: drop full analysis JSON.
    light = []
    for item in items:
        row = _enrich_report_row(item)
        row.pop("report_json", None)
        light.append(row)
    return {"items": light}


@app.get("/api/reports/{report_id}")
def report_detail(report_id: int, include_markdown: bool = False) -> dict[str, Any]:
    report = get_ui_report(DEFAULT_DB_PATH, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    payload = _enrich_report_row(report)
    payload["decisions"] = list_rop_decisions(DEFAULT_DB_PATH, report_id)
    payload["outcomes"] = list_outcomes(DEFAULT_DB_PATH, report_id)
    payload["candidate_review"] = get_candidate_review_states(
        DEFAULT_DB_PATH,
        entity_type=str(report.get("entity_type") or ""),
        entity_ids=[str(report.get("entity_id") or "")],
    ).get(str(report.get("entity_id") or ""))
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
    md_path = Path(str(report.get("report_path") or ""))
    if not md_path.exists():
        # Fallback to workspace convention.
        entity_type = str(report.get("entity_type"))
        entity_id = str(report.get("entity_id"))
        md_path = workspace_dir(entity_type, entity_id) / "analysis" / f"{entity_type}_{entity_id}_rop_report.md"
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
