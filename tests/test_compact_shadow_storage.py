from __future__ import annotations

import gc
import importlib
import tempfile
import unittest
from pathlib import Path

from storage.rop_db import (
    get_compact_shadow_feedback,
    get_compact_shadow_run,
    list_compact_shadow_runs,
    save_compact_shadow_feedback,
    save_compact_shadow_run,
)


class CompactShadowStorageTests(unittest.TestCase):
    def test_runs_are_separate_from_full_analysis_and_keep_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            save_compact_shadow_run(
                db,
                run_id="run-1",
                entity_type="lead",
                entity_id="42",
                snapshot_hash="first",
                status="completed",
                started_at="2031-02-03T10:00:00+03:00",
                analysis={"attention_required": False},
                evidence_coverage={"status": "passed"},
                fallback_class="compact_safe",
                usage={"input_tokens": 10},
                cost_rub=0.2,
            )
            save_compact_shadow_run(
                db,
                run_id="run-2",
                entity_type="lead",
                entity_id="42",
                snapshot_hash="second",
                status="evidence_coverage_failed",
                started_at="2031-02-04T10:00:00+03:00",
                fallback_class="full_fallback_recommended",
            )

            latest, previous = list_compact_shadow_runs(db, entity_type="lead", entity_id="42")
            self.assertEqual(latest["id"], "run-2")
            self.assertEqual(previous["analysis"], {"attention_required": False})
            self.assertEqual(get_compact_shadow_run(db, "run-1")["fallback_class"], "compact_safe")
            gc.collect()

    def test_feedback_upserts_one_row_per_compact_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            save_compact_shadow_run(
                db,
                run_id="run-1",
                entity_type="deal",
                entity_id="18",
                snapshot_hash="snapshot",
                status="completed",
                started_at="2031-02-03T10:00:00+03:00",
            )
            save_compact_shadow_feedback(
                db,
                compact_run_id="run-1",
                entity_type="deal",
                entity_id="18",
                snapshot_hash="snapshot",
                model="test-model",
                raw_playbook="raw",
                final_playbook="final",
                feedback_result="partly_correct",
                reason="пропущен риск",
                comment="Короткая проверка",
            )
            updated = save_compact_shadow_feedback(
                db,
                compact_run_id="run-1",
                entity_type="deal",
                entity_id="18",
                snapshot_hash="snapshot",
                model="test-model",
                raw_playbook="raw",
                final_playbook="final",
                feedback_result="correct",
            )
            self.assertEqual(updated["feedback_result"], "correct")
            self.assertEqual(get_compact_shadow_feedback(db, "run-1")["comment"], None)
            gc.collect()


class CompactRuntimeRemovedTests(unittest.TestCase):
    def test_compact_llm_modules_are_not_importable(self) -> None:
        for module_name in (
            "api.compact_shadow",
            "openai_api.llm.attention_delta",
            "openai_api.llm.attention_delta_knowledge",
            "openai_api.llm.attention_delta_report",
            "openai_api.llm.deal_attention_playbooks",
            "openai_api.llm.lead_playbook_resolver",
            "openai_api.llm.evidence_coverage",
            "benchmarks.run_attention_delta_shadow",
            "benchmarks.compare_attention_delta",
            "benchmarks.replay_attention_delta_postprocessing",
        ):
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_http_app_has_no_compact_routes(self) -> None:
        from api.app import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertFalse(any("compact" in path for path in paths))


if __name__ == "__main__":
    unittest.main()
