import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api.manager_trajectory_ui import (
    build_day_projection,
    build_entity_projection,
    build_event_detail_projection,
    build_window_projection,
)
from setup import MSK_TZ
from storage.rop_db import (
    init_db,
    observe_manager_trajectory_business_snapshot,
    record_manager_trajectory_event,
    save_deal_control_scope,
    save_manager_trajectory_collection_state,
    upsert_deal_control_deal,
)


DAY = date(2026, 8, 20)
START = datetime(2026, 8, 20, 9, 0, tzinfo=MSK_TZ)


class ManagerTrajectoryUiProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "trajectory-ui.sqlite"
        init_db(self.db_path)
        save_deal_control_scope(
            self.db_path, initial_deal_ids=[], manager_ids=["10"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="101", source="manager", title="ООО Альфа",
            manager_id="10", manager_name="Иван Петров", stage_id="NEW",
            stage_name="Новая", pipeline_id="15", amount="100", currency_id="RUB",
            created_at_crm=START.isoformat(), modified_at_crm=START.isoformat(), is_active=True,
        )
        observe_manager_trajectory_business_snapshot(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            snapshot={"TITLE": "Лид Бета", "STATUS_ID": "NEW"}, modified_at=START.isoformat(),
            field_allowlist=["TITLE", "STATUS_ID"],
        )
        self._event("deal", "101", "call", START + timedelta(minutes=5), "a1")
        self._event("lead", "202", "email", START + timedelta(minutes=15), "a2")
        self._event("deal", "101", "task", START + timedelta(minutes=35), "a3")
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="recommendation_viewed", recommendation_kind="deal_task",
            recommendation_id="77", source="manager_ui", source_event_key="view:77",
            occurred_at=(START + timedelta(minutes=2)).isoformat(),
            payload={
                "actor_verified": True, "actor_role": "manager", "actor_manager_id": "10",
                "occurrence_id": "view-77",
            },
        )
        save_manager_trajectory_collection_state(
            self.db_path, status="success", successful_through=(START + timedelta(hours=1)).isoformat(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, entity_type: str, entity_id: str, kind: str, at: datetime, activity_id: str) -> None:
        record_manager_trajectory_event(
            self.db_path, entity_type=entity_type, entity_id=entity_id, manager_id="10",
            event_type="crm_activity_observed", source="bitrix",
            source_event_key=f"activity:{activity_id}", occurred_at=at.isoformat(),
            payload={
                "activity_id": activity_id, "activity_kind": kind,
                "last_updated": at.isoformat(), "subject": f"Событие {activity_id}",
                "call": {"duration_seconds": 65} if kind == "call" else {},
            },
        )

    def test_day_is_lightweight_and_counts_deals_leads_density_and_switches(self) -> None:
        result = build_day_projection(value=DAY, bucket_minutes=30, db_path=self.db_path)

        self.assertEqual(result["totals"]["events"], 4)
        manager = result["managers"][0]
        self.assertEqual(manager["totals"]["deals"], 1)
        self.assertEqual(manager["totals"]["leads"], 1)
        self.assertEqual(manager["attention"]["context_switches"]["deal_lead_total"], 2)
        self.assertEqual([item["count"] for item in manager["buckets"]], [3, 1])
        self.assertNotIn("chronology", manager)
        self.assertNotIn("description", manager["buckets"][0])

    def test_window_adds_only_temporal_relation_not_causality(self) -> None:
        result = build_window_projection(
            manager_id="10", from_at=START, to_at=START + timedelta(minutes=30),
            db_path=self.db_path,
        )

        call = next(item for item in result["events"] if item["label"] == "Звонок")
        self.assertEqual(call["duration_seconds"], 65)
        self.assertTrue(call["expandable"])
        self.assertEqual(call["temporal_relation"]["kind"], "after_recommendation_view")
        self.assertIn("после просмотра", call["temporal_relation"]["text"])
        self.assertNotIn("вызвал", call["temporal_relation"]["text"])

    def test_filters_and_entity_projection_are_lazy(self) -> None:
        day = build_day_projection(value=DAY, category="leads", query="Бета", db_path=self.db_path)
        self.assertEqual(day["totals"]["events"], 1)

        entity = build_entity_projection(
            entity_type="lead", entity_id="202", value=DAY, db_path=self.db_path,
        )
        self.assertEqual(entity["title"], "Лид Бета")
        self.assertEqual(len(entity["chronology"]), 1)

    def test_event_detail_loads_full_comment_and_existing_transcript_only(self) -> None:
        comment = record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="crm_timeline_comment_observed", source="bitrix_timeline",
            source_event_key="comment:1", occurred_at=(START + timedelta(minutes=20)).isoformat(),
            payload={"comment_id": "1", "comment": "Полный сохранённый комментарий CRM"},
        )
        comment_detail = build_event_detail_projection(
            manager_id="10", event_id=str(comment["id"]), value=DAY, db_path=self.db_path,
        )
        self.assertEqual(comment_detail["description"], "Полный сохранённый комментарий CRM")
        self.assertIsNone(comment_detail["transcript_text"])

        with patch(
            "api.manager_trajectory_ui.find_call_transcript",
            return_value={"text": "Клиент подтвердил следующий шаг", "truncated": False},
        ):
            call_detail = build_event_detail_projection(
                manager_id="10", event_id="1", value=DAY, db_path=self.db_path,
            )
        self.assertEqual(call_detail["duration_seconds"], 65)
        self.assertEqual(call_detail["transcript_text"], "Клиент подтвердил следующий шаг")

        with patch("api.manager_trajectory_ui.find_call_transcript", return_value=None):
            no_transcript = build_event_detail_projection(
                manager_id="10", event_id="1", value=DAY, db_path=self.db_path,
            )
        self.assertIsNone(no_transcript["transcript_text"])


class ManagerTrajectoryUiAccessTests(unittest.TestCase):
    def test_day_endpoint_requires_admin_server_side(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value={"role": "rop"}):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.manager_trajectory_day_get(date_=DAY)
        self.assertEqual(raised.exception.status_code, 403)

    def test_day_endpoint_calls_projection_for_admin(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value={"role": "admin"}), patch.object(
            api_app, "build_manager_trajectory_day", return_value={"date": DAY.isoformat()},
        ) as build:
            result = api_app.manager_trajectory_day_get(date_=DAY, manager_id="10")
        self.assertEqual(result["date"], DAY.isoformat())
        self.assertEqual(build.call_args.kwargs["manager_ids"], ["10"])

    def test_http_query_parses_browser_bucket_minutes(self) -> None:
        from fastapi.testclient import TestClient
        from api import app as api_app

        admin = {
            "id": 1, "login": "admin", "role": "admin",
            "manager_id": None, "is_active": True,
        }
        with patch.object(api_app, "authenticate_request", return_value=admin), patch.object(
            api_app, "build_manager_trajectory_day", return_value={"date": DAY.isoformat()},
        ) as build:
            response = TestClient(api_app.app).get(
                "/api/admin/trajectory/day?date=2026-08-21&bucket_minutes=30",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"date": DAY.isoformat()})
        self.assertEqual(build.call_args.kwargs["bucket_minutes"], 30)

    def test_event_detail_endpoint_is_admin_only(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value={"role": "rop"}):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.manager_trajectory_event_get("1", manager_id="10", date_=DAY)
        self.assertEqual(raised.exception.status_code, 403)

        payload = {"event_id": 1, "duration_seconds": 65, "transcript_text": None}
        with patch.object(api_app, "auth_current_user", return_value={"role": "admin"}), patch.object(
            api_app, "build_manager_trajectory_event_detail", return_value=payload,
        ):
            self.assertEqual(
                api_app.manager_trajectory_event_get("1", manager_id="10", date_=DAY),
                payload,
            )


if __name__ == "__main__":
    unittest.main()
