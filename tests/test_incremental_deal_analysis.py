from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from openai_api.llm.analyze_deal import (
    COMPATIBLE_DEAL_PROMPT_VERSIONS,
    DEAL_ID_SECTION_MARKER,
    DEAL_PROMPT_CACHE_KEY,
    INCREMENTAL_DEAL_PROMPT_VERSION,
    build_prompt,
    deal_analysis_cache_options,
    load_incremental_context,
)
from openai_api.llm.analyze_deal_if_changed import incremental_context


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
                "AVAILABLE_CLIENT_EVIDENCE_IDS": ["call:101", "call:201"],
                "CURRENT_REQUIRED_CRM_FACTS": {"stage_id": "SYNTHETIC:STAGE"},
            },
        )
        for marker in (
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS",
            "CRM_SEMANTIC_DELTA",
            "NEW_OR_REVISED_CLIENT_EVIDENCE",
            "CURRENT_REQUIRED_CRM_FACTS",
            "AVAILABLE_CLIENT_EVIDENCE_IDS",
            "stable_fact",
            "synthetic-baseline",
            "synthetic-new-evidence",
            "synthetic-rule",
        ):
            self.assertIn(marker, prompt)
        self.assertIn("полный текущий analysis JSON", prompt)
        self.assertIn("TRUSTED_CONTINUITY_BASELINE", prompt)
        self.assertIn("не отменяет их молча", prompt)
        self.assertIn("NEW_OR_REVISED_CLIENT_EVIDENCE", prompt)
        self.assertIn("Повышай basis_status или BANT timing до confirmed", prompt)
        self.assertNotIn("<continuity_correction>", prompt)
        self.assertIn("исходящие активности не являются evidence", prompt)
        self.assertIn("call:101", prompt)
        self.assertNotIn("old-unchanged-history", prompt)
        self.assertNotIn("old-unchanged-transcript", prompt)

    def test_incremental_context_loader_rejects_partial_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"CRM_SEMANTIC_DELTA": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid shape"):
                load_incremental_context(str(path))

    def test_incremental_context_producer_matches_loader(self) -> None:
        context, _ = incremental_context(
            {
                "analysis": {"deal_context": {}},
                "evidence_coverage": {
                    "call:101": {"content_hash": "old", "revision": 1},
                },
            },
            {
                "owner": {"entity_id": "7"},
                "entities": {"deal:7": {"semantic": {"stage_id": "NEW"}}},
                "source_status": {},
            },
            {"entries": []},
            [
                {"evidence_id": "call:101", "content_hash": "old", "kind": "call_transcript"},
                {"evidence_id": "message:201", "content_hash": "legacy", "kind": "inbound_message"},
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps(context), encoding="utf-8")
            loaded = load_incremental_context(str(path))
        self.assertEqual(
            loaded["AVAILABLE_CLIENT_EVIDENCE_IDS"],
            ["call:101", "message:201"],
        )
        self.assertEqual(loaded["NEW_OR_REVISED_CLIENT_EVIDENCE"], [])

    def test_incremental_continuity_correction_stays_on_incremental_input(self) -> None:
        context = {
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": {"deal_state": {"summary": "synthetic-baseline"}},
            "TRUSTED_CONTINUITY_BASELINE": {"deal_context": {"critical_facts": [{"fact_id": "stable_fact"}]}},
            "CRM_SEMANTIC_DELTA": [],
            "NEW_OR_REVISED_CLIENT_EVIDENCE": [{"evidence_id": "call:201", "text": "synthetic-new-evidence"}],
            "AVAILABLE_CLIENT_EVIDENCE_IDS": ["call:201"],
            "CURRENT_REQUIRED_CRM_FACTS": {"stage_id": "SYNTHETIC:STAGE"},
        }
        prompt = build_prompt(
            "7",
            "old-unchanged-history",
            "old-unchanged-transcript",
            "synthetic-diagnostics",
            [],
            {},
            incremental_context=context,
            continuity_correction=True,
        )
        self.assertIn("<continuity_correction>", prompt)
        self.assertIn("NEW_OR_REVISED_CLIENT_EVIDENCE", prompt)
        self.assertGreater(prompt.find("<continuity_correction>"), prompt.find(DEAL_ID_SECTION_MARKER))
        self.assertNotIn("old-unchanged-history", prompt)
        self.assertNotIn("## TRUSTED CONTINUITY BASELINE", prompt)

    def test_incremental_shares_full_instruction_prefix(self) -> None:
        context = {
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": {"deal_state": {"summary": "synthetic-baseline"}},
            "TRUSTED_CONTINUITY_BASELINE": {"deal_context": {"critical_facts": [{"fact_id": "stable_fact"}]}},
            "CRM_SEMANTIC_DELTA": [],
            "NEW_OR_REVISED_CLIENT_EVIDENCE": [{"evidence_id": "call:201", "text": "synthetic-new-evidence"}],
            "AVAILABLE_CLIENT_EVIDENCE_IDS": ["call:201"],
            "CURRENT_REQUIRED_CRM_FACTS": {"stage_id": "SYNTHETIC:STAGE"},
        }
        kwargs = {
            "deal_id": "7",
            "history_text": "old-unchanged-history",
            "transcript_text": "old-unchanged-transcript",
            "context_diagnostics_text": "synthetic-diagnostics",
            "okf_sections": [(Path("synthetic-rules.md"), "synthetic-rule")],
            "stage_policy": {"closed": False},
        }
        full = build_prompt(**kwargs)
        incremental = build_prompt(**kwargs, incremental_context=context)
        marker_at = full.find(DEAL_ID_SECTION_MARKER)
        self.assertGreater(marker_at, 0)
        self.assertEqual(full[:marker_at], incremental[:incremental.find(DEAL_ID_SECTION_MARKER)])
        rules_at = incremental.find("<incremental_analysis_rules>")
        input_at = incremental.find("## INCREMENTAL INPUT")
        self.assertGreater(rules_at, incremental.find(DEAL_ID_SECTION_MARKER))
        self.assertGreater(input_at, rules_at)
        self.assertNotIn("<incremental_analysis_rules>", full[:marker_at])

    def test_incremental_prompt_version_stays_separate_from_openai_cache_key(self) -> None:
        self.assertEqual(INCREMENTAL_DEAL_PROMPT_VERSION, "neuro-rop:incremental-deal:v2")
        self.assertNotEqual(INCREMENTAL_DEAL_PROMPT_VERSION, DEAL_PROMPT_CACHE_KEY)
        self.assertIn("neuro-rop:incremental-deal:v1", COMPATIBLE_DEAL_PROMPT_VERSIONS)
        self.assertIn(INCREMENTAL_DEAL_PROMPT_VERSION, COMPATIBLE_DEAL_PROMPT_VERSIONS)
        self.assertIn(DEAL_PROMPT_CACHE_KEY, COMPATIBLE_DEAL_PROMPT_VERSIONS)

    def test_incremental_openai_cache_shares_full_key_and_id_marker(self) -> None:
        key, markers = deal_analysis_cache_options(incremental=True, transcript_text="### Звонок 1\n")
        self.assertEqual(key, DEAL_PROMPT_CACHE_KEY)
        self.assertEqual(markers, [DEAL_ID_SECTION_MARKER])
        full_key, full_markers = deal_analysis_cache_options(
            incremental=False,
            transcript_text="### Звонок 1\n",
        )
        self.assertEqual(full_key, DEAL_PROMPT_CACHE_KEY)
        self.assertGreater(len(full_markers), 1)
        self.assertEqual(full_markers[0], DEAL_ID_SECTION_MARKER)


if __name__ == "__main__":
    unittest.main()
