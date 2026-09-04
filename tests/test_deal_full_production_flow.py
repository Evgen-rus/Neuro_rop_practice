"""Characterization tests for the current production FULL deal-analysis path.

These lock the change-aware decision, FULL analyzer, validation, persistence,
JSON contract and readable legacy incremental rows. They must not call OpenAI.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_api.change_detection.decision_engine import (
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    ProcessingDecision,
    decide_deal_processing,
)
from openai_api.llm import analyze_deal_if_changed
from openai_api.llm.analyze_deal import DEAL_PROMPT_CACHE_KEY, build_prompt
from openai_api.llm.validation import (
    DEAL_REQUIRED_FIELDS,
    normalize_analysis_for_validation,
    validate_deal_analysis,
)
from progress_events import compact_decision_status
from storage.rop_db import (
    connect,
    get_entity_state,
    get_latest_deal_semantic_checkpoint,
    init_db,
    list_analysis_runs,
    save_analysis_run,
    save_deal_incremental_v2_run,
    save_deal_semantic_checkpoint,
)
from test_lead_qualification_assessment import lead_analysis


def _previous_state() -> dict:
    return {"current_fingerprint": "old", "last_analysis": {}}


def _snapshot() -> dict:
    return {"deal": {"closed": "N"}, "activities": [], "timeline_comments": [], "commercial": {}}


class DealFullProductionFlowTests(unittest.TestCase):
    def test_missing_state_decides_first_full(self) -> None:
        decision = decide_deal_processing(
            previous_state=None,
            current_snapshot=_snapshot(),
            fingerprint="new",
            diff={"changes": []},
        )
        self.assertEqual(decision.status, FIRST_FULL_ANALYSIS)

    def test_client_evidence_starts_paid_analysis_not_mini(self) -> None:
        decision = decide_deal_processing(
            previous_state=_previous_state(),
            current_snapshot=_snapshot(),
            fingerprint="new",
            diff={"changes": ["transcript_changed"], "details": {}},
        )
        self.assertIn(decision.status, {FULL_LLM_ANALYSIS, INCREMENTAL_LLM_ANALYSIS, FIRST_FULL_ANALYSIS})
        self.assertNotEqual(decision.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertEqual(compact_decision_status(decision.status), "full")

    def test_closed_deal_is_direct_full(self) -> None:
        decision = decide_deal_processing(
            previous_state=_previous_state(),
            current_snapshot={"deal": {"closed": "Y"}, "activities": [], "timeline_comments": [], "commercial": {}},
            fingerprint="new",
            diff={"changes": ["stage_changed", "closed_flag_changed"], "details": {}},
        )
        self.assertEqual(decision.status, FULL_LLM_ANALYSIS)

    def test_production_flags_run_full_analyzer_without_incremental_context(self) -> None:
        """Legacy INCREMENTAL decision is published through the FULL analyzer."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                deal_id="7",
                deal_root=str(root),
                db_path=str(root / "state.sqlite"),
                transcript="none",
                model=None,
                force_llm=False,
                dry_run_decision=False,
            )
            decision = ProcessingDecision(
                status=INCREMENTAL_LLM_ANALYSIS,
                reasons=["new evidence"],
                triggers=[],
                diff={"changes": ["transcript_changed"], "details": {}},
            )
            with (
                patch.object(analyze_deal_if_changed, "parse_args", return_value=args),
                patch.object(analyze_deal_if_changed, "load_dotenv"),
                patch.object(analyze_deal_if_changed, "init_db"),
                patch.object(analyze_deal_if_changed, "raw_bundle_path", return_value=root / "raw.json"),
                patch.object(analyze_deal_if_changed, "resolve_transcript_for_snapshot", return_value=(None, "none")),
                patch.object(analyze_deal_if_changed, "load_json", return_value={}),
                patch.object(analyze_deal_if_changed, "build_deal_snapshot", return_value={"deal": {}}),
                patch.object(analyze_deal_if_changed, "fingerprint_snapshot", return_value="new"),
                patch.object(analyze_deal_if_changed, "get_entity_state", return_value={"snapshot": {}, "last_analysis": {}}),
                patch.object(analyze_deal_if_changed, "compare_snapshots", return_value=decision.diff),
                patch.object(analyze_deal_if_changed, "get_entity_memory", return_value=None),
                patch.object(analyze_deal_if_changed, "decide_deal_processing", return_value=decision),
                patch.object(analyze_deal_if_changed, "persist_successful_llm_run", return_value=1),
                patch.object(analyze_deal_if_changed, "emit_deal_publish_ready"),
                patch.object(analyze_deal_if_changed, "run_existing_analyzer") as analyzer,
            ):
                analyze_deal_if_changed.main()
            analyzer.assert_called_once()
            self.assertEqual(analyzer.call_args.args[1], "none")

    def test_persist_full_run_writes_analysis_run_and_entity_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "state.sqlite"
            analysis_dir = root / "analysis"
            analysis_dir.mkdir()
            analysis_path = analysis_dir / "deal_7_analysis.json"
            report_path = analysis_dir / "deal_7_rop_report.md"
            raw_path = analysis_dir / "deal_7_raw_model_output.txt"
            payload = {
                "deal_id": "7",
                "analysis_mode": "full",
                "model_metadata": {"model": "test-model"},
                "analysis": {"main_risk": {"risk_level": "medium"}},
            }
            analysis_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            report_path.write_text("отчёт", encoding="utf-8")
            raw_path.write_text("", encoding="utf-8")
            args = SimpleNamespace(deal_id="7", deal_root=str(root), model=None)
            snapshot = {"deal": {"id": "7"}}
            run_id = analyze_deal_if_changed.persist_successful_llm_run(
                db_path=db_path,
                args=args,
                fingerprint="fp-1",
                snapshot=snapshot,
                decision_status=FULL_LLM_ANALYSIS,
                paths={"analysis": analysis_path, "report": report_path, "raw": raw_path},
                decision_reason={"status": FULL_LLM_ANALYSIS, "reasons": ["тест"], "diff": {}},
            )
            saved = json.loads(analysis_path.read_text(encoding="utf-8"))
            state = get_entity_state(db_path, "deal", "7")
            runs = list_analysis_runs(db_path, entity_type="deal", entity_ids=["7"])
        self.assertEqual(run_id, saved["analysis_run_id"])
        self.assertEqual(saved["analysis_mode"], "full")
        self.assertEqual(state["last_analysis_status"], FULL_LLM_ANALYSIS)
        self.assertEqual(state["current_fingerprint"], "fp-1")
        self.assertEqual(runs[0]["status"], FULL_LLM_ANALYSIS)
        self.assertEqual(runs[0]["id"], run_id)

    def test_legacy_incremental_run_and_v2_rows_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            init_db(db_path)
            run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="7",
                status=INCREMENTAL_LLM_ANALYSIS,
                fingerprint="legacy-fp",
            )
            checkpoint_id = save_deal_semantic_checkpoint(
                db_path,
                entity_id="7",
                schema_version="deal-semantic-state-v1",
                source_analysis_run_id=run_id,
                source_fingerprint="legacy-fp",
                semantic_state={"schema_version": "deal-semantic-state-v1"},
                mode="shadow",
                baseline_snapshot={"deal": {"id": "7"}},
            )
            v2_run_id = save_deal_incremental_v2_run(
                db_path,
                entity_id="7",
                mode="shadow",
                source_analysis_run_id=run_id,
            )
            runs = list_analysis_runs(db_path, entity_type="deal", entity_ids=["7"])
            checkpoint = get_latest_deal_semantic_checkpoint(db_path, "7")
            with connect(db_path) as conn:
                v2_row = conn.execute(
                    "SELECT id, mode FROM deal_incremental_v2_runs WHERE id = ?",
                    (v2_run_id,),
                ).fetchone()
        self.assertEqual(runs[0]["status"], INCREMENTAL_LLM_ANALYSIS)
        self.assertEqual(compact_decision_status(runs[0]["status"]), "full")
        self.assertEqual(checkpoint["id"], checkpoint_id)
        self.assertEqual(checkpoint["baseline_snapshot"], {"deal": {"id": "7"}})
        self.assertEqual(dict(v2_row), {"id": v2_run_id, "mode": "shadow"})

    def test_full_prompt_contract_keeps_cache_key_and_analysis_mode_full(self) -> None:
        prompt = build_prompt("7", "История сделки", "Транскрипт", "Диагностика", [], {})
        self.assertEqual(DEAL_PROMPT_CACHE_KEY, "neuro-rop:full-deal:v3")
        self.assertIn("## ID СДЕЛКИ", prompt)
        self.assertIn("История сделки", prompt)
        self.assertNotIn("PREVIOUS_ANALYSIS", prompt)
        self.assertNotIn("<incremental_analysis_rules>", prompt)

    def test_valid_deal_analysis_passes_canonical_validator(self) -> None:
        analysis = {key: {} for key in DEAL_REQUIRED_FIELDS}
        lead = lead_analysis()
        for key in ("rop_manager_message_block", "manager_action_block", "qualification_assessment", "main_risk"):
            analysis[key] = lead[key]
        analysis["qualification_assessment"].pop("lead_category", None)
        analysis["qualification_assessment"].pop("lead_route", None)
        analysis.update(deal_id="7", what_changed=[], communication_quality_audit={})
        normalize_analysis_for_validation(analysis)
        validate_deal_analysis(analysis)
        self.assertNotIn("daily_checklist_update", analysis)
        self.assertNotIn("manager_checklist", analysis.get("manager_action_block") or {})


if __name__ == "__main__":
    unittest.main()
