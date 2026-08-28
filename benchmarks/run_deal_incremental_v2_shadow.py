"""Explicit paid replay of one local evidence item through deal V2.

This benchmark never updates entity_state, analysis_runs, ui_reports, reports,
feedback or recommendation materialization. Full candidate content
stays under ignored reports runtime; stdout contains privacy-safe metrics only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.config import ANALYSIS_MODEL
from openai_api.llm.analyze_deal import build_deal_stage_policy
from openai_api.llm.deal_evidence import collect_deal_evidence, coverage_from_evidence, evidence_delta
from openai_api.llm.deal_incremental_v2 import run_incremental_v2
from openai_api.llm.deal_semantic_state import SCHEMA_VERSION, bootstrap_semantic_state
from openai_api.llm.validation import normalize_analysis_for_validation, validate_deal_analysis
from storage.rop_db import (
    DEFAULT_DB_PATH,
    connect,
    get_entity_state,
    get_latest_neuro_rop_recommendation_projection,
    loads_json,
    save_deal_incremental_v2_run,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--baseline-report-id", required=True, type=int)
    parser.add_argument("--replay-evidence-id", required=True)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--deal-root", default="reports/rop_assistant/deals")
    parser.add_argument("--model", default=ANALYSIS_MODEL)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    deal_dir = Path(args.deal_root) / f"deal_{args.deal_id}"
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT analysis_run_id, report_json FROM ui_reports WHERE id = ? AND entity_type = 'deal' AND entity_id = ?",
            (args.baseline_report_id, str(args.deal_id)),
        ).fetchone()
    if row is None:
        raise ValueError("baseline report not found")
    stored = loads_json(row["report_json"], {})
    analysis = stored.get("analysis") if isinstance(stored.get("analysis"), dict) else stored
    normalize_analysis_for_validation(analysis)
    validate_deal_analysis(analysis)

    raw_path = deal_dir / "raw" / f"deal_{args.deal_id}_context.json"
    raw_bundle = json.loads(raw_path.read_text(encoding="utf-8"))
    all_evidence = collect_deal_evidence(raw_bundle, deal_dir / "transcripts")
    replay = [item for item in all_evidence if item.get("evidence_id") == args.replay_evidence_id]
    if len(replay) != 1:
        raise ValueError("replay evidence id must resolve to exactly one local item")
    baseline_evidence = [item for item in all_evidence if item.get("evidence_id") != args.replay_evidence_id]
    baseline_coverage = coverage_from_evidence(baseline_evidence)
    delta, next_coverage = evidence_delta(all_evidence, baseline_coverage)
    state = get_entity_state(db_path, "deal", str(args.deal_id)) or {}
    fingerprint = str(state.get("current_fingerprint") or "benchmark")
    semantic_state = bootstrap_semantic_state(
        analysis,
        deal_id=str(args.deal_id),
        source_analysis_run_id=int(row["analysis_run_id"]) if row["analysis_run_id"] is not None else None,
        source_fingerprint=fingerprint,
        evidence_coverage=baseline_coverage,
    )
    result = run_incremental_v2(
        deal_id=str(args.deal_id),
        previous_analysis=analysis,
        previous_semantic_state=semantic_state,
        evidence_delta=delta,
        next_evidence_coverage=next_coverage,
        crm_delta={"change_types": ["benchmark_evidence_replay"], "details": {}},
        stage_policy=build_deal_stage_policy(deal_dir, str(args.deal_id)),
        prior_recommendation=get_latest_neuro_rop_recommendation_projection(db_path, str(args.deal_id)),
        source_fingerprint=fingerprint,
        model=args.model,
    )
    output_dir = deal_dir / "analysis" / "v2_shadow"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact = output_dir / f"benchmark_report_{args.baseline_report_id}_{stamp}.json"
    artifact.write_text(
        json.dumps({
            "mode": "shadow_benchmark",
            "baseline_report_id": args.baseline_report_id,
            "replay_evidence_ids": [item["evidence_id"] for item in delta],
            "semantic_state": result.semantic_state,
            "changed_domains": result.changed_domains,
            "affected_sections": result.affected_sections,
            "model_metadata": {key: value for key, value in result.metadata.items() if not key.startswith("_")},
            "analysis": result.analysis,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    telemetry = {
        "usage": result.metadata.get("usage") or {},
        "prompt_budget": result.metadata.get("prompt_budget") or {},
        "benchmark": True,
    }
    save_deal_incremental_v2_run(
        db_path,
        entity_id=str(args.deal_id),
        mode="shadow",
        source_analysis_run_id=int(row["analysis_run_id"]) if row["analysis_run_id"] is not None else None,
        source_fingerprint=fingerprint,
        semantic_schema_version=SCHEMA_VERSION,
        evidence_ids=[item["evidence_id"] for item in delta],
        changed_domains=result.changed_domains,
        affected_sections=result.affected_sections,
        telemetry=telemetry,
        semantic_validation="passed",
        final_validation="passed",
        artifact_path=str(artifact),
    )
    print(json.dumps({
        "status": "passed",
        "changed_domains": result.changed_domains,
        "affected_section_count": len(result.affected_sections),
        "usage": result.metadata.get("usage"),
        "prompt_budget": result.metadata.get("prompt_budget"),
        "artifact": str(artifact),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
