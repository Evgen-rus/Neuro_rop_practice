"""
Run lead LLM analysis only when the normalized lead snapshot changed.
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

from bitrix.workspace import DEFAULT_LEAD_WORKSPACE_ROOT
from openai_api.change_detection.decision_engine import (
    ERROR,
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    SKIPPED_NO_CHANGES,
    ProcessingDecision,
    decide_lead_processing,
    render_lead_mini_recommendation,
    save_mini_recommendation_markdown,
)
from openai_api.change_detection.snapshot import (
    build_lead_snapshot,
    compare_lead_snapshots,
    fingerprint_snapshot,
    load_json,
    save_json,
)
from setup import BASE_DIR, get_logger
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_entity_memory,
    get_entity_state,
    get_today_mini_trigger_types,
    init_db,
    save_analysis_run,
    save_mini_recommendation,
    update_entity_memory,
    upsert_entity_state,
    utcish_now,
)


logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze lead only if meaningful changes are detected")
    parser.add_argument("--lead-id", required=True, help="Lead ID to check")
    parser.add_argument("--lead-root", default=str(DEFAULT_LEAD_WORKSPACE_ROOT), help="Root folder with lead workspaces")
    parser.add_argument("--db-path", default=None, help="SQLite path. Default: ROP_DB_PATH or reports/rop_assistant/rop_assistant.sqlite")
    parser.add_argument("--transcript", default="latest", help="Transcript path, 'latest', or 'none'. Default: latest if exists, else none.")
    parser.add_argument("--model", default=None, help="Optional OpenAI analysis model passed to analyze_lead.py")
    parser.add_argument("--force-llm", action="store_true", help="Force a full LLM analysis regardless of change detection.")
    parser.add_argument(
        "--dry-run-decision",
        action="store_true",
        help="Build snapshot and print decision without calling analyze_lead.py or writing state.",
    )
    return parser.parse_args()


def db_path_from_args(value: str | None) -> Path:
    if value:
        return Path(value)
    env_value = os.getenv("ROP_DB_PATH", "").strip()
    return Path(env_value) if env_value else DEFAULT_DB_PATH


def lead_dir(args: argparse.Namespace) -> Path:
    return Path(args.lead_root) / f"lead_{args.lead_id}"


def raw_bundle_path(args: argparse.Namespace) -> Path:
    workspace_path = lead_dir(args) / "raw" / f"lead_{args.lead_id}_context.json"
    if workspace_path.exists():
        return workspace_path
    fallback = BASE_DIR / "reports" / "bitrix_lead_path" / "raw" / f"lead_{args.lead_id}_context.json"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(f"Lead raw context not found: {workspace_path} or {fallback}")


def latest_transcript_or_none(transcripts_dir: Path) -> Path | None:
    candidates = sorted(
        [path for path in transcripts_dir.glob("*.md") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_transcript_for_snapshot(value: str, current_lead_dir: Path) -> tuple[Path | None, str]:
    lowered = value.lower()
    if lowered == "none":
        return None, "none"
    if lowered == "latest":
        latest = latest_transcript_or_none(current_lead_dir / "transcripts")
        return latest, str(latest) if latest else "none"

    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Transcript not found: {path}")
    return path, str(path)


def analysis_paths(current_lead_dir: Path, lead_id: str) -> dict[str, Path]:
    analysis_dir = current_lead_dir / "analysis"
    return {
        "analysis": analysis_dir / f"lead_{lead_id}_analysis.json",
        "report": analysis_dir / f"lead_{lead_id}_rop_report.md",
        "raw": analysis_dir / f"lead_{lead_id}_raw_model_output.txt",
        "snapshot": analysis_dir / f"lead_{lead_id}_snapshot.json",
        "mini": analysis_dir / f"lead_{lead_id}_mini_recommendation.md",
    }


def run_existing_analyzer(args: argparse.Namespace, transcript_arg: str) -> None:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "openai_api" / "llm" / "analyze_lead.py"),
        "--lead-id",
        str(args.lead_id),
        "--lead-root",
        str(args.lead_root),
        "--transcript",
        transcript_arg,
        "--allow-direct-llm",
    ]
    if args.model:
        command.extend(["--model", str(args.model)])

    logger.info("Running existing lead analyzer: %s", " ".join(command))
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
    return {
        "manager_action_block": analysis.get("manager_action_block"),
        "rop_action": analysis.get("rop_action"),
        "call_attempt_recommendation": analysis.get("call_attempt_recommendation"),
    }


def persist_successful_llm_run(
    *,
    db_path: Path,
    args: argparse.Namespace,
    fingerprint: str,
    snapshot: dict[str, Any],
    decision_status: str,
    paths: dict[str, Path],
    decision_reason: dict[str, Any],
) -> None:
    payload = load_analysis_payload(paths["analysis"])
    analysis = extract_analysis(payload)
    memory_update = analysis.get("memory_update") if isinstance(analysis, dict) else None

    if isinstance(memory_update, dict):
        update_entity_memory(
            db_path,
            entity_type="lead",
            entity_id=str(args.lead_id),
            memory_update=memory_update,
        )

    save_analysis_run(
        db_path,
        entity_type="lead",
        entity_id=str(args.lead_id),
        status=decision_status,
        fingerprint=fingerprint,
        analysis_path=str(paths["analysis"]),
        report_path=str(paths["report"]),
        raw_path=str(paths["raw"]),
        decision_reason=decision_reason,
    )
    upsert_entity_state(
        db_path,
        entity_type="lead",
        entity_id=str(args.lead_id),
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
) -> None:
    save_analysis_run(
        db_path,
        entity_type="lead",
        entity_id=str(args.lead_id),
        status=status,
        fingerprint=fingerprint,
        mini_recommendation_path=str(mini_path) if mini_path else None,
        decision_reason=decision_reason,
    )
    upsert_entity_state(
        db_path,
        entity_type="lead",
        entity_id=str(args.lead_id),
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


def filter_today_mini_triggers(db_path: Path, lead_id: str, triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = get_today_mini_trigger_types(db_path, entity_type="lead", entity_id=lead_id)
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
    current_lead_dir = lead_dir(args)
    paths = analysis_paths(current_lead_dir, str(args.lead_id))

    try:
        init_db(db_path)
        raw_path = raw_bundle_path(args)
        transcript_path, analyzer_transcript_arg = resolve_transcript_for_snapshot(args.transcript, current_lead_dir)
        raw_bundle = load_json(raw_path)
        snapshot = build_lead_snapshot(raw_bundle, transcript_path)
        fingerprint = fingerprint_snapshot(snapshot)
        previous_state = get_entity_state(db_path, "lead", str(args.lead_id))
        previous_snapshot = (previous_state or {}).get("snapshot")
        diff = compare_lead_snapshots(previous_snapshot, snapshot)
        last_memory = get_entity_memory(db_path, "lead", str(args.lead_id))
        decision = decide_lead_processing(
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

        save_json(paths["snapshot"], {"fingerprint": fingerprint, "snapshot": snapshot, "diff": diff})

        if args.dry_run_decision:
            print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
            print(f"Fingerprint: {fingerprint}")
            print(f"Snapshot saved: {paths['snapshot']}")
            return

        if decision.status in {FIRST_FULL_ANALYSIS, FULL_LLM_ANALYSIS}:
            run_existing_analyzer(args, analyzer_transcript_arg)
            persist_successful_llm_run(
                db_path=db_path,
                args=args,
                fingerprint=fingerprint,
                snapshot=snapshot,
                decision_status=decision.status,
                paths=paths,
                decision_reason=decision.as_dict(),
            )
            print(f"{decision.status}: LLM analysis completed for lead {args.lead_id}")
            print(f"Analysis saved: {paths['analysis']}")
            print(f"ROP report saved: {paths['report']}")
            return

        if decision.status == MINI_RECOMMENDATION_NO_LLM:
            filtered_triggers = filter_today_mini_triggers(db_path, str(args.lead_id), decision.triggers)
            if not filtered_triggers:
                suppressed_decision = ProcessingDecision(
                    status=SKIPPED_NO_CHANGES,
                    reasons=[
                        "Mini recommendation подавлена: такие trigger_type уже создавались сегодня по этому лиду."
                    ],
                    triggers=decision.triggers,
                    diff=decision.diff,
                )
                persist_skip(
                    db_path=db_path,
                    args=args,
                    status=suppressed_decision.status,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    previous_state=previous_state,
                    decision_reason=suppressed_decision.as_dict(),
                )
                print(f"{SKIPPED_NO_CHANGES}: mini triggers suppressed by daily anti-spam for lead {args.lead_id}")
                return

            decision = ProcessingDecision(
                status=decision.status,
                reasons=decision.reasons,
                triggers=filtered_triggers,
                diff=decision.diff,
            )
            content = render_lead_mini_recommendation(
                lead_id=str(args.lead_id),
                decision=decision,
                previous_state=previous_state,
                current_snapshot=snapshot,
            )
            save_mini_recommendation_markdown(paths["mini"], content)
            for trigger in decision.triggers:
                save_mini_recommendation(
                    db_path,
                    entity_type="lead",
                    entity_id=str(args.lead_id),
                    trigger_type=str(trigger.get("trigger_type") or "unknown"),
                    recommendation_md_path=str(paths["mini"]),
                    fingerprint=fingerprint,
                )
            persist_skip(
                db_path=db_path,
                args=args,
                status=decision.status,
                fingerprint=fingerprint,
                snapshot=snapshot,
                previous_state=previous_state,
                decision_reason=decision.as_dict(),
                mini_path=paths["mini"],
            )
            print(f"{decision.status}: mini recommendation saved: {paths['mini']}")
            return

        if decision.status == SKIPPED_NO_CHANGES:
            persist_skip(
                db_path=db_path,
                args=args,
                status=decision.status,
                fingerprint=fingerprint,
                snapshot=snapshot,
                previous_state=previous_state,
                decision_reason=decision.as_dict(),
            )
            print(f"{decision.status}: lead {args.lead_id} skipped")
            return

        raise RuntimeError(f"Unsupported decision status: {decision.status}")

    except Exception as error:
        logger.exception("Lead change-detection analysis failed")
        try:
            save_analysis_run(
                db_path,
                entity_type="lead",
                entity_id=str(args.lead_id),
                status=ERROR,
                error=str(error),
            )
        except Exception:
            logger.exception("Could not persist ERROR run")
        print(f"{ERROR}: {error}")
        raise


if __name__ == "__main__":
    main()
