from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from openai_api.change_detection.decision_engine import FULL_LLM_ANALYSIS
from openai_api.llm.trusted_baseline import get_trusted_deal_baseline
from openai_api.llm.validation import DEAL_REQUIRED_FIELDS, normalize_analysis_for_validation
from storage.rop_db import save_analysis_run, upsert_entity_state
from test_lead_qualification_assessment import lead_analysis


PROMPT_VERSION = "synthetic-prompt-v1"
LOGIC_VERSION = "synthetic-logic-v1"


def _valid_analysis() -> dict:
    analysis = {key: {} for key in DEAL_REQUIRED_FIELDS}
    lead = deepcopy(lead_analysis())
    for key in ("rop_manager_message_block", "manager_action_block", "qualification_assessment", "main_risk"):
        analysis[key] = lead[key]
    analysis["qualification_assessment"].pop("lead_category", None)
    analysis["qualification_assessment"].pop("lead_route", None)
    analysis.update(deal_id="7", what_changed=[], communication_quality_audit={})
    normalize_analysis_for_validation(analysis)
    return analysis


def _persist_candidate(db_path: Path, **overrides) -> int:
    payload_run_id = overrides.pop("payload_run_id", None)
    analysis_mode = overrides.pop("analysis_mode", "full")
    analysis = overrides.pop("analysis", None)
    values = {
        "status": FULL_LLM_ANALYSIS,
        "fingerprint": "canonical-fp-1",
        "prompt_version": PROMPT_VERSION,
        "logic_version": LOGIC_VERSION,
        "provenance": {"snapshot_fingerprint": "canonical-fp-1"},
        "evidence_ids_included": ["call:201"],
        "evidence_coverage": {
            "call:201": {"content_hash": "hash-v1", "revision": 1, "kind": "call_transcript"},
        },
        "canonical_state": {
            "schema_id": "canonical_bitrix_state",
            "schema_version": "1",
            "owner": {"entity_type": "deal", "entity_id": "7"},
            "semantic_fingerprint": "canonical-semantic-fp-1",
        },
    }
    values.update(overrides)
    run_id = save_analysis_run(db_path, entity_type="deal", entity_id="7", **values)
    payload = {
        "analysis_run_id": run_id if payload_run_id is None else payload_run_id,
        "analysis_mode": analysis_mode,
        "analysis": _valid_analysis() if analysis is None else analysis,
    }
    upsert_entity_state(
        db_path,
        entity_type="deal",
        entity_id="7",
        fingerprint="current-fp-may-advance",
        snapshot={},
        last_analysis_status=values["status"],
        last_analysis=payload,
    )
    return run_id


class TrustedDealBaselineContractTests(unittest.TestCase):
    def test_complete_compatible_valid_run_is_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            run_id = _persist_candidate(db_path)
            baseline = get_trusted_deal_baseline(
                db_path,
                "7",
                expected_prompt_version=PROMPT_VERSION,
                expected_logic_version=LOGIC_VERSION,
            )
        self.assertEqual(baseline["analysis_run_id"], run_id)
        self.assertEqual(baseline["fingerprint"], "canonical-fp-1")
        self.assertEqual(baseline["canonical_fingerprint"], "canonical-semantic-fp-1")
        self.assertEqual(baseline["evidence_coverage"]["call:201"]["revision"], 1)

    def test_unsafe_or_incompatible_run_is_not_trusted(self) -> None:
        cases = {
            "failed": {"status": "error"},
            "missing coverage": {"evidence_coverage": None},
            "missing included ids": {"evidence_ids_included": None},
            "coverage mismatch": {"evidence_ids_included": ["email:301"]},
            "missing canonical": {"canonical_state": None},
            "invalid coverage": {"evidence_coverage": {"call:201": {"revision": 1}}},
            "wrong provenance": {"provenance": {"snapshot_fingerprint": "other"}},
            "invalid analysis": {"analysis": {}},
            "wrong mode": {"analysis_mode": "mini"},
            "wrong linkage": {"payload_run_id": -1},
        }
        for label, overrides in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "state.sqlite"
                _persist_candidate(db_path, **overrides)
                baseline = get_trusted_deal_baseline(
                    db_path,
                    "7",
                    expected_prompt_version=PROMPT_VERSION,
                    expected_logic_version=LOGIC_VERSION,
                )
                self.assertIsNone(baseline)

    def test_version_mismatch_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _persist_candidate(db_path)
            self.assertIsNone(get_trusted_deal_baseline(
                db_path,
                "7",
                expected_prompt_version="other",
                expected_logic_version=LOGIC_VERSION,
            ))

    def test_newer_skip_run_does_not_hide_trusted_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            run_id = _persist_candidate(db_path)
            save_analysis_run(db_path, entity_type="deal", entity_id="7", status="SKIPPED_NO_CHANGES")
            baseline = get_trusted_deal_baseline(
                db_path,
                "7",
                expected_prompt_version=PROMPT_VERSION,
                expected_logic_version=LOGIC_VERSION,
            )
        self.assertEqual(baseline["analysis_run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
