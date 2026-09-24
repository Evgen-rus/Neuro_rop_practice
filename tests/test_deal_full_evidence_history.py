"""FULL deal provenance and available evidence share one communication ledger.

Regression: the customer history bundle adds a lower CRM mirror id to a
confirmed client reply. FULL provenance must still name evidence that exists in
the available set, and a persist failure after a paid LLM call must not repeat
the paid call every scheduler cycle. No OpenAI or Bitrix calls.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_api.change_detection.decision_engine import ERROR
from openai_api.llm import analyze_deal, analyze_deal_if_changed
from openai_api.llm.deal_evidence import (
    EvidenceDeltaError,
    collect_deal_evidence,
    coverage_for_included_evidence,
    inbound_evidence_ids_present_in_prompt,
    workspace_normalized_communications,
)
from storage.rop_db import get_entity_state, get_latest_analysis_run, list_analysis_runs, save_analysis_run

DEAL_ID = "7"
CLIENT_TEXT = "Пришлите, пожалуйста, счёт на станок"


def _reply(source_ids: list[str]) -> dict:
    return {
        "event_id": "crm_mirror:286f26eb8895d82c",
        "source_ids": source_ids,
        "occurred_at": "2026-09-23T17:40:00+03:00",
        "channel": "max",
        "direction": "incoming",
        "participant_role": "client",
        "contact_class": "confirmed_contact",
        "content": CLIENT_TEXT,
    }


RAW_ONLY_EVENTS = [_reply(["3135187"])]
WITH_HISTORY_EVENTS = [_reply(["3135185", "3135187"])]
PROMPT = f"## ID СДЕЛКИ\n{DEAL_ID}\n## История\n[3135187] Клиент (max): {CLIENT_TEXT}\n"
CANONICAL_STATE = {
    "schema_id": "canonical_bitrix_state",
    "schema_version": "1",
    "owner": {"entity_type": "deal", "entity_id": DEAL_ID},
    "semantic_fingerprint": "canonical-fp",
    "entities": {},
}


def _normalized(_raw_bundle, history=None):
    return copy.deepcopy(WITH_HISTORY_EVENTS if isinstance(history, dict) else RAW_ONLY_EVENTS)


class DealFullEvidenceHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.deal_dir = self.root / f"deal_{DEAL_ID}"
        raw_dir = self.deal_dir / "raw"
        raw_dir.mkdir(parents=True)
        self.raw_bundle = {"deal_id": DEAL_ID, "deal": {"item": {"ID": DEAL_ID}}}
        (raw_dir / f"deal_{DEAL_ID}_context.json").write_text(
            json.dumps(self.raw_bundle, ensure_ascii=False), encoding="utf-8",
        )
        (raw_dir / f"deal_{DEAL_ID}_customer_history_bundle.json").write_text(
            json.dumps({"bundle_type": "customer_history_bundle"}), encoding="utf-8",
        )
        self.db_path = self.root / "state.sqlite"
        for item in (
            patch("openai_api.llm.deal_evidence.build_deal_normalized_communications", side_effect=_normalized),
            patch(
                "openai_api.llm.deal_evidence.include_source_lead_communications",
                side_effect=lambda bundle, _context, *, deal_id: bundle,
            ),
            patch("openai_api.llm.deal_evidence.transcript_items", return_value=[]),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _full_included_ids(self) -> list[str]:
        bundle = analyze_deal.load_raw_bundle_for_prompt_provenance(self.deal_dir, DEAL_ID)
        return inbound_evidence_ids_present_in_prompt(bundle, PROMPT)

    def _available_evidence(self) -> list[dict]:
        communications = workspace_normalized_communications(self.deal_dir, DEAL_ID, self.raw_bundle)
        with patch.object(analyze_deal_if_changed, "merge_deal_bundle", return_value=(CANONICAL_STATE, {})):
            return analyze_deal_if_changed.stage5_inputs(
                self.db_path,
                deal_id=DEAL_ID,
                raw_bundle=self.raw_bundle,
                current_deal_dir=self.deal_dir,
                baseline=None,
                normalized_communications=communications,
            )[3]

    def test_full_included_ids_are_subset_of_available_evidence(self) -> None:
        raw_only_ids = {item["evidence_id"] for item in collect_deal_evidence(self.raw_bundle, "unused")}
        included = self._full_included_ids()
        available = {item["evidence_id"] for item in self._available_evidence()}

        self.assertEqual(raw_only_ids, {"message:3135187"})
        self.assertEqual(included, ["message:3135185"])
        self.assertLessEqual(set(included), available)
        self.assertEqual(set(coverage_for_included_evidence(self._available_evidence(), included)), set(included))

    def test_prompt_match_requires_client_text(self) -> None:
        bundle = analyze_deal.load_raw_bundle_for_prompt_provenance(self.deal_dir, DEAL_ID)
        self.assertEqual(inbound_evidence_ids_present_in_prompt(bundle, "[3135187] другой текст"), [])

    def _run_main(self, *, analyzer, force_llm: bool = False) -> None:
        args = SimpleNamespace(
            deal_id=DEAL_ID, deal_root=str(self.root), db_path=str(self.db_path),
            transcript="none", model=None, force_llm=force_llm, dry_run_decision=False,
        )
        with (
            patch.object(analyze_deal_if_changed, "parse_args", return_value=args),
            patch.object(analyze_deal_if_changed, "load_dotenv"),
            patch.object(analyze_deal_if_changed, "resolve_transcript_for_snapshot", return_value=(None, "none")),
            patch.object(analyze_deal_if_changed, "load_daily_quality_context", return_value={}),
            patch.object(analyze_deal_if_changed, "build_deal_snapshot", return_value={"deal": {"id": DEAL_ID}}),
            patch.object(analyze_deal_if_changed, "fingerprint_snapshot", return_value="fp-1"),
            patch.object(analyze_deal_if_changed, "merge_deal_bundle", return_value=(CANONICAL_STATE, {"entries": []})),
            patch.object(analyze_deal_if_changed, "emit_deal_publish_ready"),
            patch.object(analyze_deal_if_changed, "run_existing_analyzer", side_effect=analyzer) as run_analyzer,
        ):
            try:
                analyze_deal_if_changed.main()
            finally:
                self.analyzer_calls = run_analyzer.call_count

    def _write_full_payload(self, *_args, **_kwargs) -> None:
        analysis_dir = self.deal_dir / "analysis"
        analysis_dir.mkdir(exist_ok=True)
        payload = {
            "deal_id": DEAL_ID,
            "analysis_mode": "full",
            "evidence_ids_included": self._full_included_ids(),
            "model_metadata": {"model": "test-model"},
            "analysis": {"main_risk": {"risk_level": "medium"}},
        }
        (analysis_dir / f"deal_{DEAL_ID}_analysis.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )

    def test_full_persist_succeeds_and_next_run_skips_llm(self) -> None:
        self._run_main(analyzer=self._write_full_payload)
        self.assertEqual(self.analyzer_calls, 1)
        state = get_entity_state(self.db_path, "deal", DEAL_ID)
        self.assertEqual(state["current_fingerprint"], "fp-1")
        published = get_latest_analysis_run(self.db_path, entity_type="deal", entity_id=DEAL_ID)
        self.assertEqual(published["evidence_ids_included"], ["message:3135185"])
        self.assertIn("message:3135185", published["evidence_coverage"])

        self._run_main(analyzer=self._write_full_payload)
        self.assertEqual(self.analyzer_calls, 0)

    def test_persist_failure_after_paid_llm_blocks_automatic_retry(self) -> None:
        with patch.object(
            analyze_deal_if_changed,
            "persist_successful_llm_run",
            side_effect=EvidenceDeltaError("included_evidence_content_missing"),
        ):
            with self.assertRaises(EvidenceDeltaError):
                self._run_main(analyzer=self._write_full_payload)
            self.assertEqual(self.analyzer_calls, 1)
            failed = get_latest_analysis_run(self.db_path, entity_type="deal", entity_id=DEAL_ID)
            self.assertEqual(failed["status"], ERROR)
            self.assertEqual(failed["fingerprint"], "fp-1")
            self.assertEqual(
                failed["decision_reason"]["failure_stage"],
                analyze_deal_if_changed.PAID_PERSIST_FAILURE_STAGE,
            )

            with self.assertRaises(analyze_deal_if_changed.PaidRetrySuppressedError):
                self._run_main(analyzer=self._write_full_payload)
            self.assertEqual(self.analyzer_calls, 0)
            with self.assertRaises(analyze_deal_if_changed.PaidRetrySuppressedError):
                self._run_main(analyzer=self._write_full_payload)
            self.assertEqual(self.analyzer_calls, 0)

        runs = list_analysis_runs(self.db_path, entity_type="deal", entity_ids=[DEAL_ID])
        self.assertEqual([run["fingerprint"] for run in runs], ["fp-1", None, None])

        self._run_main(analyzer=self._write_full_payload, force_llm=True)
        self.assertEqual(self.analyzer_calls, 1)
        self.assertEqual(get_entity_state(self.db_path, "deal", DEAL_ID)["current_fingerprint"], "fp-1")

    def test_ordinary_analyzer_error_does_not_block_retry(self) -> None:
        with self.assertRaises(RuntimeError):
            self._run_main(analyzer=RuntimeError("synthetic transport failure"))
        failed = get_latest_analysis_run(self.db_path, entity_type="deal", entity_id=DEAL_ID)
        self.assertIsNone(failed["fingerprint"])
        self.assertIsNone(analyze_deal_if_changed.recent_paid_persist_failure(self.db_path, DEAL_ID, "fp-1"))

    def test_paid_failure_block_expires_and_is_fingerprint_scoped(self) -> None:
        run_id = save_analysis_run(
            self.db_path,
            entity_type="deal",
            entity_id=DEAL_ID,
            status=ERROR,
            fingerprint="fp-1",
            decision_reason={"failure_stage": analyze_deal_if_changed.PAID_PERSIST_FAILURE_STAGE},
        )
        created_at = datetime.fromisoformat(
            get_latest_analysis_run(self.db_path, entity_type="deal", entity_id=DEAL_ID)["created_at"],
        )
        block = analyze_deal_if_changed.recent_paid_persist_failure
        self.assertEqual(block(self.db_path, DEAL_ID, "fp-1")["id"], run_id)
        self.assertIsNone(block(self.db_path, DEAL_ID, "fp-2"))
        self.assertIsNone(block(
            self.db_path, DEAL_ID, "fp-1",
            now=created_at + analyze_deal_if_changed.PAID_PERSIST_FAILURE_RETRY_AFTER + timedelta(seconds=1),
        ))


if __name__ == "__main__":
    unittest.main()
