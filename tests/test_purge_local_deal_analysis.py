from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.purge_local_deal_analysis import apply_purge
from storage.rop_db import (
    connect,
    create_deal_control_task,
    deal_analysis_purge_counts,
    get_deal_manager_situation_state,
    get_latest_ui_report,
    init_db,
    list_deal_control_tasks,
    purge_local_deal_analysis_state,
    save_analysis_run,
    save_compact_shadow_feedback,
    save_compact_shadow_run,
    save_deal_incremental_v2_run,
    save_deal_manager_quick_help,
    save_deal_manager_situation_confirmation,
    save_deal_semantic_checkpoint,
    save_ui_report,
    upsert_deal_control_deal,
    upsert_entity_state,
)


def seed_deal(db_path: Path) -> tuple[int, int]:
    upsert_deal_control_deal(
        db_path,
        deal_id="100",
        source="test",
        title="Тестовая сделка",
        manager_id="7",
        manager_name="Менеджер",
        stage_id="NEW",
        stage_name="Новая",
        pipeline_id="0",
        amount="1000",
        currency_id="RUB",
        created_at_crm=None,
        modified_at_crm=None,
        is_active=True,
    )
    report_id = save_ui_report(
        db_path,
        entity_type="deal",
        entity_id="100",
        report_json={"deal_control_brief": {"current_situation": "Старый анализ"}},
    )
    review = save_deal_manager_situation_confirmation(
        db_path,
        deal_id="100",
        source_report_id=report_id,
    )
    save_deal_manager_quick_help(
        db_path,
        deal_id="100",
        source_report_id=report_id,
        situation_review_id=int(review["id"]),
        question="Старый вопрос",
        answer_json={"summary": "Старый ответ"},
    )
    task = create_deal_control_task(
        db_path,
        deal_id="100",
        task_text="Старая ручная задача",
        touch_type="Звонок",
        expected_result="Результат",
        due_at="2026-08-15T10:00:00+03:00",
    )
    upsert_entity_state(
        db_path,
        entity_type="deal",
        entity_id="100",
        fingerprint="old",
        snapshot={"deal": {"stage_id": "NEW"}},
        last_analysis_status="full_llm_analysis",
        last_analysis={"analysis": {"old": True}},
    )
    return report_id, int(task["id"])


class PurgeLocalDealAnalysisTests(unittest.TestCase):
    def test_storage_purge_removes_deal_derivatives_and_preserves_leads_and_deal_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            seed_deal(db_path)
            lead_report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="200",
                report_json={"lead_state": {"summary": "Сохранить"}},
            )
            upsert_entity_state(
                db_path,
                entity_type="lead",
                entity_id="200",
                fingerprint="lead",
                snapshot={"lead": {"status_id": "NEW"}},
                last_analysis_status="full_llm_analysis",
                last_analysis={"analysis": {"lead": True}},
            )

            preview = deal_analysis_purge_counts(db_path)
            self.assertEqual(preview["ui_reports"], 1)
            self.assertEqual(preview["deal_manager_quick_help"], 1)
            self.assertEqual(preview["deal_control_tasks"], 1)

            deleted = purge_local_deal_analysis_state(db_path)

            self.assertEqual(deleted["ui_reports"], 1)
            self.assertIsNone(get_latest_ui_report(db_path, entity_type="deal", entity_id="100"))
            self.assertEqual(get_latest_ui_report(db_path, entity_type="lead", entity_id="200")["id"], lead_report_id)
            self.assertEqual(list_deal_control_tasks(db_path, deal_ids=["100"]), [])
            self.assertEqual(get_deal_manager_situation_state(db_path, deal_id="100")["state"], "pending")
            with connect(db_path) as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = '100'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM entity_state WHERE entity_type = 'lead'").fetchone())
                self.assertIsNone(conn.execute("SELECT 1 FROM entity_state WHERE entity_type = 'deal'").fetchone())
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertFalse(any(deal_analysis_purge_counts(db_path).values()))

    def test_purge_removes_legacy_incremental_v2_rows_without_foreign_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="100",
                status="FULL_LLM_ANALYSIS",
            )
            checkpoint_id = save_deal_semantic_checkpoint(
                db_path,
                entity_id="100",
                schema_version="deal-semantic-state-v1",
                source_analysis_run_id=run_id,
                source_fingerprint="fp",
                semantic_state={"schema_version": "deal-semantic-state-v1"},
                mode="shadow",
                baseline_snapshot={"entity_type": "deal"},
            )
            v2_run_id = save_deal_incremental_v2_run(
                db_path,
                entity_id="100",
                mode="shadow",
                source_analysis_run_id=run_id,
            )
            preview = deal_analysis_purge_counts(db_path)
            self.assertEqual(preview["deal_semantic_checkpoints"], 1)
            self.assertEqual(preview["deal_incremental_v2_runs"], 1)

            deleted = purge_local_deal_analysis_state(db_path)

            self.assertEqual(deleted["deal_semantic_checkpoints"], 1)
            self.assertEqual(deleted["deal_incremental_v2_runs"], 1)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM deal_semantic_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM deal_incremental_v2_runs WHERE id = ?", (v2_run_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
                )

    def test_purge_removes_legacy_compact_shadow_rows_without_foreign_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            save_compact_shadow_run(
                db_path,
                run_id="compact-deal-1",
                entity_type="deal",
                entity_id="100",
                snapshot_hash="snapshot",
                status="completed",
                started_at="2031-02-03T10:00:00+03:00",
            )
            save_compact_shadow_feedback(
                db_path,
                compact_run_id="compact-deal-1",
                entity_type="deal",
                entity_id="100",
                snapshot_hash="snapshot",
                model="test-model",
                raw_playbook="raw",
                final_playbook="final",
                feedback_result="correct",
            )
            save_compact_shadow_run(
                db_path,
                run_id="compact-lead-1",
                entity_type="lead",
                entity_id="200",
                snapshot_hash="lead-snapshot",
                status="completed",
                started_at="2031-02-03T10:00:00+03:00",
            )
            preview = deal_analysis_purge_counts(db_path)
            self.assertEqual(preview["compact_shadow_runs"], 1)
            self.assertEqual(preview["compact_shadow_feedback"], 1)

            deleted = purge_local_deal_analysis_state(db_path)

            self.assertEqual(deleted["compact_shadow_runs"], 1)
            self.assertEqual(deleted["compact_shadow_feedback"], 1)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM compact_shadow_runs WHERE id = ?", ("compact-deal-1",)).fetchone()
                )
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM compact_shadow_runs WHERE id = ?", ("compact-lead-1",)).fetchone()
                )

    def test_apply_moves_only_analysis_and_creates_restorable_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "reports" / "rop_assistant"
            db_path = root / "rop_assistant.sqlite"
            init_db(db_path)
            seed_deal(db_path)
            deal_root = root / "deals" / "deal_100"
            expected = {
                "analysis": "старый анализ",
                "raw": "crm export",
                "history": "history",
                "audio": "audio",
                "transcripts": "transcript",
                "diagnostics": "diagnostics",
            }
            for section, content in expected.items():
                path = deal_root / section / "sample.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            quarantine_root, _ = apply_purge(db_path, root)

            self.assertFalse((deal_root / "analysis").exists())
            self.assertEqual(
                (quarantine_root / "deals" / "deal_100" / "analysis" / "sample.txt").read_text(encoding="utf-8"),
                expected["analysis"],
            )
            self.assertTrue((quarantine_root / "rop_assistant.sqlite").is_file())
            for section in ("raw", "history", "audio", "transcripts", "diagnostics"):
                self.assertEqual((deal_root / section / "sample.txt").read_text(encoding="utf-8"), expected[section])
            self.assertFalse(any(deal_analysis_purge_counts(db_path).values()))

            backup = sqlite3.connect(quarantine_root / "rop_assistant.sqlite")
            try:
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM ui_reports WHERE entity_type = 'deal'").fetchone()[0], 1)
            finally:
                backup.close()


if __name__ == "__main__":
    unittest.main()
