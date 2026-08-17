from __future__ import annotations

import sqlite3
import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api.manager_trajectory import build_manager_trajectory_report, collect_manager_trajectory
from openai_api.change_detection.provenance import analysis_run_provenance
from setup import MSK_TZ
from storage.rop_db import (
    get_manager_trajectory_collection_state,
    init_db,
    list_manager_trajectory_events,
    materialize_deal_recommendation_from_report,
    record_manager_trajectory_event,
    record_recommendation_lifecycle_event,
    save_analysis_run,
    save_deal_control_scope,
    save_deal_manager_quick_help,
    save_deal_manager_situation_confirmation,
    save_ui_report,
    upsert_deal_control_deal,
)


NOW = datetime(2026, 8, 16, 20, 0, tzinfo=MSK_TZ)


class FakeBitrixClient:
    def __init__(self, *, activity_ok: bool = True, stage: str = "NEW") -> None:
        self.activity_ok = activity_ok
        self.stage = stage

    def safe_list_all(self, method, payload):
        if method == "crm.activity.list":
            if not self.activity_ok:
                return {"ok": False, "error": "activity unavailable", "items": []}
            return {
                "ok": True,
                "items": [
                    {
                        "ID": "a1", "OWNER_TYPE_ID": "2", "OWNER_ID": "900",
                        "RESPONSIBLE_ID": "10", "TYPE_ID": "2", "PROVIDER_ID": "VOXIMPLANT_CALL",
                        "DIRECTION": "2", "COMPLETED": "Y",
                        "START_TIME": "2026-08-16T19:10:00+03:00",
                        "LAST_UPDATED": "2026-08-16T19:20:00+03:00",
                    },
                    {
                        "ID": "a2", "OWNER_TYPE_ID": "1", "OWNER_ID": "901",
                        "RESPONSIBLE_ID": "10", "TYPE_ID": "4", "PROVIDER_ID": "CRM_EMAIL",
                        "DIRECTION": "1", "COMPLETED": "Y",
                        "START_TIME": "2026-08-16T19:30:00+03:00",
                        "LAST_UPDATED": "2026-08-16T19:31:00+03:00",
                    },
                    {
                        "ID": "foreign", "OWNER_TYPE_ID": "2", "OWNER_ID": "999",
                        "RESPONSIBLE_ID": "77", "LAST_UPDATED": "2026-08-16T19:40:00+03:00",
                    },
                ],
            }
        if method == "crm.deal.list":
            return {"ok": True, "items": [{
                "ID": "900", "ASSIGNED_BY_ID": "10", "STAGE_ID": self.stage,
                "DATE_MODIFY": "2026-08-16T19:45:00+03:00",
            }]}
        if method == "crm.lead.list":
            return {"ok": True, "items": [{
                "ID": "901", "ASSIGNED_BY_ID": "10", "STATUS_ID": "IN_PROCESS",
                "DATE_MODIFY": "2026-08-16T19:46:00+03:00",
            }]}
        raise AssertionError(method)


class ManagerTrajectoryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "trajectory.sqlite"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_is_idempotent_and_keeps_utf8_payload(self) -> None:
        kwargs = dict(
            entity_type="deal", entity_id="1", manager_id="10",
            event_type="crm_activity_observed", source="fixture", source_event_key="one",
            occurred_at=NOW.isoformat(), payload={"факт": "звонок"},
        )
        first = record_manager_trajectory_event(self.db_path, **kwargs)
        second = record_manager_trajectory_event(self.db_path, **kwargs)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["payload"], {"факт": "звонок"})
        conn = sqlite3.connect(self.db_path)
        try:
            raw = conn.execute("SELECT payload_json FROM manager_trajectory_events").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("звонок", raw)
        self.assertNotIn("\\u", raw)

    def test_provenance_is_compact_and_excludes_raw_model_output(self) -> None:
        result = analysis_run_provenance(
            {
                "model_metadata": {
                    "model": "gpt-test",
                    "raw_output_text": "sensitive raw",
                    "request_fingerprint": {"prompt": {"sha256_16": "abc", "chars": 12}},
                    "input_tokens": 10,
                }
            },
            fingerprint="snapshot-fp",
            decision_reason={"status": "FULL_LLM_ANALYSIS", "reasons": ["changed"], "diff": {"stage": True}},
            prompt_version="prompt-v1",
        )
        self.assertEqual(result["model"], "gpt-test")
        self.assertEqual(result["provenance"]["prompt_fingerprint"]["sha256_16"], "abc")
        self.assertNotIn("raw_output_text", result["provenance"]["model_metadata"])
        self.assertNotIn("request_fingerprint", result["provenance"]["model_metadata"])

    def test_generated_and_ui_lifecycle_link_to_exact_run_and_report(self) -> None:
        save_deal_control_scope(
            self.db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="101", source="initial", title="Тест", manager_id="10",
            manager_name="Менеджер", stage_id="NEW", stage_name="Новая", pipeline_id="15",
            amount="100", currency_id="RUB", created_at_crm=NOW.isoformat(),
            modified_at_crm=NOW.isoformat(), is_active=True,
        )
        run_id = save_analysis_run(
            self.db_path, entity_type="deal", entity_id="101", status="FULL_LLM_ANALYSIS",
            fingerprint="fp", model="gpt-test", prompt_version="v1", logic_version="logic-v1",
            provenance={"причина": "изменение"},
        )
        report_json = {
            "rop_manager_message_block": {
                "message_to_manager": "Позвонить клиенту", "success_condition": "Получить ответ",
                "deadline": "2026-08-17",
            },
            "manager_action_block": {"recommended_channel": "call"},
        }
        report_id = save_ui_report(
            self.db_path, entity_type="deal", entity_id="101", report_json=report_json,
            analysis_run_id=run_id,
        )
        task = materialize_deal_recommendation_from_report(
            self.db_path, "101", report_id, report_json,
        )
        self.assertIsNotNone(task)
        assert task is not None
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO auth_users (
                       id, login, password_hash, role, manager_id, is_active, created_at, updated_at
                   ) VALUES (1, 'manager-1', 'unused', 'manager', '10', 1, ?, ?)""",
                (NOW.isoformat(), NOW.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        shown = record_recommendation_lifecycle_event(
            self.db_path, deal_id="101", recommendation_kind="deal_task",
            recommendation_id=task["id"], event_type="recommendation_shown", auth_user_id=1,
        )
        repeated = record_recommendation_lifecycle_event(
            self.db_path, deal_id="101", recommendation_kind="deal_task",
            recommendation_id=task["id"], event_type="recommendation_shown", auth_user_id=1,
        )
        self.assertEqual(shown["id"], repeated["id"])
        events = list_manager_trajectory_events(
            self.db_path, from_at=(NOW - timedelta(days=1)).isoformat(),
            to_at=(NOW + timedelta(days=2)).isoformat(), manager_ids=["10"],
        )
        generated = next(item for item in events if item["event_type"] == "recommendation_generated")
        self.assertEqual(generated["analysis_run_id"], run_id)
        self.assertEqual(generated["report_id"], report_id)
        with self.assertRaises(ValueError):
            record_recommendation_lifecycle_event(
                self.db_path, deal_id="other", recommendation_kind="deal_task",
                recommendation_id=task["id"], event_type="recommendation_viewed", auth_user_id=1,
            )

    def test_quick_help_save_creates_generated_event(self) -> None:
        save_deal_control_scope(
            self.db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="101", source="initial", title="Тест", manager_id="10",
            manager_name="Менеджер", stage_id="NEW", stage_name="Новая", pipeline_id="15",
            amount="100", currency_id="RUB", created_at_crm=NOW.isoformat(),
            modified_at_crm=NOW.isoformat(), is_active=True,
        )
        report_id = save_ui_report(
            self.db_path, entity_type="deal", entity_id="101", report_json={"analysis": True},
        )
        review = save_deal_manager_situation_confirmation(
            self.db_path, deal_id="101", source_report_id=report_id,
        )
        quick = save_deal_manager_quick_help(
            self.db_path, deal_id="101", source_report_id=report_id,
            situation_review_id=review["id"], question="Что сказать?",
            answer_json={"next_action": "Позвонить"}, mode="push",
        )
        events = list_manager_trajectory_events(
            self.db_path, from_at=(NOW - timedelta(days=2)).isoformat(),
            to_at=(NOW + timedelta(days=2)).isoformat(), manager_ids=["10"],
        )
        self.assertTrue(any(
            item["event_type"] == "recommendation_generated"
            and item["recommendation_kind"] == "quick_help"
            and item["recommendation_id"] == str(quick["id"])
            for item in events
        ))


class ManagerTrajectoryCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "collection.sqlite"
        init_db(self.db_path)
        save_deal_control_scope(
            self.db_path, initial_deal_ids=[], manager_ids=["10"], pipeline_id="15",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_collection_is_scoped_idempotent_and_reports_other_entities(self) -> None:
        result = collect_manager_trajectory(
            FakeBitrixClient(), db_path=self.db_path,
            from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["activities"], 2)
        collect_manager_trajectory(
            FakeBitrixClient(), db_path=self.db_path,
            from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        events = list_manager_trajectory_events(
            self.db_path, from_at=(NOW - timedelta(days=1)).isoformat(),
            to_at=NOW.isoformat(), manager_ids=["10"],
        )
        activities = [item for item in events if item["event_type"] == "crm_activity_observed"]
        self.assertEqual(len(activities), 2)
        self.assertEqual({item["entity_type"] for item in activities}, {"deal", "lead"})
        self.assertTrue(all(item["payload"].get("contact_class") is None for item in activities))

        report = build_manager_trajectory_report(
            db_path=self.db_path, from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        self.assertEqual(set(report), {"period", "collection_status", "managers", "warnings"})
        self.assertEqual(report["managers"][0]["counts"]["crm_activity_observed"], 2)

    def test_stage_change_is_append_only_and_partial_does_not_advance_watermark(self) -> None:
        collect_manager_trajectory(
            FakeBitrixClient(stage="NEW"), db_path=self.db_path,
            from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        collect_manager_trajectory(
            FakeBitrixClient(stage="WON"), db_path=self.db_path,
            from_at=NOW - timedelta(days=1), to_at=NOW + timedelta(minutes=1),
        )
        successful_after_stage = get_manager_trajectory_collection_state(self.db_path)
        events = list_manager_trajectory_events(
            self.db_path, from_at=(NOW - timedelta(days=1)).isoformat(),
            to_at=(NOW + timedelta(days=1)).isoformat(), manager_ids=["10"],
        )
        self.assertEqual(sum(item["event_type"] == "deal_stage_changed" for item in events), 1)

        partial = collect_manager_trajectory(
            FakeBitrixClient(activity_ok=False), db_path=self.db_path,
            from_at=NOW, to_at=NOW + timedelta(minutes=2),
        )
        after = get_manager_trajectory_collection_state(self.db_path)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(after["last_success_at"], successful_after_stage["last_success_at"])


class ManagerTrajectoryApiTests(unittest.TestCase):
    def test_endpoint_derives_actor_and_expands_event_type(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "require_deal"), patch.object(
            api_app, "auth_current_user", return_value={"id": 42, "role": "manager", "manager_id": "10"},
        ), patch.object(
            api_app.storage, "record_recommendation_lifecycle_event", return_value={"id": 7}, create=True,
        ) as record:
            result = api_app.deal_recommendation_event_create(
                "101",
                api_app.RecommendationEventRequest(
                    event_type="viewed", recommendation_kind="quick_help", recommendation_id=9,
                ),
            )
        self.assertEqual(result, {"ok": True, "event_id": 7})
        self.assertEqual(record.call_args.kwargs["auth_user_id"], 42)
        self.assertEqual(record.call_args.kwargs["event_type"], "recommendation_viewed")


class ManagerTrajectoryCliTests(unittest.TestCase):
    def test_report_json_is_local_and_has_stable_top_level_contract(self) -> None:
        from scripts import manager_trajectory as cli

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "cli.sqlite"
            init_db(db_path)
            save_deal_control_scope(
                db_path, initial_deal_ids=[], manager_ids=["10"], pipeline_id="15",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main([
                    "--db-path", str(db_path), "report",
                    "--from", "2026-08-16", "--to", "2026-08-16", "--format", "json",
                ])
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(payload), {"period", "collection_status", "managers", "warnings"})
        self.assertEqual(payload["managers"][0]["manager_id"], "10")


if __name__ == "__main__":
    unittest.main()
