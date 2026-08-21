from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from setup import MSK_TZ
from storage.rop_db import (
    connect,
    init_db,
    list_manager_trajectory_events,
    list_manager_trajectory_presence_events,
    observe_manager_trajectory_business_snapshot,
    record_manager_trajectory_event,
    record_manager_trajectory_presence_event,
    record_quick_help_opened_event,
    save_deal_control_scope,
    save_deal_manager_quick_help,
    save_deal_manager_situation_confirmation,
    save_ui_report,
    upsert_deal_control_deal,
)


NOW = datetime(2026, 8, 21, 10, 0, tzinfo=MSK_TZ)


class ManagerTrajectoryStorageV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "trajectory.sqlite"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event_range(self) -> tuple[str, str]:
        return (NOW - timedelta(days=1)).isoformat(), (NOW + timedelta(days=1)).isoformat()

    def test_v3_schema_migration_is_idempotent_and_preserves_utf8(self) -> None:
        with connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_trajectory_entity_state)")}
            self.assertTrue({
                "business_snapshot_json", "business_snapshot_hash", "business_snapshot_at",
            }.issubset(columns))
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manager_trajectory_presence_events'",
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_manager_trajectory_event_type_time'",
            ).fetchone())

        first = record_manager_trajectory_event(
            self.db_path,
            entity_type="deal", entity_id="1", manager_id="10",
            event_type="crm_task_history_observed", source="fixture",
            source_event_key="task-history:1", occurred_at=NOW.isoformat(),
            payload={"комментарий": "Перенёс срок"},
        )
        init_db(self.db_path)
        second = record_manager_trajectory_event(
            self.db_path,
            entity_type="deal", entity_id="1", manager_id="10",
            event_type="crm_task_history_observed", source="fixture",
            source_event_key="task-history:1", occurred_at=NOW.isoformat(),
            payload={"комментарий": "Другая версия не должна затереть факт"},
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["payload"], {"комментарий": "Перенёс срок"})

    def test_business_snapshot_is_allowlisted_and_changes_are_append_only(self) -> None:
        first = observe_manager_trajectory_business_snapshot(
            self.db_path,
            entity_type="deal", entity_id="101", manager_id="10",
            snapshot={
                "STAGE_ID": "NEW", "OPPORTUNITY": "100000",
                "UF_CRM_SECRET_TECHNICAL": "не включать",
            },
            modified_at=NOW.isoformat(), changed_by_id="10",
        )
        self.assertEqual(first["changed_fields"], [])
        second = observe_manager_trajectory_business_snapshot(
            self.db_path,
            entity_type="deal", entity_id="101", manager_id="10",
            snapshot={
                "STAGE_ID": "PREPARATION", "OPPORTUNITY": "125000",
                "UF_CRM_SECRET_TECHNICAL": "не включать",
            },
            modified_at=(NOW + timedelta(minutes=5)).isoformat(), changed_by_id="10",
        )
        self.assertEqual(
            {item["field_name"] for item in second["changed_fields"]},
            {"STAGE_ID", "OPPORTUNITY"},
        )
        self.assertNotIn("UF_CRM_SECRET_TECHNICAL", second["snapshot"])
        unchanged = observe_manager_trajectory_business_snapshot(
            self.db_path,
            entity_type="deal", entity_id="101", manager_id="10",
            snapshot={"STAGE_ID": "PREPARATION", "OPPORTUNITY": "125000"},
            modified_at=(NOW + timedelta(minutes=5)).isoformat(), changed_by_id="10",
        )
        self.assertEqual(unchanged["changed_fields"], [])
        reverted = observe_manager_trajectory_business_snapshot(
            self.db_path,
            entity_type="deal", entity_id="101", manager_id="10",
            snapshot={"STAGE_ID": "NEW", "OPPORTUNITY": "100000"},
            modified_at=(NOW + timedelta(minutes=10)).isoformat(), changed_by_id="10",
        )
        self.assertEqual(len(reverted["changed_fields"]), 2)
        events = list_manager_trajectory_events(
            self.db_path, from_at=(NOW - timedelta(minutes=1)).isoformat(),
            to_at=(NOW + timedelta(minutes=11)).isoformat(), manager_ids=["10"],
        )
        changes = [item for item in events if item["event_type"] == "crm_business_field_changed"]
        self.assertEqual(len(changes), 4)
        self.assertTrue(any(
            item["payload"]["to_value"] == "PREPARATION"
            for item in changes
        ))
        with connect(self.db_path) as conn:
            raw_snapshot = conn.execute(
                "SELECT business_snapshot_json FROM manager_trajectory_entity_state WHERE entity_id = '101'",
            ).fetchone()[0]
        self.assertIn("OPPORTUNITY", raw_snapshot)

    def test_presence_is_idempotent_and_listable(self) -> None:
        first = record_manager_trajectory_presence_event(
            self.db_path,
            manager_id="10", status="online", observed_at=NOW.isoformat(),
            source_event_key="presence:10:1", last_activity_at=NOW.isoformat(),
            payload={"состояние": "онлайн"},
        )
        second = record_manager_trajectory_presence_event(
            self.db_path,
            manager_id="10", status="online", observed_at=NOW.isoformat(),
            source_event_key="presence:10:1", payload={"состояние": "дубликат"},
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["payload"], {"состояние": "онлайн"})
        record_manager_trajectory_presence_event(
            self.db_path,
            manager_id="10", status="offline",
            observed_at=(NOW + timedelta(minutes=5)).isoformat(),
            source_event_key="presence:10:2",
        )
        rows = list_manager_trajectory_presence_events(
            self.db_path,
            from_at=(NOW - timedelta(minutes=1)).isoformat(),
            to_at=(NOW + timedelta(minutes=6)).isoformat(),
            manager_ids=["10"],
        )
        self.assertEqual([row["status"] for row in rows], ["online", "offline"])
        self.assertTrue(rows[0]["is_online"])
        self.assertFalse(rows[1]["is_online"])

    def test_quick_help_open_requires_verified_manager_and_is_idempotent(self) -> None:
        save_deal_control_scope(
            self.db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="101", source="fixture", title="Тестовая сделка",
            manager_id="10", manager_name="Менеджер", stage_id="NEW", stage_name="Новая",
            pipeline_id="15", amount="100", currency_id="RUB",
            created_at_crm=NOW.isoformat(), modified_at_crm=NOW.isoformat(), is_active=True,
        )
        report_id = save_ui_report(
            self.db_path, entity_type="deal", entity_id="101", report_json={"analysis": True},
        )
        review = save_deal_manager_situation_confirmation(
            self.db_path, deal_id="101", source_report_id=report_id,
        )
        quick_help = save_deal_manager_quick_help(
            self.db_path, deal_id="101", source_report_id=report_id,
            situation_review_id=review["id"], question="Что сказать?",
            answer_json={"next_action": "Позвонить"},
        )
        with connect(self.db_path) as conn:
            now = NOW.isoformat()
            conn.execute(
                """INSERT INTO auth_users
                   (id, login, password_hash, role, manager_id, is_active, created_at, updated_at)
                   VALUES (1, 'manager-1', 'unused', 'manager', '10', 1, ?, ?)""",
                (now, now),
            )
            conn.execute(
                """INSERT INTO auth_users
                   (id, login, password_hash, role, manager_id, is_active, created_at, updated_at)
                   VALUES (2, 'manager-2', 'unused', 'manager', '11', 1, ?, ?)""",
                (now, now),
            )
            conn.commit()
        first = record_quick_help_opened_event(
            self.db_path, deal_id="101", auth_user_id=1,
            occurrence_id="open-1", active_quick_help_id=quick_help["id"],
            assistant_mode="push",
        )
        second = record_quick_help_opened_event(
            self.db_path, deal_id="101", auth_user_id=1,
            occurrence_id="open-1", active_quick_help_id=quick_help["id"],
            assistant_mode="push",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["event_type"], "quick_help_opened")
        self.assertTrue(first["payload"]["actor_verified"])
        self.assertEqual(first["recommendation_id"], str(quick_help["id"]))
        with self.assertRaises(PermissionError):
            record_quick_help_opened_event(
                self.db_path, deal_id="101", auth_user_id=2,
                occurrence_id="open-other-manager",
            )


if __name__ == "__main__":
    unittest.main()
