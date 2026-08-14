"""On-demand full conversation script jobs for the existing manager pipeline."""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import (
    _load_local_communications,
    build_communication_pattern_context,
)
from api.deal_manager_situation import (
    DEFAULT_DB_PATH,
    _safe_model_meta,
    _storage_call,
    load_manager_screen_context,
)
from openai_api.llm.deal_manager_full_script import (
    CALL_SCRIPT_CONTRACT,
    SCRIPT_MODES,
    STRATEGIES,
    generate_deal_manager_full_script,
)
from openai_api.llm.deal_manager_email import generate_deal_manager_email


@dataclass
class DealManagerFullScriptJob:
    job_id: str
    deal_id: str
    quick_help_id: int
    selected_strategy: str
    script_mode: str = "message"
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем рабочий сценарий"
    percent: int = 5
    created_at: str = field(default="")
    updated_at: str = field(default="")
    script_id: int | None = None
    reused: bool = False
    error: str | None = None


_FULL_SCRIPT_JOBS: dict[str, DealManagerFullScriptJob] = {}
_FULL_SCRIPT_LOCK = threading.Lock()


def _public_objections(analysis_projection: dict[str, Any]) -> dict[str, Any] | None:
    handling = analysis_projection.get("objection_handling")
    if not isinstance(handling, dict) or handling.get("applicable") is not True:
        return None
    items: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(handling.get("likely_objections") or [], start=1):
        if not isinstance(raw, dict):
            continue
        base_id = str(raw.get("objection_type") or f"objection_{index}").strip().lower()
        objection_id = base_id if base_id not in used_ids else f"{base_id}_{index}"
        used_ids.add(objection_id)
        item = {
            "objection_id": objection_id,
            "objection": str(raw.get("client_phrase") or "").strip(),
            "manager_reply": str(raw.get("manager_reply") or "").strip(),
            "follow_up_question": str(raw.get("follow_up_question") or "").strip(),
            "next_step_goal": str(raw.get("next_step_goal") or "").strip(),
            "what_not_to_do": str(raw.get("what_not_to_do") or "").strip(),
        }
        if item["objection"] and item["manager_reply"]:
            items.append(item)
    return {"items": items} if items else None


def _current_inputs(db_path: str | Path, deal_id: str, quick_help_id: int, selected_strategy: str) -> dict[str, Any]:
    if selected_strategy not in STRATEGIES:
        raise ValueError("Неизвестный вариант сообщения")
    context = load_manager_screen_context(db_path, str(deal_id), require_confirmed_situation=True)
    situation_id = context.get("situation_id")
    if situation_id is None:
        raise ValueError("У текущей ситуации отсутствует идентификатор")
    quick_help = _storage_call(
        "get_deal_manager_quick_help", db_path, deal_id=str(deal_id), quick_help_id=int(quick_help_id),
    )
    if not isinstance(quick_help, dict):
        raise ValueError("Ответ Quick Help не найден")
    if int(quick_help.get("source_report_id") or 0) != int(context["source_report_id"]) or int(quick_help.get("situation_review_id") or 0) != int(situation_id):
        raise ValueError("Ответ Quick Help устарел для текущей ситуации")
    content = quick_help.get("content") if isinstance(quick_help.get("content"), dict) else {}
    messages = content.get("client_messages") if isinstance(content.get("client_messages"), dict) else {}
    if not str(messages.get(selected_strategy) or "").strip():
        raise ValueError("В выбранном варианте Quick Help нет сообщения")
    return {"context": context, "quick_help": quick_help, "quick_help_content": content, "situation_id": int(situation_id)}


def _cached_script(db_path: str | Path, inputs: dict[str, Any], selected_strategy: str, script_mode: str) -> dict[str, Any] | None:
    context = inputs["context"]
    method = {
        "call": "get_deal_manager_call_script",
        "email": "get_deal_manager_email_script",
    }.get(script_mode, "get_deal_manager_full_script")
    result = _storage_call(
        method, db_path,
        deal_id=str(context["deal"]["deal_id"]), source_report_id=int(context["source_report_id"]),
        situation_review_id=int(inputs["situation_id"]), quick_help_id=int(inputs["quick_help"]["id"]),
        selected_strategy=selected_strategy,
    )
    if script_mode == "call" and isinstance(result, dict):
        content = result.get("content") if isinstance(result.get("content"), dict) else {}
        if content.get("script_contract") != CALL_SCRIPT_CONTRACT:
            return None
    return result


def get_full_script_job(job_id: str) -> dict[str, Any] | None:
    with _FULL_SCRIPT_LOCK:
        job = _FULL_SCRIPT_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _touch(job: DealManagerFullScriptJob, *, stage: str, detail: str, percent: int) -> None:
    with _FULL_SCRIPT_LOCK:
        job.stage, job.detail, job.percent = stage, detail, percent


def _run_full_script_job(job_id: str, db_path: str | Path) -> None:
    with _FULL_SCRIPT_LOCK:
        job = _FULL_SCRIPT_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, stage="context", detail="Собираем актуальный manager context", percent=20)
        inputs = _current_inputs(db_path, job.deal_id, job.quick_help_id, job.selected_strategy)
        existing = _cached_script(db_path, inputs, job.selected_strategy, job.script_mode)
        if isinstance(existing, dict):
            with _FULL_SCRIPT_LOCK:
                job.script_id = int(existing["id"])
                job.reused = True
                job.status = "done"
            _touch(job, stage="done", detail="Открываем сохранённый актуальный сценарий", percent=100)
            return
        context = inputs["context"]
        checklist = _storage_call(
            "get_deal_daily_checklist_analysis_projection",
            db_path,
            deal_id=str(job.deal_id),
        )
        tactics = inputs["quick_help_content"].get("lifehacks")
        relevant_tactics = tactics if isinstance(tactics, list) else []
        communication_context = build_communication_pattern_context(_load_local_communications(job.deal_id))
        _touch(job, stage="llm", detail="AI строит сценарий выбранного подхода", percent=55)
        generator = generate_deal_manager_email if job.script_mode == "email" else generate_deal_manager_full_script
        script, metadata = generator(
            analysis_projection=context["analysis_projection"], situation_projection=context["situation_projection"],
            deal=context["deal"], current_bitrix_task=context["current_bitrix_task"], checklist=checklist,
            communication_pattern_context=communication_context, quick_help=inputs["quick_help_content"],
            selected_strategy=job.selected_strategy, relevant_tactics=relevant_tactics, script_mode=job.script_mode,
            objection_handling=_public_objections(context["analysis_projection"]),
        )
        _touch(job, stage="saving", detail="Сохраняем проверенный сценарий", percent=88)
        save_method = {
            "call": "save_deal_manager_call_script",
            "email": "save_deal_manager_email_script",
        }.get(job.script_mode, "save_deal_manager_full_script")
        saved = _storage_call(
            save_method, db_path, deal_id=job.deal_id,
            source_report_id=context["source_report_id"], situation_review_id=inputs["situation_id"],
            quick_help_id=job.quick_help_id, selected_strategy=job.selected_strategy,
            script_json=script, model_meta=_safe_model_meta(metadata),
        )
        with _FULL_SCRIPT_LOCK:
            job.script_id = int(saved["id"])
            job.status = "done"
        _touch(job, stage="done", detail="Сценарий разговора готов", percent=100)
    except Exception as error:  # noqa: BLE001 - do not expose model or CRM content
        with _FULL_SCRIPT_LOCK:
            job.status = "error"
            job.error = f"{error.__class__.__name__}: операция не выполнена"
        _touch(job, stage="error", detail="Не удалось подготовить сценарий", percent=100)


def start_full_script_job(*, db_path: str | Path = DEFAULT_DB_PATH, deal_id: str, quick_help_id: int, selected_strategy: str, script_mode: str = "message", confirm_paid: bool) -> dict[str, Any]:
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    inputs = _current_inputs(db_path, str(deal_id), int(quick_help_id), selected_strategy)
    existing = _cached_script(db_path, inputs, selected_strategy, script_mode)
    if isinstance(existing, dict):
        job = DealManagerFullScriptJob(
            job_id=uuid.uuid4().hex, deal_id=str(deal_id), quick_help_id=int(quick_help_id),
            selected_strategy=selected_strategy, script_mode=script_mode, status="done", stage="done",
            detail="Открываем сохранённый актуальный сценарий", percent=100,
            script_id=int(existing["id"]), reused=True,
        )
        with _FULL_SCRIPT_LOCK:
            _FULL_SCRIPT_JOBS[job.job_id] = job
        return asdict(job)
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для полного скрипта")
    with _FULL_SCRIPT_LOCK:
        active = next((item for item in _FULL_SCRIPT_JOBS.values() if item.deal_id == str(deal_id) and item.quick_help_id == int(quick_help_id) and item.selected_strategy == selected_strategy and item.script_mode == script_mode and item.status in {"queued", "running"}), None)
        if active:
            return asdict(active)
        job = DealManagerFullScriptJob(job_id=uuid.uuid4().hex, deal_id=str(deal_id), quick_help_id=int(quick_help_id), selected_strategy=selected_strategy, script_mode=script_mode)
        _FULL_SCRIPT_JOBS[job.job_id] = job
    threading.Thread(target=_run_full_script_job, args=(job.job_id, db_path), daemon=True).start()
    return asdict(job)


def _public_disc_profile(analysis_projection: dict[str, Any]) -> dict[str, Any] | None:
    profile = analysis_projection.get("client_communication_profile")
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


def get_full_script_workspace(*, db_path: str | Path = DEFAULT_DB_PATH, deal_id: str, quick_help_id: int, selected_strategy: str, script_mode: str = "message") -> dict[str, Any]:
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    inputs = _current_inputs(db_path, str(deal_id), int(quick_help_id), selected_strategy)
    script = _cached_script(db_path, inputs, selected_strategy, script_mode)
    checklist = _storage_call(
        "get_deal_daily_checklist_analysis_projection",
        db_path,
        deal_id=str(deal_id),
    )
    return {
        "script": script,
        "script_mode": script_mode,
        "disc_profile": _public_disc_profile(inputs["context"]["analysis_projection"]),
        "checklist": checklist,
        "objection_handling": _public_objections(inputs["context"]["analysis_projection"]),
    }
