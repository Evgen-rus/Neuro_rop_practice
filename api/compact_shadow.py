"""Manual, isolated Compact Shadow runs for the local review UI.

The module only reads the input files recorded by a completed full analysis. It
never invokes the legacy pipeline and has no Bitrix client dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.run_attention_delta_shadow import (
    build_shadow_request,
    load_shadow_inputs,
    response_metrics,
)
from openai_api.config import ANALYSIS_MODEL, ATTENTION_DELTA_MAX_OUTPUT_TOKENS
from openai_api.llm.attention_delta import (
    materialize_deal_attention_delta,
    materialize_lead_attention_delta,
)
from openai_api.llm.evidence_coverage import validate_evidence_context_coverage
from openai_api.llm.lead_playbook_resolver import normalize_lead_action_playbook
from openai_api.llm.llm_client import call_structured_output_json
from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_compact_shadow_feedback,
    get_compact_shadow_run,
    list_compact_shadow_runs,
    save_compact_shadow_run,
)

from api.jobs import analysis_paths, unwrap_analysis_payload


_LOCK = threading.Lock()
_JOBS: dict[str, "CompactJob"] = {}
_TRANSCRIPT_SECTION = re.compile(
    r"(?ms)^### (?P<header>.*?activity_id=(?P<id>[A-Za-z0-9:_-]+).*?)$\s*^```(?:text)?\s*\n(?P<body>.*?)^```"
)
_TYPED_ID = re.compile(r"\b(?:activity_id|task_id|comment_id|call_id|id)\s*[=:]\s*([A-Za-z0-9:_-]+)\b", re.I)


@dataclass
class CompactJob:
    job_id: str
    entity_type: str
    entity_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(MSK_TZ).isoformat(timespec="seconds"))
    run_id: str | None = None
    error: str | None = None


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


def _touch(job: CompactJob) -> None:
    job.updated_at = _now()


def _analysis_envelope(entity_type: str, entity_id: str) -> tuple[Path, dict[str, Any]]:
    path = analysis_paths(entity_type, entity_id)["analysis_json"]
    if not path.exists():
        raise FileNotFoundError("Текущий полный анализ ещё не выполнен")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Файл полного анализа имеет неверный формат")
    return path, payload


def _prepared_inputs(entity_type: str, entity_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    path, full_payload = _analysis_envelope(entity_type, entity_id)
    # The shadow loader intentionally trusts only input paths recorded by the
    # completed full analysis; this prevents an implicit CRM/full re-run.
    inputs = load_shadow_inputs(
        {
            "case_id": f"ui-{entity_type}-{entity_id}",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "baseline": {"analysis_json": str(path)},
        }
    )
    return path, full_payload, inputs


def _snapshot_hash(inputs: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in ("entity_type", "entity_id", "history_text", "transcript_text", "diagnostics_text"):
        digest.update(str(inputs.get(key) or "").encode("utf-8"))
        digest.update(b"\0")
    stage_policy = inputs.get("stage_policy") or {}
    digest.update(json.dumps(stage_policy, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()


def _run_view(run: dict[str, Any] | None, *, current_snapshot_hash: str | None = None) -> dict[str, Any] | None:
    if not run:
        return None
    value = dict(run)
    value.pop("error", None)  # Detailed errors may contain local filesystem paths.
    value["is_current"] = bool(current_snapshot_hash and value.get("snapshot_hash") == current_snapshot_hash)
    value["feedback"] = get_compact_shadow_feedback(DEFAULT_DB_PATH, str(value["id"]))
    return value


def review_payload(entity_type: str, entity_id: str, *, selected_run_id: str | None = None) -> dict[str, Any]:
    full_analysis: dict[str, Any] | None = None
    preflight_error: str | None = None
    snapshot_hash: str | None = None
    try:
        _path, full_payload, inputs = _prepared_inputs(entity_type, entity_id)
        full_analysis = unwrap_analysis_payload(full_payload)
        snapshot_hash = _snapshot_hash(inputs)
    except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        preflight_error = "Compact-анализ недоступен: нужен полный анализ с сохранённым контекстом и транскриптом."

    runs = list_compact_shadow_runs(DEFAULT_DB_PATH, entity_type=entity_type, entity_id=entity_id)
    selected = next((item for item in runs if item["id"] == selected_run_id), None) if selected_run_id else None
    selected = selected or (runs[0] if runs else None)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "full_analysis": full_analysis,
        "snapshot_hash": snapshot_hash,
        "preflight_error": preflight_error,
        "selected_run": _run_view(selected, current_snapshot_hash=snapshot_hash),
        "runs": [_run_view(item, current_snapshot_hash=snapshot_hash) for item in runs],
    }


def _fallback_class(coverage: dict[str, Any]) -> str:
    return "compact_safe" if coverage.get("status") == "passed" else "full_fallback_recommended"


def _execute_run(entity_type: str, entity_id: str, run_id: str) -> dict[str, Any]:
    started_at = _now()
    _path, _full_payload, inputs = _prepared_inputs(entity_type, entity_id)
    snapshot_hash = _snapshot_hash(inputs)
    prompt, schema, schema_name, validator = build_shadow_request(inputs)
    save_compact_shadow_run(
        DEFAULT_DB_PATH,
        run_id=run_id,
        entity_type=entity_type,
        entity_id=entity_id,
        snapshot_hash=snapshot_hash,
        status="running",
        started_at=started_at,
        model=ANALYSIS_MODEL,
    )
    try:
        delta, metadata = call_structured_output_json(
            prompt,
            schema=schema,
            schema_name=schema_name,
            model=ANALYSIS_MODEL,
            max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
        )
        raw_review_key = "lead_review" if entity_type == "lead" else "deal_review"
        raw_review = delta.get(raw_review_key) if isinstance(delta.get(raw_review_key), dict) else {}
        raw_playbook = raw_review.get("action_playbook")
        if entity_type == "lead":
            delta, normalization = normalize_lead_action_playbook(
                delta, history_text=inputs["history_text"], transcript_text=inputs["transcript_text"]
            )
            delta = materialize_lead_attention_delta(delta)
            metadata["lead_playbook_normalization"] = normalization
        else:
            delta = materialize_deal_attention_delta(delta)
        validator(delta)
        coverage = validate_evidence_context_coverage(
            delta,
            history_text=inputs["history_text"],
            transcript_text=inputs["transcript_text"],
            stage_policy=inputs["stage_policy"],
            structured_blocks=[{f"{entity_type}_id": entity_id}],
        )
        metrics = response_metrics(metadata, max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS)
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        cost = metadata.get("estimated_cost") if isinstance(metadata.get("estimated_cost"), dict) else {}
        status = "completed" if coverage["status"] == "passed" else "evidence_coverage_failed"
        # Keep the original model choice as UI metadata without modifying the
        # validated Compact contract that is passed to the business validator.
        stored_analysis = {
            **delta,
            "_ui": {
                "raw_playbook": raw_playbook,
                "normalization_reason": metadata.get("lead_playbook_normalization", {}).get("normalization_reason")
                if isinstance(metadata.get("lead_playbook_normalization"), dict)
                else None,
            },
        }
        save_compact_shadow_run(
            DEFAULT_DB_PATH,
            run_id=run_id,
            entity_type=entity_type,
            entity_id=entity_id,
            snapshot_hash=snapshot_hash,
            status=status,
            started_at=started_at,
            completed_at=_now(),
            model=str(metadata.get("model") or ANALYSIS_MODEL),
            analysis=stored_analysis,
            evidence_coverage=coverage,
            fallback_class=_fallback_class(coverage),
            usage={**usage, "response_metrics": metrics},
            cost_rub=cost.get("estimated_cost_rub"),
        )
    except Exception as error:  # A compact failure is persisted, never retried automatically.
        save_compact_shadow_run(
            DEFAULT_DB_PATH,
            run_id=run_id,
            entity_type=entity_type,
            entity_id=entity_id,
            snapshot_hash=snapshot_hash,
            status="error",
            started_at=started_at,
            completed_at=_now(),
            model=ANALYSIS_MODEL,
            fallback_class="full_fallback_recommended",
            error=str(error),
        )
        raise
    result = get_compact_shadow_run(DEFAULT_DB_PATH, run_id)
    return result or {}


def _run_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        job.status = "running"
        _touch(job)
    run_id = uuid.uuid4().hex
    try:
        _execute_run(job.entity_type, job.entity_id, run_id)
        with _LOCK:
            job = _JOBS[job_id]
            job.status = "done"
            job.run_id = run_id
            _touch(job)
    except Exception as error:
        with _LOCK:
            job = _JOBS[job_id]
            job.status = "error"
            job.run_id = run_id
            job.error = "Compact-анализ не завершён. Откройте сохранённый fallback или выполните полный анализ."
            _touch(job)


def start_compact_job(entity_type: str, entity_id: str) -> dict[str, Any]:
    # Preflight before starting a paid/manual request: no implicit run is ever created.
    _prepared_inputs(entity_type, entity_id)
    with _LOCK:
        if any(
            job.entity_type == entity_type and job.entity_id == entity_id and job.status in {"queued", "running"}
            for job in _JOBS.values()
        ):
            raise ValueError("Compact-анализ для этой карточки уже выполняется")
        job = CompactJob(job_id=uuid.uuid4().hex[:12], entity_type=entity_type, entity_id=str(entity_id))
        _JOBS[job.job_id] = job
    threading.Thread(target=_run_job, args=(job.job_id,), daemon=True).start()
    return asdict(job)


def get_compact_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return asdict(job) if job else None


def _evidence_source(inputs: dict[str, Any], evidence_id: str) -> dict[str, Any] | None:
    # Exact typed IDs only: never match numeric text by substring.
    for match in _TRANSCRIPT_SECTION.finditer(inputs["transcript_text"]):
        if match.group("id") == evidence_id:
            header = match.group("header")
            return {
                "evidence_id": evidence_id,
                "source_type": "transcript",
                "timestamp": None,
                "namespace": "activity",
                "is_full_evidence": True,
                "fragment": match.group("body").strip(),
                "header": header,
            }
    for line in inputs["history_text"].splitlines():
        ids = _TYPED_ID.findall(line)
        if evidence_id in ids:
            source_type = "history"
            lowered = line.lower()
            if "task" in lowered:
                source_type = "task"
            elif "comment" in lowered:
                source_type = "comment"
            elif "call" in lowered:
                source_type = "call"
            return {
                "evidence_id": evidence_id,
                "source_type": source_type,
                "timestamp": None,
                "namespace": "history",
                "is_full_evidence": True,
                "fragment": line.strip(),
                "header": None,
            }
    return None


def get_evidence(entity_type: str, entity_id: str, evidence_id: str) -> dict[str, Any] | None:
    _path, _payload, inputs = _prepared_inputs(entity_type, entity_id)
    return _evidence_source(inputs, evidence_id)
