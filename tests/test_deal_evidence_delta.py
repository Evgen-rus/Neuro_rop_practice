from __future__ import annotations

import unittest
from unittest.mock import patch

from openai_api.change_detection.snapshot import text_hash
from openai_api.llm.deal_evidence import (
    EvidenceDeltaError,
    collect_deal_evidence,
    coverage_for_included_evidence,
    evidence_delta,
    mentionable_audio_reference_ids,
)


class DealEvidenceDeltaContractTests(unittest.TestCase):
    def test_full_coverage_contains_only_evidence_actually_in_prompt(self) -> None:
        items = [
            {"evidence_id": "call:1", "content_hash": "h1", "kind": "call_transcript"},
            {"evidence_id": "email:2", "content_hash": "h2", "kind": "inbound_email"},
        ]
        self.assertEqual(set(coverage_for_included_evidence(items, ["call:1"])), {"call:1"})
        with self.assertRaisesRegex(EvidenceDeltaError, "included_evidence_content_missing"):
            coverage_for_included_evidence(items, ["message:3"])

    def test_trusted_coverage_is_required(self) -> None:
        with self.assertRaisesRegex(EvidenceDeltaError, "trusted_evidence_coverage_missing"):
            evidence_delta([], None)

    def test_new_unchanged_and_revised_evidence(self) -> None:
        first = {
            "evidence_id": "email:101",
            "kind": "inbound_email",
            "content_hash": "hash-v1",
            "text": "synthetic-v1",
        }
        new_delta, coverage_v1 = evidence_delta([first], {})
        self.assertEqual(new_delta[0]["delta_kind"], "new_evidence")
        self.assertEqual(new_delta[0]["revision"], 1)

        unchanged_delta, unchanged_coverage = evidence_delta([first], coverage_v1)
        self.assertEqual(unchanged_delta, [])
        self.assertEqual(unchanged_coverage, coverage_v1)

        revised = {**first, "content_hash": "hash-v2", "text": "synthetic-v2"}
        revision_delta, coverage_v2 = evidence_delta([revised], coverage_v1)
        self.assertEqual(revision_delta[0]["delta_kind"], "evidence_revision")
        self.assertEqual(revision_delta[0]["revision"], 2)
        self.assertEqual(coverage_v2["email:101"]["content_hash"], "hash-v2")

        retry_delta, _ = evidence_delta([revised], coverage_v1)
        self.assertEqual(retry_delta, revision_delta)

    @patch("openai_api.llm.deal_evidence.transcript_items")
    def test_activity_ids_are_evidence_identity_and_outbound_is_excluded(self, transcript_items) -> None:
        transcript_items.return_value = [{
            "activity_id": "201",
            "text": "synthetic-call-v1",
            "call_start": "2026-01-01T10:00:00+03:00",
        }]
        raw_bundle = {
            "deal_id": "1",
            "activities": {"items": [
                {
                    "ID": "301",
                    "TYPE_ID": "4",
                    "PROVIDER_ID": "CRM_EMAIL",
                    "DIRECTION": "1",
                    "DESCRIPTION": "synthetic-email",
                },
                {
                    "ID": "302",
                    "TYPE_ID": "1",
                    "PROVIDER_ID": "IMOPENLINES",
                    "DIRECTION": "1",
                    "DESCRIPTION": "synthetic-message",
                },
                {
                    "ID": "303",
                    "TYPE_ID": "4",
                    "PROVIDER_ID": "CRM_EMAIL",
                    "DIRECTION": "2",
                    "DESCRIPTION": "synthetic-outbound",
                },
            ]},
        }

        evidence = collect_deal_evidence(raw_bundle, "unused")
        self.assertEqual(
            {item["evidence_id"] for item in evidence},
            {"call:201", "email:301", "message:302"},
        )

    @patch("openai_api.llm.deal_evidence.transcript_items", return_value=[])
    def test_confirmed_inbound_messenger_mirror_is_evidence(self, _transcript_items) -> None:
        event = {
            "source_ids": ["401"],
            "occurred_at": "2026-01-01T10:00:00+03:00",
            "channel": "max",
            "direction": "incoming",
            "participant_role": "client",
            "contact_class": "confirmed_contact",
            "content": "synthetic-client-message",
        }
        outbound = {**event, "source_ids": ["402"], "direction": "outgoing", "participant_role": "employee"}
        evidence = collect_deal_evidence(
            {"deal_id": "1", "normalized_communications": [event, outbound]},
            "unused",
        )
        self.assertEqual([item["evidence_id"] for item in evidence], ["message:401"])

    @patch("openai_api.llm.deal_evidence.transcript_items")
    def test_same_call_transcript_is_omitted_until_content_changes(self, transcript_items) -> None:
        transcript_items.return_value = [{"activity_id": "201", "text": "synthetic-call-v1"}]
        first = collect_deal_evidence({"deal_id": "1"}, "unused")
        first_delta, coverage = evidence_delta(first, {})
        self.assertEqual(first_delta[0]["evidence_id"], "call:201")

        unchanged = collect_deal_evidence({"deal_id": "1"}, "unused")
        self.assertEqual(evidence_delta(unchanged, coverage)[0], [])

        transcript_items.return_value = [{"activity_id": "201", "text": "synthetic-call-v2"}]
        revised = collect_deal_evidence({"deal_id": "1"}, "unused")
        revision_delta, _ = evidence_delta(revised, coverage)
        self.assertEqual(revision_delta[0]["evidence_id"], "call:201")
        self.assertEqual(revision_delta[0]["delta_kind"], "evidence_revision")
        self.assertEqual(revision_delta[0]["revision"], 2)
        self.assertEqual(revision_delta[0]["content_hash"], text_hash("synthetic-call-v2"))

    def test_mentionable_audio_ids_are_calls_and_max_voice_not_emails(self) -> None:
        ids = mentionable_audio_reference_ids(
            canonical_state={
                "entities": {
                    "activity:667045": {
                        "entity_type": "activity",
                        "subtype": "call",
                        "source_id": "667045",
                    },
                    "activity:655627": {
                        "entity_type": "activity",
                        "subtype": "email",
                        "source_id": "655627",
                    },
                }
            },
            manifest_calls=[{
                "audio_kind": "max_voice",
                "timeline_comment_id": "3083729",
                "activity_id": "max_3083729_abc",
            }],
            available_evidence=[{"evidence_id": "call:201"}],
        )
        self.assertEqual(ids, ["call:201", "call:3083729", "call:667045", "message:3083729"])


if __name__ == "__main__":
    unittest.main()
