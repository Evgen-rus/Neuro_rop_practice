"""
FastAPI entrypoint for local ROP assistant UI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
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
from api.access import (
    actor_source_role,
    can_view_job,
    deal_access,
    get_deal,
    require_deal,
    require_report,
    require_task,
    scoped_deal_metrics,
    scoped_dashboard,
)
from api.auth import (
    ALLOWED_ROLES,
    AUTH_COOKIE_NAME,
    AuthStorageUnavailable,
    authenticate_request,
    begin_request_context,
    current_user as auth_current_user,
    end_request_context,
    hash_password,
    login_user,
    public_user,
    session_logout,
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
from api.deal_control import add_task as add_deal_control_task
from api.deal_control import build_deal_control_dashboard, edit_task as edit_deal_control_task
from api.deal_control import confirm_task_crm_match as confirm_deal_control_task_crm_match
from api.deal_control import record_task_outcome as record_deal_control_task_outcome
from api.deal_control import record_task_event as record_deal_control_task_event
from api.deal_control import (
    refresh_deal_control,
    save_bitrix_task_completion,
    save_checklist_item_completion,
    save_deal_fields,
    save_scope as save_deal_control_scope,
)
from api.deal_control import review_task_crm_fact as review_deal_control_task_crm_fact
from api.deal_control import task_history as deal_control_task_history
from api.deal_task_guidance import get_task_guidance_job, start_task_guidance_job
from api.deal_manager_quick_help import (
    get_manager_assistant_workspace,
    get_quick_help_job,
    list_quick_help_history,
    record_manager_communication_completed,
    start_quick_help_job,
)
from api.deal_manager_full_script import (
    get_full_script_job,
    get_full_script_workspace,
    start_full_script_job,
)
from api.deal_manager_followups import get_followups_job, get_followups_workspace, start_followups_job
from api.deal_manager_situation import (
    StorageContractUnavailable,
    confirm_deal_manager_situation,
    get_situation_job,
    start_situation_refine_job,
)
from api.deal_transcription import AudioTranscriptionRequestError, transcribe_manager_voice
from openai_api.bitrix_links import bitrix_entity_url
from setup import BASE_DIR, MSK_TZ
from storage import rop_db as storage
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
    get_or_create_ui_report_share_token,
    get_ui_report,
    get_ui_report_by_share_token,
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
    manager_message_options: list[str] | None = Field(default=None, min_length=1, max_length=3)
    manager_full_review_text: str | None = Field(default=None, max_length=20000)
    manager_task_text: str | None = Field(default=None, max_length=12000)
    review_completed: bool | None = None
    task_completed: bool | None = None
    control_mode: Literal["days", "date", "daily"] | None = None
    control_days: int | None = Field(default=None, ge=1, le=365)
    control_date: str | None = Field(default=None, max_length=40)
    control_completed: bool | None = None


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


class DealControlScopeRequest(BaseModel):
    initial_deal_ids: list[str] = Field(default_factory=list, max_length=200)
    manager_ids: list[str] = Field(default_factory=list, max_length=30)
    pipeline_id: str = Field(default="15", min_length=1, max_length=40)


class DealControlFieldsRequest(BaseModel):
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_payment_period: str | None = Field(default=None, max_length=60)
    next_control_at: str | None = Field(default=None, max_length=40)


class DealControlTaskRequest(BaseModel):
    task_text: str = Field(min_length=1, max_length=12000)
    touch_type: str | None = Field(default=None, max_length=80)
    expected_result: str | None = Field(default=None, max_length=4000)
    due_at: str = Field(min_length=1, max_length=40)


class DealControlTaskUpdateRequest(BaseModel):
    task_text: str | None = Field(default=None, min_length=1, max_length=12000)
    touch_type: str | None = Field(default=None, max_length=80)
    expected_result: str | None = Field(default=None, max_length=4000)
    due_at: str | None = Field(default=None, max_length=40)
    local_status: Literal["active", "completed", "cancelled"] | None = None
    business_result_status: Literal["no_result", "client_fact", "next_step", "needs_rop_review"] | None = None
    business_result_note: str | None = Field(default=None, max_length=4000)
    reschedule_reason: str | None = Field(default=None, max_length=1000)


class DealControlBitrixTaskCompletionRequest(BaseModel):
    deal_id: str = Field(min_length=1, max_length=80)
    completed: bool


class DealControlChecklistItemCompletionRequest(BaseModel):
    completed: bool


class DealTaskGuidanceRequest(BaseModel):
    confirm_paid: bool = False


class DealManagerSituationRefineRequest(BaseModel):
    context: str = Field(min_length=1, max_length=4000)
    confirm_paid: bool = False


class DealManagerQuickHelpRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    confirm_paid: bool = False


class DealManagerFullScriptRequest(BaseModel):
    quick_help_id: int = Field(ge=1)
    selected_strategy: Literal["primary", "alternative", "pattern_break"] = "primary"
    script_mode: Literal["message", "call", "email"] = "message"
    confirm_paid: bool = False


class DealManagerFollowupsRequest(BaseModel):
    confirm_paid: bool = False


class DealManagerCommunicationCompletedRequest(BaseModel):
    quick_help_id: int = Field(ge=1)


class DealControlTaskOutcomeRequest(BaseModel):
    contact_status: Literal["not_attempted", "attempt_no_contact", "confirmed_contact", "unknown"]
    result_status: Literal["pending", "achieved", "partial", "postponed", "refused", "not_applicable", "needs_rop_review"]
    result_note: str | None = Field(default=None, max_length=4000)
    next_step_text: str | None = Field(default=None, max_length=4000)
    next_step_at: str | None = Field(default=None, max_length=40)
    evidence_kind: Literal["crm_activity", "transcript", "manager_confirmation", "rop_confirmation"] | None = None
    evidence_id: str | None = Field(default=None, max_length=120)


class DealControlCrmFactReviewRequest(BaseModel):
    review_status: Literal["confirmed", "rejected"]
    contact_class: Literal["attempt", "confirmed_contact", "internal_information", "unknown", "deal_progress"] | None = None


class DealControlTaskEventRequest(BaseModel):
    event_type: Literal["guidance_opened", "guidance_copied"]
    event_key: str | None = Field(default=None, max_length=160)


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


class AuthLoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class AuthUserCreateRequest(BaseModel):
    login: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)
    role: Literal["admin", "rop", "manager"]
    manager_id: str | None = Field(default=None, max_length=80)
    is_active: bool = True


class AuthUserUpdateRequest(BaseModel):
    role: Literal["admin", "rop", "manager"] | None = None
    manager_id: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None


class AuthPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


_PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/logout"}
_SAFE_ORIGINS = {
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
}


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/api/review/")


def _origin_allowed(origin: str | None, request_host: str | None) -> bool:
    if not origin or origin.casefold() in {value.casefold() for value in _SAFE_ORIGINS}:
        return True
    # The external Nginx/quick-tunnel entrypoint is same-origin from the
    # browser's perspective.  Compare the parsed host rather than trusting a
    # forwarded header supplied by the client.
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        return False
    try:
        request = urlsplit(f"//{str(request_host or '')}")
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = request.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        bool(request.hostname)
        and parsed.hostname.casefold() == request.hostname.casefold()
        and origin_port == request_port
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Authenticate every API request except health/login/logout/review links."""

    origin = request.headers.get("origin")
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_allowed(
        origin,
        request.headers.get("host"),
    ):
        return Response(content='{"detail":"Origin not allowed"}', status_code=403, media_type="application/json")
    if _is_public_path(request.url.path) or request.method.upper() == "OPTIONS":
        tokens = begin_request_context(None)
        try:
            return await call_next(request)
        finally:
            end_request_context(tokens)
    try:
        user = authenticate_request(request)
    except AuthStorageUnavailable:
        return Response(content='{"detail":"Authentication storage unavailable"}', status_code=503, media_type="application/json")
    if user is None:
        return Response(content='{"detail":"Authentication required"}', status_code=401, media_type="application/json")
    tokens = begin_request_context(user)
    try:
        return await call_next(request)
    finally:
        end_request_context(tokens)


def _raise_storage_error(error: ValueError) -> None:
    message = str(error)
    lowered = message.casefold()
    status = 409 if any(
        marker in lowered
        for marker in ("unique", "already", "duplicate", "последн", "last active", "manager_id занят", "занят")
    ) else 400
    raise HTTPException(status_code=status, detail=message) from error


def _require_admin() -> dict[str, Any]:
    return _require_roles("admin")


def _require_roles(*roles: str) -> dict[str, Any]:
    user = auth_current_user()
    if str(user.get("role") or "") not in set(roles):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def _require_admin_or_rop() -> dict[str, Any]:
    return _require_roles("admin", "rop")


def _require_lead_access() -> dict[str, Any]:
    """Lead ownership is not available in the auth contract yet."""

    return _require_admin_or_rop()


def _require_entity_access(
    entity_type: str,
    entity_id: str,
    *,
    action: str = "open",
    allow_lead_roles: tuple[str, ...] = ("admin", "rop"),
) -> dict[str, Any]:
    user = auth_current_user()
    if entity_type == "deal":
        require_deal(entity_id, user=user, action=action)
        return user
    if entity_type == "lead" and str(user.get("role")) in allow_lead_roles:
        if action == "paid_ai" and str(user.get("role")) != "admin":
            raise HTTPException(status_code=403, detail="Paid AI is not available for this role")
        return user
    raise HTTPException(status_code=403, detail="Entity access forbidden")


def _job_is_visible(job: dict[str, Any], user: dict[str, Any]) -> bool:
    return can_view_job(job, user)


def _require_admin_paid() -> dict[str, Any]:
    return _require_roles("admin")


def _require_checklist_edit(deal_id: str) -> dict[str, Any]:
    user = auth_current_user()
    if str(user.get("role")) == "admin":
        return user
    require_deal(deal_id, user=user, action="edit")
    if str(user.get("role")) != "manager":
        raise HTTPException(status_code=403, detail="Only the manager can edit the daily checklist")
    return user


def _require_analyze_scope(entity_type: str, ids: list[str], *, paid: bool) -> dict[str, Any]:
    user = auth_current_user()
    role = str(user.get("role") or "")
    if role == "admin":
        return user
    if role == "rop":
        if paid:
            raise HTTPException(status_code=403, detail="Paid analysis is not available for this role")
        if entity_type != "deal":
            raise HTTPException(status_code=403, detail="ROP analysis is limited to team deals")
        for entity_id in ids:
            require_deal(entity_id, user=user, action="open")
        return user
    if entity_type not in {"deal", "auto"}:
        raise HTTPException(status_code=403, detail="Managers can analyze deals only")
    if entity_type == "auto":
        raise HTTPException(status_code=403, detail="Managers must specify a deal")
    for entity_id in ids:
        access = require_deal(entity_id, user=user, action="paid_ai" if paid else "open")
        if not access.is_own:
            raise HTTPException(status_code=403, detail="Deal access forbidden")
    return user


@app.post("/api/auth/login")
def auth_login(body: AuthLoginRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        return login_user(login=body.login, password=body.password, request=request, response=response)
    except AuthStorageUnavailable as error:
        raise HTTPException(status_code=503, detail="Authentication storage unavailable") from error


@app.post("/api/auth/logout", status_code=204)
def auth_logout(request: Request, response: Response) -> Response:
    try:
        session_logout(request, response)
    except AuthStorageUnavailable:
        # Logout remains idempotent even while the backing store is unavailable;
        # deleting the browser cookie still prevents accidental reuse.
        response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    response.status_code = 204
    return response


@app.get("/api/auth/me")
def auth_me() -> dict[str, Any]:
    user = auth_current_user()
    return {"authenticated": True, "user": public_user(user)}


@app.get("/api/auth/users")
def auth_users() -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "list_auth_users", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    return {"items": [user for item in function(DEFAULT_DB_PATH) if (user := public_user(item)) is not None]}


@app.post("/api/auth/users", status_code=201)
def auth_user_create(body: AuthUserCreateRequest) -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "create_auth_user", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    try:
        user = function(
            DEFAULT_DB_PATH,
            login=body.login,
            password=body.password,
            role=body.role,
            manager_id=body.manager_id,
            is_active=body.is_active,
        )
    except ValueError as error:
        _raise_storage_error(error)
    return {"user": public_user(user)}


@app.patch("/api/auth/users/{user_id}")
def auth_user_update(user_id: int, body: AuthUserUpdateRequest) -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "update_auth_user", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    # `manager_id=None` is meaningful: it explicitly clears an old manager
    # assignment when changing a user to a non-manager role.  Preserve the
    # storage contract's `_UNSET` behavior for fields omitted by the client.
    kwargs: dict[str, Any] = {"user_id": user_id, **body.model_dump(exclude_unset=True)}
    try:
        user = function(DEFAULT_DB_PATH, **kwargs)
    except ValueError as error:
        if "not found" in str(error).casefold() or "не найден" in str(error).casefold():
            raise HTTPException(status_code=404, detail="User not found") from error
        _raise_storage_error(error)
    return {"user": public_user(user)}


@app.post("/api/auth/users/{user_id}/password")
def auth_user_password(user_id: int, body: AuthPasswordRequest) -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "set_auth_user_password", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    try:
        user = function(
            DEFAULT_DB_PATH,
            user_id=user_id,
            password_hash=hash_password(body.password),
        )
    except ValueError as error:
        if "not found" in str(error).casefold() or "не найден" in str(error).casefold():
            raise HTTPException(status_code=404, detail="User not found") from error
        _raise_storage_error(error)
    return {"user": public_user(user)}


@app.post("/api/auth/me/password")
def auth_self_password(body: AuthPasswordRequest) -> dict[str, Any]:
    user = auth_current_user()
    function = getattr(storage, "set_auth_user_password", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    try:
        updated = function(
            DEFAULT_DB_PATH,
            user_id=int(user["id"]),
            password_hash=hash_password(body.password),
        )
    except ValueError as error:
        _raise_storage_error(error)
    return {"user": public_user(updated)}


@app.post("/api/auth/users/{user_id}/deactivate")
def auth_user_deactivate(user_id: int) -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "deactivate_auth_user", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    try:
        user = function(DEFAULT_DB_PATH, user_id=user_id)
    except ValueError as error:
        if "not found" in str(error).casefold() or "не найден" in str(error).casefold():
            raise HTTPException(status_code=404, detail="User not found") from error
        _raise_storage_error(error)
    return {"user": public_user(user)}


@app.post("/api/auth/users/{user_id}/activate")
def auth_user_activate(user_id: int) -> dict[str, Any]:
    _require_admin()
    function = getattr(storage, "activate_auth_user", None)
    if not callable(function):
        raise HTTPException(status_code=503, detail="Authentication storage unavailable")
    try:
        user = function(DEFAULT_DB_PATH, user_id=user_id)
    except ValueError as error:
        if "not found" in str(error).casefold() or "не найден" in str(error).casefold():
            raise HTTPException(status_code=404, detail="User not found") from error
        _raise_storage_error(error)
    return {"user": public_user(user)}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "rop-assistant-api",
        "db_path": str(DEFAULT_DB_PATH),
    }


@app.get("/api/deal-control")
def deal_control_dashboard() -> dict[str, Any]:
    user = auth_current_user()
    return scoped_dashboard(build_deal_control_dashboard(db_path=DEFAULT_DB_PATH), user)


@app.put("/api/deal-control/scope")
def deal_control_scope_put(body: DealControlScopeRequest) -> dict[str, Any]:
    _require_roles("admin")
    try:
        scope = save_deal_control_scope(
            db_path=DEFAULT_DB_PATH,
            initial_deal_ids=body.initial_deal_ids,
            manager_ids=body.manager_ids,
            pipeline_id=body.pipeline_id,
        )
        return {"ok": True, "scope": scope}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/sync")
def deal_control_sync() -> dict[str, Any]:
    _require_roles("admin", "rop")
    try:
        dashboard = refresh_deal_control(db_path=DEFAULT_DB_PATH)
        return scoped_dashboard(dashboard, auth_current_user())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001 - surface a read-only CRM problem in the local UI
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.put("/api/deal-control/deals/{deal_id}")
def deal_control_deal_update(deal_id: str, body: DealControlFieldsRequest) -> dict[str, Any]:
    require_deal(deal_id, action="edit")
    try:
        return save_deal_fields(
            db_path=DEFAULT_DB_PATH, deal_id=deal_id, probability=body.probability,
            expected_payment_period=body.expected_payment_period, next_control_at=body.next_control_at,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/deals/{deal_id}/tasks")
def deal_control_task_create(deal_id: str, body: DealControlTaskRequest) -> dict[str, Any]:
    require_deal(deal_id, action="edit")
    try:
        return add_deal_control_task(
            db_path=DEFAULT_DB_PATH, deal_id=deal_id, task_text=body.task_text,
            touch_type=body.touch_type, expected_result=body.expected_result, due_at=body.due_at,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/deal-control/tasks/{task_id}")
def deal_control_task_update(task_id: int, body: DealControlTaskUpdateRequest) -> dict[str, Any]:
    access, _task = require_task(task_id, action="edit")
    try:
        return edit_deal_control_task(
            db_path=DEFAULT_DB_PATH, task_id=task_id, task_text=body.task_text, touch_type=body.touch_type,
            expected_result=body.expected_result, due_at=body.due_at, local_status=body.local_status,
            business_result_status=body.business_result_status, business_result_note=body.business_result_note,
            reschedule_reason=body.reschedule_reason, source_role=actor_source_role(access.user),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/deal-control/bitrix-tasks/{activity_id}/completion")
def deal_control_bitrix_task_completion(
    activity_id: str,
    body: DealControlBitrixTaskCompletionRequest,
) -> dict[str, Any]:
    access = require_deal(body.deal_id, action="edit")
    try:
        state = save_bitrix_task_completion(
            db_path=DEFAULT_DB_PATH,
            deal_id=body.deal_id,
            activity_id=activity_id,
            completed=body.completed,
            source_role=actor_source_role(access.user),
        )
        return {"ok": True, "state": state}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/tasks/{task_id}/confirm-crm-match")
def deal_control_task_confirm_crm_match(task_id: int) -> dict[str, Any]:
    require_task(task_id, action="edit")
    try:
        return confirm_deal_control_task_crm_match(db_path=DEFAULT_DB_PATH, task_id=task_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/tasks/{task_id}/history")
def deal_control_task_history_get(task_id: int) -> dict[str, Any]:
    require_task(task_id, action="open")
    return deal_control_task_history(db_path=DEFAULT_DB_PATH, task_id=task_id)


@app.post("/api/deal-control/tasks/{task_id}/outcomes")
def deal_control_task_outcome_create(task_id: int, body: DealControlTaskOutcomeRequest) -> dict[str, Any]:
    access, _task = require_task(task_id, action="edit")
    try:
        return record_deal_control_task_outcome(
            db_path=DEFAULT_DB_PATH,
            task_id=task_id,
            contact_status=body.contact_status,
            result_status=body.result_status,
            result_note=body.result_note,
            next_step_text=body.next_step_text,
            next_step_at=body.next_step_at,
            evidence_kind=body.evidence_kind,
            evidence_id=body.evidence_id,
            source_role=actor_source_role(access.user),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/tasks/{task_id}/crm-facts/{fact_id}/review")
def deal_control_task_crm_fact_review(
    task_id: int,
    fact_id: int,
    body: DealControlCrmFactReviewRequest,
) -> dict[str, Any]:
    require_task(task_id, action="edit")
    try:
        return review_deal_control_task_crm_fact(
            db_path=DEFAULT_DB_PATH,
            task_id=task_id,
            fact_id=fact_id,
            review_status=body.review_status,
            contact_class=body.contact_class,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/metrics")
def deal_control_metrics_get(manager_id: str | None = None) -> dict[str, Any]:
    return scoped_deal_metrics(auth_current_user(), manager_id)


@app.post("/api/deal-control/tasks/{task_id}/events")
def deal_control_task_event_create(task_id: int, body: DealControlTaskEventRequest) -> dict[str, bool]:
    require_task(task_id, action="open")
    try:
        return record_deal_control_task_event(
            db_path=DEFAULT_DB_PATH,
            task_id=task_id,
            event_type=body.event_type,
            event_key=body.event_key,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/tasks/{task_id}/guidance")
def deal_control_task_guidance_start(task_id: int, body: DealTaskGuidanceRequest) -> dict[str, Any]:
    access, _task = require_task(task_id, action="paid_ai")
    if not access.can_run_paid_ai:
        raise HTTPException(status_code=403, detail="Paid AI is not available for this role or deal")
    try:
        return start_task_guidance_job(
            db_path=DEFAULT_DB_PATH,
            task_id=task_id,
            confirm_paid=body.confirm_paid,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/guidance-jobs/{job_id}")
def deal_control_task_guidance_job_get(job_id: str) -> dict[str, Any]:
    job = get_task_guidance_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание подготовки менеджера не найдено")
    require_task(int(job["task_id"]), action="open")
    return job


@app.post("/api/deal-control/deals/{deal_id}/situation/confirm")
def deal_manager_situation_confirm(deal_id: str) -> dict[str, Any]:
    require_deal(deal_id, action="edit")
    try:
        return confirm_deal_manager_situation(db_path=DEFAULT_DB_PATH, deal_id=deal_id)
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур ситуации сделки ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/deals/{deal_id}/situation/refine")
def deal_manager_situation_refine(
    deal_id: str,
    body: DealManagerSituationRefineRequest,
) -> dict[str, Any]:
    require_deal(deal_id, action="paid_ai")
    try:
        return start_situation_refine_job(
            db_path=DEFAULT_DB_PATH,
            deal_id=deal_id,
            context=body.context,
            confirm_paid=body.confirm_paid,
        )
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур ситуации сделки ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/situation-jobs/{job_id}")
def deal_manager_situation_job_get(job_id: str) -> dict[str, Any]:
    job = get_situation_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание уточнения ситуации не найдено")
    require_deal(str(job["deal_id"]), action="open")
    return job


@app.post("/api/deal-control/deals/{deal_id}/quick-help")
def deal_manager_quick_help_start(
    deal_id: str,
    body: DealManagerQuickHelpRequest,
) -> dict[str, Any]:
    require_deal(deal_id, action="paid_ai")
    try:
        return start_quick_help_job(
            db_path=DEFAULT_DB_PATH,
            deal_id=deal_id,
            question=body.question,
            confirm_paid=body.confirm_paid,
        )
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур quick help ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/quick-help-jobs/{job_id}")
def deal_manager_quick_help_job_get(job_id: str) -> dict[str, Any]:
    job = get_quick_help_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание quick help не найдено")
    require_deal(str(job["deal_id"]), action="open")
    return job


@app.get("/api/deal-control/deals/{deal_id}/quick-help-history")
def deal_manager_quick_help_history_get(
    deal_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    before_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    require_deal(deal_id, action="open")
    try:
        return list_quick_help_history(
            db_path=DEFAULT_DB_PATH,
            deal_id=deal_id,
            limit=limit,
            before_id=before_id,
        )
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур quick help ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/deals/{deal_id}/full-script")
def deal_manager_full_script_start(deal_id: str, body: DealManagerFullScriptRequest) -> dict[str, Any]:
    require_deal(deal_id, action="paid_ai")
    try:
        return start_full_script_job(
            db_path=DEFAULT_DB_PATH, deal_id=deal_id, quick_help_id=body.quick_help_id,
            selected_strategy=body.selected_strategy, script_mode=body.script_mode, confirm_paid=body.confirm_paid,
        )
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур полного скрипта ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/full-script-jobs/{job_id}")
def deal_manager_full_script_job_get(job_id: str) -> dict[str, Any]:
    job = get_full_script_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание полного скрипта не найдено")
    require_deal(str(job["deal_id"]), action="open")
    return job


@app.get("/api/deal-control/deals/{deal_id}/full-script")
def deal_manager_full_script_get(
    deal_id: str,
    quick_help_id: int = Query(ge=1),
    selected_strategy: Literal["primary", "alternative", "pattern_break"] = Query(default="primary"),
    script_mode: Literal["message", "call", "email"] = Query(default="message"),
) -> dict[str, Any]:
    require_deal(deal_id, action="open")
    try:
        return get_full_script_workspace(
            db_path=DEFAULT_DB_PATH, deal_id=deal_id, quick_help_id=quick_help_id,
            selected_strategy=selected_strategy, script_mode=script_mode,
        )
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур полного скрипта ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/deals/{deal_id}/followups")
def deal_manager_followups_start(deal_id: str, body: DealManagerFollowupsRequest) -> dict[str, Any]:
    require_deal(deal_id, action="paid_ai")
    try:
        return start_followups_job(db_path=DEFAULT_DB_PATH, deal_id=deal_id, confirm_paid=body.confirm_paid)
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур фоллоуапов ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/followup-jobs/{job_id}")
def deal_manager_followups_job_get(job_id: str) -> dict[str, Any]:
    job = get_followups_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание фоллоуапов не найдено")
    require_deal(str(job["deal_id"]), action="open")
    return job


@app.get("/api/deal-control/deals/{deal_id}/followups")
def deal_manager_followups_get(deal_id: str) -> dict[str, Any]:
    require_deal(deal_id, action="open")
    try:
        return get_followups_workspace(db_path=DEFAULT_DB_PATH, deal_id=deal_id)
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур фоллоуапов ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.put("/api/deal-control/deals/{deal_id}/checklist/{item_id}/completion")
def deal_control_checklist_item_completion(
    deal_id: str,
    item_id: str,
    body: DealControlChecklistItemCompletionRequest,
) -> dict[str, Any]:
    _require_checklist_edit(deal_id)
    try:
        checklist = save_checklist_item_completion(
            db_path=DEFAULT_DB_PATH,
            deal_id=deal_id,
            item_id=item_id,
            completed=body.completed,
        )
        return {"ok": True, "checklist": checklist}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/deal-control/deals/{deal_id}/assistant-workspace")
def deal_manager_assistant_workspace_get(deal_id: str) -> dict[str, Any]:
    require_deal(deal_id, action="open")
    try:
        return get_manager_assistant_workspace(db_path=DEFAULT_DB_PATH, deal_id=deal_id)
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур помощника ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/deals/{deal_id}/assistant/communication-completed")
def deal_manager_assistant_communication_completed(
    deal_id: str,
    body: DealManagerCommunicationCompletedRequest,
) -> dict[str, Any]:
    require_deal(deal_id, action="edit")
    try:
        event = record_manager_communication_completed(
            db_path=DEFAULT_DB_PATH,
            deal_id=deal_id,
            quick_help_id=body.quick_help_id,
        )
        return {"ok": True, "event": event}
    except StorageContractUnavailable as error:
        raise HTTPException(status_code=503, detail="Контур помощника ещё не подключён") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/deal-control/voice/transcribe")
async def deal_manager_voice_transcribe(
    request: Request,
) -> dict[str, str]:
    try:
        form = await request.form()
        audio = form.get("audio")
        if audio is None or not hasattr(audio, "read"):
            raise AudioTranscriptionRequestError("Аудио не передано")
        deal_id = str(form.get("deal_id") or "").strip()
        require_deal(deal_id, action="paid_ai")
        confirm_paid = str(form.get("confirm_paid") or "").strip().lower() == "true"
        language = str(form.get("language") or "ru")
        return await transcribe_manager_voice(
            audio=audio,
            deal_id=deal_id,
            confirm_paid=confirm_paid,
            language=language,
        )
    except AudioTranscriptionRequestError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - provider details stay out of HTTP/log output
        raise HTTPException(status_code=502, detail="Транскрибация не выполнена") from error


@app.get("/api/pipelines")
def pipelines() -> dict[str, Any]:
    _require_roles("admin", "rop")
    return list_crm_pipelines()


@app.get("/api/candidate-filters")
def candidate_filters_get() -> dict[str, Any]:
    _require_roles("admin", "rop")
    return {"filter": get_candidate_filter(DEFAULT_DB_PATH)}


@app.put("/api/candidate-filters")
def candidate_filters_put(body: CandidateFilterSaveRequest) -> dict[str, Any]:
    _require_roles("admin", "rop")
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
    _require_admin()
    return {
        "items": list_analysis_profiles(DEFAULT_DB_PATH),
        "selected": get_last_analysis_profile(DEFAULT_DB_PATH),
    }


@app.post("/api/analysis-profiles")
def analysis_profile_create(body: AnalysisProfileRequest) -> dict[str, Any]:
    _require_admin()
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
    _require_admin()
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
    _require_admin()
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
    _require_admin()
    try:
        profile = set_last_analysis_profile(DEFAULT_DB_PATH, profile_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Профиль не найден") from error
    return {"ok": True, "selected": profile}


@app.post("/api/analysis-profiles/{profile_id}/preview")
def analysis_profile_preview(profile_id: int, body: AnalysisProfilePreviewRequest | None = None) -> dict[str, Any]:
    _require_admin()
    profile = get_analysis_profile(DEFAULT_DB_PATH, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    try:
        profile_settings = profile.get("profile") if isinstance(profile.get("profile"), dict) else {}
        timezone_name = "Europe/Moscow"
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
    _require_admin()
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
    _require_admin()
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
    _require_admin()
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
    _require_admin()
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
    _require_admin_or_rop()
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
    _require_admin_or_rop()
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
    paid = bool(body.force_llm or (body.analyze and body.confirm_paid) or (body.transcribe_audio and body.confirm_paid))
    _require_analyze_scope(body.entity_type, ids, paid=paid)
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
    user = auth_current_user()
    if str(user.get("role")) == "admin":
        items = list_jobs(limit)
    else:
        # Fetch enough of the in-memory queue to apply entity-level ownership
        # before truncating the response.  A foreign deal must not displace an
        # actor's own visible job merely because it was created later.
        items = [item for item in list_jobs(10000) if _job_is_visible(item, user)]
    return {"items": items[:limit]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    user = auth_current_user()
    if not _job_is_visible(job, user):
        raise HTTPException(status_code=403, detail="Job access forbidden")
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


def _workflow_default_texts(report: dict[str, Any]) -> tuple[str, list[str], str]:
    analysis = unwrap_analysis_payload(report.get("report_json") if isinstance(report.get("report_json"), dict) else {})
    rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
    manager_action = analysis.get("manager_action_block") if isinstance(analysis.get("manager_action_block"), dict) else {}
    manager_quality = analysis.get("manager_quality") if isinstance(analysis.get("manager_quality"), dict) else {}
    lead_state = analysis.get("lead_state") if isinstance(analysis.get("lead_state"), dict) else {}

    def lines(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        return [text] if text else []

    review_text = str(rop.get("manager_review_text") or "").strip()
    has_new_message_contract = bool(review_text)
    if not review_text:
        review_parts: list[str] = []
        done_well = lines(manager_quality.get("what_done_well"))
        if done_well:
            review_parts.append("Хорошо, что " + "; ".join(item.rstrip(".") for item in done_well) + ".")
        situation = str(lead_state.get("summary") or "").strip()
        if situation:
            review_parts.append(situation)
        next_step = str(rop.get("check_for_rop") or rop.get("why_it_matters") or "").strip()
        if next_step:
            review_parts.append(next_step)
        review_text = " ".join(review_parts)

    primary = manager_action.get("primary_text") if isinstance(manager_action.get("primary_text"), dict) else {}
    backups = manager_action.get("backup_texts") if isinstance(manager_action.get("backup_texts"), list) else []
    message_options = lines(primary.get("text"))
    message_options.extend(
        str(item.get("text") or "").strip()
        for item in backups
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    )
    if not message_options:
        message_options = lines(rop.get("manager_message_options"))

    task_parts = lines(rop.get("message_to_manager"))
    expected = str(rop.get("expected_crm_update") or "").strip()
    deadline = str(rop.get("deadline") or "").strip()
    success = str(rop.get("success_condition") or "").strip()
    if not has_new_message_contract:
        if expected:
            task_parts.append(f"Зафиксировать в CRM: {expected}")
        if deadline:
            task_parts.append(f"Срок: {deadline}")
        if success:
            task_parts.append(f"Результат: {success}")
    return review_text, message_options[:3], "\n\n".join(task_parts)


def _compose_manager_full_review(review_text: str, message_options: list[str]) -> str:
    tone_labels = [
        "Деловой и прямой",
        "Партнёрский и доброжелательный",
        "Спокойный и консультативный",
    ]
    parts = [str(review_text or "").strip()]
    if message_options:
        parts.append("Предлагаю три варианта, как можно обратиться к клиенту:")
    for index, message in enumerate(message_options):
        if index:
            parts.append("────────────────────")
        title = tone_labels[index] if index < len(tone_labels) else "Готовый текст"
        parts.extend([f"Вариант {index + 1} — {title}", f"«{str(message).strip()}»"])
    return "\n\n".join(part for part in parts if part)


def _workflow_status(workflow: dict[str, Any]) -> str:
    if workflow.get("control_mode"):
        return "На контроле"
    if workflow.get("review_completed") or workflow.get("task_completed") or workflow.get("control_completed"):
        return "В работе"
    return "Готов к разбору"


def _lead_workflow_payload(lead_id: str, report: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = get_lead_workflow_state(DEFAULT_DB_PATH, lead_id)
    default_review, default_options, default_task = _workflow_default_texts(report or {})
    default_full_review = _compose_manager_full_review(default_review, default_options)
    if saved is not None:
        workflow = saved
        analysis = unwrap_analysis_payload(report.get("report_json") if report and isinstance(report.get("report_json"), dict) else {})
        rop = analysis.get("rop_manager_message_block") if isinstance(analysis.get("rop_manager_message_block"), dict) else {}
        report_id = report.get("id") if report else None
        report_changed = report_id is not None and workflow.get("source_report_id") != report_id
        is_legacy_report = not str(rop.get("manager_review_text") or "").strip()
        if report_changed:
            workflow["source_report_id"] = report_id
            workflow["manager_review_text"] = default_review
            workflow["manager_message_options"] = default_options
            workflow["manager_full_review_text"] = default_full_review
            workflow["manager_task_text"] = default_task
            workflow["review_completed"] = False
            workflow["task_completed"] = False
        elif is_legacy_report:
            legacy_options = [
                str(item).strip()
                for item in rop.get("manager_message_options", [])
                if str(item).strip()
            ] if isinstance(rop.get("manager_message_options"), list) else []
            if not workflow.get("manager_review_text"):
                workflow["manager_review_text"] = default_review
            if not workflow.get("manager_message_options") or workflow.get("manager_message_options") == legacy_options:
                workflow["manager_message_options"] = default_options
        else:
            if not workflow.get("manager_review_text"):
                workflow["manager_review_text"] = default_review
            if not workflow.get("manager_message_options"):
                workflow["manager_message_options"] = default_options
        if not workflow.get("manager_full_review_text"):
            workflow["manager_full_review_text"] = _compose_manager_full_review(
                str(workflow.get("manager_review_text") or ""),
                list(workflow.get("manager_message_options") or []),
            )
    else:
        workflow = {
            "lead_id": str(lead_id),
            "source_report_id": report.get("id") if report else None,
            "manager_review_text": default_review,
            "manager_message_options": default_options,
            "manager_full_review_text": default_full_review,
            "manager_task_text": default_task,
            "review_completed": False,
            "task_completed": False,
            "control_mode": None,
            "control_days": 2,
            "control_date": None,
            "control_completed": False,
            "created_at": None,
            "updated_at": None,
        }
    workflow.pop("final_decision", None)
    return {**workflow, "status_label": _workflow_status(workflow)}


@app.get("/api/leads/{lead_id}/workflow")
def lead_workflow(lead_id: str, report_id: int | None = None) -> dict[str, Any]:
    _require_lead_access()
    report = get_ui_report(DEFAULT_DB_PATH, report_id) if report_id is not None else get_latest_ui_report(
        DEFAULT_DB_PATH, entity_type="lead", entity_id=str(lead_id)
    )
    if report and (str(report.get("entity_type")) != "lead" or str(report.get("entity_id")) != str(lead_id)):
        raise HTTPException(status_code=400, detail="Report does not belong to this lead")
    return _lead_workflow_payload(str(lead_id), report)


@app.put("/api/leads/{lead_id}/workflow")
def save_lead_workflow(lead_id: str, body: LeadWorkflowRequest) -> dict[str, Any]:
    _require_lead_access()
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
    saved = upsert_lead_workflow_state(
        DEFAULT_DB_PATH,
        lead_id=lead_id,
        source_report_id=merged["source_report_id"],
        manager_review_text=merged.get("manager_review_text"),
        manager_message_options=merged.get("manager_message_options"),
        manager_full_review_text=merged.get("manager_full_review_text"),
        manager_task_text=merged.get("manager_task_text"),
        review_completed=bool(merged.get("review_completed")),
        task_completed=bool(merged.get("task_completed")),
        control_mode=control_mode,
        control_days=merged.get("control_days"),
        control_date=merged.get("control_date"),
        control_completed=bool(merged.get("control_completed")),
        final_decision=None,
    )

    previous_control = (existing or {}).get("control_mode")
    next_control_date: str | None = None
    state = "active"
    decision: str | None = None
    if saved.get("control_mode"):
        state, decision = "snoozed", "Назначен контроль"
        if saved.get("control_mode") == "days":
            next_control_date = (datetime.now(MSK_TZ).date() + timedelta(days=int(saved.get("control_days") or 1))).isoformat()
        elif saved.get("control_mode") == "daily":
            next_control_date = (datetime.now(MSK_TZ).date() + timedelta(days=1)).isoformat()
        else:
            next_control_date = str(saved.get("control_date") or "") or None
    else:
        decision = "Снят с контроля" if previous_control else None
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
    if decision and saved.get("control_mode") != previous_control:
        save_rop_decision(
            DEFAULT_DB_PATH,
            report_id=int(report["id"]),
            decision=decision,
            next_control_date=next_control_date,
        )
    saved.pop("final_decision", None)
    return {**saved, "status_label": _workflow_status(saved)}


@app.get("/api/reports")
def reports(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    user = auth_current_user()
    items = list_ui_reports(DEFAULT_DB_PATH, limit=limit)
    # Keep list payload light: drop full analysis JSON.
    light = []
    for item in items:
        entity_type = str(item.get("entity_type") or "")
        entity_id = str(item.get("entity_id") or "")
        if entity_type == "deal":
            deal = get_deal(entity_id)
            if deal is None or not deal_access(user, deal).can_open:
                continue
        elif str(user.get("role")) == "manager":
            continue
        row = _enrich_report_row(item)
        row.pop("report_json", None)
        row.pop("report_meta", None)
        row.pop("technical_log", None)
        row.pop("model_context", None)
        light.append(row)
    return {"items": light}


@app.get("/api/reports/{report_id}")
def report_detail(report_id: int, include_markdown: bool = False) -> dict[str, Any]:
    report = require_report(report_id)
    report["share_token"] = get_or_create_ui_report_share_token(DEFAULT_DB_PATH, report_id)
    payload = _enrich_report_row(report)
    if str(report.get("entity_type") or "") == "lead":
        current_meta = build_lead_report_meta(str(report.get("entity_id") or "")) or {}
        saved_meta = payload.get("report_meta") if isinstance(payload.get("report_meta"), dict) else {}
        payload["report_meta"] = {**current_meta, **saved_meta}
        for key in ("last_attempt", "last_confirmed_contact", "last_internal_information"):
            payload["report_meta"][key] = current_meta.get(key)
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
    markdown_path = _report_markdown_path(report)
    payload["markdown_available"] = markdown_path.exists()
    payload["report_markdown"] = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else None
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


@app.get("/api/review/{share_token}")
def review_report(share_token: str) -> dict[str, Any]:
    """Return one saved report for the lightweight read-only review page."""
    if len(share_token) > 128:
        raise HTTPException(status_code=404, detail="Review report not found")
    report = get_ui_report_by_share_token(DEFAULT_DB_PATH, share_token)
    if not report:
        raise HTTPException(status_code=404, detail="Review report not found")
    payload = _enrich_report_row(report)
    if str(report.get("entity_type") or "") == "lead":
        current_meta = build_lead_report_meta(str(report.get("entity_id") or "")) or {}
        saved_meta = payload.get("report_meta") if isinstance(payload.get("report_meta"), dict) else {}
        payload["report_meta"] = {**current_meta, **saved_meta}
        for key in ("last_attempt", "last_confirmed_contact", "last_internal_information"):
            payload["report_meta"][key] = current_meta.get(key)
        payload["workflow"] = _lead_workflow_payload(str(report.get("entity_id") or ""), report)
    # Review links intentionally expose the selected card only: no history, decisions,
    # technical log, model context, or filesystem paths belong to this response.
    for key in (
        "analysis_path",
        "report_path",
        "technical_log",
        "model_context",
        "job_id",
        "share_token",
    ):
        payload.pop(key, None)
    return payload


@app.get("/api/reports/{report_id}/markdown")
def report_markdown(report_id: int) -> dict[str, Any]:
    report = require_report(report_id)
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
    _require_admin_or_rop()
    report = require_report(report_id, action="edit")
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
            next_control_date=(datetime.now(MSK_TZ).date() + timedelta(days=2)).isoformat(),
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
    _require_admin_or_rop()
    require_report(report_id, action="edit")
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
    _require_admin_or_rop()
    report = require_report(report_id, action="edit")
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
    _require_entity_access(entity_type, entity_id, action="open")
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
    _require_entity_access(entity_type, entity_id, action="open")
    return review_payload(entity_type, entity_id, selected_run_id=run_id)


@app.post("/api/entity/{entity_type}/{entity_id}/compact-runs")
def compact_run(entity_type: Literal["lead", "deal"], entity_id: str) -> dict[str, Any]:
    _require_entity_access(entity_type, entity_id, action="paid_ai")
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
    _require_entity_access(str(job.get("entity_type") or ""), str(job.get("entity_id") or ""), action="open")
    return job


@app.get("/api/entity/{entity_type}/{entity_id}/compact-evidence/{evidence_id}")
def compact_evidence(
    entity_type: Literal["lead", "deal"], entity_id: str, evidence_id: str
) -> dict[str, Any]:
    _require_entity_access(entity_type, entity_id, action="open")
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
    _require_entity_access(entity_type, entity_id, action="edit")
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
