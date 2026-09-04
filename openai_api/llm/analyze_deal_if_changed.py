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
from bitrix.customer_history import build_deal_normalized_communications
from bitrix.deals.communication_history import include_source_lead_communications
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
    DEAL_PROMPT_CACHE_KEY,
    load_context_diagnostics_for_analysis,
    render_report,
)
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
    init_db,
    merge_deal_daily_quality_state,
    save_analysis_run,
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


def normalized_communications_for_snapshot(
    current_deal_dir: Path,
    deal_id: str,
    raw_bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    """Read the already saved canonical ledger; never refresh Bitrix from change detection."""
    history_path = (
        current_deal_dir / "raw" / f"deal_{deal_id}_customer_history_bundle.json"
    )
    history: dict[str, Any] | None = None
    if history_path.exists():
        try:
            value = load_json(history_path)
        except (OSError, ValueError):
            value = None
        if isinstance(value, dict):
            history = include_source_lead_communications(
                value,
                raw_bundle,
                deal_id=str(deal_id),
            )
    return build_deal_normalized_communications(raw_bundle, history)


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
    }


def run_existing_analyzer(args: argparse.Namespace, transcript_arg: str) -> None:
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
                or DEAL_PROMPT_CACHE_KEY
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
        normalized_communications = normalized_communications_for_snapshot(
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

        if decision.status == INCREMENTAL_LLM_ANALYSIS:
            decision = ProcessingDecision(
                status=FULL_LLM_ANALYSIS,
                reasons=decision.reasons,
                triggers=decision.triggers,
                diff=decision.diff,
            )

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
