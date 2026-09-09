from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_legacy_benchmark import collect_case, legacy_metrics
from benchmarks.run_incremental_evaluation import RUNS, collect_evaluation
from openai_api.llm.validation import DEAL_REQUIRED_FIELDS, normalize_analysis_for_validation
from test_lead_qualification_assessment import lead_analysis


class BenchmarkRunnerTests(unittest.TestCase):
    def test_reads_existing_baseline_without_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis = root / "analysis.json"
            prompt = root / "request_prompt.txt"
            report = root / "rop_report.md"
            budget = root / "prompt_budget.json"
            analysis.write_text(
                json.dumps(
                    {
                        "model_metadata": {
                            "model": "gpt-5.4-mini",
                            "usage": {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 5}},
                            "estimated_cost_rub": 0.1,
                        }
                    }
                ),
                encoding="utf-8",
            )
            prompt.write_text("sanitized prompt", encoding="utf-8")
            report.write_text("sanitized report", encoding="utf-8")
            budget.write_text(json.dumps({"total": {"chars": 16}}), encoding="utf-8")
            case = {
                "case_id": "deal-test",
                "entity_type": "deal",
                "baseline": {
                    "analysis_json": str(analysis),
                    "request_prompt": str(prompt),
                    "rop_report": str(report),
                    "prompt_budget_json": str(budget),
                },
            }
            metrics = legacy_metrics(case["baseline"])
            result = collect_case(case, execute_legacy=False)
            self.assertEqual(metrics["cached_input_tokens"], 5)
            self.assertEqual(result["baseline_metrics"]["elapsed_seconds"], None)
            self.assertEqual(result["manual_review"]["scores"]["no_hallucinated_facts"], "not_reviewed")

    def test_incremental_evaluation_collects_seven_artifacts_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = {}
            analysis = {key: {} for key in DEAL_REQUIRED_FIELDS}
            source = lead_analysis()
            for key in ("rop_manager_message_block", "manager_action_block", "qualification_assessment", "main_risk"):
                analysis[key] = source[key]
            analysis["qualification_assessment"].pop("lead_category", None)
            analysis["qualification_assessment"].pop("lead_route", None)
            analysis.update(deal_id="7", what_changed=[], communication_quality_audit={})
            normalize_analysis_for_validation(analysis)
            states = {"full_a": "A", "incremental_b": "B", "full_b": "B", "incremental_c": "C", "incremental_d": "D", "incremental_e": "E", "full_e": "E"}
            baselines = {"incremental_b": "full_a", "incremental_c": "incremental_b", "incremental_d": "incremental_c", "incremental_e": "incremental_d"}
            for name in RUNS:
                path = root / f"{name}.json"
                path.write_text(json.dumps({
                    "analysis_mode": "incremental" if name.startswith("incremental_") else "full",
                    "model_metadata": {
                        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                        "estimated_cost_usd": 0.01,
                        "estimated_cost_rub": 1.0,
                    },
                    "analysis": analysis,
                }), encoding="utf-8")
                artifacts[name] = {
                    "analysis_json": str(path),
                    "state_fingerprint": states[name],
                    "baseline": baselines.get(name),
                }
            result = collect_evaluation({"case_id": "synthetic", "artifacts": artifacts})
        self.assertEqual(result["planned_paid_analysis_runs"], 7)
        self.assertEqual(result["usage_cost_totals"]["input_tokens"], 70)
        self.assertEqual(
            result["comparisons"]["multi_step_drift_e"]["manual_review"]["status"],
            "not_reviewed",
        )


if __name__ == "__main__":
    unittest.main()
