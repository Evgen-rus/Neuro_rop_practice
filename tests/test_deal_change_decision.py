from __future__ import annotations

import unittest

from openai_api.change_detection.decision_engine import (
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    decide_deal_processing,
)


def snapshot(*, closed: str = "N", activities=None) -> dict:
    return {
        "deal": {"closed": closed},
        "activities": activities or [],
        "timeline_comments": [],
        "commercial": {},
    }


def decision(diff: dict, current: dict):
    return decide_deal_processing(
        previous_state={"current_fingerprint": "old", "last_analysis": {}},
        current_snapshot=current,
        fingerprint="new",
        diff=diff,
        last_memory={"next_actions_update": ["Следующий шаг есть"]},
    )


class DealChangeDecisionTests(unittest.TestCase):
    def test_disabling_structured_commercial_sources_does_not_signal_a_deal_change(self):
        from openai_api.change_detection.snapshot import build_deal_snapshot, compare_snapshots
        legacy = {"deal": {"item": {"ID": "7", "CLOSED": "N"}},
                  "product_rows": {"ok": True, "response": {"result": [{"ID": "1", "PRICE": "500"}]}},
                  "invoice_attempts": [{"ok": True, "items": [{"ID": "2", "PRICE": "500"}]}]}
        previous = build_deal_snapshot(legacy)
        self.assertEqual(previous["commercial"]["product_rows_count"], 1)
        self.assertEqual(previous["commercial"]["invoice_refs_count"], 1)
        # Actual legacy stored snapshots predate the new metadata field.
        previous["commercial"].pop("structured_sources_enabled")
        current = build_deal_snapshot({**legacy, "structured_commercial_sources_enabled": False,
                                       "product_rows": None, "invoice_attempts": []})
        diff = compare_snapshots(previous, current)
        self.assertFalse(diff["has_semantic_changes"], diff)
        baseline = decision(compare_snapshots(previous, previous), previous)
        result = decision(diff, current)
        self.assertEqual(result.status, baseline.status)
        self.assertNotIn(result.status, (FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS))
        # Disabling structured sources must not hide changes in commercial files.
        current["commercial"]["commercial_file_refs_hash"] = "new-commercial-file"
        diff = compare_snapshots(previous, current)
        self.assertEqual(diff["details"]["commercial_refs_changed"], ["commercial_file_refs_hash"])

    def test_internal_context_change_is_mini_not_client_evidence(self):
        from openai_api.change_detection.snapshot import compare_snapshots
        previous = {**snapshot(), "operational_context_fingerprint": "old"}
        current = {**snapshot(), "operational_context_fingerprint": "new"}
        diff = compare_snapshots(previous, current)
        self.assertIn("operational_context_changed", diff["changes"])
        result = decision(diff, current)
        self.assertEqual(result.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertIn("operational_context_changed_without_llm", [row["trigger_type"] for row in result.triggers])

    def test_task_and_stage_change_do_not_start_paid_deal_analysis(self):
        current = snapshot()
        task = decision({"changes": ["new_task"], "details": {"new_activity_ids": ["1"]}}, current)
        stage = decision({"changes": ["stage_changed"], "details": {}}, current)
        self.assertEqual(task.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertEqual(stage.status, MINI_RECOMMENDATION_NO_LLM)

    def test_new_call_without_transcript_does_not_start_paid_analysis(self):
        current = snapshot(activities=[{"id": "1", "kind": "call", "direction": "2"}])
        result = decision({"changes": ["new_activity", "new_call"], "details": {"new_activity_ids": ["1"]}}, current)
        self.assertEqual(result.status, MINI_RECOMMENDATION_NO_LLM)

    def test_transcript_and_inbound_customer_message_start_full_analysis(self):
        transcript = decision({"changes": ["transcript_changed"], "details": {}}, snapshot())
        message = decision(
            {"changes": ["new_activity", "new_email"], "details": {"new_activity_ids": ["2"]}},
            snapshot(activities=[{"id": "2", "kind": "email", "direction": "1"}]),
        )
        self.assertEqual(transcript.status, INCREMENTAL_LLM_ANALYSIS)
        self.assertEqual(message.status, INCREMENTAL_LLM_ANALYSIS)

    def test_non_meaningful_transcript_change_stays_local(self):
        result = decision({"changes": ["transcript_changed_non_meaningful"], "details": {}}, snapshot())
        self.assertEqual(result.status, MINI_RECOMMENDATION_NO_LLM)

    def test_outgoing_message_and_internal_comment_stay_local(self):
        outgoing = decision(
            {"changes": ["new_activity", "new_message"], "details": {"new_activity_ids": ["2"]}},
            snapshot(activities=[{"id": "2", "kind": "message", "direction": "2"}]),
        )
        comment = decision({"changes": ["new_comment"], "details": {"new_comment_ids": ["3"]}}, snapshot())
        self.assertEqual(outgoing.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertEqual(comment.status, MINI_RECOMMENDATION_NO_LLM)

    def test_closed_deal_and_substantial_amount_change_start_full_analysis(self):
        closed = decision({"changes": ["stage_changed", "closed_flag_changed"], "details": {}}, snapshot(closed="Y"))
        amount = decision(
            {"changes": ["amount_changed"], "details": {"amount_changed": {"before": "100000", "after": "120000"}}},
            snapshot(),
        )
        minor = decision(
            {"changes": ["amount_changed"], "details": {"amount_changed": {"before": "100000", "after": "105000"}}},
            snapshot(),
        )
        self.assertEqual(closed.status, FULL_LLM_ANALYSIS)
        self.assertEqual(amount.status, FULL_LLM_ANALYSIS)
        self.assertEqual(minor.status, MINI_RECOMMENDATION_NO_LLM)
