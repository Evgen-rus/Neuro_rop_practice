import inspect
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api.manager_trajectory import build_manager_trajectory_report, project_manager_trajectory_for_display
from api.manager_trajectory_ui import (
    build_day_export,
    build_day_projection,
    build_entity_projection,
    build_event_detail_projection,
    build_window_projection,
    day_export_filename,
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


class _ManagerTrajectoryUiFixture(unittest.TestCase):
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
        self._event("deal", "101", "call", START + timedelta(minutes=5), "a1", direction="2")
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

    def _event(
        self,
        entity_type: str,
        entity_id: str,
        kind: str,
        at: datetime,
        activity_id: str,
        *,
        manager_id: str = "10",
        description: str | None = None,
        direction: str | None = None,
        duration_seconds: int | None = 65,
    ) -> None:
        record_manager_trajectory_event(
            self.db_path, entity_type=entity_type, entity_id=entity_id, manager_id=manager_id,
            event_type="crm_activity_observed", source="bitrix",
            source_event_key=f"activity:{activity_id}", occurred_at=at.isoformat(),
            payload={
                "activity_id": activity_id, "activity_kind": kind,
                "last_updated": at.isoformat(), "subject": f"Событие {activity_id}",
                "description": description,
                "completed": kind == "task",
                "direction": direction,
                "call": {"duration_seconds": duration_seconds} if kind == "call" else {},
            },
        )


class ManagerTrajectoryUiProjectionTests(_ManagerTrajectoryUiFixture):
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
        self.assertFalse(result["collection"]["is_current_day"])
        self.assertIsNotNone(result["collection"]["last_success_at"])

    def test_current_day_excludes_events_after_now_from_buckets_and_totals(self) -> None:
        now = datetime(2026, 8, 20, 8, 30, tzinfo=MSK_TZ)

        with patch("api.manager_trajectory_ui._now_moscow", return_value=now):
            result = build_day_projection(value=DAY, bucket_minutes=60, db_path=self.db_path)

        self.assertEqual(result["period"]["to"], "2026-08-20T08:30:00+03:00")
        self.assertEqual(result["axis"]["to"], "2026-08-20T08:30:00+03:00")
        self.assertEqual(result["totals"]["events"], 0)
        manager = result["managers"][0]
        self.assertEqual(manager["totals"]["events"], 0)
        self.assertEqual(manager["totals"]["communications"], 0)
        self.assertEqual(manager["totals"]["tasks"], 0)
        self.assertEqual(manager["totals"]["crm"], 0)
        self.assertEqual(manager["buckets"], [])

    def test_historical_day_keeps_real_event_at_1712(self) -> None:
        occurred_at = datetime(2026, 8, 20, 17, 12, tzinfo=MSK_TZ)
        self._event("lead", "202", "call", occurred_at, "historical-1712")

        with patch(
            "api.manager_trajectory_ui._now_moscow",
            return_value=datetime(2026, 8, 22, 8, 30, tzinfo=MSK_TZ),
        ):
            result = build_day_projection(value=DAY, bucket_minutes=60, db_path=self.db_path)

        manager = result["managers"][0]
        bucket = next(item for item in manager["buckets"] if item["from"].startswith("2026-08-20T17:00"))
        self.assertEqual(bucket["count"], 1)
        self.assertEqual(manager["totals"]["events"], 5)

    def test_call_summary_splits_direction_and_known_duration(self) -> None:
        self._event(
            "deal", "101", "call", START + timedelta(minutes=6), "incoming",
            direction="1", duration_seconds=125,
        )
        self._event(
            "deal", "101", "call", START + timedelta(minutes=7), "outgoing",
            direction="outgoing", duration_seconds=35,
        )
        self._event(
            "deal", "101", "call", START + timedelta(minutes=8), "unknown",
            duration_seconds=None,
        )

        manager = build_day_projection(value=DAY, db_path=self.db_path)["managers"][0]

        self.assertEqual(manager["totals"]["calls"], 4)
        self.assertEqual(
            manager["call_summary"]["incoming"],
            {"count": 1, "duration_seconds": 125, "missing_duration": 0},
        )
        self.assertEqual(
            manager["call_summary"]["outgoing"],
            {"count": 2, "duration_seconds": 100, "missing_duration": 0},
        )
        self.assertEqual(
            manager["call_summary"]["unknown"],
            {"count": 1, "duration_seconds": 0, "missing_duration": 1},
        )

    def test_default_bucket_is_one_hour_and_fifteen_minutes_is_rejected(self) -> None:
        result = build_day_projection(value=DAY, db_path=self.db_path)

        self.assertEqual(result["bucket_minutes"], 60)
        with self.assertRaisesRegex(ValueError, "30 или 60"):
            build_day_projection(value=DAY, bucket_minutes=15, db_path=self.db_path)

    def test_density_uses_one_maximum_across_visible_managers(self) -> None:
        save_deal_control_scope(
            self.db_path, initial_deal_ids=["101", "303"], manager_ids=["10", "20"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="303", source="manager", title="ООО Гамма",
            manager_id="20", manager_name="Анна Смирнова", stage_id="NEW",
            stage_name="Новая", pipeline_id="15", amount="100", currency_id="RUB",
            created_at_crm=START.isoformat(), modified_at_crm=START.isoformat(), is_active=True,
        )
        for index in range(8):
            self._event(
                "deal", "303", "call", START + timedelta(minutes=index), f"m20-{index}",
                manager_id="20",
            )

        result = build_day_projection(value=DAY, bucket_minutes=30, db_path=self.db_path)
        managers = {item["manager_id"]: item for item in result["managers"]}

        self.assertEqual(managers["20"]["buckets"][0]["density"], "peak")
        self.assertEqual(managers["10"]["buckets"][0]["count"], 3)
        self.assertEqual(managers["10"]["buckets"][0]["density"], "moderate")

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

    def test_ui_hides_generated_shown_and_renames_visible_neurorop_events(self) -> None:
        actor = {
            "actor_verified": True, "actor_role": "manager", "actor_manager_id": "10",
        }
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="recommendation_generated", recommendation_kind="deal_task",
            recommendation_id="77", source="fixture", source_event_key="gen:77",
            occurred_at=(START + timedelta(minutes=1)).isoformat(),
        )
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="recommendation_shown", recommendation_kind="deal_task",
            recommendation_id="77", source="fixture", source_event_key="shown:77",
            occurred_at=(START + timedelta(minutes=1, seconds=30)).isoformat(),
            payload=actor,
        )
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="recommendation_viewed", recommendation_kind="quick_help",
            recommendation_id="88", source="fixture", source_event_key="view:88",
            occurred_at=(START + timedelta(minutes=3)).isoformat(),
            payload={**actor, "occurrence_id": "view-88"},
        )
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="quick_help_opened", recommendation_kind="quick_help",
            recommendation_id="88", source="fixture", source_event_key="open:88",
            occurred_at=(START + timedelta(minutes=4)).isoformat(),
            payload={**actor, "occurrence_id": "open-88", "entrypoint": "workspace"},
        )

        window = build_window_projection(
            manager_id="10", from_at=START, to_at=START + timedelta(minutes=30),
            db_path=self.db_path,
        )
        neurorop = [item for item in window["events"] if item["category"] == "neurorop"]
        labels = [item["label"] for item in neurorop]
        event_types = {item["event_type"] for item in neurorop}

        self.assertEqual(
            labels,
            ["Кликнул сделку в НейроРОПе", "Открыл ответ Quick Help", "Зашёл в дожим сделки"],
        )
        self.assertEqual(event_types, {"recommendation_viewed", "quick_help_opened"})
        self.assertNotIn("recommendation_generated", event_types)
        self.assertNotIn("recommendation_shown", event_types)

        day = build_day_projection(value=DAY, bucket_minutes=30, db_path=self.db_path)
        self.assertEqual(day["managers"][0]["totals"]["neurorop"], 3)
        self.assertEqual(day["totals"]["neurorop"], 3)

        payload = build_day_export(value=DAY, db_path=self.db_path)
        usage = payload["managers"][0]["product_usage"]
        self.assertNotIn("generated", usage)
        self.assertNotIn("shown", usage)
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Рекомендация сформирована", dumped)
        self.assertNotIn("Рекомендация показана", dumped)
        self.assertNotIn("recommendation_generated", dumped)
        for recommendation in usage.get("recommendations") or []:
            self.assertNotIn("generated_at", recommendation)
            self.assertNotIn("shown_at", recommendation)

    def test_entity_communications_can_load_saved_content_lazily(self) -> None:
        self._event(
            "deal", "101", "email", START + timedelta(minutes=25), "a4",
            description="Полный текст письма",
        )
        self._event(
            "deal", "101", "message", START + timedelta(minutes=26), "a5",
            description="Полный текст сообщения",
        )
        entity = build_entity_projection(
            entity_type="deal", entity_id="101", value=DAY, db_path=self.db_path,
        )

        communications = {
            item["label"]: item for item in entity["chronology"]
            if item["label"] in {"Звонок", "Письмо", "Сообщение", "Задача"}
        }
        self.assertTrue(communications["Звонок"]["expandable"])
        self.assertEqual(communications["Звонок"]["duration_seconds"], 65)
        self.assertTrue(communications["Письмо"]["expandable"])
        self.assertTrue(communications["Сообщение"]["expandable"])
        self.assertTrue(communications["Задача"]["expandable"])

        with patch("api.manager_trajectory_ui.find_call_transcript") as transcript_lookup:
            email_detail = build_event_detail_projection(
                manager_id="10", event_id=str(communications["Письмо"]["event_id"]),
                value=DAY, db_path=self.db_path,
            )
        self.assertEqual(email_detail["description"], "Полный текст письма")
        transcript_lookup.assert_not_called()

        task_detail = build_event_detail_projection(
            manager_id="10", event_id=str(communications["Задача"]["event_id"]),
            value=DAY, db_path=self.db_path,
        )
        self.assertIn({"label": "Состояние", "value": "Завершена"}, task_detail["details"])

    def test_lead_stage_change_uses_pipeline_map_names(self) -> None:
        recorded = record_manager_trajectory_event(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            event_type="lead_stage_changed", source="bitrix",
            source_event_key="lead-stage-1",
            occurred_at=(START + timedelta(minutes=40)).isoformat(),
            payload={"from_stage_id": "UC_RVPA19", "to_stage_id": "UC_I4YXDJ"},
        )
        catalog = {
            "deal_pipelines": [],
            "lead_pipeline": {
                "id": "lead",
                "name": "Лиды",
                "stages": [
                    {"id": "UC_RVPA19", "name": "Перезвонить"},
                    {"id": "UC_I4YXDJ", "name": "Не удалось связаться"},
                ],
            },
        }

        with patch("api.manager_trajectory.list_crm_pipelines", return_value=catalog):
            window = build_window_projection(
                manager_id="10", from_at=START, to_at=START + timedelta(hours=1),
                db_path=self.db_path,
            )
            detail = build_event_detail_projection(
                manager_id="10", event_id=str(recorded["id"]), value=DAY, db_path=self.db_path,
            )

        stage_event = next(item for item in window["events"] if item["label"] == "Смена стадии")
        self.assertEqual(stage_event["description"], "Перезвонить → Не удалось связаться")
        facts = {item["label"]: item["value"] for item in detail["details"]}
        self.assertEqual(facts["Предыдущая стадия"], "Перезвонить")
        self.assertEqual(facts["Новая стадия"], "Не удалось связаться")

    def test_lead_creation_is_a_marker_not_manager_work(self) -> None:
        created_at = START - timedelta(hours=7)
        creation = record_manager_trajectory_event(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            event_type="crm_stage_history_observed", source="bitrix_stage_history",
            source_event_key="lead-created", occurred_at=created_at.isoformat(),
            payload={"history_type_id": "1", "stage_id": "NEW"},
        )
        moved = record_manager_trajectory_event(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            event_type="crm_stage_history_observed", source="bitrix_stage_history",
            source_event_key="lead-taken", occurred_at=(START + timedelta(minutes=16)).isoformat(),
            payload={"history_type_id": "2", "stage_id": "IN_PROCESS"},
        )

        day = build_day_projection(value=DAY, bucket_minutes=60, db_path=self.db_path)
        entity = build_entity_projection(
            entity_type="lead", entity_id="202", value=DAY, db_path=self.db_path,
        )

        self.assertEqual(day["totals"]["events"], 5)
        self.assertEqual(entity["created_at"], created_at.isoformat())
        chronology_ids = {item["event_id"] for item in entity["chronology"]}
        self.assertNotIn(creation["id"], chronology_ids)
        self.assertIn(moved["id"], chronology_ids)

    def test_stage_change_falls_back_to_id_when_name_is_missing(self) -> None:
        recorded = record_manager_trajectory_event(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            event_type="lead_stage_changed", source="bitrix",
            source_event_key="lead-stage-unknown",
            occurred_at=(START + timedelta(minutes=41)).isoformat(),
            payload={"from_stage_id": "UC_UNKNOWN", "to_stage_id": "UC_I4YXDJ"},
        )
        catalog = {
            "deal_pipelines": [],
            "lead_pipeline": {
                "id": "lead",
                "name": "Лиды",
                "stages": [
                    {"id": "UC_I4YXDJ", "name": "Не удалось связаться"},
                ],
            },
        }

        with patch("api.manager_trajectory.list_crm_pipelines", return_value=catalog):
            window = build_window_projection(
                manager_id="10", from_at=START, to_at=START + timedelta(hours=1),
                db_path=self.db_path,
            )
            detail = build_event_detail_projection(
                manager_id="10", event_id=str(recorded["id"]), value=DAY, db_path=self.db_path,
            )

        stage_event = next(item for item in window["events"] if item["label"] == "Смена стадии")
        self.assertEqual(stage_event["description"], "UC_UNKNOWN → Не удалось связаться")
        facts = {item["label"]: item["value"] for item in detail["details"]}
        self.assertEqual(facts["Предыдущая стадия"], "UC_UNKNOWN")
        self.assertEqual(facts["Новая стадия"], "Не удалось связаться")

    def test_business_field_change_detail_is_human_readable(self) -> None:
        observed = observe_manager_trajectory_business_snapshot(
            self.db_path, entity_type="lead", entity_id="202", manager_id="10",
            snapshot={"TITLE": "Лид Бета", "STATUS_ID": "IN_PROCESS"},
            modified_at=(START + timedelta(minutes=45)).isoformat(),
            field_allowlist=["TITLE", "STATUS_ID"],
        )
        status_change = next(
            item for item in observed["events"]
            if item["payload"]["field_name"] == "STATUS_ID"
        )

        detail = build_event_detail_projection(
            manager_id="10", event_id=str(status_change["id"]), value=DAY, db_path=self.db_path,
        )

        facts = {item["label"]: item["value"] for item in detail["details"]}
        self.assertEqual(facts["Поле"], "STATUS_ID")
        self.assertEqual(facts["Было"], "NEW")
        self.assertEqual(facts["Стало"], "IN_PROCESS")

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

    def test_messenger_mirror_is_a_communication_in_ui_and_json(self) -> None:
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="crm_timeline_comment_observed", source="bitrix_timeline",
            source_event_key="comment:max-1",
            occurred_at=(START + timedelta(minutes=25)).isoformat(),
            payload={
                "comment_id": "max-1",
                "author_id": "99",
                "comment": (
                    "[img]https://static.wazzup24.com/images/bitrix/max.png[/img] "
                    "Александр:\nНаправляем предварительное КП."
                ),
                "is_messenger_mirror": True,
                "channel": "max",
                "speaker": "Александр",
                "content": "Направляем предварительное КП.",
                "author_is_manager": False,
                "responsible_id": "10",
            },
        )
        record_manager_trajectory_event(
            self.db_path, entity_type="deal", entity_id="101", manager_id="10",
            event_type="crm_timeline_comment_observed", source="bitrix_timeline",
            source_event_key="comment:note-1",
            occurred_at=(START + timedelta(minutes=26)).isoformat(),
            payload={"comment_id": "note-1", "author_id": "10", "comment": "Заметка менеджера"},
        )

        day = build_day_projection(value=DAY, bucket_minutes=60, db_path=self.db_path)
        self.assertEqual(day["managers"][0]["totals"]["communications"], 3)
        self.assertGreaterEqual(day["managers"][0]["totals"]["crm"], 1)

        entity = build_entity_projection(
            entity_type="deal", entity_id="101", value=DAY, db_path=self.db_path,
        )
        labels = [item["label"] for item in entity["chronology"]]
        self.assertIn("Сообщение Max", labels)
        self.assertIn("Комментарий CRM", labels)
        message = next(item for item in entity["chronology"] if item["label"] == "Сообщение Max")
        self.assertEqual(message["category"], "communications")
        self.assertEqual(message["description"], "Направляем предварительное КП.")
        note = next(item for item in entity["chronology"] if item["label"] == "Комментарий CRM")
        self.assertEqual(note["category"], "crm")

        export = build_day_export(value=DAY, db_path=self.db_path)
        comments = [
            item
            for entity_row in export["managers"][0]["workday"]["entities"]
            for item in entity_row.get("timeline_comments") or []
            if entity_row.get("entity_id") == "101"
        ]
        mirrored = next(item for item in comments if item["payload"].get("channel") == "max")
        self.assertEqual(mirrored["activity_kind"], "message")
        self.assertEqual(mirrored["subject"], "Сообщение Max")
        self.assertEqual(mirrored["description"], "Направляем предварительное КП.")
        self.assertEqual(mirrored["payload"]["author_id"], "99")

        communications = build_day_export(value=DAY, category="communications", db_path=self.db_path)
        exported_comments = [
            item
            for entity_row in communications["managers"][0]["workday"]["entities"]
            for item in entity_row.get("timeline_comments") or []
        ]
        self.assertTrue(any(item["payload"].get("channel") == "max" for item in exported_comments))
        self.assertFalse(any(item["payload"].get("comment_id") == "note-1" for item in exported_comments))


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

    def test_http_default_bucket_is_one_hour(self) -> None:
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
                "/api/admin/trajectory/day?date=2026-08-21",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(build.call_args.kwargs["bucket_minutes"], 60)

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


class ManagerTrajectoryDayExportTests(_ManagerTrajectoryUiFixture):
    def _export(self, **kwargs):
        return build_day_export(value=DAY, db_path=self.db_path, **kwargs)

    def _crm_actions(self, payload: dict, entity_type: str | None = None) -> list[dict]:
        actions = []
        for manager in payload.get("managers") or []:
            for entity in manager.get("workday", {}).get("entities") or []:
                if entity_type and entity.get("entity_type") != entity_type:
                    continue
                actions.extend(entity.get("crm_actions") or [])
        return actions

    def test_historical_day_export_uses_the_whole_day(self) -> None:
        occurred_at = datetime(2026, 8, 20, 17, 12, tzinfo=MSK_TZ)
        self._event("lead", "202", "call", occurred_at, "historical-export-1712")

        with patch(
            "api.manager_trajectory_ui._now_moscow",
            return_value=datetime(2026, 8, 22, 8, 30, tzinfo=MSK_TZ),
        ):
            payload = self._export()

        self.assertEqual(payload["period"]["from"], "2026-08-20T00:00:00+03:00")
        self.assertEqual(payload["period"]["to"], "2026-08-21T00:00:00+03:00")
        activity_ids = {item.get("activity_id") for item in self._crm_actions(payload)}
        self.assertIn("historical-export-1712", activity_ids)

    def test_current_day_export_is_clipped_to_now(self) -> None:
        now = datetime(2026, 8, 20, 8, 30, tzinfo=MSK_TZ)

        with patch("api.manager_trajectory_ui._now_moscow", return_value=now):
            payload = self._export()

        self.assertEqual(payload["period"]["to"], "2026-08-20T08:30:00+03:00")
        self.assertEqual(self._crm_actions(payload), [])
        self.assertEqual(payload["summary"]["unique_crm_actions"], 0)

    def test_manager_id_keeps_only_that_manager(self) -> None:
        save_deal_control_scope(
            self.db_path, initial_deal_ids=["101", "303"], manager_ids=["10", "20"], pipeline_id="15",
        )
        upsert_deal_control_deal(
            self.db_path, deal_id="303", source="manager", title="ООО Гамма",
            manager_id="20", manager_name="Анна Смирнова", stage_id="NEW",
            stage_name="Новая", pipeline_id="15", amount="100", currency_id="RUB",
            created_at_crm=START.isoformat(), modified_at_crm=START.isoformat(), is_active=True,
        )
        self._event("deal", "303", "call", START + timedelta(minutes=3), "m20-call", manager_id="20")

        payload = self._export(manager_ids=["10"])

        self.assertEqual([item["manager_id"] for item in payload["managers"]], ["10"])
        self.assertEqual(payload["export"]["filters"]["manager_id"], "10")
        self.assertEqual(day_export_filename(DAY, "10"), "trajectory-2026-08-20-manager-10.json")

    def test_category_deals_drops_lead_only_data(self) -> None:
        payload = self._export(category="deals")
        entities = [
            entity
            for manager in payload["managers"]
            for entity in manager.get("workday", {}).get("entities") or []
        ]
        self.assertTrue(entities)
        self.assertTrue(all(item.get("entity_type") == "deal" for item in entities))
        self.assertEqual(payload["managers"][0]["workday"]["leads_touched"], 0)
        self.assertFalse(any(item.get("entity_type") == "lead" for item in self._crm_actions(payload)))

    def test_category_communications_keeps_full_communication_payload(self) -> None:
        long_text = "Полный технический текст письма клиента про поставку и оплату. " * 8
        self._event(
            "deal", "101", "email", START + timedelta(minutes=12), "full-mail",
            description=long_text,
        )

        payload = self._export(category="communications")
        mail = next(item for item in self._crm_actions(payload) if item.get("activity_id") == "full-mail")
        call = next(item for item in self._crm_actions(payload) if item.get("activity_id") == "a1")

        self.assertGreater(len(long_text), 220)
        self.assertEqual(mail["description"], long_text)
        self.assertFalse(str(mail["description"]).endswith("…"))
        self.assertEqual(call["call"]["duration_seconds"], 65)
        self.assertEqual(call["subject"], "Событие a1")
        task_ids = {item.get("activity_id") for item in self._crm_actions(payload)}
        self.assertNotIn("a3", task_ids)
        self.assertEqual(payload["managers"][0]["workday"]["task_history_events"], 0)
        self.assertEqual(payload["managers"][0]["product_usage"]["viewed"], 0)
        self.assertNotIn("generated", payload["managers"][0]["product_usage"])
        self.assertNotIn("shown", payload["managers"][0]["product_usage"])

    def test_query_filters_by_entity_id_and_title(self) -> None:
        by_title = self._export(query="Бета")
        by_id = self._export(query="101")

        titles = [
            entity.get("title")
            for manager in by_title["managers"]
            for entity in manager.get("workday", {}).get("entities") or []
        ]
        self.assertEqual(titles, ["Лид Бета"])
        ids = [
            entity.get("entity_id")
            for manager in by_id["managers"]
            for entity in manager.get("workday", {}).get("entities") or []
        ]
        self.assertEqual(ids, ["101"])

    def test_unfiltered_export_keeps_full_technical_report(self) -> None:
        start = datetime(2026, 8, 20, 0, 0, tzinfo=MSK_TZ)
        end = datetime(2026, 8, 21, 0, 0, tzinfo=MSK_TZ)
        with patch(
            "api.manager_trajectory_ui._now_moscow",
            return_value=datetime(2026, 8, 22, 12, 0, tzinfo=MSK_TZ),
        ):
            payload = self._export()
            report = build_manager_trajectory_report(
                db_path=self.db_path, from_at=start, to_at=end,
            )
        content = {key: value for key, value in payload.items() if key != "export"}
        self.assertEqual(content, project_manager_trajectory_for_display(report))
        usage = payload["managers"][0]["product_usage"]
        self.assertNotIn("generated", usage)
        self.assertNotIn("shown", usage)
        self.assertNotIn("recommendations_generated", payload["summary"])
        self.assertNotIn("recommendations_shown", payload["summary"])
        for recommendation in usage.get("recommendations") or []:
            self.assertNotIn("generated_at", recommendation)
            self.assertNotIn("shown_at", recommendation)
        call = next(item for item in self._crm_actions(payload) if item.get("activity_id") == "a1")
        self.assertEqual(call["call"]["duration_seconds"], 65)
        self.assertIn("payload", call)
        self.assertEqual(payload["export"]["filters"], {
            "manager_id": None, "category": "all", "q": "",
        })
        self.assertEqual(payload["export"]["timezone"], "Europe/Moscow")

    def test_bucket_minutes_is_not_part_of_export_contract(self) -> None:
        from api import app as api_app

        self.assertNotIn(
            "bucket_minutes",
            inspect.signature(api_app.manager_trajectory_export_day_get).parameters,
        )
        self.assertNotIn(
            "bucket_minutes",
            inspect.signature(build_day_export).parameters,
        )

        admin = {
            "id": 1, "login": "admin", "role": "admin",
            "manager_id": None, "is_active": True,
        }
        with patch.object(api_app, "authenticate_request", return_value=admin), patch.object(
            api_app, "build_manager_trajectory_day_export", return_value={"ok": True},
        ) as build:
            from fastapi.testclient import TestClient
            response = TestClient(api_app.app).get(
                "/api/admin/trajectory/export/day?date=2026-08-20&bucket_minutes=30",
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("bucket_minutes", build.call_args.kwargs)

    def test_export_endpoint_requires_admin_and_sets_download_headers(self) -> None:
        from fastapi.testclient import TestClient
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value={"role": "rop"}):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.manager_trajectory_export_day_get(date_=DAY)
        self.assertEqual(raised.exception.status_code, 403)

        admin = {
            "id": 1, "login": "admin", "role": "admin",
            "manager_id": None, "is_active": True,
        }
        with patch.object(api_app, "authenticate_request", return_value=admin), patch.object(
            api_app, "build_manager_trajectory_day_export",
            return_value={"title": "ООО Альфа", "note": "Кириллица"},
        ):
            response = TestClient(api_app.app).get(
                "/api/admin/trajectory/export/day?date=2026-08-20&manager_id=10",
            )

        body = response.content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        self.assertIn("charset=utf-8", response.headers["content-type"])
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="trajectory-2026-08-20-manager-10.json"',
        )
        self.assertIn("ООО Альфа", body)
        self.assertNotIn("\\u041e", body)
        self.assertEqual(json.loads(body)["title"], "ООО Альфа")

    def test_frontend_window_loads_all_categories_then_filters_locally(self) -> None:
        source = Path("frontend/src/ManagerTrajectory.tsx").read_text(encoding="utf-8")
        self.assertIn("category: 'all'", source)
        self.assertIn("filterWindowEvents(windowData.events, selected)", source)
        self.assertIn("fetchTrajectoryDayExport", source)
        export_helper = Path("frontend/src/api.ts").read_text(encoding="utf-8").split("fetchTrajectoryDayExport")[1].split("fetchTrajectoryWindow")[0]
        self.assertNotIn("bucket_minutes", export_helper)


if __name__ == "__main__":
    unittest.main()
