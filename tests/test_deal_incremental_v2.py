from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.deal_evidence import collect_deal_evidence, coverage_from_evidence, evidence_delta
from openai_api.llm.deal_incremental_v2 import run_incremental_v2
from openai_api.llm.deal_semantic_dependencies import resolve_affected_sections
from openai_api.llm.deal_semantic_state import (
    SCHEMA_VERSION,
    bootstrap_semantic_state,
    validate_semantic_state_v1,
)
from storage.rop_db import (
    get_latest_deal_semantic_checkpoint,
    init_db,
    save_deal_incremental_v2_run,
    save_deal_semantic_checkpoint,
)


class DealIncrementalV2Tests(unittest.TestCase):
    def _transcript(self, directory: Path, activity_id: str, text: str, name: str) -> None:
        (directory / name).write_text(
            json.dumps({
                "metadata": {"activity_id": activity_id, "call_start": "2026-08-19T10:00:00+03:00"},
                "text": text,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_same_activity_in_different_representation_is_not_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._transcript(root, "648405", "Клиент подтвердил следующий шаг", "first.json")
            items = collect_deal_evidence({"deal_id": "18633"}, root)
            coverage = coverage_from_evidence(items)
            (root / "first.json").rename(root / "individual_call.json")
            same_items = collect_deal_evidence({"deal_id": "18633"}, root)
            delta, next_coverage = evidence_delta(same_items, coverage)
        self.assertEqual(delta, [])
        self.assertEqual(next_coverage, coverage)

    def test_new_activity_and_revision_are_distinguished(self) -> None:
        old = [{"evidence_id": "call:1", "kind": "call_transcript", "content_hash": "a", "occurred_at": "", "text": "a"}]
        coverage = coverage_from_evidence(old)
        current = [
            {"evidence_id": "call:1", "kind": "call_transcript", "content_hash": "b", "occurred_at": "", "text": "edited"},
            {"evidence_id": "call:2", "kind": "call_transcript", "content_hash": "c", "occurred_at": "", "text": "new"},
        ]
        delta, updated = evidence_delta(current, coverage)
        self.assertEqual([item["delta_kind"] for item in delta], ["evidence_revision", "new_evidence"])
        self.assertEqual(updated["call:1"]["revision"], 2)
        self.assertEqual(updated["call:2"]["revision"], 1)

    def test_deterministic_bootstrap_and_dependency_scope(self) -> None:
        analysis = {
            "deal_state": {"summary": "Открыта"},
            "deal_mode": {"mode": "active"},
            "deal_context": {
                "current_truth": {"status": "ожидание"},
                "critical_facts": [{"fact": "Бюджет согласован"}],
                "commitments": [], "decision_path": {}, "open_questions": [],
                "source_conflicts": [], "turning_points": [], "pain_points": [],
                "pressure_levers": [], "journey": [],
            },
            "qualification_assessment": {"bant": {}},
            "price_comparability_check": {}, "payment_blocker": {},
            "competitor_defense_checklist": {}, "money_path_diagnosis": {},
            "main_risk": {}, "client_communication_profile": {},
        }
        first = bootstrap_semantic_state(
            analysis, deal_id="7", source_analysis_run_id=1, source_fingerprint="fp",
            evidence_coverage={"call:1": {"content_hash": "a", "revision": 1}},
        )
        second = bootstrap_semantic_state(
            analysis, deal_id="7", source_analysis_run_id=1, source_fingerprint="fp",
            evidence_coverage={"call:1": {"content_hash": "a", "revision": 1}},
        )
        self.assertEqual(first, second)
        validate_semantic_state_v1(first)
        affected = resolve_affected_sections(["competitor_state"])
        self.assertIn("competitor_defense_checklist", affected)
        self.assertNotIn("payment_blocker", affected)

    def test_partial_merge_keeps_unaffected_section(self) -> None:
        previous_analysis = {"unaffected": {"exact": [1, 2]}, "main_risk": {"risk_level": "low"}}
        previous_state = bootstrap_semantic_state(
            {"deal_context": {}, "main_risk": {"risk_level": "low"}},
            deal_id="7", source_analysis_run_id=1, source_fingerprint="old", evidence_coverage={},
        )
        new_state = json.loads(json.dumps(previous_state, ensure_ascii=False))
        new_state["risk_state"] = {"risk_level": "high"}
        semantic_envelope = {
            "changed_domains": ["risk_state"],
            "change_reasons": {
                "risk_state": {
                    "reason": "new call changes risk",
                    "evidence_refs": ["call:2"],
                    "crm_change_types": [],
                }
            },
            "semantic_state": new_state,
        }
        materialized = {
            "sections": {
                key: ({"risk_level": "high"} if key == "main_risk" else {})
                for key in resolve_affected_sections(["risk_state"])
            }
        }
        with (
            patch("openai_api.llm.deal_incremental_v2.call_validated_analysis_json", side_effect=[(semantic_envelope, {}), (materialized, {})]),
            patch("openai_api.llm.deal_incremental_v2.normalize_analysis_for_validation"),
            patch("openai_api.llm.deal_incremental_v2.validate_deal_analysis"),
        ):
            result = run_incremental_v2(
                deal_id="7", previous_analysis=previous_analysis, previous_semantic_state=previous_state,
                evidence_delta=[{"evidence_id": "call:2", "kind": "call_transcript", "delta_kind": "new_evidence", "text": "new"}], next_evidence_coverage={},
                crm_delta={}, stage_policy={}, prior_recommendation=None, daily_checklist=None,
                source_fingerprint="new", model="test-model",
            )
        self.assertEqual(result.analysis["unaffected"], {"exact": [1, 2]})
        self.assertEqual(result.analysis["main_risk"], {"risk_level": "high"})

    def test_storage_is_append_only_and_optional_for_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.sqlite"
            init_db(db)
            state = bootstrap_semantic_state(
                {"deal_context": {}}, deal_id="7", source_analysis_run_id=None,
                source_fingerprint="fp", evidence_coverage={},
            )
            first = save_deal_semantic_checkpoint(
                db, entity_id="7", schema_version=SCHEMA_VERSION, source_analysis_run_id=None,
                source_fingerprint="fp", semantic_state=state, mode="shadow",
            )
            second = save_deal_semantic_checkpoint(
                db, entity_id="7", schema_version=SCHEMA_VERSION, source_analysis_run_id=None,
                source_fingerprint="fp2", semantic_state=state, mode="on",
            )
            save_deal_incremental_v2_run(db, entity_id="7", mode="shadow", evidence_ids=["call:1"])
            latest = get_latest_deal_semantic_checkpoint(db, "7", schema_version=SCHEMA_VERSION)
        self.assertGreater(second, first)
        self.assertEqual(latest["source_fingerprint"], "fp2")


if __name__ == "__main__":
    unittest.main()
