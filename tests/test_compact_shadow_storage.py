from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from api.compact_shadow import _evidence_source, _snapshot_hash
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

    def test_evidence_lookup_requires_exact_typed_source_id(self) -> None:
        inputs = {
            "history_text": "source=lead:42 type=call id=1234 result=busy",
            "transcript_text": "### Call: activity_id=call-1\n\n```text\nПерезвоните позже\n```",
        }
        self.assertIsNone(_evidence_source(inputs, "123"))
        self.assertEqual(_evidence_source(inputs, "call-1")["source_type"], "transcript")

    def test_snapshot_hash_changes_with_sent_context(self) -> None:
        base = {"entity_type": "lead", "entity_id": "42", "history_text": "A", "transcript_text": "B", "diagnostics_text": "", "stage_policy": {}}
        changed = {**base, "history_text": "C"}
        self.assertNotEqual(_snapshot_hash(base), _snapshot_hash(changed))


if __name__ == "__main__":
    unittest.main()
