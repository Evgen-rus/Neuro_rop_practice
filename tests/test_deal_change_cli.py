from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai_api.change_detection.decision_engine import (
    FIRST_FULL_ANALYSIS,
    FULL_LLM_ANALYSIS,
    INCREMENTAL_LLM_ANALYSIS,
    MINI_RECOMMENDATION_NO_LLM,
    SKIPPED_NO_CHANGES,
    ProcessingDecision,
)
from openai_api.llm import analyze_deal, analyze_deal_if_changed
from openai_api.llm.validation import AnalysisValidationError


class DealChangeCliTests(unittest.TestCase):
    def _run_main(
        self,
        root: Path,
        *,
        decision: ProcessingDecision,
        incremental_enabled: bool = False,
        stage5_error: Exception | None = None,
        analyzer_error: Exception | None = None,
        persistence_error: Exception | None = None,
        force_llm: bool = False,
        source_status: dict | None = None,
        trusted_baseline: bool | None = None,
    ) -> tuple[Mock, Mock]:
        args = SimpleNamespace(
            deal_id="7", deal_root=str(root), db_path=str(root / "state.sqlite"),
            transcript="none", model=None, force_llm=force_llm, dry_run_decision=False,
        )
        if trusted_baseline is None:
            trusted_baseline = incremental_enabled
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
            patch.object(analyze_deal_if_changed, "DEAL_INCREMENTAL_ANALYSIS_ENABLED", incremental_enabled),
            patch.object(analyze_deal_if_changed, "stage5_inputs", return_value=(
                {
                    "analysis_run_id": 1,
                    "analysis": {"deal_state": {}},
                    "canonical_fingerprint": "old",
                    "evidence_coverage": {},
                } if trusted_baseline else None,
                {
                    "owner": {"entity_id": "7"},
                    "entities": {"deal:7": {"semantic": {}}},
                    "source_status": source_status or {},
                },
                {"entries": [], "from_semantic_fingerprint": "old", "to_semantic_fingerprint": "new"},
                [],
            ), side_effect=stage5_error),
            patch.object(
                analyze_deal_if_changed,
                "persist_successful_llm_run",
                return_value=1,
                side_effect=persistence_error,
            ),
            patch.object(analyze_deal_if_changed, "emit_deal_publish_ready"),
            patch.object(analyze_deal_if_changed, "save_analysis_run", return_value=2),
            patch.object(analyze_deal_if_changed, "run_existing_analyzer", side_effect=analyzer_error),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        try:
            analyze_deal_if_changed.main()
        except Exception:
            if persistence_error is None and stage5_error is None:
                raise
        return (
            analyze_deal_if_changed.run_existing_analyzer,
            analyze_deal_if_changed.persist_successful_llm_run,
        )

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

    def test_legacy_incremental_decision_runs_full_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, _persist = self._run_main(
                Path(directory),
                decision=ProcessingDecision(
                    status=INCREMENTAL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
            analyzer.assert_called_once()
            self.assertEqual(analyzer.call_args.args[1], "none")
            self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context"))

    def test_opt_in_routes_incremental_and_persists_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertIsNotNone(analyzer.call_args.kwargs["incremental_context"])
        self.assertEqual(persist.call_args.kwargs["decision_status"], INCREMENTAL_LLM_ANALYSIS)

    def test_client_reply_routes_incremental_when_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["client reply"],
                    triggers=[],
                    diff={"changes": ["new_client_reply"], "details": {}},
                ),
            )
        self.assertIsNotNone(analyzer.call_args.kwargs["incremental_context"])
        self.assertEqual(persist.call_args.kwargs["decision_status"], INCREMENTAL_LLM_ANALYSIS)

    def test_repairable_continuity_error_runs_one_incremental_correction(self) -> None:
        error = AnalysisValidationError(
            "Invalid deal analysis continuity: confirmation upgrade without new evidence: decision_path",
            errors=["confirmation upgrade without new evidence: decision_path"],
        )
        persist_calls = {"count": 0}

        def persist_once(*_args, **_kwargs):
            persist_calls["count"] += 1
            if persist_calls["count"] == 1:
                raise error
            return 1

        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                persistence_error=persist_once,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertEqual(analyzer.call_count, 2)
        self.assertTrue(analyzer.call_args_list[1].kwargs.get("continuity_correction"))
        self.assertIsNotNone(analyzer.call_args_list[1].kwargs.get("incremental_context"))
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(persist.call_args.kwargs["decision_status"], INCREMENTAL_LLM_ANALYSIS)

    def test_failed_incremental_correction_runs_exactly_one_full_fallback(self) -> None:
        error = AnalysisValidationError(
            "Invalid deal analysis continuity: lost unresolved commitment: manager_check",
            errors=["lost unresolved commitment: manager_check"],
        )

        def persist_incremental_fails(*_args, **kwargs):
            if kwargs.get("decision_status") == INCREMENTAL_LLM_ANALYSIS:
                raise error
            return 1

        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                persistence_error=persist_incremental_fails,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertEqual(analyzer.call_count, 3)
        self.assertTrue(analyzer.call_args_list[1].kwargs.get("continuity_correction"))
        self.assertIsNone(analyzer.call_args_list[2].kwargs.get("incremental_context"))
        self.assertEqual(persist.call_args.kwargs["decision_status"], FULL_LLM_ANALYSIS)

    def test_unrepairable_continuity_error_skips_correction(self) -> None:
        error = AnalysisValidationError(
            "Invalid deal analysis continuity: unknown domain rule",
            errors=["unknown domain rule"],
        )

        def persist_incremental_fails(*_args, **kwargs):
            if kwargs.get("decision_status") == INCREMENTAL_LLM_ANALYSIS:
                raise error
            return 1

        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                persistence_error=persist_incremental_fails,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertEqual(analyzer.call_count, 2)
        self.assertFalse(analyzer.call_args_list[0].kwargs.get("continuity_correction"))
        self.assertIsNone(analyzer.call_args_list[1].kwargs.get("incremental_context"))
        self.assertEqual(persist.call_args.kwargs["decision_status"], FULL_LLM_ANALYSIS)

    def test_incremental_error_runs_exactly_one_full_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = 0

            def fail_first(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("synthetic incremental failure")

            analyzer, persist = self._run_main(
                root,
                incremental_enabled=True,
                analyzer_error=fail_first,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertEqual(analyzer.call_count, 2)
        self.assertIsNotNone(analyzer.call_args_list[0].kwargs["incremental_context"])
        self.assertIsNone(analyzer.call_args_list[1].kwargs.get("incremental_context"))
        self.assertTrue(persist.call_args.kwargs["decision_reason"]["fallback"])

    def test_technical_only_skip_never_calls_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, _persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=SKIPPED_NO_CHANGES,
                    reasons=["technical only"],
                    triggers=[],
                    diff={"changes": ["technical_only"], "details": {}},
                ),
            )
        analyzer.assert_not_called()

    def test_incremental_persistence_error_does_not_trigger_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                persistence_error=OSError("synthetic persistence failure"),
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        analyzer.assert_called_once()
        persist.assert_called_once()

    def test_stage5_preflight_error_stops_before_paid_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                stage5_error=ValueError("synthetic canonical metadata failure"),
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        analyzer.assert_not_called()
        persist.assert_not_called()

    def test_force_and_unsupported_context_keep_full_with_opt_in(self) -> None:
        cases = (
            ("force", True, {"changes": ["transcript_changed"], "details": {}}, {}),
            ("commercial", False, {"changes": ["commercial_refs_changed"], "details": {}}, {}),
            ("failed source", False, {"changes": ["transcript_changed"], "details": {}}, {"activities": "failed"}),
        )
        for label, force_llm, diff, source_status in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                analyzer, persist = self._run_main(
                    Path(directory),
                    incremental_enabled=True,
                    force_llm=force_llm,
                    source_status=source_status,
                    decision=ProcessingDecision(
                        status=FULL_LLM_ANALYSIS,
                        reasons=[label],
                        triggers=[],
                        diff=diff,
                    ),
                )
            analyzer.assert_called_once()
            self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context"))
            self.assertEqual(persist.call_args.kwargs["decision_status"], FULL_LLM_ANALYSIS)

    def test_size_routing_keeps_incremental_when_unique_context_is_much_smaller(self) -> None:
        routing = {
            "full_variable_bytes": 10000,
            "incremental_variable_bytes": 1000,
            "routing_size_ratio": 0.1,
            "routing_size_threshold": 0.90,
            "chosen_analysis_mode": "incremental",
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            analyze_deal_if_changed,
            "adaptive_variable_size_routing",
            return_value=routing,
        ):
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        self.assertIsNotNone(analyzer.call_args.kwargs["incremental_context"])
        self.assertEqual(persist.call_args.kwargs["decision_status"], INCREMENTAL_LLM_ANALYSIS)
        reason = persist.call_args.kwargs["decision_reason"]
        self.assertEqual(reason["chosen_analysis_mode"], "incremental")
        self.assertEqual(reason["full_variable_bytes"], 10000)
        self.assertEqual(reason["incremental_variable_bytes"], 1000)
        self.assertEqual(reason["routing_size_ratio"], 0.1)
        self.assertEqual(reason["routing_size_threshold"], 0.90)

    def test_size_routing_falls_back_to_full_when_incremental_is_not_smaller(self) -> None:
        routing = {
            "full_variable_bytes": 1000,
            "incremental_variable_bytes": 950,
            "routing_size_ratio": 0.95,
            "routing_size_threshold": 0.90,
            "chosen_analysis_mode": "full",
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            analyze_deal_if_changed,
            "adaptive_variable_size_routing",
            return_value=routing,
        ):
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        analyzer.assert_called_once()
        self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context"))
        self.assertEqual(persist.call_args.kwargs["decision_status"], FULL_LLM_ANALYSIS)
        reason = persist.call_args.kwargs["decision_reason"]
        self.assertTrue(reason["fallback"])
        self.assertEqual(reason["fallback_reason"], "incremental_variable_size_not_advantageous")
        self.assertEqual(reason["chosen_analysis_mode"], "full")
        self.assertEqual(reason["full_variable_bytes"], 1000)
        self.assertEqual(reason["incremental_variable_bytes"], 950)
        self.assertEqual(reason["routing_size_ratio"], 0.95)
        self.assertEqual(reason["routing_size_threshold"], 0.90)

    def test_first_full_does_not_call_size_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            analyze_deal_if_changed,
            "adaptive_variable_size_routing",
        ) as size_routing:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                decision=ProcessingDecision(
                    status=FIRST_FULL_ANALYSIS,
                    reasons=["first analysis"],
                    triggers=[],
                    diff={"changes": [], "details": {}},
                ),
            )
        size_routing.assert_not_called()
        analyzer.assert_called_once()
        self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context"))
        self.assertEqual(persist.call_args.kwargs["decision_status"], FIRST_FULL_ANALYSIS)

    def test_unsafe_baseline_keeps_full_without_size_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            analyze_deal_if_changed,
            "adaptive_variable_size_routing",
        ) as size_routing:
            analyzer, persist = self._run_main(
                Path(directory),
                incremental_enabled=True,
                trusted_baseline=False,
                decision=ProcessingDecision(
                    status=FULL_LLM_ANALYSIS,
                    reasons=["new evidence"],
                    triggers=[],
                    diff={"changes": ["transcript_changed"], "details": {}},
                ),
            )
        size_routing.assert_not_called()
        analyzer.assert_called_once()
        self.assertIsNone(analyzer.call_args.kwargs.get("incremental_context"))
        self.assertEqual(persist.call_args.kwargs["decision_status"], FULL_LLM_ANALYSIS)
        self.assertEqual(
            persist.call_args.kwargs["decision_reason"]["fallback_reason"],
            "unsafe_trusted_baseline",
        )

    def test_mini_and_skip_do_not_call_size_routing(self) -> None:
        cases = (
            ProcessingDecision(
                status=MINI_RECOMMENDATION_NO_LLM,
                reasons=["soft change"],
                triggers=[{"trigger_type": "stale_activity"}],
                diff={"changes": ["new_comment"], "details": {}},
            ),
            ProcessingDecision(
                status=SKIPPED_NO_CHANGES,
                reasons=["no changes"],
                triggers=[],
                diff={"changes": [], "details": {}},
            ),
        )
        for decision in cases:
            with self.subTest(status=decision.status), tempfile.TemporaryDirectory() as directory:
                extra_patches = []
                if decision.status == MINI_RECOMMENDATION_NO_LLM:
                    extra_patches = [
                        patch.object(
                            analyze_deal_if_changed,
                            "filter_today_mini_triggers",
                            return_value=decision.triggers,
                        ),
                        patch.object(
                            analyze_deal_if_changed,
                            "render_mini_recommendation",
                            return_value="mini",
                        ),
                        patch.object(analyze_deal_if_changed, "save_mini_recommendation_markdown"),
                        patch.object(analyze_deal_if_changed, "save_mini_recommendation"),
                        patch.object(analyze_deal_if_changed, "persist_skip", return_value=3),
                    ]
                started = [item.start() for item in extra_patches]
                try:
                    with patch.object(
                        analyze_deal_if_changed,
                        "adaptive_variable_size_routing",
                    ) as size_routing:
                        analyzer, _persist = self._run_main(
                            Path(directory),
                            incremental_enabled=True,
                            decision=decision,
                        )
                    size_routing.assert_not_called()
                    analyzer.assert_not_called()
                finally:
                    for item in reversed(extra_patches):
                        item.stop()
                    del started


if __name__ == "__main__":
    unittest.main()
