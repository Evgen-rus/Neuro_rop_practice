from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai_api.change_detection.decision_engine import INCREMENTAL_LLM_ANALYSIS, ProcessingDecision
from openai_api.llm import analyze_deal, analyze_deal_if_changed
from openai_api.llm.deal_incremental import IncrementalContextError


class DealChangeCliTests(unittest.TestCase):
    def _run_incremental_main(
        self,
        root: Path,
        *,
        enabled: bool,
        shadow: bool = False,
        force_full: bool = False,
        context_error: Exception | None = None,
        analyzer_side_effect=None,
    ):
        args = SimpleNamespace(
            deal_id="7", deal_root=str(root), db_path=str(root / "state.sqlite"),
            transcript="none", model=None, force_llm=False, dry_run_decision=False,
        )
        decision = ProcessingDecision(
            status=INCREMENTAL_LLM_ANALYSIS,
            reasons=["new evidence"], triggers=[],
            diff={"changes": ["transcript_changed"], "details": {}},
        )
        context_side_effect = context_error or {
            "previous_analysis": {}, "new_events": [{"type": "transcript"}], "crm_delta": {}
        }
        patches = (
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
            patch.object(analyze_deal_if_changed, "build_incremental_context", side_effect=context_side_effect if context_error else None, return_value=None if context_error else context_side_effect),
            patch.object(analyze_deal_if_changed, "persist_successful_llm_run", return_value=1),
            patch.object(analyze_deal_if_changed, "emit_deal_publish_ready"),
            patch.object(analyze_deal_if_changed, "run_existing_analyzer", side_effect=analyzer_side_effect),
            patch.object(analyze_deal_if_changed, "CONTEXT_MEMORY_OPTIMIZATION_ENABLED", enabled),
            patch.object(analyze_deal_if_changed, "CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE", shadow),
            patch.object(analyze_deal_if_changed, "CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK", force_full),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        analyze_deal_if_changed.main()
        return analyze_deal_if_changed.run_existing_analyzer

    def test_latest_transcript_ignores_generated_all_calls_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcripts = Path(temp_dir)
            source = transcripts / "call_42.md"
            aggregate = transcripts / "deal_7_all_calls_transcript.md"
            source.write_text("source", encoding="utf-8")
            aggregate.write_text("aggregate", encoding="utf-8")
            aggregate.touch()

            self.assertEqual(analyze_deal.latest_transcript(transcripts), source)
            self.assertEqual(analyze_deal_if_changed.latest_transcript_or_none(transcripts), source)

    def test_dry_run_decision_does_not_save_snapshot_or_analysis_state(self) -> None:
        args = SimpleNamespace(
            deal_id="7",
            deal_root="unused",
            db_path=None,
            transcript="none",
            model=None,
            force_llm=False,
            dry_run_decision=True,
        )
        decision = Mock()
        decision.as_dict.return_value = {"status": "SKIPPED_NO_CHANGES"}
        with (
            patch.object(analyze_deal_if_changed, "parse_args", return_value=args),
            patch.object(analyze_deal_if_changed, "load_dotenv"),
            patch.object(analyze_deal_if_changed, "init_db"),
            patch.object(analyze_deal_if_changed, "raw_bundle_path", return_value=Path("raw.json")),
            patch.object(analyze_deal_if_changed, "resolve_transcript_for_snapshot", return_value=(None, "none")),
            patch.object(analyze_deal_if_changed, "load_json", return_value={}),
            patch.object(analyze_deal_if_changed, "build_deal_snapshot", return_value={"deal": {}}),
            patch.object(analyze_deal_if_changed, "fingerprint_snapshot", return_value="fingerprint"),
            patch.object(analyze_deal_if_changed, "get_entity_state", return_value=None),
            patch.object(analyze_deal_if_changed, "compare_snapshots", return_value={"changes": []}),
            patch.object(analyze_deal_if_changed, "get_entity_memory", return_value=None),
            patch.object(analyze_deal_if_changed, "decide_deal_processing", return_value=decision),
            patch.object(analyze_deal_if_changed, "save_json") as save_json,
            patch.object(analyze_deal_if_changed, "save_analysis_run") as save_analysis_run,
        ):
            analyze_deal_if_changed.main()

        save_json.assert_not_called()
        save_analysis_run.assert_not_called()

    def test_incremental_feature_disabled_preserves_full_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer = self._run_incremental_main(Path(directory), enabled=False)
            analyzer.assert_called_once()
            self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context_path"))

    def test_incremental_enabled_uses_prepared_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer = self._run_incremental_main(Path(directory), enabled=True)
            analyzer.assert_called_once()
            self.assertEqual(analyzer.call_args.args[1], "none")
            self.assertIsInstance(analyzer.call_args.kwargs.get("incremental_context_path"), Path)

    def test_incremental_context_failure_falls_back_to_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer = self._run_incremental_main(
                Path(directory), enabled=True,
                context_error=IncrementalContextError("previous_analysis_invalid"),
            )
            analyzer.assert_called_once()
            self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context_path"))

    def test_invalid_incremental_output_process_falls_back_to_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer = self._run_incremental_main(
                Path(directory), enabled=True,
                analyzer_side_effect=[subprocess.CalledProcessError(1, ["analyze"]), None],
            )
            self.assertEqual(analyzer.call_count, 2)
            self.assertIsInstance(analyzer.call_args_list[0].kwargs.get("incremental_context_path"), Path)
            self.assertIsNone(analyzer.call_args_list[1].kwargs.get("incremental_context_path"))

    def test_shadow_builds_context_but_publishes_full_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer = self._run_incremental_main(Path(directory), enabled=False, shadow=True)
            analyzer.assert_called_once()
            self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context_path"))


if __name__ == "__main__":
    unittest.main()
