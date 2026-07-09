"""
FastAPI entrypoint for local ROP assistant UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.candidates import DEFAULT_DAYS, DEFAULT_LIMIT, search_candidates
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
from setup import BASE_DIR
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_ui_report,
    init_db,
    list_outcomes,
    list_rop_decisions,
    list_ui_reports,
    save_outcome,
    save_rop_decision,
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


class CandidatesSearchRequest(BaseModel):
    entity_type: Literal["all", "lead", "deal"] = "all"
    days: int = Field(default=DEFAULT_DAYS, ge=0)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=100)
    priority: Literal["high", "medium", "low"] | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "rop-assistant-api",
        "db_path": str(DEFAULT_DB_PATH),
    }


@app.get("/api/candidates")
def candidates(
    entity_type: Literal["all", "lead", "deal"] = "all",
    days: int = Query(default=DEFAULT_DAYS, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=100),
    priority: Literal["high", "medium", "low"] | None = None,
) -> dict[str, Any]:
    try:
        return search_candidates(entity_type=entity_type, days=days, limit=limit, priority=priority)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/candidates/search")
def candidates_search(body: CandidatesSearchRequest) -> dict[str, Any]:
    try:
        return search_candidates(
            entity_type=body.entity_type,
            days=body.days,
            limit=body.limit,
            priority=body.priority,
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
    analysis = unwrap_analysis_payload(row.get("report_json") if isinstance(row.get("report_json"), dict) else {})
    if analysis:
        row["report_json"] = analysis
        summary = extract_summary_fields(analysis, str(row.get("entity_type") or "deal"))
        if not row.get("risk_level"):
            row["risk_level"] = summary.get("risk_level")
        if not row.get("attention_reason"):
            row["attention_reason"] = summary.get("attention_reason")
        if not row.get("recommended_action"):
            row["recommended_action"] = summary.get("recommended_action")
    return row


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
    if not get_ui_report(DEFAULT_DB_PATH, report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    decision_id = save_rop_decision(
        DEFAULT_DB_PATH,
        report_id=report_id,
        decision=body.decision,
        comment=body.comment,
        next_control_date=body.next_control_date,
    )
    return {"ok": True, "decision_id": decision_id, "decisions": list_rop_decisions(DEFAULT_DB_PATH, report_id)}


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
