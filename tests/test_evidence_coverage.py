from __future__ import annotations

import unittest

from openai_api.llm.evidence_coverage import validate_evidence_context_coverage


def delta(ids: list[str], *, attention: bool = True) -> dict:
    return {
        "attention_required": attention,
        "rop_action": {"evidence_ids": ids} if attention else None,
        "nested": {"evidence_ids": ids},
    }


class EvidenceCoverageTests(unittest.TestCase):
    def validate(self, value: dict, *, history: str = "", transcript: str = "", stage_policy: dict | None = None) -> dict:
        return validate_evidence_context_coverage(
            value,
            history_text=history,
            transcript_text=transcript,
            stage_policy=stage_policy,
        )

    def test_all_numeric_evidence_ids_present(self) -> None:
        result = self.validate(delta(["123", "456"]), history="source=x type=call id=123\nsource=x type=task id=456")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["coverage_percent"], 100)

    def test_missing_numeric_evidence_fails(self) -> None:
        result = self.validate(delta(["123", "999"]), history="source=x type=call id=123")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing_ids"], ["999"])

    def test_present_im_namespace_id_passes(self) -> None:
        result = self.validate(delta(["im:10:20"]), history="source=x type=comment id=im:10:20")
        self.assertEqual(result["status"], "passed")

    def test_missing_im_namespace_id_fails(self) -> None:
        result = self.validate(delta(["im:10:21"]), history="source=x type=comment id=im:10:20")
        self.assertEqual(result["status"], "failed")

    def test_raw_snapshot_id_not_in_prompt_fails(self) -> None:
        raw_snapshot = "source=x type=call id=777"
        self.assertIn("777", raw_snapshot)
        result = self.validate(delta(["777"]), history="", transcript="")
        self.assertEqual(result["status"], "failed")

    def test_numeric_id_does_not_substring_match(self) -> None:
        result = self.validate(delta(["123"]), history="source=x type=call id=1234")
        self.assertEqual(result["status"], "failed")

    def test_duplicate_evidence_is_deduplicated(self) -> None:
        result = self.validate(delta(["123", "123"]), history="source=x type=call id=123")
        self.assertEqual(result["referenced_ids"], ["123"])
        self.assertEqual(result["coverage_percent"], 100)

    def test_exact_busy_transcript_evidence_passes(self) -> None:
        transcript = "### Call: activity_id=busy-1\n\n```text\nBusy system outcome\n```"
        result = self.validate(delta(["busy-1"]), transcript=transcript)
        self.assertEqual(result["status"], "passed")

    def test_summary_only_evidence_fails(self) -> None:
        transcript = "| 1 | timestamp | source | 123 | summary only |"
        result = self.validate(delta(["123"]), transcript=transcript)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["index_only_ids"], ["123"])

    def test_empty_evidence_is_not_allowed_for_required_action(self) -> None:
        result = self.validate(delta([]), history="source=x type=call id=123")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing_ids"], ["required_action_evidence"])

    def test_evidence_after_materialization_shape_is_checked_recursively(self) -> None:
        value = {"attention_required": True, "rop_action": {"evidence_ids": ["123"]}, "lead_review": {"evidence_ids": ["456"]}}
        result = self.validate(value, history="source=x type=call id=123\nsource=x type=task id=456")
        self.assertEqual(result["status"], "passed")

    def test_one_missing_of_ten_fails_whole_coverage(self) -> None:
        ids = [str(number) for number in range(1, 11)]
        history = "\n".join(f"source=x type=call id={number}" for number in range(1, 10))
        result = self.validate(delta(ids), history=history)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing_ids"], ["10"])


if __name__ == "__main__":
    unittest.main()
