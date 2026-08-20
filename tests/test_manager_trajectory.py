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


def _recent_event_range() -> tuple[str, str]:
    """Окно включает и фикстуру NOW, и фактический utcish_now() при записи события."""
    start = (NOW - timedelta(days=2)).isoformat()
    end = (max(NOW, datetime.now(MSK_TZ)) + timedelta(days=1)).isoformat()
    return start, end


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
            conn.executemany(
                """INSERT INTO auth_users (
                       id, login, password_hash, role, manager_id, is_active, created_at, updated_at
                   ) VALUES (?, ?, 'unused', ?, ?, ?, ?, ?)""",
                [
                    (2, "manager-2", "manager", "11", 1, NOW.isoformat(), NOW.isoformat()),
                    (3, "admin", "admin", None, 1, NOW.isoformat(), NOW.isoformat()),
                    (4, "rop", "rop", None, 1, NOW.isoformat(), NOW.isoformat()),
                    (5, "inactive-manager", "manager", "10", 0, NOW.isoformat(), NOW.isoformat()),
                ],
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
        self.assertEqual(shown["payload"], {
            "actor_verified": True,
            "actor_role": "manager",
            "actor_manager_id": "10",
        })
        first_view = record_recommendation_lifecycle_event(
            self.db_path, deal_id="101", recommendation_kind="deal_task",
            recommendation_id=task["id"], event_type="recommendation_viewed", auth_user_id=1,
            occurrence_id="view-one",
        )
        first_view_retry = record_recommendation_lifecycle_event(
            self.db_path, deal_id="101", recommendation_kind="deal_task",
            recommendation_id=task["id"], event_type="recommendation_viewed", auth_user_id=1,
            occurrence_id="view-one",
        )
        second_view = record_recommendation_lifecycle_event(
            self.db_path, deal_id="101", recommendation_kind="deal_task",
            recommendation_id=task["id"], event_type="recommendation_viewed", auth_user_id=1,
            occurrence_id="view-two",
        )
        self.assertEqual(first_view["id"], first_view_retry["id"])
        self.assertNotEqual(first_view["id"], second_view["id"])
        self.assertEqual(first_view["payload"]["occurrence_id"], "view-one")
        from_at, to_at = _recent_event_range()
        events = list_manager_trajectory_events(
            self.db_path, from_at=from_at, to_at=to_at, manager_ids=["10"],
        )
        generated = next(item for item in events if item["event_type"] == "recommendation_generated")
        self.assertEqual(generated["analysis_run_id"], run_id)
        self.assertEqual(generated["report_id"], report_id)
        with self.assertRaises(ValueError):
            record_recommendation_lifecycle_event(
                self.db_path, deal_id="other", recommendation_kind="deal_task",
                recommendation_id=task["id"], event_type="recommendation_viewed", auth_user_id=1,
            )
        for denied_user_id in (2, 3, 4, 5):
            with self.subTest(auth_user_id=denied_user_id), self.assertRaises(PermissionError):
                record_recommendation_lifecycle_event(
                    self.db_path, deal_id="101", recommendation_kind="deal_task",
                    recommendation_id=task["id"], event_type="recommendation_viewed",
                    auth_user_id=denied_user_id,
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
        from_at, to_at = _recent_event_range()
        events = list_manager_trajectory_events(
            self.db_path, from_at=from_at, to_at=to_at, manager_ids=["10"],
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
        self.assertEqual(
            set(report),
            {"schema_version", "period", "collection_status", "summary", "managers", "warnings"},
        )
        self.assertEqual(report["managers"][0]["counts"]["crm_activity_observed"], 2)

    def test_report_projects_manager_name_deal_timeline_and_each_view(self) -> None:
        upsert_deal_control_deal(
            self.db_path, deal_id="900", source="manager", title="Сделка", manager_id="10",
            manager_name="Иван Петров", stage_id="NEW", stage_name="Новая", pipeline_id="15",
            amount="100", currency_id="RUB", created_at_crm=NOW.isoformat(),
            modified_at_crm=NOW.isoformat(), is_active=True,
        )
        events = [
            ("generated", "recommendation_generated", NOW - timedelta(hours=3), None),
            ("shown", "recommendation_shown", NOW - timedelta(hours=2, minutes=55), {
                "actor_verified": True, "actor_role": "manager", "actor_manager_id": "10",
            }),
            ("view-1", "recommendation_viewed", NOW - timedelta(hours=2, minutes=50), {
                "actor_verified": True, "actor_role": "manager", "actor_manager_id": "10",
                "occurrence_id": "view-1",
            }),
            ("view-2", "recommendation_viewed", NOW - timedelta(hours=2), {
                "actor_verified": True, "actor_role": "manager", "actor_manager_id": "10",
                "occurrence_id": "view-2",
            }),
        ]
        for key, event_type, occurred_at, payload in events:
            record_manager_trajectory_event(
                self.db_path, entity_type="deal", entity_id="900", manager_id="10",
                event_type=event_type, recommendation_kind="deal_task", recommendation_id="77",
                source="fixture", source_event_key=key, occurred_at=occurred_at.isoformat(), payload=payload,
            )
        for key, occurred_at, last_updated, activity_id in (
            ("activity-old", NOW - timedelta(hours=3, minutes=10), NOW - timedelta(hours=3), "a1"),
            ("activity-new-version", NOW - timedelta(hours=3, minutes=10), NOW - timedelta(hours=1), "a1"),
            ("activity-after-first-view", NOW - timedelta(hours=2, minutes=40), NOW - timedelta(hours=2, minutes=40), "a2"),
        ):
            record_manager_trajectory_event(
                self.db_path, entity_type="deal", entity_id="900", manager_id="10",
                event_type="crm_activity_observed", source="bitrix", source_event_key=key,
                occurred_at=occurred_at.isoformat(), payload={
                    "activity_id": activity_id, "activity_kind": "call", "direction": "2",
                    "completed": True, "last_updated": last_updated.isoformat(),
                },
            )
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="900", manager_id="10",
            event_type="deal_stage_changed", source="bitrix", source_event_key="stage-after-second-view",
            occurred_at=(NOW - timedelta(hours=1, minutes=30)).isoformat(),
            payload={"from_stage_id": "NEW", "to_stage_id": "PREPAYMENT_INVOICE"},
        )

        report = build_manager_trajectory_report(
            db_path=self.db_path, from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        manager = report["managers"][0]
        self.assertEqual(manager["manager_name"], "Иван Петров")
        self.assertEqual(manager["workday"]["unique_crm_actions"], 2)
        deal = next(item for item in manager["workday"]["entities"] if item["entity_id"] == "900")
        self.assertEqual(len(deal["crm_actions"]), 2)
        self.assertEqual(len(deal["stage_changes"]), 1)
        recommendation = manager["product_usage"]["recommendations"][0]
        self.assertEqual(recommendation["view_count"], 2)
        self.assertEqual(recommendation["view_tracking_precision"], "occurrence")
        views = manager["correlations"][0]["views"]
        self.assertEqual(len(views), 2)
        self.assertEqual([item["activity_id"] for item in views[0]["actions_before_since_previous_view"]], ["a1"])
        self.assertEqual([item["activity_id"] for item in views[0]["actions_after_until_next_view"]], ["a2"])
        self.assertEqual(views[1]["actions_after_until_next_view"][0]["action_type"], "stage_change")

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

    def test_report_counts_only_verified_manager_adoption(self) -> None:
        viewed_at = NOW - timedelta(hours=2)
        for source_key, payload in (
            ("verified", {"actor_verified": True, "actor_role": "manager", "actor_manager_id": "10"}),
            ("legacy", None),
            ("wrong-actor", {"actor_verified": True, "actor_role": "manager", "actor_manager_id": "77"}),
        ):
            record_manager_trajectory_event(
                self.db_path,
                entity_type="deal",
                entity_id="900",
                manager_id="10",
                event_type="recommendation_viewed",
                recommendation_kind="deal_task",
                recommendation_id=source_key,
                source="manager_ui",
                source_event_key=f"viewed:{source_key}",
                occurred_at=viewed_at.isoformat(),
                payload=payload,
            )
        record_manager_trajectory_event(
            self.db_path,
            entity_type="deal",
            entity_id="900",
            manager_id="10",
            event_type="crm_activity_observed",
            source="bitrix",
            source_event_key="activity:inside-window",
            occurred_at=(viewed_at + timedelta(minutes=15)).isoformat(),
        )

        report = build_manager_trajectory_report(
            db_path=self.db_path, from_at=NOW - timedelta(days=1), to_at=NOW,
        )
        manager = report["managers"][0]
        self.assertEqual(manager["counts"]["recommendation_viewed"], 1)
        self.assertEqual(manager["excluded_unverified_lifecycle_events"], 2)
        self.assertEqual(len(manager["viewed_windows_60m"]), 1)
        self.assertEqual(manager["viewed_windows_60m"][0]["target_entity_events"], 1)
        self.assertTrue(any("Исключено неподтверждённых" in warning for warning in report["warnings"]))
        self.assertTrue(any("LAST_UPDATED" in warning for warning in report["warnings"]))
        self.assertTrue(any("между ручными сборами" in warning for warning in report["warnings"]))


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
                    occurrence_id="view-123",
                ),
            )
        self.assertEqual(result, {"ok": True, "event_id": 7})
        self.assertEqual(record.call_args.kwargs["auth_user_id"], 42)
        self.assertEqual(record.call_args.kwargs["event_type"], "recommendation_viewed")
        self.assertEqual(record.call_args.kwargs["occurrence_id"], "view-123")

    def test_endpoint_maps_actor_permission_error_to_403(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "require_deal"), patch.object(
            api_app, "auth_current_user", return_value={"id": 7, "role": "admin", "manager_id": None},
        ), patch.object(
            api_app.storage,
            "record_recommendation_lifecycle_event",
            side_effect=PermissionError("только менеджер"),
            create=True,
        ):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.deal_recommendation_event_create(
                    "101",
                    api_app.RecommendationEventRequest(
                        event_type="shown", recommendation_kind="deal_task", recommendation_id=9,
                    ),
                )
        self.assertEqual(raised.exception.status_code, 403)


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
        self.assertEqual(
            set(payload),
            {"schema_version", "period", "collection_status", "summary", "managers", "warnings"},
        )
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["managers"][0]["manager_id"], "10")

    def test_report_text_has_three_human_readable_sections(self) -> None:
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
                    "--from", "2026-08-16", "--to", "2026-08-16",
                ])
            rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("1. Чем занимался в течение дня", rendered)
        self.assertIn("2. Как использовал НейроРОП", rendered)
        self.assertIn("3. Что происходило до и после просмотров", rendered)


if __name__ == "__main__":
    unittest.main()
