from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.incremental_deal_analysis import (
    INCREMENTAL_DEAL_PROMPT_VERSION,
    build_incremental_deal_prompt,
    run_incremental_deal_analysis,
)
from openai_api.llm.validation import normalize_analysis_for_validation, validate_deal_analysis


class IncrementalDealAnalysisTests(unittest.TestCase):
    def test_prompt_contains_only_trusted_baseline_and_supplied_deltas(self) -> None:
        prompt = build_incremental_deal_prompt(
            deal_id="7",
            previous_analysis={"deal_state": {"summary": "synthetic-baseline"}},
            crm_semantic_delta=[{"key": "deal:7", "change_type": "UPDATED_MEANINGFUL"}],
            evidence_delta=[{"evidence_id": "call:201", "text": "synthetic-new-evidence"}],
            current_required_crm_facts={"stage_id": "SYNTHETIC:STAGE"},
            context_diagnostics_text="synthetic-diagnostics",
            okf_sections=[(Path("synthetic-rules.md"), "synthetic-rule")],
            stage_policy={"closed": False},
        )
        for marker in (
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS",
            "CRM_SEMANTIC_DELTA",
            "NEW_OR_REVISED_CLIENT_EVIDENCE",
            "CURRENT_REQUIRED_CRM_FACTS",
            "synthetic-baseline",
            "synthetic-new-evidence",
            "synthetic-rule",
        ):
            self.assertIn(marker, prompt)
        self.assertIn("полный текущий analysis JSON", prompt)
        self.assertNotIn("old-unchanged-transcript", prompt)

    @patch("openai_api.llm.incremental_deal_analysis.call_validated_analysis_json")
    def test_runner_reuses_production_validator(self, validated_call) -> None:
        expected = ({"deal_state": {}}, {"model": "synthetic-model"})
        validated_call.return_value = expected
        result = run_incremental_deal_analysis(
            "synthetic-prompt",
            deal_id="7",
            model="synthetic-model",
        )
        self.assertEqual(result, expected)
        kwargs = validated_call.call_args.kwargs
        self.assertIs(kwargs["validator"], validate_deal_analysis)
        self.assertIs(kwargs["normalizer"], normalize_analysis_for_validation)
        self.assertEqual(kwargs["prompt_cache_key"], INCREMENTAL_DEAL_PROMPT_VERSION)
        self.assertEqual(kwargs["call_type"], "incremental_deal_analysis")


if __name__ == "__main__":
    unittest.main()
