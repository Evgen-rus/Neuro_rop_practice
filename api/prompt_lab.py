"""Safe Prompt Lab execution path.

Reads production context and reuses generate_*/builders/validators.
Never writes production manager tables, trajectory, Bitrix or V2 checkpoints.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from api.deal_manager_companion import find_last_contact
from api.deal_manager_full_script import _public_objections
from api.deal_manager_quick_help import (
    build_communication_pattern_context,
    _load_local_communications,
    public_quick_help_error,
)
from api.deal_manager_situation import (
    DEFAULT_DB_PATH,
    _safe_model_meta,
    _storage_call,
    load_manager_screen_context,
)
from api.prompt_lab_modules import get_module, production_prompt_template, public_modules
from openai_api.llm.deal_manager_companion import generate_deal_manager_companion
from openai_api.llm.deal_manager_email import generate_deal_manager_email
from openai_api.llm.deal_manager_followups import generate_deal_manager_followups
from openai_api.llm.deal_manager_full_script import generate_deal_manager_full_script
from openai_api.llm.deal_manager_quick_help import generate_deal_manager_quick_help
from openai_api.llm.manager_tactics import load_manager_tactics
from openai_api.llm.prompt_lab_models import list_lab_models, resolved_runtime_config, validate_model_reasoning
from openai_api.llm.prompt_parts import assemble_prompt, sha256_json, sha256_text
from openai_api.llm.llm_client import ModelJsonParseError, ModelResponseIncompleteError
from setup import MSK_TZ
from storage import prompt_lab_db as lab_db
from storage.prompt_lab_db import DEFAULT_PROMPT_LAB_DB_PATH


logger = logging.getLogger(__name__)

BRANCHES = ("current", "experiment")


@dataclass
class PromptLabJob:
    job_id: str
    deal_id: str
    module_key: str
    branch: str
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Готовим Prompt Lab"
    percent: int = 5
    run_id: int | None = None
    reused: bool = False
    existing: bool = False
    error: str | None = None
    created_at: str = field(default="")


_JOBS: dict[str, PromptLabJob] = {}
_LOCK = threading.Lock()
_SNAPSHOT_LOCK = threading.Lock()


def _now() -> str:
    from datetime import datetime
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


def _touch(job: PromptLabJob, stage: str, detail: str, percent: int) -> None:
    with _LOCK:
        job.stage, job.detail, job.percent = stage, detail, percent


def get_lab_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(str(job_id))
        return asdict(job) if job else None


def _gate_state(context: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    status = str(context.get("situation_status") or "")
    report_id = context.get("source_report_id")
    situation_id = context.get("situation_id")
    blocked_reason = None
    if spec["requires_confirmed_situation"]:
        if not report_id:
            blocked_reason = "Нет корректного полного анализа сделки"
        elif situation_id is None:
            blocked_reason = "У текущей ситуации отсутствует идентификатор"
        elif status != "confirmed":
            if status == "refined":
                blocked_reason = "Ситуация уточнена, но ещё не подтверждена"
            else:
                blocked_reason = "Сначала подтвердите текущую ситуацию сделки"
    return {
        "ok": blocked_reason is None,
        "reason": blocked_reason,
        "situation_status": status or None,
        "source_report_id": report_id,
        "situation_id": situation_id,
    }


def _analysis_run_id(report: dict[str, Any] | None) -> int | None:
    if not isinstance(report, dict):
        return None
    for key in ("analysis_run_id", "source_analysis_run_id"):
        raw = report.get(key)
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            continue
    meta = report.get("report_meta")
    if isinstance(meta, dict):
        try:
            if meta.get("analysis_run_id") is not None:
                return int(meta["analysis_run_id"])
        except (TypeError, ValueError):
            return None
    return None


def _entry_model_reasoning(entry: dict[str, Any], runtime: dict[str, str]) -> tuple[str, str]:
    meta = entry.get("model_meta") if isinstance(entry.get("model_meta"), dict) else {}
    model = str(meta.get("model") or runtime["model"])
    reasoning = str(meta.get("reasoning_effort") or meta.get("reasoning") or runtime["reasoning"])
    try:
        return validate_model_reasoning(model, reasoning)
    except ValueError:
        return model, reasoning


def _production_result(entry: dict[str, Any]) -> dict[str, Any] | None:
    content = entry.get("content")
    if isinstance(content, dict):
        return content
    return None


QH_UPSTREAM_REQUIRED = "Сначала получите Quick Help этой ветки на текущем контексте"
QH_MODES = ("push", "reanimator")


def normalize_quick_help_mode(value: str | None) -> str | None:
    mode = str(value or "").strip()
    return mode if mode in QH_MODES else None


def qh_upstream_matches(
    run: dict[str, Any] | None,
    *,
    deal_id: str,
    branch: str,
    snapshot_id: int,
    mode: str,
) -> bool:
    expected_key = f"quick_help.{normalize_quick_help_mode(mode) or ''}"
    if not isinstance(run, dict):
        return False
    try:
        run_snapshot = int(run.get("snapshot_id") or 0)
        run_id = int(run.get("id") or 0)
    except (TypeError, ValueError):
        return False
    return (
        run_id > 0
        and str(run.get("deal_id") or "") == str(deal_id)
        and str(run.get("branch") or "") == str(branch)
        and run_snapshot == int(snapshot_id)
        and str(run.get("module_key") or "") == expected_key
        and str(run.get("status") or "") == "success"
    )


def select_qh_upstream_run(
    runs: list[dict[str, Any]],
    *,
    branch: str,
    mode: str,
    deal_id: str,
    snapshot_id: int,
) -> dict[str, Any] | None:
    for item in runs:
        if qh_upstream_matches(
            item,
            deal_id=str(deal_id),
            branch=str(branch),
            snapshot_id=int(snapshot_id),
            mode=mode,
        ):
            return item
    return None


def require_qh_upstream(
    *,
    lab_db_path: Path,
    upstream_run_id: int | None,
    deal_id: str,
    branch: str,
    snapshot_id: int,
    mode: str | None,
) -> dict[str, Any]:
    normalized = normalize_quick_help_mode(mode)
    if upstream_run_id is None or normalized is None:
        raise ValueError(QH_UPSTREAM_REQUIRED)
    upstream = lab_db.get_run(lab_db_path, int(upstream_run_id))
    if not qh_upstream_matches(
        upstream,
        deal_id=str(deal_id),
        branch=str(branch),
        snapshot_id=int(snapshot_id),
        mode=normalized,
    ):
        raise ValueError(QH_UPSTREAM_REQUIRED)
    return upstream


def _production_current(
    db_path: Path,
    context: dict[str, Any],
    spec: dict[str, Any],
    *,
    selected_strategy: str | None = None,
    quick_help_mode: str | None = None,
) -> dict[str, Any]:
    deal_id = str(context["deal"]["deal_id"])
    report_id = context.get("source_report_id")
    situation_id = context.get("situation_id")
    entry = None
    stale = False
    production_quick_help = None
    try:
        if spec["family"] == "quick_help" and report_id is not None and situation_id is not None:
            entry = _storage_call(
                "get_current_deal_manager_quick_help",
                db_path,
                deal_id=deal_id,
                source_report_id=int(report_id),
                situation_review_id=int(situation_id),
                mode=spec["mode"],
            )
            if not isinstance(entry, dict):
                latest = _storage_call("list_deal_manager_quick_help", db_path, deal_id=deal_id, limit=20, before_id=None)
                for item in latest or []:
                    if str(item.get("mode") or "") != spec["mode"]:
                        continue
                    entry = item
                    stale = True
                    break
        elif spec["family"] == "followups" and report_id is not None and situation_id is not None:
            entry = _storage_call(
                "get_deal_manager_followups",
                db_path,
                deal_id=deal_id,
                source_report_id=int(report_id),
                situation_review_id=int(situation_id),
            )
        elif spec["family"] == "companion":
            if report_id is not None:
                last_contact = find_last_contact(deal_id)
                event_id = str((last_contact or {}).get("event_id") or "")
                if event_id:
                    entry = _storage_call(
                        "get_deal_manager_companion",
                        db_path,
                        deal_id=deal_id,
                        source_report_id=int(report_id),
                        last_event_id=event_id,
                    )
        elif spec["family"] == "full_script" and report_id is not None and situation_id is not None:
            strategy = selected_strategy or "primary"
            mode = normalize_quick_help_mode(quick_help_mode)
            getter = {
                "message": "get_deal_manager_full_script",
                "call": "get_deal_manager_call_script",
                "email": "get_deal_manager_email_script",
            }.get(str(spec.get("script_mode") or "message"), "get_deal_manager_full_script")
            if mode:
                quick_help = _storage_call(
                    "get_current_deal_manager_quick_help",
                    db_path,
                    deal_id=deal_id,
                    source_report_id=int(report_id),
                    situation_review_id=int(situation_id),
                    mode=mode,
                )
                if isinstance(quick_help, dict) and quick_help.get("id"):
                    material = _storage_call(
                        getter,
                        db_path,
                        deal_id=deal_id,
                        source_report_id=int(report_id),
                        situation_review_id=int(situation_id),
                        quick_help_id=int(quick_help["id"]),
                        selected_strategy=strategy,
                    )
                    if isinstance(material, dict):
                        entry = material
                        production_quick_help = quick_help
    except Exception:
        logger.exception("Prompt Lab could not read production CURRENT")
        entry = None
    runtime = resolved_runtime_config()
    prompt_template = production_prompt_template(spec["key"])
    model, reasoning = runtime["model"], runtime["reasoning"]
    if isinstance(entry, dict):
        model, reasoning = _entry_model_reasoning(entry, runtime)
    return {
        "exists": isinstance(entry, dict),
        "stale": stale,
        "entry": entry if isinstance(entry, dict) else None,
        "production_quick_help": production_quick_help if isinstance(production_quick_help, dict) else None,
        "model": model,
        "reasoning": reasoning,
        "prompt_template": prompt_template,
        "prompt_hash": sha256_text(prompt_template),
    }


def _snapshot_context(db_path: Path, deal_id: str) -> dict[str, Any]:
    context = load_manager_screen_context(db_path, str(deal_id), require_confirmed_situation=False)
    communications = _load_local_communications(str(deal_id))
    checklist = None
    try:
        checklist = _storage_call("get_deal_daily_checklist_analysis_projection", db_path, deal_id=str(deal_id))
    except Exception:
        checklist = {}
    last_contact = find_last_contact(str(deal_id))
    tactics = load_manager_tactics()
    bounded = {
        "deal": context["deal"],
        "deal_projection": context["deal_projection"],
        "analysis_projection": context["analysis_projection"],
        "situation_projection": context["situation_projection"],
        "current_bitrix_task": context.get("current_bitrix_task"),
        "communication_pattern_context": build_communication_pattern_context(communications),
        "checklist": checklist or {},
        "last_contact": last_contact,
        "objection_handling": _public_objections(context["analysis_projection"]),
        "source_report_id": context.get("source_report_id"),
        "situation_id": context.get("situation_id"),
        "situation_status": context.get("situation_status"),
        "analysis_run_id": _analysis_run_id(context.get("report") if isinstance(context.get("report"), dict) else None),
        "manager_tactics_hash": sha256_text(tactics),
    }
    return {"screen": context, "bounded": bounded, "tactics": tactics}


def create_snapshot(*, deal_id: str, db_path: Path = DEFAULT_DB_PATH, lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH) -> dict[str, Any]:
    return get_or_create_snapshot(deal_id=deal_id, db_path=db_path, lab_db_path=lab_db_path)


def get_or_create_snapshot(*, deal_id: str, db_path: Path = DEFAULT_DB_PATH, lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH) -> dict[str, Any]:
    with _SNAPSHOT_LOCK:
        packed = _snapshot_context(db_path, str(deal_id))
        bounded = packed["bounded"]
        snapshot_hash = sha256_json({
            "analysis_projection": bounded["analysis_projection"],
            "situation_projection": bounded["situation_projection"],
            "deal_projection": bounded["deal_projection"],
            "current_bitrix_task": bounded["current_bitrix_task"],
            "communication_pattern_context": bounded["communication_pattern_context"],
            "checklist": bounded["checklist"],
            "last_contact": bounded["last_contact"],
            "manager_tactics_hash": bounded["manager_tactics_hash"],
        })
        latest = lab_db.latest_snapshot(lab_db_path, deal_id=str(deal_id))
        if latest and latest.get("snapshot_hash") == snapshot_hash:
            return latest
        provenance = {
            "deal_id": str(deal_id),
            "source_report_id": bounded.get("source_report_id"),
            "analysis_run_id": bounded.get("analysis_run_id"),
            "situation_id": bounded.get("situation_id"),
            "situation_status": bounded.get("situation_status"),
            "snapshot_hash": snapshot_hash,
            "manager_tactics_hash": bounded.get("manager_tactics_hash"),
            "created_at": _now(),
        }
        return lab_db.save_snapshot(
            lab_db_path,
            deal_id=str(deal_id),
            source_report_id=bounded.get("source_report_id"),
            analysis_run_id=bounded.get("analysis_run_id"),
            situation_id=bounded.get("situation_id"),
            situation_status=bounded.get("situation_status"),
            snapshot_hash=snapshot_hash,
            provenance=provenance,
            context=bounded,
        )


def _materialize_production_run(
    *,
    lab_db_path: Path,
    deal_id: str,
    spec: dict[str, Any],
    current: dict[str, Any],
    snapshot: dict[str, Any],
    selected_strategy: str | None,
    upstream_run_id: int | None,
    question: str = "",
) -> dict[str, Any] | None:
    entry = current.get("entry")
    if not isinstance(entry, dict):
        return None
    result = _production_result(entry)
    if not isinstance(result, dict):
        return None
    fingerprints = {
        "prompt_hash": current["prompt_hash"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "schema_version": spec["schema_version"],
        "material_revision": spec["material_revision"],
        "manager_tactics_hash": (snapshot.get("context") or {}).get("manager_tactics_hash"),
        "model": current["model"],
        "reasoning": current["reasoning"],
        "question": str(entry.get("question") or question or ""),
        "selected_strategy": selected_strategy,
        "upstream_run_id": upstream_run_id,
        "manager_note": "",
        "previous_message": "",
    }
    fingerprint = _fingerprint(fingerprints)
    existing = lab_db.find_run_by_fingerprint(lab_db_path, fingerprint, branch="current")
    if existing is not None:
        return existing
    stored = {
        **fingerprints,
        "origin": "production_reference",
        "production_id": entry.get("id"),
        "zero_cost": True,
        "source_report_id": snapshot.get("source_report_id"),
        "situation_id": snapshot.get("situation_id"),
    }
    return lab_db.save_run(
        lab_db_path,
        session_id=None,
        turn_id=None,
        snapshot_id=int(snapshot["id"]),
        deal_id=str(deal_id),
        module_key=spec["key"],
        branch="current",
        prompt_version_id=None,
        prompt_hash=current["prompt_hash"],
        prompt_text=current["prompt_template"],
        effective_prompt=None,
        dependency_fingerprints=stored,
        schema_version=spec["schema_version"],
        material_revision=spec["material_revision"],
        model=current["model"],
        reasoning=current["reasoning"],
        max_output_tokens=spec["max_output_tokens"],
        question=str(entry.get("question") or question or ""),
        selected_strategy=selected_strategy,
        upstream_run_id=upstream_run_id,
        fingerprint=fingerprint,
        status="success",
        error=None,
        result=result,
        usage={},
        cost={"estimated_cost": 0, "estimated_cost_usd": 0, "estimated_cost_rub": 0},
        latency_seconds=0,
        response_status="imported",
        semantic_attempt_count=0,
        call_type=spec["call_type"],
    )


def import_production_current(
    *,
    deal_id: str,
    module_key: str,
    context: dict[str, Any],
    selected_strategy: str | None = None,
    quick_help_mode: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    spec = get_module(module_key)
    current = _production_current(
        db_path,
        context,
        spec,
        selected_strategy=selected_strategy,
        quick_help_mode=quick_help_mode,
    )
    if not current["exists"]:
        snapshot = lab_db.latest_snapshot(lab_db_path, deal_id=str(deal_id))
        return current, None if snapshot is None else snapshot
    snapshot = get_or_create_snapshot(deal_id=str(deal_id), db_path=db_path, lab_db_path=lab_db_path)
    upstream_run_id = None
    linked = current.get("production_quick_help")
    if spec["requires_upstream_quick_help"] and isinstance(linked, dict):
        mode = normalize_quick_help_mode(quick_help_mode) or normalize_quick_help_mode(str(linked.get("mode") or ""))
        if mode:
            qh_spec = get_module(f"quick_help.{mode}")
            qh_current = _production_current(db_path, context, qh_spec)
            qh_run = _materialize_production_run(
                lab_db_path=lab_db_path,
                deal_id=str(deal_id),
                spec=qh_spec,
                current=qh_current,
                snapshot=snapshot,
                selected_strategy=None,
                upstream_run_id=None,
            )
            if qh_upstream_matches(
                qh_run,
                deal_id=str(deal_id),
                branch="current",
                snapshot_id=int(snapshot["id"]),
                mode=mode,
            ):
                upstream_run_id = int(qh_run["id"])
    if spec["requires_upstream_quick_help"] and upstream_run_id is None:
        current["lab_run"] = None
        return current, snapshot
    lab_run = _materialize_production_run(
        lab_db_path=lab_db_path,
        deal_id=str(deal_id),
        spec=spec,
        current=current,
        snapshot=snapshot,
        selected_strategy=selected_strategy if spec["family"] == "full_script" else None,
        upstream_run_id=upstream_run_id,
        question=str((current.get("entry") or {}).get("question") or ""),
    )
    current["lab_run"] = lab_run
    return current, snapshot


def bootstrap_prompt_lab(
    *,
    deal_id: str,
    module_key: str = "quick_help.push",
    selected_strategy: str | None = None,
    quick_help_mode: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH,
) -> dict[str, Any]:
    spec = get_module(module_key)
    lab_run = None
    snapshot = lab_db.latest_snapshot(lab_db_path, deal_id=str(deal_id))
    try:
        context = load_manager_screen_context(db_path, str(deal_id), require_confirmed_situation=False)
        gate = _gate_state(context, spec)
        current, snapshot = import_production_current(
            deal_id=str(deal_id),
            module_key=module_key,
            context=context,
            selected_strategy=selected_strategy,
            quick_help_mode=spec.get("mode") if spec["family"] == "quick_help" else quick_help_mode,
            db_path=db_path,
            lab_db_path=lab_db_path,
        )
        lab_run = current.get("lab_run")
    except ValueError as error:
        gate = {
            "ok": False,
            "reason": str(error),
            "situation_status": None,
            "source_report_id": None,
            "situation_id": None,
        }
        runtime = resolved_runtime_config()
        prompt_template = production_prompt_template(module_key)
        current = {
            "exists": False,
            "stale": False,
            "entry": None,
            "lab_run": None,
            "model": runtime["model"],
            "reasoning": runtime["reasoning"],
            "prompt_template": prompt_template,
            "prompt_hash": sha256_text(prompt_template),
        }
    if snapshot is None:
        snapshot = lab_db.latest_snapshot(lab_db_path, deal_id=str(deal_id))
    return {
        "module": spec["key"],
        "modules": public_modules(),
        "models": list_lab_models(),
        "runtime": resolved_runtime_config(),
        "gate": gate,
        "production_current": {
            "exists": current["exists"],
            "stale": current.get("stale") or False,
            "entry": current.get("entry"),
            "lab_run": lab_run,
            "model": current["model"],
            "reasoning": current["reasoning"],
            "prompt_template": current["prompt_template"],
            "prompt_hash": current["prompt_hash"],
        },
        "snapshot": {
            "id": snapshot.get("id") if snapshot else None,
            "created_at": snapshot.get("created_at") if snapshot else None,
            "snapshot_hash": snapshot.get("snapshot_hash") if snapshot else None,
            "provenance": snapshot.get("provenance") if snapshot else None,
        },
        "versions": lab_db.list_prompt_versions(lab_db_path, prompt_key=spec["key"]),
    }


def _effective_prompt(spec: dict[str, Any], template: str, snapshot: dict[str, Any], extra: dict[str, Any]) -> str:
    full_kwargs = _generate_kwargs(spec, snapshot, extra, prompt_template=None)
    # Rebuild a production prompt then replace the static prefix.
    if spec["family"] == "quick_help":
        from openai_api.llm.deal_manager_quick_help import assemble_quick_help_prompt
        context = snapshot["context"]
        return assemble_quick_help_prompt(
            prompt_template=template,
            question=str(extra.get("question") or ""),
            analysis_projection=context["analysis_projection"],
            deal=context["deal"],
            current_bitrix_task=context.get("current_bitrix_task"),
            situation_projection=context["situation_projection"],
            communication_pattern_context=context["communication_pattern_context"],
        )
    if spec["family"] == "followups":
        from openai_api.llm.deal_manager_followups import followups_context_sections
        return assemble_prompt(template, followups_context_sections(**full_kwargs))
    if spec["family"] == "companion":
        from openai_api.llm.deal_manager_companion import companion_context_sections
        return assemble_prompt(template, companion_context_sections(**full_kwargs))
    if spec.get("script_mode") == "email":
        from openai_api.llm.deal_manager_email import email_context_sections
        return assemble_prompt(template, email_context_sections(**full_kwargs))
    from openai_api.llm.deal_manager_full_script import assemble_full_script_prompt
    return assemble_full_script_prompt(prompt_template=template, **full_kwargs)


def _generate_kwargs(spec: dict[str, Any], snapshot: dict[str, Any], extra: dict[str, Any], prompt_template: str | None) -> dict[str, Any]:
    context = snapshot["context"]
    kwargs: dict[str, Any] = {
        "analysis_projection": context["analysis_projection"],
        "situation_projection": context["situation_projection"],
        "deal": context["deal"],
        "current_bitrix_task": context.get("current_bitrix_task"),
        "communication_pattern_context": context["communication_pattern_context"],
    }
    if prompt_template:
        kwargs["prompt_template"] = prompt_template
    if spec["family"] == "quick_help":
        kwargs.update({"question": str(extra.get("question") or ""), "mode": spec["mode"]})
    elif spec["family"] == "followups":
        pass
    elif spec["family"] == "companion":
        kwargs.update({
            "last_contact": extra.get("last_contact") or context.get("last_contact") or {},
            "previous_message": extra.get("previous_message") or "",
            "manager_note": extra.get("manager_note") or "",
        })
    else:
        quick_help = extra.get("quick_help") or {}
        kwargs.update({
            "checklist": context.get("checklist") or {},
            "quick_help": quick_help,
            "selected_strategy": extra.get("selected_strategy") or "primary",
            "relevant_tactics": quick_help.get("lifehacks") if isinstance(quick_help.get("lifehacks"), list) else [],
            "objection_handling": context.get("objection_handling") or {"items": []},
            "script_mode": spec.get("script_mode") or "message",
        })
    return kwargs


def _run_generator(spec: dict[str, Any], kwargs: dict[str, Any], model: str, reasoning: str) -> tuple[dict[str, Any], dict[str, Any]]:
    call_type = spec["call_type"]
    kwargs = {**kwargs, "model": model, "reasoning_effort": reasoning, "call_type": call_type}
    if spec["family"] == "quick_help":
        return generate_deal_manager_quick_help(**kwargs)
    if spec["family"] == "followups":
        return generate_deal_manager_followups(**kwargs)
    if spec["family"] == "companion":
        return generate_deal_manager_companion(**kwargs)
    if spec.get("script_mode") == "email":
        kwargs.pop("script_mode", None)
        kwargs.pop("checklist", None)
        kwargs.pop("relevant_tactics", None)
        kwargs.pop("objection_handling", None)
        return generate_deal_manager_email(**kwargs)
    return generate_deal_manager_full_script(**kwargs)


def _fingerprint(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


def _public_error(error: BaseException) -> str:
    if isinstance(error, (ModelResponseIncompleteError, ModelJsonParseError, ValueError)):
        return public_quick_help_error(error)
    return public_quick_help_error(error)


def _run_lab_job(
    job_id: str,
    *,
    db_path: Path,
    lab_db_path: Path,
    deal_id: str,
    module_key: str,
    branch: str,
    snapshot_id: int,
    session_id: int | None,
    turn_id: int | None,
    prompt_template: str,
    prompt_version_id: int | None,
    model: str,
    reasoning: str,
    question: str,
    selected_strategy: str | None,
    upstream_run_id: int | None,
    manager_note: str,
    previous_message: str,
    quick_help_mode: str | None = None,
) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, "context", "Собираем frozen snapshot", 20)
        spec = get_module(module_key)
        snapshot = lab_db.get_snapshot(lab_db_path, snapshot_id)
        if snapshot is None:
            raise ValueError("Snapshot Prompt Lab не найден")
        gate = _gate_state(
            {
                "situation_status": snapshot.get("situation_status"),
                "source_report_id": snapshot.get("source_report_id"),
                "situation_id": snapshot.get("situation_id"),
            },
            spec,
        )
        if not gate["ok"]:
            raise ValueError(gate["reason"] or "Prompt Lab заблокирован текущей ситуацией")
        extra: dict[str, Any] = {
            "question": question,
            "selected_strategy": selected_strategy or "primary",
            "manager_note": manager_note,
            "previous_message": previous_message,
        }
        if spec["requires_upstream_quick_help"]:
            upstream = require_qh_upstream(
                lab_db_path=lab_db_path,
                upstream_run_id=upstream_run_id,
                deal_id=str(deal_id),
                branch=str(branch),
                snapshot_id=int(snapshot_id),
                mode=quick_help_mode,
            )
            extra["quick_help"] = upstream.get("result") or {}
        template = prompt_template or production_prompt_template(module_key, manager_note=manager_note)
        kwargs = _generate_kwargs(spec, snapshot, extra, prompt_template=template)
        effective = _effective_prompt(spec, template, snapshot, extra)
        fingerprints = {
            "prompt_hash": sha256_text(template),
            "snapshot_hash": snapshot["snapshot_hash"],
            "schema_version": spec["schema_version"],
            "material_revision": spec["material_revision"],
            "manager_tactics_hash": (snapshot.get("context") or {}).get("manager_tactics_hash"),
            "model": model,
            "reasoning": reasoning,
            "question": question,
            "selected_strategy": selected_strategy,
            "upstream_run_id": upstream_run_id,
            "manager_note": manager_note,
            "previous_message": previous_message,
        }
        fingerprint = _fingerprint(fingerprints)
        _touch(job, "llm", "Вызываем модель", 55)
        try:
            result, metadata = _run_generator(spec, kwargs, model, reasoning)
            status = "success"
            error_text = None
        except Exception as error:
            result, metadata = None, {}
            status = "error"
            error_text = _public_error(error)
            logger.info("Prompt Lab run failed: %s", error)
        safe_meta = _safe_model_meta(metadata if isinstance(metadata, dict) else None)
        usage = safe_meta.get("usage") if isinstance(safe_meta.get("usage"), dict) else None
        cost = {
            "estimated_cost": safe_meta.get("estimated_cost"),
            "estimated_cost_usd": safe_meta.get("estimated_cost_usd"),
            "estimated_cost_rub": safe_meta.get("estimated_cost_rub"),
        }
        saved = lab_db.save_run(
            lab_db_path,
            session_id=session_id,
            turn_id=turn_id,
            snapshot_id=int(snapshot_id),
            deal_id=str(deal_id),
            module_key=module_key,
            branch=branch,
            prompt_version_id=prompt_version_id,
            prompt_hash=sha256_text(template),
            prompt_text=template,
            effective_prompt=effective,
            dependency_fingerprints=fingerprints,
            schema_version=spec["schema_version"],
            material_revision=spec["material_revision"],
            model=model,
            reasoning=reasoning,
            max_output_tokens=spec["max_output_tokens"],
            question=question,
            selected_strategy=selected_strategy,
            upstream_run_id=upstream_run_id,
            fingerprint=fingerprint,
            status=status,
            error=error_text,
            result=result,
            usage=usage,
            cost=cost,
            latency_seconds=safe_meta.get("latency_seconds"),
            response_status=safe_meta.get("response_status"),
            semantic_attempt_count=safe_meta.get("semantic_attempt_count"),
            call_type=spec["call_type"],
        )
        with _LOCK:
            job.run_id = int(saved["id"])
            job.status = "done" if status == "success" else "error"
            job.error = error_text
        _touch(job, job.status, "Готово" if status == "success" else (error_text or "Ошибка"), 100)
    except Exception as error:
        logger.exception("Prompt Lab job failed")
        with _LOCK:
            job.status = "error"
            job.error = _public_error(error)
        _touch(job, "error", job.error or "Ошибка Prompt Lab", 100)


def start_lab_run(
    *,
    deal_id: str,
    module_key: str,
    branch: str,
    snapshot_id: int | None = None,
    session_id: int | None = None,
    turn_id: int | None = None,
    prompt_template: str | None = None,
    prompt_version_id: int | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    question: str = "",
    selected_strategy: str | None = None,
    upstream_run_id: int | None = None,
    manager_note: str = "",
    previous_message: str = "",
    reuse_existing: bool | None = None,
    quick_help_mode: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH,
) -> dict[str, Any]:
    if branch not in BRANCHES:
        raise ValueError("branch должен быть current или experiment")
    spec = get_module(module_key)
    runtime = resolved_runtime_config()
    model_id, effort = validate_model_reasoning(model or runtime["model"], reasoning or runtime["reasoning"])
    template = prompt_template
    if prompt_version_id is not None:
        version = lab_db.get_prompt_version(lab_db_path, int(prompt_version_id))
        if version is None:
            raise ValueError("Версия prompt не найдена")
        if version["prompt_key"] != module_key:
            raise ValueError("Версия prompt относится к другому модулю")
        template = version["prompt_text"]
    if not template:
        template = production_prompt_template(module_key, manager_note=manager_note)
    snapshot = lab_db.get_snapshot(lab_db_path, int(snapshot_id)) if snapshot_id else None
    if snapshot is None:
        snapshot = get_or_create_snapshot(deal_id=str(deal_id), db_path=db_path, lab_db_path=lab_db_path)
    if spec["requires_upstream_quick_help"]:
        require_qh_upstream(
            lab_db_path=lab_db_path,
            upstream_run_id=upstream_run_id,
            deal_id=str(deal_id),
            branch=str(branch),
            snapshot_id=int(snapshot["id"]),
            mode=quick_help_mode,
        )
    fingerprints = {
        "prompt_hash": sha256_text(template),
        "snapshot_hash": snapshot["snapshot_hash"],
        "schema_version": spec["schema_version"],
        "material_revision": spec["material_revision"],
        "manager_tactics_hash": (snapshot.get("context") or {}).get("manager_tactics_hash"),
        "model": model_id,
        "reasoning": effort,
        "question": str(question or ""),
        "selected_strategy": selected_strategy,
        "upstream_run_id": upstream_run_id,
        "manager_note": str(manager_note or ""),
        "previous_message": str(previous_message or ""),
    }
    existing = lab_db.find_run_by_fingerprint(lab_db_path, _fingerprint(fingerprints), branch=branch)
    if existing is not None and reuse_existing is not False:
        job = PromptLabJob(
            job_id=str(uuid.uuid4()),
            deal_id=str(deal_id),
            module_key=module_key,
            branch=branch,
            status="done",
            stage="done",
            detail="Такая конфигурация уже запускалась",
            percent=100,
            run_id=int(existing["id"]),
            reused=True,
            existing=True,
            created_at=_now(),
        )
        if reuse_existing is None:
            job.status = "exists"
            job.stage = "exists"
        with _LOCK:
            _JOBS[job.job_id] = job
        payload = asdict(job)
        payload["run"] = existing
        return payload
    if session_id is None:
        session_id = int(lab_db.create_session(lab_db_path, deal_id=str(deal_id))["id"])
    if turn_id is None:
        turn_id = int(lab_db.create_turn(
            lab_db_path,
            session_id=session_id,
            snapshot_id=int(snapshot["id"]),
            module_key=module_key,
            question=str(question or ""),
        )["id"])
    job = PromptLabJob(
        job_id=str(uuid.uuid4()),
        deal_id=str(deal_id),
        module_key=module_key,
        branch=branch,
        created_at=_now(),
    )
    with _LOCK:
        _JOBS[job.job_id] = job
    thread = threading.Thread(
        target=_run_lab_job,
        kwargs={
            "job_id": job.job_id,
            "db_path": db_path,
            "lab_db_path": lab_db_path,
            "deal_id": str(deal_id),
            "module_key": module_key,
            "branch": branch,
            "snapshot_id": int(snapshot["id"]),
            "session_id": session_id,
            "turn_id": turn_id,
            "prompt_template": template,
            "prompt_version_id": prompt_version_id,
            "model": model_id,
            "reasoning": effort,
            "question": str(question or ""),
            "selected_strategy": selected_strategy,
            "upstream_run_id": upstream_run_id,
            "manager_note": str(manager_note or ""),
            "previous_message": str(previous_message or ""),
            "quick_help_mode": quick_help_mode,
        },
        daemon=True,
    )
    thread.start()
    return asdict(job)


def save_version(
    *,
    prompt_key: str,
    prompt_text: str,
    based_on_id: int | None = None,
    note: str | None = None,
    lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH,
) -> dict[str, Any]:
    get_module(prompt_key)
    production_hash = sha256_text(production_prompt_template(prompt_key))
    return lab_db.save_prompt_version(
        lab_db_path,
        prompt_key=prompt_key,
        prompt_text=prompt_text,
        prompt_hash=sha256_text(prompt_text),
        base_production_hash=production_hash,
        based_on_id=based_on_id,
        note=note,
    )


def export_payload(
    *,
    mode: str,
    prompt_key: str | None = None,
    version_id: int | None = None,
    lab_db_path: Path = DEFAULT_PROMPT_LAB_DB_PATH,
) -> dict[str, Any]:
    if mode == "current":
        if version_id is None:
            raise ValueError("Нужен version_id выбранной версии")
        version = lab_db.get_prompt_version(lab_db_path, int(version_id))
        if version is None:
            raise ValueError("Версия prompt не найдена")
        if prompt_key and version["prompt_key"] != prompt_key:
            raise ValueError("Версия prompt относится к другому модулю")
        versions = [version]
    elif mode == "candidates_module":
        if not prompt_key:
            raise ValueError("Нужен prompt_key модуля")
        versions = lab_db.list_prompt_versions(lab_db_path, prompt_key=prompt_key, candidates_only=True)
    elif mode == "candidates_all":
        versions = lab_db.list_prompt_versions(lab_db_path, candidates_only=True)
    else:
        raise ValueError("Неизвестный режим экспорта")
    items = []
    for version in versions:
        stats = lab_db.review_stats(lab_db_path, prompt_version_id=int(version["id"]))
        runs = lab_db.list_runs(lab_db_path, prompt_version_id=int(version["id"]), limit=20)
        items.append({
            "prompt_key": version["prompt_key"],
            "version": version["version_number"],
            "created_at": version["created_at"],
            "candidate": version["candidate"],
            "verified": version["verified"],
            "note": version.get("note"),
            "prompt_text": version["prompt_text"],
            "base_production_hash": version.get("base_production_hash"),
            "schema_version": get_module(version["prompt_key"])["schema_version"],
            "models_tested": sorted({f"{row['model']} / {row['reasoning']}" for row in runs}),
            "run_ids": [row["id"] for row in runs],
            "review_stats": stats,
        })
    return {"mode": mode, "items": items}
