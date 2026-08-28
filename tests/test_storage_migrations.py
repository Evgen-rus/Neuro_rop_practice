from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage.rop_db import connect, init_db


INDEX_NAME = "idx_deal_control_tasks_neuro_report"


def index_sql(db_path: Path) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (INDEX_NAME,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


class StorageMigrationTests(unittest.TestCase):
    def test_daily_checklist_tables_are_not_created_and_legacy_data_is_preserved(self) -> None:
        tables = ("deal_daily_checklists", "deal_daily_checklist_items", "deal_daily_checklist_events")
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            init_db(db_path)
            with connect(db_path) as conn:
                existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue(set(tables).isdisjoint(existing))
                self.assertNotIn("checklist_state_json", {row[1] for row in conn.execute("PRAGMA table_info(deal_control_deals)")})
                for table in tables:
                    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)")
                    conn.execute(f"INSERT INTO {table} VALUES (1, ?)", ("Исторические отметки",))
                conn.execute("ALTER TABLE deal_control_deals ADD COLUMN checklist_state_json TEXT DEFAULT '{}'")
            init_db(db_path)
            init_db(db_path)
            with connect(db_path) as conn:
                for table in tables:
                    self.assertEqual([row[0] for row in conn.execute(f"SELECT payload FROM {table}")], ["Исторические отметки"])

    def test_legacy_deal_control_tasks_migrates_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE deal_control_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        deal_id TEXT NOT NULL,
                        task_text TEXT NOT NULL,
                        touch_type TEXT,
                        expected_result TEXT,
                        due_at TEXT NOT NULL,
                        local_status TEXT NOT NULL DEFAULT 'active',
                        crm_execution_status TEXT NOT NULL DEFAULT 'not_reflected',
                        crm_match_activity_id TEXT,
                        crm_match_confidence TEXT,
                        business_result_status TEXT NOT NULL DEFAULT 'no_result',
                        business_result_note TEXT,
                        result_activity_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(deal_control_tasks)")
                }
            finally:
                conn.close()
            self.assertIn("source_kind", columns)
            self.assertIn("source_report_id", columns)
            self.assertIsNotNone(index_sql(db_path))

            init_db(db_path)
            self.assertIsNotNone(index_sql(db_path))

    def test_fresh_database_creates_neuro_report_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "fresh.sqlite"

            init_db(db_path)
            self.assertIsNotNone(index_sql(db_path))

    def test_fresh_database_creates_manager_trajectory_and_provenance_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "fresh.sqlite"
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                analysis_columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis_runs)")}
                report_columns = {row[1] for row in conn.execute("PRAGMA table_info(ui_reports)")}
                event_columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_trajectory_events)")}
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list(manager_trajectory_events)")
                }
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                conn.close()
            self.assertTrue({"model", "prompt_version", "logic_version", "provenance_json"} <= analysis_columns)
            self.assertIn("analysis_run_id", report_columns)
            self.assertIn("payload_json", event_columns)
            self.assertIn("idx_manager_trajectory_entity_time", indexes)
            self.assertIn("idx_manager_trajectory_manager_time", indexes)
            self.assertIn("automatic_analysis_runs", tables)
            self.assertIn("automatic_analysis_items", tables)
            self.assertIn("daily_control_reports", tables)
            self.assertIn("deal_manager_companion_messages", tables)

            init_db(db_path)
            self.assertIsNotNone(index_sql(db_path))


if __name__ == "__main__":
    unittest.main()
