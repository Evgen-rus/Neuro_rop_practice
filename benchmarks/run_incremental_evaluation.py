"""Collect a local FULL-vs-INCREMENTAL evaluation without API calls."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.run_legacy_benchmark import empty_manual_scores, legacy_metrics, read_json
from openai_api.llm.validation import validate_deal_analysis


RUNS = (
    "full_a",
    "incremental_b",
    "full_b",
    "incremental_c",
    "incremental_d",
    "incremental_e",
    "full_e",
)
BASELINES = {
    "full_a": None,
    "incremental_b": "full_a",
    "full_b": None,
    "incremental_c": "incremental_b",
    "incremental_d": "incremental_c",
    "incremental_e": "incremental_d",
    "full_e": None,
}


def collect_evaluation(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or any(name not in artifacts for name in RUNS):
        raise ValueError(f"artifacts must contain the required evaluation runs: {', '.join(RUNS)}")

    runs = {}
    for name in RUNS:
        spec = artifacts[name]
        if not isinstance(spec, dict):
            raise ValueError(f"artifact {name} must be an object")
        analysis_path = Path(str(spec.get("analysis_json") or ""))
        if not analysis_path.is_file():
            raise FileNotFoundError(f"analysis artifact not found: {analysis_path}")
        payload = read_json(analysis_path)
        mode = payload.get("analysis_mode") or payload.get("mode")
        expected_mode = "incremental" if name.startswith("incremental_") else "full"
        if expected_mode not in str(mode):
            raise ValueError(f"artifact {name} has incompatible mode: {mode!r}")
        analysis = payload.get("analysis")
        if not isinstance(analysis, dict):
            raise ValueError(f"artifact {name} has no complete analysis object")
        validate_deal_analysis(deepcopy(analysis))
        fingerprint = str(spec.get("state_fingerprint") or "").strip()
        if not fingerprint or spec.get("baseline") != BASELINES[name]:
            raise ValueError(f"artifact {name} has invalid state fingerprint or baseline lineage")
        runs[name] = {
            "mode": expected_mode,
            "state_fingerprint": fingerprint,
            "baseline": spec.get("baseline"),
            "metrics": legacy_metrics(spec),
        }

    for left, right in (("incremental_b", "full_b"), ("incremental_e", "full_e")):
        if runs[left]["state_fingerprint"] != runs[right]["state_fingerprint"]:
            raise ValueError(f"comparison artifacts {left} and {right} must describe the same state")

    numeric_fields = ("input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd", "estimated_cost_rub")
    totals = {
        field: sum(
            float(run["metrics"].get(field) or 0)
            for run in runs.values()
        )
        for field in numeric_fields
    }
    comparisons = {
        "single_step_b": {"candidate": "incremental_b", "control": "full_b"},
        "multi_step_drift_e": {"candidate": "incremental_e", "control": "full_e"},
    }
    for comparison in comparisons.values():
        comparison["manual_review"] = {
            "status": "not_reviewed",
            "scores": empty_manual_scores(),
            "notes": "",
        }
    return {
        "version": 1,
        "mode": "artifact_collection_no_api_call",
        "case_id": manifest.get("case_id"),
        "planned_paid_analysis_runs": len(RUNS),
        "runs": runs,
        "usage_cost_totals": totals,
        "comparisons": comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", default="benchmarks/results/incremental_evaluation.json")
    args = parser.parse_args()
    result = collect_evaluation(read_json(Path(args.manifest)))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Incremental evaluation saved: {output}")


if __name__ == "__main__":
    main()
