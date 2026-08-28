"""
Run deal LLM analysis only when the normalized deal snapshot changed.

This is an orchestration layer over analyze_deal.py. The existing manual CLI
remains unchanged; this script adds SQLite state, snapshot comparison, skip
logic, and deterministic mini recommendations.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_DEAL_WORKSPACE_ROOT
from openai_api.audio.build_deal_transcript_context import build_all_deal_transcript_context
from openai_api.audio.transcript_context import AGGREGATE_STEM
from openai_api.change_detection.decision_engine import (
    ERROR,
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    SKIPPED_NO_CHANGES,
    ProcessingDecision,
    decide_deal_processing,
    mini_triggers,
    render_mini_recommendation,
    save_mini_recommendation_markdown,
    soft_diff_triggers,
)
from openai_api.config import (
    ANALYSIS_MODEL,
    CONTEXT_MEMORY_OPTIMIZATION_ENABLED,
    CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK,
    CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE,
    DEAL_INCREMENTAL_V2_MODE,
)
from openai_api.llm.deal_incremental import IncrementalContextError, build_incremental_context
from openai_api.llm.deal_incremental import previous_business_analysis
from openai_api.llm.deal_evidence import (
    collect_deal_evidence,
    coverage_from_evidence,
    evidence_delta,
)
from openai_api.llm.deal_incremental_v2 import (
    IncrementalV2Result,
    build_v2_compact_diagnostics,
    build_v2_compact_policy,
    render_v2_compact_diagnostics,
    run_incremental_v2,
)
from openai_api.llm.analyze_deal import (
    load_context_diagnostics_for_analysis,
    render_report,
)
from openai_api.llm.deal_semantic_state import SCHEMA_VERSION, bootstrap_semantic_state
from openai_api.change_detection.snapshot import (
    build_deal_snapshot,
    compare_snapshots,
    fingerprint_snapshot,
    load_json,
    save_json,
)
from openai_api.change_detection.stage_policy import build_deal_stage_policy
from openai_api.change_detection.provenance import analysis_run_provenance
from progress_events import compact_decision_status, emit_progress
from setup import BASE_DIR, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_analysis_run_evidence_ids,
    get_today_mini_trigger_types,
    get_entity_memory,
    get_entity_state,
    get_latest_deal_semantic_checkpoint,
    get_latest_neuro_rop_recommendation_projection,
    init_db,
    save_analysis_run,
    save_deal_incremental_v2_run,
    save_deal_semantic_checkpoint,
    save_mini_recommendation,
    update_entity_memory,
    upsert_entity_state,
    utcish_now,
)


logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze deal only if meaningful changes are detected")
    parser.add_argument("--deal-id", required=True, help="Deal ID to check")
    parser.add_argument("--deal-root", default=str(DEFAULT_DEAL_WORKSPACE_ROOT), help="Root folder with deal workspaces")
    parser.add_argument("--db-path", default=None, help="SQLite path. Default: ROP_DB_PATH or reports/rop_assistant/rop_assistant.sqlite")
    parser.add_argument("--transcript", default="latest", help="Transcript path, 'all', 'latest', or 'none'. Default: latest if exists, else none.")
    parser.add_argument("--model", default=None, help="Optional OpenAI analysis model passed to analyze_deal.py")
    parser.add_argument("--force-llm", action="store_true", help="Force a full LLM analysis regardless of change detection.")
    parser.add_argument(
        "--dry-run-decision",
        action="store_true",
        help="Build snapshot and print decision without calling analyze_deal.py or writing state.",
    )
    return parser.parse_args()


def db_path_from_args(value: str | None) -> Path:
    if value:
        return Path(value)
    env_value = os.getenv("ROP_DB_PATH", "").strip()
    return Path(env_value) if env_value else DEFAULT_DB_PATH


def deal_dir(args: argparse.Namespace) -> Path:
    return Path(args.deal_root) / f"deal_{args.deal_id}"


def raw_bundle_path(args: argparse.Namespace) -> Path:
    workspace_path = deal_dir(args) / "raw" / f"deal_{args.deal_id}_context.json"
    if workspace_path.exists():
        return workspace_path
    fallback = BASE_DIR / "reports" / "bitrix_customer_path" / "raw" / f"deal_{args.deal_id}_context.json"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"Deal raw context not found: {workspace_path} or {fallback}")


def latest_transcript_or_none(transcripts_dir: Path) -> Path | None:
    candidates = sorted(
        [
            path
            for path in transcripts_dir.glob("*.md")
            if path.is_file() and AGGREGATE_STEM not in path.stem
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_transcript_for_snapshot(value: str, current_deal_dir: Path) -> tuple[Path | None, str]:
    lowered = value.lower()
    if lowered == "none":
        return None, "none"
    if lowered == "latest":
        latest = latest_transcript_or_none(current_deal_dir / "transcripts")
        return latest, str(latest) if latest else "none"
    if lowered == "all":
        deal_id = current_deal_dir.name.removeprefix("deal_")
        path = build_all_deal_transcript_context(current_deal_dir, deal_id)
        return path, str(path)

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path, str(path)


def analysis_paths(current_deal_dir: Path, deal_id: str) -> dict[str, Path]:
    analysis_dir = current_deal_dir / "analysis"
    return {
        "analysis": analysis_dir / f"deal_{deal_id}_analysis.json",
        "report": analysis_dir / f"deal_{deal_id}_rop_report.md",
        "raw": analysis_dir / f"deal_{deal_id}_raw_model_output.txt",
        "snapshot": analysis_dir / f"deal_{deal_id}_snapshot.json",
        "mini": analysis_dir / f"deal_{deal_id}_mini_recommendation.md",
        "incremental_context": analysis_dir / f"deal_{deal_id}_incremental_context.json",
        "v2_shadow": analysis_dir / "v2_shadow" / f"deal_{deal_id}_latest.json",
    }


def _v2_crm_delta(snapshot: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    deal = snapshot.get("deal") if isinstance(snapshot.get("deal"), dict) else {}
    return {
        "change_types": list(diff.get("changes") or []),
        "details": dict(diff.get("details") or {}),
        "current_deal": {
            key: deal.get(key)
            for key in ("id", "stage_id", "category_id", "opportunity", "currency_id", "assigned_by_id", "closed", "moved_time")
        },
    }


def _trusted_v2_checkpoint(
    db_path: Path, deal_id: str, previous_state: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Trust ties the checkpoint to its source analysis run, not to polling state.

    MINI/SKIP runs move ``entity_state.current_fingerprint`` forward without any
    LLM analysis, so a fingerprint mismatch alone must not invalidate a
    checkpoint whose source analysis is still the latest one. A checkpoint
    without the baseline snapshot cannot build a cumulative delta and falls
    back to FULL safely.
    """
    checkpoint = get_latest_deal_semantic_checkpoint(db_path, deal_id, schema_version=SCHEMA_VERSION)
    if not checkpoint or not previous_state:
        return None
    payload = previous_state.get("last_analysis") if isinstance(previous_state.get("last_analysis"), dict) else {}
    run_id = _int_or_none(payload.get("analysis_run_id"))
    if run_id is None or checkpoint.get("source_analysis_run_id") != run_id:
        return None
    if not isinstance(checkpoint.get("baseline_snapshot"), dict):
        return None
    return checkpoint


def _save_v2_checkpoint_from_analysis(
    *, db_path: Path, deal_id: str, payload: dict[str, Any], fingerprint: str,
    raw_bundle: dict[str, Any], transcripts_dir: Path, mode: str,
    transcript_path: Path | None = None, snapshot: dict[str, Any] | None = None,
) -> int:
    """Bootstrap a V2 checkpoint from a proven analysis run.

    Coverage is built only from evidence the analysis provably received
    (recorded in ``analysis_runs.evidence_ids_included_json``). Legacy analyses
    without provenance must not be guessed: no trusted checkpoint is created
    and the next incremental run falls back to FULL.
    """
    analysis = extract_analysis(payload)
    source_run_id = _int_or_none(payload.get("analysis_run_id"))
    included_ids = (
        get_analysis_run_evidence_ids(db_path, source_run_id)
        if source_run_id is not None
        else None
    )
    if included_ids is None:
        logger.warning(
            "Deal %s has no evidence provenance for run %s; skipping V2 checkpoint bootstrap.",
            deal_id,
            source_run_id,
        )
        return 0
    available = {str(item["evidence_id"]): item for item in collect_deal_evidence(raw_bundle, transcripts_dir)}
    covered = {
        evidence_id: coverage_from_evidence([available[evidence_id]])[evidence_id]
        for evidence_id in sorted(set(included_ids) & set(available))
    }
    state = bootstrap_semantic_state(
        analysis,
        deal_id=deal_id,
        source_analysis_run_id=source_run_id,
        source_fingerprint=fingerprint,
        evidence_coverage=covered,
    )
    return save_deal_semantic_checkpoint(
        db_path,
        entity_id=deal_id,
        schema_version=SCHEMA_VERSION,
        source_analysis_run_id=source_run_id,
        source_fingerprint=fingerprint,
        semantic_state=state,
        mode=mode,
        baseline_snapshot=snapshot,
    )


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_v2_candidate(
    *, paths: dict[str, Path], args: argparse.Namespace, result: IncrementalV2Result,
    stage_policy: dict[str, Any], prior_recommendation: dict[str, Any] | None,
    production: bool,
) -> dict[str, Any]:
    payload = {
        "deal_id": str(args.deal_id),
        "analysis_mode": "incremental_v2",
        "model_metadata": {
            key: value for key, value in result.metadata.items() if not key.startswith("_")
        },
        "crm_stage_policy": stage_policy,
        "PRIOR_NEURO_ROP_RECOMMENDATION": prior_recommendation,
        "evidence_ids_included": sorted(result.semantic_state.get("evidence_coverage") or {}),
        "analysis": result.analysis,
    }
    target = paths["analysis"] if production else paths["v2_shadow"]
    save_json(target, payload)
    if production:
        _, context_diagnostics_payload, _ = load_context_diagnostics_for_analysis(
            entity_type="deal",
            entity_id=str(args.deal_id),
            workspace_root=Path(args.deal_root),
        )
        report_metadata = dict(result.metadata)
        paths["report"].write_text(
            render_report(result.analysis, report_metadata, context_diagnostics_payload),
            encoding="utf-8",
        )
        raw_outputs = [str(value) for value in result.metadata.get("_raw_outputs", [])]
        paths["raw"].write_text("\n\n--- V2 CALL ---\n\n".join(raw_outputs), encoding="utf-8")
    return payload


def run_existing_analyzer(
    args: argparse.Namespace,
    transcript_arg: str,
    *,
    incremental_context_path: Path | None = None,
) -> None:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "openai_api" / "llm" / "analyze_deal.py"),
        "--deal-id",
        str(args.deal_id),
        "--deal-root",
        str(args.deal_root),
        "--transcript",
        transcript_arg,
        "--allow-direct-llm",
    ]
    if args.model:
        command.extend(["--model", str(args.model)])
    if incremental_context_path is not None:
        command.extend(["--incremental-context", str(incremental_context_path)])

    logger.info("Running existing deal analyzer: %s", " ".join(command))
    subprocess.run(command, cwd=BASE_DIR, check=True)


def full_fallback_decision(decision: ProcessingDecision, reason: str) -> ProcessingDecision:
    return ProcessingDecision(
        status=FULL_LLM_ANALYSIS,
        reasons=[*decision.reasons, f"Безопасный FULL fallback: {reason}."],
        triggers=decision.triggers,
        diff=decision.diff,
    )


def load_analysis_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Analysis JSON was not created: {path}")
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Analysis JSON is not an object: {path}")
    return payload


def extract_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    analysis = payload.get("analysis")
    return analysis if isinstance(analysis, dict) else payload


def extract_risk_level(payload: dict[str, Any]) -> str | None:
    analysis = extract_analysis(payload)
    risk = analysis.get("main_risk", {}) if isinstance(analysis, dict) else {}
    value = risk.get("risk_level") if isinstance(risk, dict) else None
    return str(value) if value else None


def extract_last_recommendation(payload: dict[str, Any]) -> dict[str, Any] | None:
    analysis = extract_analysis(payload)
    if not isinstance(analysis, dict):
        return None
    recommendation = {
        "manager_action_block": analysis.get("manager_action_block"),
        "rop_action": analysis.get("rop_action"),
        "call_attempt_recommendation": analysis.get("call_attempt_recommendation"),
    }
    return recommendation


def persist_successful_llm_run(
    *,
    db_path: Path,
    args: argparse.Namespace,
    fingerprint: str,
    snapshot: dict[str, Any],
    decision_status: str,
    paths: dict[str, Path],
    decision_reason: dict[str, Any],
    prompt_version: str | None = None,
    evidence_ids_included: list[str] | None = None,
) -> int:
    payload = load_analysis_payload(paths["analysis"])
    analysis = extract_analysis(payload)
    if evidence_ids_included is None and isinstance(payload.get("evidence_ids_included"), list):
        evidence_ids_included = [str(item) for item in payload["evidence_ids_included"]]
    memory_update = analysis.get("memory_update") if isinstance(analysis, dict) else None

    if isinstance(memory_update, dict):
        update_entity_memory(
            db_path,
            entity_type="deal",
            entity_id=str(args.deal_id),
            memory_update=memory_update,
        )

    run_id = save_analysis_run(
        db_path,
        entity_type="deal",
        entity_id=str(args.deal_id),
        status=decision_status,
        fingerprint=fingerprint,
        analysis_path=str(paths["analysis"]),
        report_path=str(paths["report"]),
        raw_path=str(paths["raw"]),
        decision_reason=decision_reason,
        evidence_ids_included=evidence_ids_included,
        **analysis_run_provenance(
            payload,
            fingerprint=fingerprint,
            decision_reason=decision_reason,
            prompt_version=(
                prompt_version
                or (
                    "neuro-rop:incremental-deal:v1"
                    if decision_status == INCREMENTAL_LLM_ANALYSIS
                    else "neuro-rop:full-deal:v2"
                )
            ),
            model_override=args.model,
        ),
    )
    payload["analysis_run_id"] = run_id
    save_json(paths["analysis"], payload)
    upsert_entity_state(
        db_path,
        entity_type="deal",
        entity_id=str(args.deal_id),
        fingerprint=fingerprint,
        snapshot=snapshot,
        last_analysis_status=decision_status,
        last_analysis_path=str(paths["analysis"]),
        last_report_path=str(paths["report"]),
        last_risk_level=extract_risk_level(payload),
        last_analysis=payload,
        last_recommendation=extract_last_recommendation(payload),
        last_analysis_at=utcish_now(),
    )
    return run_id


def emit_deal_publish_ready(
    deal_id: str,
    *,
    analysis_run_id: int | None,
    engine_status: str,
    error: str | None = None,
) -> None:
    """Terminal event after SQLite and files are written. Inner analyze_deal `done` is not enough."""
    compact = compact_decision_status(engine_status) or "error"
    details = {
        "full": "FULL-анализ сохранён",
        "mini": "MINI-рекомендация сохранена",
        "skip": "Изменений нет, анализ пропущен",
        "error": "Анализ не сформирован",
    }
    emit_progress(
        "deal",
        str(deal_id),
        "error" if compact == "error" else "done",
        status="error" if compact == "error" else "done",
        detail=details.get(compact, details["error"]),
        error=error,
        publish_ready=True,
        analysis_run_id=analysis_run_id,
        decision_status=compact,
    )


def persist_skip(
    *,
    db_path: Path,
    args: argparse.Namespace,
    status: str,
    fingerprint: str,
    snapshot: dict[str, Any],
    previous_state: dict[str, Any] | None,
    decision_reason: dict[str, Any],
    mini_path: Path | None = None,
) -> int:
    run_id = save_analysis_run(
        db_path,
        entity_type="deal",
        entity_id=str(args.deal_id),
        status=status,
        fingerprint=fingerprint,
        mini_recommendation_path=str(mini_path) if mini_path else None,
        decision_reason=decision_reason,
        **analysis_run_provenance(
            {},
            fingerprint=fingerprint,
            decision_reason=decision_reason,
            prompt_version="neuro-rop:full-deal:v2",
            model_override=args.model,
        ),
    )
    upsert_entity_state(
        db_path,
        entity_type="deal",
        entity_id=str(args.deal_id),
        fingerprint=fingerprint,
        snapshot=snapshot,
        last_analysis_status=status,
        last_analysis_path=(previous_state or {}).get("last_analysis_path"),
        last_report_path=(previous_state or {}).get("last_report_path"),
        last_risk_level=(previous_state or {}).get("last_risk_level"),
        last_analysis=(previous_state or {}).get("last_analysis"),
        last_recommendation=(previous_state or {}).get("last_recommendation"),
        last_analysis_at=(previous_state or {}).get("last_analysis_at"),
    )
    return run_id


def filter_today_mini_triggers(db_path: Path, deal_id: str, triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = get_today_mini_trigger_types(db_path, entity_type="deal", entity_id=deal_id)
    filtered = []
    seen = set()
    for trigger in triggers:
        trigger_type = str(trigger.get("trigger_type") or "")
        if not trigger_type or trigger_type in existing or trigger_type in seen:
            continue
        seen.add(trigger_type)
        filtered.append(trigger)
    return filtered


def duplicate_evidence_legacy_decision(
    *, diff: dict[str, Any], snapshot: dict[str, Any], previous_state: dict[str, Any] | None,
    last_memory: dict[str, Any] | None,
) -> ProcessingDecision:
    remaining_triggers = (
        soft_diff_triggers(diff)
        + mini_triggers(
            current_snapshot=snapshot,
            previous_state=previous_state,
            last_memory=last_memory,
            last_analysis=(previous_state or {}).get("last_analysis"),
        )
    )
    if remaining_triggers:
        return ProcessingDecision(
            status=MINI_RECOMMENDATION_NO_LLM,
            reasons=[
                "V2 evidence identity подтвердил, что выбранное представление транскрипта уже покрыто предыдущим анализом.",
                "После отсечения ложного transcript change остались soft-изменения или контрольные триггеры: MINI-рекомендация без LLM.",
            ],
            triggers=remaining_triggers,
            diff=diff,
        )
    return ProcessingDecision(
        status=SKIPPED_NO_CHANGES,
        reasons=[
            "V2 evidence identity подтвердил, что выбранное представление транскрипта уже покрыто предыдущим анализом."
        ],
        triggers=[],
        diff=diff,
    )


def main() -> None:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    db_path = db_path_from_args(args.db_path)
    current_deal_dir = deal_dir(args)
    paths = analysis_paths(current_deal_dir, str(args.deal_id))

    try:
        init_db(db_path)
        raw_path = raw_bundle_path(args)
        transcript_path, analyzer_transcript_arg = resolve_transcript_for_snapshot(args.transcript, current_deal_dir)
        raw_bundle = load_json(raw_path)
        snapshot = build_deal_snapshot(raw_bundle, transcript_path)
        fingerprint = fingerprint_snapshot(snapshot)
        previous_state = get_entity_state(db_path, "deal", str(args.deal_id))
        previous_snapshot = (previous_state or {}).get("snapshot")
        diff = compare_snapshots(previous_snapshot, snapshot)
        last_memory = get_entity_memory(db_path, "deal", str(args.deal_id))
        decision = decide_deal_processing(
            previous_state=previous_state,
            current_snapshot=snapshot,
            fingerprint=fingerprint,
            diff=diff,
            last_memory=last_memory,
        )
        if args.force_llm:
            decision = ProcessingDecision(
                status=FULL_LLM_ANALYSIS,
                reasons=["Ручной принудительный запуск: --force-llm."],
                triggers=[],
                diff=diff,
            )

        if args.dry_run_decision:
            print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
            print(f"Fingerprint: {fingerprint}")
            print("Dry run: snapshot and SQLite state were not changed.")
            return

        save_json(paths["snapshot"], {"fingerprint": fingerprint, "snapshot": snapshot, "diff": diff})

        if decision.status == INCREMENTAL_LLM_ANALYSIS and DEAL_INCREMENTAL_V2_MODE in {"shadow", "on"}:
            checkpoint = _trusted_v2_checkpoint(db_path, str(args.deal_id), previous_state)
            source_run_id = _int_or_none((checkpoint or {}).get("source_analysis_run_id"))
            evidence_ids: list[str] = []
            try:
                if checkpoint is None:
                    raise ValueError("trusted_semantic_checkpoint_missing")
                current_evidence = collect_deal_evidence(raw_bundle, current_deal_dir / "transcripts")
                delta, next_coverage = evidence_delta(
                    current_evidence,
                    (checkpoint.get("semantic_state") or {}).get("evidence_coverage"),
                )
                evidence_ids = [str(item.get("evidence_id") or "") for item in delta]
                if not delta:
                    raise ValueError("no_genuinely_new_or_revised_evidence")
                previous_analysis = previous_business_analysis((previous_state or {}).get("last_analysis"))
                stage_policy = build_deal_stage_policy(current_deal_dir, str(args.deal_id))
                prior_recommendation = get_latest_neuro_rop_recommendation_projection(
                    db_path, str(args.deal_id)
                )
                context_diagnostics = load_context_diagnostics_for_analysis(
                    entity_type="deal",
                    entity_id=str(args.deal_id),
                    workspace_root=Path(args.deal_root),
                )[1]
                compact_policy = build_v2_compact_policy(
                    deal_dir=current_deal_dir,
                    deal_id=str(args.deal_id),
                )
                compact_diagnostics_text = render_v2_compact_diagnostics(
                    build_v2_compact_diagnostics(context_diagnostics)
                )
                cumulative_diff = compare_snapshots(checkpoint["baseline_snapshot"], snapshot)
                v2_result = run_incremental_v2(
                    deal_id=str(args.deal_id),
                    previous_analysis=previous_analysis,
                    previous_semantic_state=checkpoint["semantic_state"],
                    evidence_delta=delta,
                    next_evidence_coverage=next_coverage,
                    crm_delta=_v2_crm_delta(snapshot, cumulative_diff),
                    stage_policy=stage_policy,
                    prior_recommendation=prior_recommendation,
                    source_fingerprint=fingerprint,
                    model=str(args.model or ANALYSIS_MODEL),
                    compact_policy=compact_policy,
                    compact_diagnostics_text=compact_diagnostics_text,
                )
                payload = _write_v2_candidate(
                    paths=paths,
                    args=args,
                    result=v2_result,
                    stage_policy=stage_policy,
                    prior_recommendation=prior_recommendation,
                    production=DEAL_INCREMENTAL_V2_MODE == "on",
                )
                save_deal_incremental_v2_run(
                    db_path,
                    entity_id=str(args.deal_id),
                    mode=DEAL_INCREMENTAL_V2_MODE,
                    source_analysis_run_id=source_run_id,
                    source_fingerprint=fingerprint,
                    semantic_schema_version=SCHEMA_VERSION,
                    evidence_ids=evidence_ids,
                    changed_domains=v2_result.changed_domains,
                    affected_sections=v2_result.affected_sections,
                    telemetry={
                        "usage": v2_result.metadata.get("usage") or {},
                        "prompt_budget": v2_result.metadata.get("prompt_budget") or {},
                    },
                    semantic_validation="passed",
                    final_validation="passed",
                    artifact_path=str(paths["analysis"] if DEAL_INCREMENTAL_V2_MODE == "on" else paths["v2_shadow"]),
                )
                if DEAL_INCREMENTAL_V2_MODE == "on":
                    run_id = persist_successful_llm_run(
                        db_path=db_path,
                        args=args,
                        fingerprint=fingerprint,
                        snapshot=snapshot,
                        decision_status=decision.status,
                        paths=paths,
                        decision_reason={**decision.as_dict(), "incremental_version": "v2"},
                        prompt_version="neuro-rop:incremental-deal:v2",
                        evidence_ids_included=None,
                    )
                    payload["analysis_run_id"] = run_id
                    save_json(paths["analysis"], payload)
                    semantic_state = dict(v2_result.semantic_state)
                    semantic_state["source_analysis_run_id"] = run_id
                    save_deal_semantic_checkpoint(
                        db_path,
                        entity_id=str(args.deal_id),
                        schema_version=SCHEMA_VERSION,
                        source_analysis_run_id=run_id,
                        source_fingerprint=fingerprint,
                        semantic_state=semantic_state,
                        mode="on",
                        baseline_snapshot=snapshot,
                    )
                    emit_deal_publish_ready(
                        str(args.deal_id), analysis_run_id=run_id, engine_status=decision.status
                    )
                    print(f"{decision.status}: V2 analysis completed for deal {args.deal_id}")
                    return
            except Exception as error:
                safe_reason = type(error).__name__ + ":" + str(error)
                logger.warning("Incremental V2 %s failed safely: %s", DEAL_INCREMENTAL_V2_MODE, safe_reason)
                save_deal_incremental_v2_run(
                    db_path,
                    entity_id=str(args.deal_id),
                    mode=DEAL_INCREMENTAL_V2_MODE,
                    source_analysis_run_id=source_run_id,
                    source_fingerprint=fingerprint,
                    semantic_schema_version=SCHEMA_VERSION,
                    evidence_ids=evidence_ids,
                    semantic_validation="failed",
                    final_validation="not_run",
                    fallback_reason=safe_reason,
                )
                if DEAL_INCREMENTAL_V2_MODE == "on" and str(error) == "no_genuinely_new_or_revised_evidence":
                    # The transcript change was only a representation change, but the
                    # snapshot may still hold real changes or deterministic risks:
                    # re-evaluate them deterministically instead of a blind SKIP or
                    # a paid FULL.
                    decision = duplicate_evidence_legacy_decision(
                        diff=diff,
                        snapshot=snapshot,
                        previous_state=previous_state,
                        last_memory=last_memory,
                    )
                elif DEAL_INCREMENTAL_V2_MODE == "on":
                    decision = full_fallback_decision(decision, f"v2:{safe_reason}")

        if decision.status == INCREMENTAL_LLM_ANALYSIS:
            incremental_context_path: Path | None = None
            if CONTEXT_MEMORY_OPTIMIZATION_ENABLED or CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE:
                try:
                    incremental_context = build_incremental_context(
                        previous_state=previous_state,
                        previous_snapshot=previous_snapshot,
                        current_snapshot=snapshot,
                        diff=diff,
                        raw_bundle=raw_bundle,
                        transcript_path=transcript_path,
                    )
                    save_json(paths["incremental_context"], incremental_context)
                    incremental_context_path = paths["incremental_context"]
                except IncrementalContextError as error:
                    decision = full_fallback_decision(decision, str(error))
            else:
                decision = full_fallback_decision(decision, "optimization_disabled")

            if decision.status == INCREMENTAL_LLM_ANALYSIS and (
                CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE
                or CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK
            ):
                mode_reason = (
                    "shadow_mode"
                    if CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE
                    else "force_full_fallback"
                )
                decision = full_fallback_decision(decision, mode_reason)

            if decision.status == INCREMENTAL_LLM_ANALYSIS:
                try:
                    run_existing_analyzer(
                        args,
                        "none",
                        incremental_context_path=incremental_context_path,
                    )
                except subprocess.CalledProcessError:
                    decision = full_fallback_decision(decision, "incremental_analyzer_failed")
                else:
                    run_id = persist_successful_llm_run(
                        db_path=db_path,
                        args=args,
                        fingerprint=fingerprint,
                        snapshot=snapshot,
                        decision_status=decision.status,
                        paths=paths,
                        decision_reason=decision.as_dict(),
                        evidence_ids_included=None,
                    )
                    if DEAL_INCREMENTAL_V2_MODE == "shadow":
                        _save_v2_checkpoint_from_analysis(
                            db_path=db_path,
                            deal_id=str(args.deal_id),
                            payload=load_analysis_payload(paths["analysis"]),
                            fingerprint=fingerprint,
                            raw_bundle=raw_bundle,
                            transcripts_dir=current_deal_dir / "transcripts",
                            mode="shadow",
                            snapshot=snapshot,
                        )
                    emit_deal_publish_ready(
                        str(args.deal_id),
                        analysis_run_id=run_id,
                        engine_status=decision.status,
                    )
                    print(f"{decision.status}: LLM analysis completed for deal {args.deal_id}")
                    print(f"Analysis saved: {paths['analysis']}")
                    print(f"ROP report saved: {paths['report']}")
                    return

        if decision.status in {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS}:
            run_existing_analyzer(args, analyzer_transcript_arg)
            run_id = persist_successful_llm_run(
                db_path=db_path,
                args=args,
                fingerprint=fingerprint,
                snapshot=snapshot,
                decision_status=decision.status,
                paths=paths,
                decision_reason=decision.as_dict(),
                evidence_ids_included=None,
            )
            if DEAL_INCREMENTAL_V2_MODE in {"shadow", "on"}:
                _save_v2_checkpoint_from_analysis(
                    db_path=db_path,
                    deal_id=str(args.deal_id),
                    payload=load_analysis_payload(paths["analysis"]),
                    fingerprint=fingerprint,
                    raw_bundle=raw_bundle,
                    transcripts_dir=current_deal_dir / "transcripts",
                    mode=DEAL_INCREMENTAL_V2_MODE,
                    snapshot=snapshot,
                )
            emit_deal_publish_ready(
                str(args.deal_id),
                analysis_run_id=run_id,
                engine_status=decision.status,
            )
            print(f"{decision.status}: LLM analysis completed for deal {args.deal_id}")
            print(f"Analysis saved: {paths['analysis']}")
            print(f"ROP report saved: {paths['report']}")
            return

        if decision.status == MINI_RECOMMENDATION_NO_LLM:
            filtered_triggers = filter_today_mini_triggers(db_path, str(args.deal_id), decision.triggers)
            if not filtered_triggers:
                suppressed_decision = ProcessingDecision(
                    status=SKIPPED_NO_CHANGES,
                    reasons=[
                        "Mini recommendation подавлена: такие trigger_type уже создавались сегодня по этой сделке."
                    ],
                    triggers=decision.triggers,
                    diff=decision.diff,
                )
                run_id = persist_skip(
                    db_path=db_path,
                    args=args,
                    status=suppressed_decision.status,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    previous_state=previous_state,
                    decision_reason=suppressed_decision.as_dict(),
                )
                emit_deal_publish_ready(
                    str(args.deal_id),
                    analysis_run_id=run_id,
                    engine_status=suppressed_decision.status,
                )
                print(f"{SKIPPED_NO_CHANGES}: mini triggers suppressed by daily anti-spam for deal {args.deal_id}")
                return

            decision = ProcessingDecision(
                status=decision.status,
                reasons=decision.reasons,
                triggers=filtered_triggers,
                diff=decision.diff,
            )
            content = render_mini_recommendation(
                deal_id=str(args.deal_id),
                decision=decision,
                previous_state=previous_state,
                current_snapshot=snapshot,
            )
            save_mini_recommendation_markdown(paths["mini"], content)
            for trigger in decision.triggers:
                save_mini_recommendation(
                    db_path,
                    entity_type="deal",
                    entity_id=str(args.deal_id),
                    trigger_type=str(trigger.get("trigger_type") or "unknown"),
                    recommendation_md_path=str(paths["mini"]),
                    fingerprint=fingerprint,
                )
            run_id = persist_skip(
                db_path=db_path,
                args=args,
                status=decision.status,
                fingerprint=fingerprint,
                snapshot=snapshot,
                previous_state=previous_state,
                decision_reason=decision.as_dict(),
                mini_path=paths["mini"],
            )
            emit_deal_publish_ready(
                str(args.deal_id),
                analysis_run_id=run_id,
                engine_status=decision.status,
            )
            print(f"{decision.status}: mini recommendation saved: {paths['mini']}")
            return

        if decision.status == SKIPPED_NO_CHANGES:
            run_id = persist_skip(
                db_path=db_path,
                args=args,
                status=decision.status,
                fingerprint=fingerprint,
                snapshot=snapshot,
                previous_state=previous_state,
                decision_reason=decision.as_dict(),
            )
            emit_deal_publish_ready(
                str(args.deal_id),
                analysis_run_id=run_id,
                engine_status=decision.status,
            )
            print(f"{decision.status}: deal {args.deal_id} skipped")
            return

        raise RuntimeError(f"Unsupported decision status: {decision.status}")

    except Exception as error:
        logger.exception("Deal change-detection analysis failed")
        error_run_id = None
        try:
            error_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id=str(args.deal_id),
                status=ERROR,
                model=args.model,
                prompt_version="neuro-rop:full-deal:v2",
                logic_version="change-aware-v1",
                provenance={"trigger": ERROR},
                error=str(error),
            )
        except Exception:
            logger.exception("Could not persist ERROR run")
        emit_deal_publish_ready(
            str(args.deal_id),
            analysis_run_id=error_run_id,
            engine_status=ERROR,
            error=str(error),
        )
        print(f"{ERROR}: {error}")
        raise


if __name__ == "__main__":
    main()
