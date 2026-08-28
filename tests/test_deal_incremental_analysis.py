from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.analyze_deal import HISTORY_SECTION_MARKER, build_prompt
from openai_api.llm.deal_incremental import IncrementalContextError, build_incremental_context


def activity_container(items: list[dict]) -> dict:
    return {"ok": True, "response": {"result": items}}


class DealIncrementalContextTests(unittest.TestCase):
    def _state(self) -> dict:
        return {
            "last_analysis": {
                "generated_at": "technical-envelope-value",
                "model_metadata": {"input_tokens": 999},
                "analysis": {"deal_id": "101", "deal_state": {"summary": "База"}},
            }
        }

    def test_new_transcript_and_inbound_message_are_materialized_without_old_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "call_77.md"
            transcript.write_text("Новый разговор с клиентом", encoding="utf-8")
            (Path(directory) / "call_77.json").write_text(
                json.dumps({
                    "metadata": {"activity_id": "77", "call_start": "2026-08-23T10:00:00+03:00"},
                    "transcript_md_path": str(transcript),
                    "text": "Новый разговор с клиентом",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            previous_snapshot = {"transcript": {"path": str(Path(directory) / "call_old.md"), "content_hash": "old"}}
            current_snapshot = {
                "deal": {"id": "101", "stage_id": "C15:NEW", "opportunity": "120000"},
                "transcript": {"path": str(transcript), "content_hash": "new"},
            }
            diff = {
                "changes": ["transcript_changed", "new_activity", "new_email", "stage_changed"],
                "details": {
                    "new_activity_ids": ["55"],
                    "stage_changed": {"before": "C15:PREPARATION", "after": "C15:NEW"},
                },
            }
            raw = {
                "deal_id": "101",
                "activities": activity_container([{
                    "ID": "55", "TYPE_ID": "4", "DIRECTION": "1",
                    "SUBJECT": "Ответ клиента", "DESCRIPTION": "Просит уточнить срок",
                }]),
                "activity_details": {},
            }
            with patch("openai_api.llm.deal_incremental.normalize_analysis_for_validation"), \
                 patch("openai_api.llm.deal_incremental.validate_deal_analysis"):
                context = build_incremental_context(
                    previous_state=self._state(), previous_snapshot=previous_snapshot,
                    current_snapshot=current_snapshot, diff=diff, raw_bundle=raw,
                    transcript_path=transcript,
                )

        self.assertEqual(context["previous_analysis"]["deal_state"]["summary"], "База")
        encoded_previous = json.dumps(context["previous_analysis"], ensure_ascii=False)
        self.assertNotIn("technical-envelope-value", encoded_previous)
        self.assertNotIn("model_metadata", encoded_previous)
        self.assertEqual([event["type"] for event in context["new_events"]], ["transcript", "inbound_communication"])
        self.assertEqual(context["evidence_ids_included"], ["call:77", "email:55"])
        self.assertEqual(context["crm_delta"]["current_deal"]["stage_id"], "C15:NEW")

        prompt = build_prompt(
            "101", "СТАРАЯ ИСТОРИЯ НЕ ДОЛЖНА ПОПАСТЬ", "", "diagnostics",
            [(Path("rules.md"), "OKF RULE")], {"stage": "current"},
            {"source_report_id": 7}, incremental_context=context,
        )
        self.assertIn("## PREVIOUS_ANALYSIS", prompt)
        self.assertIn("## NEW_EVENTS", prompt)
        self.assertIn("## CRM_DELTA", prompt)
        self.assertIn("OKF RULE", prompt)
        self.assertIn("source_report_id", prompt)
        self.assertNotIn("СТАРАЯ ИСТОРИЯ НЕ ДОЛЖНА ПОПАСТЬ", prompt)
        self.assertNotIn(HISTORY_SECTION_MARKER, prompt)

    def test_transcript_changed_in_place_requires_full_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "call.md"
            transcript.write_text("edited", encoding="utf-8")
            snapshot = {"deal": {}, "transcript": {"path": str(transcript), "content_hash": "new"}}
            with patch("openai_api.llm.deal_incremental.normalize_analysis_for_validation"), \
                 patch("openai_api.llm.deal_incremental.validate_deal_analysis"):
                with self.assertRaisesRegex(IncrementalContextError, "transcript_changed_in_place"):
                    build_incremental_context(
                        previous_state=self._state(),
                        previous_snapshot={"transcript": {"path": str(transcript), "content_hash": "old"}},
                        current_snapshot=snapshot,
                        diff={"changes": ["transcript_changed"], "details": {}},
                        raw_bundle={}, transcript_path=transcript,
                    )

    def test_invalid_previous_analysis_is_rejected(self) -> None:
        with patch(
            "openai_api.llm.deal_incremental.validate_deal_analysis",
            side_effect=ValueError("invalid"),
        ):
            with self.assertRaisesRegex(IncrementalContextError, "previous_analysis_invalid"):
                build_incremental_context(
                    previous_state=self._state(), previous_snapshot={}, current_snapshot={"deal": {}},
                    diff={"changes": [], "details": {}}, raw_bundle={}, transcript_path=None,
                )


if __name__ == "__main__":
    unittest.main()
