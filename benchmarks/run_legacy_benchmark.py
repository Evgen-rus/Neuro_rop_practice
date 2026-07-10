"""Collect legacy baseline metrics without sending data to OpenAI by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUBRIC_PATH = PROJECT_ROOT / "benchmarks" / "rubric.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect local legacy-analysis benchmark baselines")
    parser.add_argument("--manifest", required=True, help="Local manifest path, normally benchmarks/local/cases.json")
    parser.add_argument("--output", default=None, help="Local output JSON path; default is beside the manifest")
    parser.add_argument(
        "--execute-legacy",
        action="store_true",
        help="Run the command declared by a local case. Requires --allow-paid-api.",
    )
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        help="Explicit acknowledgement required before a legacy command can call OpenAI.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def file_summary(path_value: str | None) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists():
        return {"path": str(path), "exists": False}
    data = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "bytes": len(data),
        "chars": len(data.decode("utf-8")),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def legacy_metrics(baseline: dict[str, Any]) -> dict[str, Any]:
    analysis_path = Path(str(baseline.get("analysis_json") or ""))
    analysis = read_json(analysis_path) if analysis_path.exists() else {}
    metadata = analysis.get("model_metadata") if isinstance(analysis.get("model_metadata"), dict) else {}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    budget_path = Path(str(baseline.get("prompt_budget_json") or ""))
    budget = read_json(budget_path) if budget_path.exists() else None
    return {
        "model": metadata.get("model"),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": details.get("cached_tokens", usage.get("cached_input_tokens")),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "estimated_cost_rub": metadata.get("estimated_cost_rub"),
        "elapsed_seconds": None,
        "prompt": file_summary(baseline.get("request_prompt")),
        "analysis": file_summary(baseline.get("analysis_json")),
        "rop_report": file_summary(baseline.get("rop_report")),
        "prompt_budget": budget,
    }


def run_declared_legacy_command(case: dict[str, Any]) -> float:
    command = case.get("legacy_command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError(f"Case {case.get('case_id')} requires a non-empty legacy_command list")
    started = time.monotonic()
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    return round(time.monotonic() - started, 3)


def empty_manual_scores() -> dict[str, str]:
    rubric = read_json(RUBRIC_PATH)
    return {criterion: "not_reviewed" for criterion in rubric["criteria"]}


def collect_case(case: dict[str, Any], *, execute_legacy: bool) -> dict[str, Any]:
    if case.get("entity_type") not in {"lead", "deal"}:
        raise ValueError(f"Unsupported entity_type in case {case.get('case_id')!r}")
    elapsed = run_declared_legacy_command(case) if execute_legacy else None
    baseline = case.get("baseline") if isinstance(case.get("baseline"), dict) else {}
    metrics = legacy_metrics(baseline)
    if elapsed is not None:
        metrics["elapsed_seconds"] = elapsed
    manual = case.get("manual_review") if isinstance(case.get("manual_review"), dict) else {}
    return {
        "case_id": case.get("case_id"),
        "entity_type": case.get("entity_type"),
        "baseline_metrics": metrics,
        "manual_review": {
            "status": manual.get("status", "not_reviewed"),
            "scores": {**empty_manual_scores(), **(manual.get("scores") or {})},
            "notes": manual.get("notes", ""),
        },
    }


def main() -> None:
    args = parse_args()
    if args.execute_legacy and not args.allow_paid_api:
        raise SystemExit("Refusing to run legacy analysis without --allow-paid-api.")
    manifest_path = Path(args.manifest)
    manifest = read_json(manifest_path)
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    if not cases:
        raise SystemExit("Manifest has no cases.")
    output_path = Path(args.output) if args.output else PROJECT_ROOT / "benchmarks" / "results" / "benchmark_results.json"
    result = {
        "version": 1,
        "mode": "legacy_execution" if args.execute_legacy else "reuse_baseline_no_api_call",
        "manifest": str(manifest_path),
        "cases": [collect_case(case, execute_legacy=args.execute_legacy) for case in cases if isinstance(case, dict)],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Benchmark results saved: {output_path}")


if __name__ == "__main__":
    main()
