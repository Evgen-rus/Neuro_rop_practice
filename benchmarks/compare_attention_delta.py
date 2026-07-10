"""Prepare manual legacy-versus-compact attention-delta comparisons."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.run_legacy_benchmark import empty_manual_scores, read_json


def _nested(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _legacy_summary(analysis_payload: dict[str, Any]) -> dict[str, Any]:
    analysis = analysis_payload.get("analysis") if isinstance(analysis_payload.get("analysis"), dict) else {}
    metadata = analysis_payload.get("model_metadata") if isinstance(analysis_payload.get("model_metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    return {
        "attention_required": _first(analysis.get("attention_required"), _nested(analysis, "rop_action", "attention_required")),
        "main_risk": _first(_nested(analysis, "main_risk", "summary"), _nested(analysis, "main_risk", "description"), _nested(analysis, "main_risk", "reason")),
        "message_to_manager": _first(_nested(analysis, "rop_manager_message_block", "message_to_manager"), _nested(analysis, "rop_action", "message_to_manager")),
        "evidence": _first(_nested(analysis, "rop_manager_message_block", "evidence"), _nested(analysis, "main_risk", "evidence")),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "estimated_cost_rub": metadata.get("estimated_cost_rub"),
    }


def _compact_summary(payload: dict[str, Any]) -> dict[str, Any]:
    delta = payload.get("attention_delta") if isinstance(payload.get("attention_delta"), dict) else {}
    metadata = payload.get("model_metadata") if isinstance(payload.get("model_metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    action = delta.get("rop_action") if isinstance(delta.get("rop_action"), dict) else {}
    return {
        "attention_required": delta.get("attention_required"),
        "reason": delta.get("reason"),
        "message_to_manager": action.get("message_to_manager"),
        "evidence_ids": action.get("evidence_ids"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "estimated_cost_rub": metadata.get("estimated_cost_rub"),
    }


def compare_case(case: dict[str, Any], shadow_root: Path) -> dict[str, Any]:
    baseline = case.get("baseline") if isinstance(case.get("baseline"), dict) else {}
    legacy = _legacy_summary(read_json(Path(str(baseline.get("analysis_json") or ""))))
    compact_path = shadow_root / str(case.get("case_id")) / "attention_delta.json"
    compact = _compact_summary(read_json(compact_path)) if compact_path.exists() else None
    legacy_output = legacy.get("output_tokens")
    compact_output = compact.get("output_tokens") if compact else None
    reduction = None
    if isinstance(legacy_output, (int, float)) and legacy_output > 0 and isinstance(compact_output, (int, float)):
        reduction = round((1 - compact_output / legacy_output) * 100, 2)
    return {
        "case_id": case.get("case_id"),
        "entity_type": case.get("entity_type"),
        "legacy": legacy,
        "compact": compact,
        "output_token_reduction_percent": reduction,
        "manual_review": {"status": "not_reviewed", "scores": empty_manual_scores(), "notes": ""},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build manual legacy/attention-delta comparison data")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shadow-output", default=str(PROJECT_ROOT / "benchmarks" / "results" / "attention_delta"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / "benchmarks" / "results" / "attention_delta_comparison.json"))
    args = parser.parse_args()
    manifest = read_json(Path(args.manifest))
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    result = {"version": 1, "comparison_type": "manual_quality_review_required", "cases": [compare_case(case, Path(args.shadow_output)) for case in cases if isinstance(case, dict)]}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Attention-delta comparison saved: {output}")


if __name__ == "__main__":
    main()
