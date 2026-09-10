from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from openai_api.llm.analyze_deal import (
    INCREMENTAL_DEAL_PROMPT_VERSION,
    build_prompt,
    load_incremental_context,
)


class IncrementalDealAnalysisTests(unittest.TestCase):
    def test_prompt_contains_only_trusted_baseline_and_supplied_deltas(self) -> None:
        prompt = build_prompt(
            "7",
            "old-unchanged-history",
            "old-unchanged-transcript",
            "synthetic-diagnostics",
            [(Path("synthetic-rules.md"), "synthetic-rule")],
            {"closed": False},
            incremental_context={
                "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": {"deal_state": {"summary": "synthetic-baseline"}},
                "TRUSTED_CONTINUITY_BASELINE": {"deal_context": {"critical_facts": [{"fact_id": "stable_fact"}]}},
                "CRM_SEMANTIC_DELTA": [{"key": "deal:7", "change_type": "UPDATED_MEANINGFUL"}],
                "NEW_OR_REVISED_CLIENT_EVIDENCE": [{
                    "evidence_id": "call:201", "text": "synthetic-new-evidence",
                }],
                "CURRENT_REQUIRED_CRM_FACTS": {"stage_id": "SYNTHETIC:STAGE"},
            },
        )
        for marker in (
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS",
            "CRM_SEMANTIC_DELTA",
            "NEW_OR_REVISED_CLIENT_EVIDENCE",
            "CURRENT_REQUIRED_CRM_FACTS",
            "stable_fact",
            "synthetic-baseline",
            "synthetic-new-evidence",
            "synthetic-rule",
        ):
            self.assertIn(marker, prompt)
        self.assertIn("полный текущий analysis JSON", prompt)
        self.assertIn("TRUSTED_CONTINUITY_BASELINE", prompt)
        self.assertIn("не отменяет их молча", prompt)
        self.assertIn("исходящие активности не являются evidence", prompt)
        self.assertNotIn("old-unchanged-history", prompt)
        self.assertNotIn("old-unchanged-transcript", prompt)

    def test_incremental_context_loader_rejects_partial_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"CRM_SEMANTIC_DELTA": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid shape"):
                load_incremental_context(str(path))

    def test_incremental_prompt_has_separate_cache_version(self) -> None:
        self.assertEqual(INCREMENTAL_DEAL_PROMPT_VERSION, "neuro-rop:incremental-deal:v1")


if __name__ == "__main__":
    unittest.main()
