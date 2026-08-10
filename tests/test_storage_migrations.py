from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage.rop_db import init_db


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

            init_db(db_path)
            self.assertIsNotNone(index_sql(db_path))


if __name__ == "__main__":
    unittest.main()
