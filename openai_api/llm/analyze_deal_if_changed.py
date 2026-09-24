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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.workspace import DEFAULT_DEAL_WORKSPACE_ROOT
from bitrix.canonical_state import merge_deal_bundle
from openai_api.llm.deal_daily_quality import load_daily_quality_context
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
    render_mini_recommendation,
    save_mini_recommendation_markdown,
)
from openai_api.llm.analyze_deal import (
    COMPATIBLE_DEAL_PROMPT_VERSIONS,
    DEAL_PROMPT_CACHE_KEY,
    INCREMENTAL_DEAL_PROMPT_VERSION,
    load_context_diagnostics_for_analysis,
    render_report,
)
from openai_api.config import DEAL_INCREMENTAL_ANALYSIS_ENABLED
from openai_api.llm.incremental_analysis_patch import (
    OUTPUT_KIND_FULL,
    OUTPUT_KIND_PATCH,
    OUTPUT_KIND_PATCH_FALLBACK_FULL,
    OUTPUT_KIND_PATCH_NO_CHANGE,
)
from openai_api.llm.deal_evidence import (
    collect_deal_evidence,
    coverage_for_included_evidence,
    evidence_delta,
    workspace_normalized_communications,
)
from openai_api.llm.trusted_baseline import get_trusted_deal_baseline
from openai_api.change_detection.snapshot import (
    build_deal_snapshot,
    compare_snapshots,
    fingerprint_snapshot,
    load_json,
    save_json,
)
from openai_api.change_detection.provenance import analysis_run_provenance
from progress_events import compact_decision_status, emit_progress
from setup import BASE_DIR, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_today_mini_trigger_types,
    get_entity_memory,
    get_entity_state,
    get_latest_analysis_run,
    init_db,
    merge_deal_daily_quality_state,
    publish_analysis_run,
    save_analysis_run,
    save_mini_recommendation,
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
        "incremental": analysis_dir / f"deal_{deal_id}_incremental_context.json",
        "prompt": analysis_dir / f"deal_{deal_id}_request_prompt.txt",
        "error": analysis_dir / f"deal_{deal_id}_analysis_error.json",
    }


PAID_PERSIST_FAILURE_STAGE = "persist_after_paid_llm"
PAID_PERSIST_FAILURE_RETRY_AFTER = timedelta(hours=24)


class PaidRetrySuppressedError(RuntimeError):
    """The same snapshot was already paid for but its result could not be persisted."""


def recent_paid_persist_failure(
    db_path: Path,
    deal_id: str,
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the ERROR run that blocks an automatic paid retry of this fingerprint."""
    run = get_latest_analysis_run(
        db_path,
        entity_type="deal",
        entity_id=str(deal_id),
        statuses=(ERROR,),
        fingerprint=fingerprint,
    )
    reason = (run or {}).get("decision_reason")
    if not isinstance(reason, dict) or reason.get("failure_stage") != PAID_PERSIST_FAILURE_STAGE:
        return None
    try:
        created_at = datetime.fromisoformat(str(run.get("created_at") or ""))
    except ValueError:
        return None
    if created_at.tzinfo is None:
        return None
    current = now or datetime.fromisoformat(utcish_now())
    return run if current - created_at < PAID_PERSIST_FAILURE_RETRY_AFTER else None


def run_existing_analyzer(
    args: argparse.Namespace,
    transcript_arg: str,
    *,
    incremental_context: Path | None = None,
    db_path: Path | None = None,
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
    if incremental_context is not None:
        command.extend(["--incremental-context", str(incremental_context)])
    if db_path is not None:
        command.extend(["--db-path", str(db_path)])

    logger.info("Running existing deal analyzer: %s", " ".join(command))
    subprocess.run(command, cwd=BASE_DIR, check=True)


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


def incremental_patch_error_reason(paths: dict[str, Path]) -> str | None:
    """Read analyzer error file written after a rejected PATCH."""
    error_path = paths.get("error")
    if error_path is None or not error_path.is_file():
        return None
    try:
        payload = json.loads(error_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("error_type") == "incremental_patch_invalid":
        return "incremental_patch_invalid"
    return None


def payload_output_kind(payload: dict[str, Any], default: str) -> str:
    kind = payload.get("analysis_output_kind")
    if kind in {OUTPUT_KIND_PATCH, OUTPUT_KIND_PATCH_NO_CHANGE, OUTPUT_KIND_FULL}:
        return str(kind)
    metadata = payload.get("model_metadata")
    if isinstance(metadata, dict) and metadata.get("analysis_output_kind") in {
        OUTPUT_KIND_PATCH,
        OUTPUT_KIND_PATCH_NO_CHANGE,
        OUTPUT_KIND_FULL,
    }:
        return str(metadata["analysis_output_kind"])
    return default


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
    evidence_coverage: dict[str, Any] | None = None,
    canonical_state: dict[str, Any] | None = None,
    available_evidence: list[dict[str, Any]] | None = None,
) -> int:
    payload = load_analysis_payload(paths["analysis"])
    analysis = extract_analysis(payload)
    output_kind = payload_output_kind(
        payload,
        OUTPUT_KIND_PATCH if decision_status == INCREMENTAL_LLM_ANALYSIS else OUTPUT_KIND_FULL,
    )
    if "analysis_output_kind" not in decision_reason:
        decision_reason = {
            **decision_reason,
            "analysis_output_kind": output_kind,
            "no_change": output_kind == OUTPUT_KIND_PATCH_NO_CHANGE,
        }
    if evidence_ids_included is None and isinstance(payload.get("evidence_ids_included"), list):
        evidence_ids_included = [str(item) for item in payload["evidence_ids_included"]]
    if evidence_coverage is None and available_evidence is not None and evidence_ids_included is not None:
        evidence_coverage = coverage_for_included_evidence(available_evidence, evidence_ids_included)
    if canonical_state is None or evidence_ids_included is None or evidence_coverage is None:
        raise ValueError("Trusted analysis persistence requires canonical state and evidence coverage")
    audit = analysis.get("communication_quality_audit") if isinstance(analysis, dict) else None
    if isinstance(audit, dict):
        details = ((decision_reason.get("diff") or {}).get("details") or {})
        invalidated_event_ids = [
            *list(details.get("revised_daily_quality_event_ids") or []),
            *list(details.get("daily_quality_removed_event_ids") or []),
        ]
        quality_state = merge_deal_daily_quality_state(
            db_path,
            deal_id=str(args.deal_id),
            audit=audit,
            invalidated_event_ids=invalidated_event_ids,
        )
        if quality_state is not None:
            analysis["communication_quality_audit"] = quality_state["audit"]
            save_json(paths["analysis"], payload)
            _, diagnostics, _ = load_context_diagnostics_for_analysis(
                entity_type="deal",
                entity_id=str(args.deal_id),
                workspace_root=Path(args.deal_root),
            )
            paths["report"].write_text(
                render_report(
                    analysis,
                    payload.get("model_metadata"),
                    diagnostics,
                ),
                encoding="utf-8",
            )
    run_id = save_analysis_run(
        db_path,
        entity_type="deal",
        entity_id=str(args.deal_id),
        status="PUBLISHING",
        fingerprint=fingerprint,
        analysis_path=str(paths["analysis"]),
        report_path=str(paths["report"]),
        raw_path=str(paths["raw"]),
        decision_reason=decision_reason,
        evidence_ids_included=evidence_ids_included,
        evidence_coverage=evidence_coverage,
        canonical_state=canonical_state,
        **analysis_run_provenance(
            payload,
            fingerprint=fingerprint,
            decision_reason=decision_reason,
            prompt_version=(
                prompt_version
                or DEAL_PROMPT_CACHE_KEY
            ),
            model_override=args.model,
        ),
    )
    payload["analysis_run_id"] = run_id
    save_json(paths["analysis"], payload)
    memory_update = analysis.get("memory_update") if isinstance(analysis, dict) else None
    publish_analysis_run(
        db_path,
        run_id,
        decision_status,
        state={
            "entity_type": "deal",
            "entity_id": str(args.deal_id),
            "fingerprint": fingerprint,
            "snapshot": snapshot,
            "last_analysis_status": decision_status,
            "last_analysis_path": str(paths["analysis"]),
            "last_report_path": str(paths["report"]),
            "last_risk_level": extract_risk_level(payload),
            "last_analysis": payload,
            "last_recommendation": extract_last_recommendation(payload),
            "last_analysis_at": utcish_now(),
        },
        memory_update=memory_update if isinstance(memory_update, dict) else None,
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
            prompt_version=DEAL_PROMPT_CACHE_KEY,
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


def stage5_inputs(
    db_path: Path,
    *,
    deal_id: str,
    raw_bundle: dict[str, Any],
    current_deal_dir: Path,
    baseline: dict[str, Any] | None,
    normalized_communications: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    canonical_state, canonical_delta = merge_deal_bundle(
        (baseline or {}).get("canonical_state"),
        raw_bundle,
        observed_at=str(raw_bundle.get("generated_at") or utcish_now()),
    )
    evidence_bundle = (
        {**raw_bundle, "normalized_communications": normalized_communications}
        if normalized_communications is not None
        else raw_bundle
    )
    available_evidence = collect_deal_evidence(evidence_bundle, current_deal_dir / "transcripts")
    return baseline, canonical_state, canonical_delta, available_evidence


# Только клиентские тексты, которые evidence_delta умеет классифицировать.
# Раньше фильтр требовал уже покрытый ID или call_transcript — новое письмо/сообщение
# не доходило до NEW_OR_REVISED_CLIENT_EVIDENCE.
INCREMENTAL_CLIENT_EVIDENCE_KINDS = frozenset({
    "call_transcript",
    "inbound_message",
    "inbound_email",
})


def incremental_context(
    baseline: dict[str, Any],
    canonical_state: dict[str, Any],
    canonical_delta: dict[str, Any],
    available_evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    delta_evidence = [
        item for item in available_evidence
        if item.get("kind") in INCREMENTAL_CLIENT_EVIDENCE_KINDS
    ]
    revised_evidence, next_coverage = evidence_delta(
        delta_evidence,
        baseline["evidence_coverage"],
    )
    crm_delta = [
        entry for entry in canonical_delta.get("entries", [])
        if entry.get("change_type") in {"NEW", "UPDATED_MEANINGFUL"}
    ]
    deal = canonical_state.get("entities", {}).get(
        f"deal:{canonical_state['owner']['entity_id']}",
        {},
    )
    return {
        "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": baseline["analysis"],
        "CRM_SEMANTIC_DELTA": crm_delta,
        "NEW_OR_REVISED_CLIENT_EVIDENCE": revised_evidence,
        "AVAILABLE_CLIENT_EVIDENCE_IDS": [
            str(item["evidence_id"])
            for item in available_evidence
            if item.get("evidence_id") is not None
        ],
        "CURRENT_REQUIRED_CRM_FACTS": {
            "deal": deal.get("semantic") or {},
            "source_status": canonical_state.get("source_status") or {},
        },
    }, next_coverage


def main() -> None:
    args = parse_args()
    load_dotenv(BASE_DIR / ".env")
    db_path = db_path_from_args(args.db_path)
    current_deal_dir = deal_dir(args)
    paths = analysis_paths(current_deal_dir, str(args.deal_id))
    fingerprint: str | None = None
    paid_llm_status: str | None = None

    try:
        init_db(db_path)
        raw_path = raw_bundle_path(args)
        transcript_path, analyzer_transcript_arg = resolve_transcript_for_snapshot(args.transcript, current_deal_dir)
        raw_bundle = load_json(raw_path)
        normalized_communications = workspace_normalized_communications(
            current_deal_dir,
            str(args.deal_id),
            raw_bundle,
        )
        daily_quality_context = load_daily_quality_context(current_deal_dir, str(args.deal_id))
        snapshot = build_deal_snapshot(
            raw_bundle,
            transcript_path,
            daily_quality_context=daily_quality_context,
            normalized_communications=normalized_communications,
        )
        fingerprint = fingerprint_snapshot(snapshot)
        previous_state = get_entity_state(db_path, "deal", str(args.deal_id))
        baseline = get_trusted_deal_baseline(
            db_path,
            str(args.deal_id),
            compatible_prompt_versions=COMPATIBLE_DEAL_PROMPT_VERSIONS,
            expected_logic_version="change-aware-v1",
        )
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

        if (
            decision.status in {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS}
            and not args.force_llm
        ):
            blocking_run = recent_paid_persist_failure(db_path, str(args.deal_id), fingerprint)
            if blocking_run is not None:
                raise PaidRetrySuppressedError(
                    "paid_llm_retry_suppressed: analysis_run "
                    f"{blocking_run['id']} already paid for this fingerprint but was not persisted"
                )

        save_json(paths["snapshot"], {"fingerprint": fingerprint, "snapshot": snapshot, "diff": diff})
        canonical_state = canonical_delta = None
        available_evidence: list[dict[str, Any]] = []
        if decision.status in {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS}:
            baseline, canonical_state, canonical_delta, available_evidence = stage5_inputs(
                db_path,
                deal_id=str(args.deal_id),
                raw_bundle=raw_bundle,
                current_deal_dir=current_deal_dir,
                baseline=baseline,
                normalized_communications=normalized_communications,
            )
        full_decision_reason: dict[str, Any] | None = None
        incremental_blocker = None
        if baseline is None:
            incremental_blocker = "unsafe_trusted_baseline"
        elif args.force_llm:
            incremental_blocker = "forced_full"

        if decision.status == FULL_LLM_ANALYSIS and DEAL_INCREMENTAL_ANALYSIS_ENABLED:
            if incremental_blocker is None:
                decision = ProcessingDecision(
                    status=INCREMENTAL_LLM_ANALYSIS,
                    reasons=decision.reasons,
                    triggers=decision.triggers,
                    diff=decision.diff,
                )
            elif incremental_blocker != "forced_full":
                full_decision_reason = {
                    **decision.as_dict(),
                    "fallback": True,
                    "fallback_reason": incremental_blocker,
                }

        if decision.status == INCREMENTAL_LLM_ANALYSIS:
            if DEAL_INCREMENTAL_ANALYSIS_ENABLED and incremental_blocker is None:
                use_incremental_llm = False
                try:
                    context, next_coverage = incremental_context(
                        baseline,
                        canonical_state,
                        canonical_delta,
                        available_evidence,
                    )
                    use_incremental_llm = True
                    save_json(paths["incremental"], context)
                    run_existing_analyzer(
                        args,
                        analyzer_transcript_arg,
                        incremental_context=paths["incremental"],
                        db_path=db_path,
                    )
                    paid_llm_status = INCREMENTAL_LLM_ANALYSIS
                except Exception as incremental_error:
                    logger.warning(
                        "Incremental deal analysis failed; running one FULL_REBUILD: %s",
                        type(incremental_error).__name__,
                    )
                    patch_reason = incremental_patch_error_reason(paths)
                    full_decision_reason = {
                        **decision.as_dict(),
                        "fallback": True,
                        "fallback_reason": patch_reason or "incremental_execution_failed",
                        "analysis_output_kind": OUTPUT_KIND_PATCH_FALLBACK_FULL,
                        "baseline_run_id": baseline["analysis_run_id"],
                    }
                else:
                    if use_incremental_llm:
                        incremental_reason = {
                            **decision.as_dict(),
                            "baseline_run_id": baseline["analysis_run_id"],
                            "baseline_fingerprint": baseline["canonical_fingerprint"],
                            "canonical_from_fingerprint": canonical_delta.get("from_semantic_fingerprint"),
                            "canonical_to_fingerprint": canonical_delta.get("to_semantic_fingerprint"),
                            "changed_entity_count": len(context["CRM_SEMANTIC_DELTA"]),
                            "evidence_delta_count": len(context["NEW_OR_REVISED_CLIENT_EVIDENCE"]),
                        }
                        run_id = persist_successful_llm_run(
                            db_path=db_path,
                            args=args,
                            fingerprint=fingerprint,
                            snapshot=snapshot,
                            decision_status=INCREMENTAL_LLM_ANALYSIS,
                            paths=paths,
                            decision_reason=incremental_reason,
                            prompt_version=INCREMENTAL_DEAL_PROMPT_VERSION,
                            evidence_coverage=next_coverage,
                            canonical_state=canonical_state,
                            available_evidence=available_evidence,
                        )
                        emit_deal_publish_ready(
                            str(args.deal_id),
                            analysis_run_id=run_id,
                            engine_status=INCREMENTAL_LLM_ANALYSIS,
                        )
                        print(f"{INCREMENTAL_LLM_ANALYSIS}: LLM analysis completed for deal {args.deal_id}")
                        return
            else:
                full_decision_reason = {
                    **decision.as_dict(),
                    "fallback": DEAL_INCREMENTAL_ANALYSIS_ENABLED,
                    "fallback_reason": (
                        incremental_blocker or "unsafe_trusted_baseline"
                        if DEAL_INCREMENTAL_ANALYSIS_ENABLED else "incremental_feature_disabled"
                    ),
                }
            decision = ProcessingDecision(
                status=FULL_LLM_ANALYSIS,
                reasons=decision.reasons,
                triggers=decision.triggers,
                diff=decision.diff,
            )

        if decision.status in {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS}:
            run_existing_analyzer(
                args,
                analyzer_transcript_arg,
                db_path=db_path,
            )
            paid_llm_status = decision.status
            persist_kwargs = {
                "db_path": db_path,
                "args": args,
                "fingerprint": fingerprint,
                "snapshot": snapshot,
                "decision_status": decision.status,
                "paths": paths,
                "decision_reason": full_decision_reason or decision.as_dict(),
                "evidence_ids_included": None,
                "canonical_state": canonical_state,
                "available_evidence": available_evidence,
            }
            run_id = persist_successful_llm_run(**persist_kwargs)
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
                fingerprint=fingerprint if paid_llm_status else None,
                decision_reason=(
                    {"failure_stage": PAID_PERSIST_FAILURE_STAGE, "decision_status": paid_llm_status}
                    if paid_llm_status else None
                ),
                model=args.model,
                prompt_version=DEAL_PROMPT_CACHE_KEY,
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
