from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.run_legacy_benchmark import collect_case, legacy_metrics


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


if __name__ == "__main__":
    unittest.main()
