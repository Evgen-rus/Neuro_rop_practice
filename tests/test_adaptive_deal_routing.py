from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openai_api.llm.analyze_deal_if_changed import (
    INCREMENTAL_VARIABLE_SIZE_RATIO_THRESHOLD,
    adaptive_variable_size_routing,
    choose_safe_llm_mode_by_variable_size,
    json_utf8_byte_len,
    measure_full_variable_bytes,
    measure_incremental_variable_bytes,
    utf8_byte_len,
)


def _routing(full_bytes: int, incremental_bytes: int) -> dict:
    return choose_safe_llm_mode_by_variable_size(
        full_variable_bytes=full_bytes,
        incremental_variable_bytes=incremental_bytes,
    )


def _incremental_payload(**overrides) -> dict:
    payload = {
        "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": {"deal_state": {"summary": "baseline"}},
        "TRUSTED_CONTINUITY_BASELINE": {"deal_context": {"critical_facts": [{"fact_id": "stable_fact"}]}},
        "CRM_SEMANTIC_DELTA": [{"key": "deal:7", "change_type": "UPDATED_MEANINGFUL"}],
        "NEW_OR_REVISED_CLIENT_EVIDENCE": [{"evidence_id": "call:201", "text": "новый звонок"}],
        "AVAILABLE_CLIENT_EVIDENCE_IDS": ["call:101", "call:201"],
        "CURRENT_REQUIRED_CRM_FACTS": {"deal": {"stage_id": "NEW"}},
    }
    payload.update(overrides)
    return payload


class AdaptiveDealRoutingUnitTests(unittest.TestCase):
    def test_threshold_constant_is_single_source(self) -> None:
        self.assertEqual(INCREMENTAL_VARIABLE_SIZE_RATIO_THRESHOLD, 0.90)

    def test_incremental_significantly_smaller_selects_incremental(self) -> None:
        routing = _routing(1000, 500)
        self.assertEqual(routing["chosen_analysis_mode"], "incremental")
        self.assertEqual(routing["full_variable_bytes"], 1000)
        self.assertEqual(routing["incremental_variable_bytes"], 500)
        self.assertEqual(routing["routing_size_ratio"], 0.5)
        self.assertEqual(routing["routing_size_threshold"], 0.90)

    def test_incremental_advantage_below_ten_percent_selects_full(self) -> None:
        routing = _routing(1000, 910)
        self.assertEqual(routing["chosen_analysis_mode"], "full")
        self.assertEqual(routing["routing_size_ratio"], 0.91)

    def test_equal_sizes_select_full(self) -> None:
        routing = _routing(1000, 1000)
        self.assertEqual(routing["chosen_analysis_mode"], "full")
        self.assertEqual(routing["routing_size_ratio"], 1.0)

    def test_incremental_larger_selects_full(self) -> None:
        routing = _routing(1000, 1500)
        self.assertEqual(routing["chosen_analysis_mode"], "full")
        self.assertGreater(routing["routing_size_ratio"], 1.0)

    def test_exact_threshold_selects_incremental(self) -> None:
        routing = _routing(1000, 900)
        self.assertEqual(routing["chosen_analysis_mode"], "incremental")
        self.assertEqual(routing["routing_size_ratio"], 0.9)

    def test_zero_full_bytes_selects_full_without_division(self) -> None:
        routing = _routing(0, 120)
        self.assertEqual(routing["chosen_analysis_mode"], "full")
        self.assertIsNone(routing["routing_size_ratio"])
        self.assertEqual(routing["routing_size_threshold"], 0.90)

    def test_utf8_russian_text_counts_bytes_not_chars(self) -> None:
        text = "Привет"
        self.assertEqual(len(text), 6)
        self.assertEqual(utf8_byte_len(text), 12)
        self.assertEqual(measure_full_variable_bytes(text, "я"), 12 + 2)

    def test_incremental_bytes_exclude_shared_continuity_block(self) -> None:
        context = _incremental_payload()
        unique = {
            key: context[key]
            for key in (
                "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS",
                "CRM_SEMANTIC_DELTA",
                "NEW_OR_REVISED_CLIENT_EVIDENCE",
                "AVAILABLE_CLIENT_EVIDENCE_IDS",
                "CURRENT_REQUIRED_CRM_FACTS",
            )
        }
        self.assertEqual(measure_incremental_variable_bytes(context), json_utf8_byte_len(unique))
        self.assertLess(
            measure_incremental_variable_bytes(context),
            json_utf8_byte_len(context),
        )

    def test_diagnostics_contain_sizes_ratio_and_mode(self) -> None:
        routing = _routing(200, 100)
        self.assertEqual(
            set(routing),
            {
                "full_variable_bytes",
                "incremental_variable_bytes",
                "routing_size_ratio",
                "routing_size_threshold",
                "chosen_analysis_mode",
            },
        )
        serialized = json.dumps(routing, ensure_ascii=False)
        self.assertNotIn("Привет", serialized)
        self.assertNotIn("новый звонок", serialized)

    def test_missing_history_skips_size_routing(self) -> None:
        context = _incremental_payload()
        with tempfile.TemporaryDirectory() as directory:
            routing = adaptive_variable_size_routing(
                context=context,
                current_deal_dir=Path(directory) / "deal_7",
                deal_id="7",
                transcript_path=None,
            )
        self.assertIsNone(routing)


if __name__ == "__main__":
    unittest.main()
