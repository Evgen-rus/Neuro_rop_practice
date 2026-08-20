from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from api.deal_control import (
    _analysis_coaching,
    _today_communications,
    build_deal_control_dashboard,
    refresh_deal_control,
    save_checklist_item_completion,
)
from storage.rop_db import (
    confirm_deal_control_task_crm_match,
    connect,
    create_deal_control_task,
    dumps_json,
    get_deal_control_metrics,
    get_deal_control_scope,
    init_db,
    list_deal_control_task_history,
    list_deal_control_tasks,
    materialize_deal_recommendation_from_report,
    review_deal_control_task_crm_fact,
    save_deal_control_bitrix_tasks,
    save_deal_control_scope,
    save_deal_control_task_crm_fact,
    save_deal_control_task_outcome,
    save_ui_report,
    set_deal_control_bitrix_task_completion,
    update_deal_control_task,
    upsert_deal_control_deal,
)


MSK = ZoneInfo("Europe/Moscow")


class FakeBitrixClient:
    def __init__(self, *, initial=None, pipeline=None, activities=None):
        self.initial = initial or {}
        self.pipeline = pipeline or []
        self.activities = activities or {}
        self.calls: list[tuple[str, dict]] = []

    def list_all(self, method, payload=None):
        self.calls.append((method, payload or {}))
        if method == "crm.deal.list":
            deal_filter = (payload or {}).get("filter") or {}
            ids = deal_filter.get("ID")
            if ids:
                return [self.initial[item] for item in ids if item in self.initial]
            rows = list(self.pipeline)
            category_id = deal_filter.get("CATEGORY_ID")
            if category_id is not None:
                wanted = {str(item) for item in category_id} if isinstance(category_id, list) else {str(category_id)}
                rows = [item for item in rows if str(item.get("CATEGORY_ID") or "") in wanted]
            return rows
        if method == "user.get":
            return [
                {"ID": "10", "LAST_NAME": "Иванов", "NAME": "Иван"},
                {"ID": "20", "LAST_NAME": "Петров", "NAME": "Пётр"},
            ]
        raise AssertionError(method)

    def safe_list_all(self, method, payload=None):
        if method != "crm.activity.list":
            raise AssertionError(method)
        self.calls.append((method, payload or {}))
        owner_ids = ((payload or {}).get("filter") or {}).get("OWNER_ID") or []
        owner_ids = owner_ids if isinstance(owner_ids, list) else [owner_ids]
        result = []
        for owner_id in owner_ids:
            result.extend(self.activities.get(str(owner_id), []))
        activity_filter = (payload or {}).get("filter") or {}
        if activity_filter.get("PROVIDER_ID"):
            result = [item for item in result if item.get("PROVIDER_ID") == activity_filter["PROVIDER_ID"]]
        if activity_filter.get("COMPLETED"):
            result = [item for item in result if item.get("COMPLETED") == activity_filter["COMPLETED"]]
        if activity_filter.get(">LAST_UPDATED"):
            result = [
                item for item in result
                if str(item.get("LAST_UPDATED") or "") > str(activity_filter[">LAST_UPDATED"])
            ]
        return {"ok": True, "items": result}


def deal(
    deal_id: str,
    *,
    manager_id: str,
    closed: str = "N",
    category_id: str = "15",
    stage_id: str = "C15:NEW",
) -> dict:
    return {
        "ID": deal_id,
        "TITLE": f"Сделка {deal_id}",
        "ASSIGNED_BY_ID": manager_id,
        "CATEGORY_ID": category_id,
        "STAGE_ID": stage_id,
        "CLOSED": closed,
        "OPPORTUNITY": "120000",
        "CURRENCY_ID": "RUB",
        "DATE_CREATE": "2026-07-19T09:00:00+03:00",
        "DATE_MODIFY": "2026-07-20T09:00:00+03:00",
    }


class DealControlTests(unittest.TestCase):
    def test_init_db_migrates_legacy_deal_tasks_before_creating_neuro_index(self):
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
                        due_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()
            init_db(db_path)
            with connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(deal_control_tasks)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(deal_control_tasks)")}
            self.assertIn("source_kind", columns)
            self.assertIn("source_report_id", columns)
            self.assertIn("idx_deal_control_tasks_neuro_report", indexes)

            init_db(db_path)
            with connect(db_path) as conn:
                self.assertIn(
                    "idx_deal_control_tasks_neuro_report",
                    {row[1] for row in conn.execute("PRAGMA index_list(deal_control_tasks)")},
                )

    def _save_deal(self, db_path: Path) -> None:
        upsert_deal_control_deal(
            db_path,
            deal_id="101",
            source="initial",
            title="Сделка 101",
            manager_id="10",
            manager_name="Иванов Иван",
            stage_id="C15:NEW",
            stage_name="Новая",
            pipeline_id="15",
            amount="120000",
            currency_id="RUB",
            created_at_crm="2026-07-19T09:00:00+03:00",
            modified_at_crm="2026-07-20T09:00:00+03:00",
            is_active=True,
        )

    @staticmethod
    def _recommendation(text: str = "Связаться с клиентом") -> dict:
        return {
            "rop_manager_message_block": {
                "message_to_manager": text,
                "success_condition": "В CRM зафиксирован подтверждённый следующий шаг.",
                "deadline": "2026-08-10",
            },
            "manager_action_block": {"recommended_channel": "phone"},
        }

    def test_neuro_recommendation_materialization_is_idempotent_and_uses_latest_report(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            first_report = save_ui_report(
                db_path, entity_type="deal", entity_id="101", report_json=self._recommendation()
            )
            first = materialize_deal_recommendation_from_report(
                db_path, "101", first_report, self._recommendation()
            )
            again = materialize_deal_recommendation_from_report(
                db_path, "101", first_report, self._recommendation("Другой текст")
            )
            self.assertIsNotNone(first)
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(first["source_kind"], "neuro_rop")
            self.assertEqual(first["source_report_id"], first_report)
            self.assertEqual(first["due_at"], "2026-08-10T18:00:00+03:00")
            self.assertEqual(first["recommendation_state"], "not_done")
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_tasks").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_events").fetchone()[0], 1)

            second_report = save_ui_report(
                db_path, entity_type="deal", entity_id="101", report_json=self._recommendation("Новый шаг")
            )
            second = materialize_deal_recommendation_from_report(
                db_path, "101", second_report, self._recommendation("Новый шаг")
            )
            self.assertNotEqual(first["id"], second["id"])
            dashboard = build_deal_control_dashboard(db_path=db_path)
            self.assertEqual(dashboard["deals"][0]["current_task"]["id"], second["id"])

    def test_neuro_recommendation_rejects_out_of_scope_and_incomplete_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            report_id = save_ui_report(
                db_path, entity_type="deal", entity_id="101", report_json=self._recommendation()
            )
            save_deal_control_scope(db_path, initial_deal_ids=["999"], manager_ids=["10"], pipeline_id="15")
            self.assertIsNone(
                materialize_deal_recommendation_from_report(
                    db_path, "999", report_id, self._recommendation()
                )
            )
            incomplete = self._recommendation()
            incomplete["rop_manager_message_block"]["success_condition"] = None
            self.assertIsNone(
                materialize_deal_recommendation_from_report(db_path, "101", report_id, incomplete)
            )

    def test_recommendation_state_respects_evidence_boundaries_and_legacy_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            legacy = create_deal_control_task(
                db_path, deal_id="101", task_text="Ручная задача", touch_type="phone",
                expected_result=None, due_at="2026-08-10T12:00:00+03:00",
            )
            report_id = save_ui_report(
                db_path, entity_type="deal", entity_id="101", report_json=self._recommendation()
            )
            neuro = materialize_deal_recommendation_from_report(
                db_path, "101", report_id, self._recommendation()
            )
            self.assertEqual(legacy["source_kind"], "manual")
            self.assertIsNone(legacy["source_report_id"])
            self.assertEqual(list_deal_control_tasks(db_path)[0]["recommendation_state"], "not_done")

            save_deal_control_task_crm_fact(
                db_path, task_id=int(neuro["id"]), fact_key="stage:1", activity_id="1",
                fact_kind="stage_changed", summary="Стадия изменилась", occurred_at=None,
                contact_class="deal_progress",
            )
            self.assertEqual(
                next(item for item in list_deal_control_tasks(db_path) if item["id"] == neuro["id"])["recommendation_state"],
                "unconfirmed",
            )
            save_deal_control_task_outcome(
                db_path, task_id=int(neuro["id"]), contact_status="confirmed_contact",
                result_status="pending", result_note="Клиент ответил", next_step_text="Ждать решение",
                next_step_at="2026-08-11T12:00:00+03:00", evidence_kind="manager_confirmation",
                evidence_id=None, source_role="manager",
            )
            current = next(item for item in list_deal_control_tasks(db_path) if item["id"] == neuro["id"])
            self.assertEqual(current["recommendation_state"], "contacted")
            self.assertTrue(current["needs_follow_up"])

    def test_distinct_stage_progress_facts_with_no_activity_id_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            task = create_deal_control_task(
                db_path,
                deal_id="101",
                task_text="Проверить движение сделки",
                touch_type=None,
                expected_result=None,
                due_at="2026-08-11T12:00:00+03:00",
            )

            first = save_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_key="stage:NEW:PREPARATION:P",
                activity_id=None,
                fact_kind="stage_changed",
                summary="Стадия изменилась: Новая → Подготовка",
                occurred_at="2026-08-11T09:00:00+03:00",
                contact_class="deal_progress",
            )
            second = save_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_key="stage:PREPARATION:WON:S",
                activity_id=None,
                fact_kind="stage_changed",
                summary="Стадия изменилась: Подготовка → Успешно",
                occurred_at="2026-08-11T10:00:00+03:00",
                contact_class="deal_progress",
            )

            self.assertNotEqual(first["id"], second["id"])
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            self.assertEqual(
                {fact["fact_key"] for fact in history["crm_facts"]},
                {"stage:NEW:PREPARATION:P", "stage:PREPARATION:WON:S"},
            )

    def test_today_communications_counts_completed_calls_and_messages_in_moscow_day(self):
        activities = [
            {
                "ID": "701", "OWNER_ID": "101", "TYPE_ID": "4", "PROVIDER_ID": "CRM_EMAIL",
                "SUBJECT": "Сообщение клиенту", "DIRECTION": "2", "COMPLETED": "Y",
                "START_TIME": "2026-07-20T09:14:00+03:00",
            },
            {
                "ID": "702", "OWNER_ID": "101", "TYPE_ID": "2", "PROVIDER_ID": "CRM_CALL",
                "SUBJECT": "Первая попытка дозвона", "DIRECTION": "2", "COMPLETED": "Y",
                "START_TIME": "2026-07-20T10:22:00+03:00", "END_TIME": "2026-07-20T10:22:10+03:00",
            },
            {
                "ID": "703", "OWNER_ID": "101", "TYPE_ID": "2", "PROVIDER_ID": "CRM_CALL",
                "SUBJECT": "Повторная попытка дозвона", "DIRECTION": "2", "COMPLETED": "Y",
                "START_TIME": "2026-07-20T12:47:00+03:00", "END_TIME": "2026-07-20T12:47:10+03:00",
            },
            {
                "ID": "704", "OWNER_ID": "101", "TYPE_ID": "2", "PROVIDER_ID": "CRM_CALL",
                "SUBJECT": "Запланированный звонок", "DIRECTION": "2", "COMPLETED": "N",
                "START_TIME": "2026-07-20T15:00:00+03:00",
            },
            {
                "ID": "705", "OWNER_ID": "101", "TYPE_ID": "2", "PROVIDER_ID": "CRM_CALL",
                "SUBJECT": "Вчерашний звонок", "DIRECTION": "2", "COMPLETED": "Y",
                "START_TIME": "2026-07-19T12:00:00+03:00", "END_TIME": "2026-07-19T12:00:30+03:00",
            },
        ]

        result = _today_communications(activities, datetime(2026, 7, 20, 14, tzinfo=MSK))

        self.assertTrue(result["available"])
        self.assertEqual(result["target"], 3)
        self.assertEqual(result["completed"], 3)
        self.assertEqual(result["progress_percent"], 100)
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["duration_seconds"], 20)
        self.assertEqual([item["event_id"] for item in result["items"]], [
            "crm_activity:701", "crm_activity:702", "crm_activity:703",
        ])

    def test_analysis_coaching_projects_manager_message_call_variants_and_crm_checklist(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={
                    "deal_control_brief": {
                        "manager_coaching": "Красавчик, что согласовал следующий звонок. Теперь получи дату решения.",
                        "call_script": "Добрый день! Хочу сверить статус решения.",
                        "call_opening_variants": [
                            "Добрый день! Уточню статус нашего предложения.",
                            "Добрый день! Давайте определим следующий шаг.",
                        ],
                    },
                    "manager_action_block": {
                        "manager_checklist": ["Позиция клиента", "Дата следующего шага"],
                    },
                    "communication_quality_audit": {
                        "status": "insufficient_evidence",
                        "scope_summary": "Есть только попытка дозвона.",
                        "criteria": {
                            "next_action": {"score": None},
                            "value_development": {"score": None},
                            "data_collection": {"score": None},
                        },
                        "zero_reasons": [],
                        "summary_for_rop": None,
                        "insufficient_reason": "Нет содержательной коммуникации.",
                    },
                },
            )

            coaching = _analysis_coaching(db_path, "101")

            self.assertTrue(coaching["manager_coaching"].startswith("Красавчик"))
            self.assertEqual(len(coaching["script_variants"]), 2)
            self.assertEqual(coaching["crm_checklist"], ["Позиция клиента", "Дата следующего шага"])
            self.assertEqual(coaching["communication_quality_audit"]["status"], "insufficient_evidence")
            self.assertTrue(coaching["analysis_created_at"])

    def test_live_dashboard_reuses_daily_review_card_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={
                    "deal_control_brief": {
                        "current_situation": "Клиент получил КП.",
                        "direct_manager_question": "Ты получил дату решения комиссии?",
                        "manager_coaching": "Уточни дату решения комиссии и зафиксируй её в CRM.",
                        "call_script": "Добрый день, сверю статус.",
                    },
                    "communication_quality_audit": {
                        "status": "assessed",
                        "scope_summary": "Учтён звонок.",
                        "criteria": {
                            "next_action": {"score": 0},
                            "value_development": {"score": 1},
                            "data_collection": {"score": 1},
                        },
                        "zero_reasons": [
                            {"criterion": "next_action", "explanation": "Нет даты", "quote": "позже"}
                        ],
                        "summary_for_rop": "Нужна дата решения.",
                        "insufficient_reason": None,
                    },
                },
            )

            deal = build_deal_control_dashboard(db_path=db_path)["deals"][0]
            review = deal["review"]

            self.assertEqual(review["generic_question"], "Что было сделано и какой сейчас статус?")
            self.assertEqual(review["direct_question"], "Ты получил дату решения комиссии?")
            self.assertEqual(review["script"], "Уточни дату решения комиссии и зафиксируй её в CRM.")
            self.assertNotEqual(review["script"], "Добрый день, сверю статус.")
            self.assertEqual(review["quality"]["criteria"]["next_action"]["score"], 0)
            self.assertEqual(review["status"], "yellow")
            self.assertEqual(review["ai_context"]["current_situation"], "Клиент получил КП.")
            self.assertEqual(review["checklist"]["completed"], deal["checklist"]["completed"])
            self.assertEqual(review["checklist"]["total"], deal["checklist"]["total"])
            self.assertFalse(review["communications_today"].get("content_available"))

    def test_shared_deal_checklist_projects_actions_and_persists_manager_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            upsert_deal_control_deal(
                db_path,
                deal_id="101",
                source="initial",
                title="Сделка 101",
                manager_id="10",
                manager_name="Иванов Иван",
                stage_id="C15:NEW",
                stage_name="Новая",
                pipeline_id="15",
                amount="120000",
                currency_id="RUB",
                created_at_crm="2026-07-19T09:00:00+03:00",
                modified_at_crm="2026-07-20T09:00:00+03:00",
                is_active=True,
            )
            save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={
                    "deal_control_brief": {
                        "current_situation": "Клиент получил КП.",
                        "missing_facts": ["Не подтверждён ЛПР", "Бюджет клиента", "Дата решения"],
                        "what_to_check_now": "Получить дату решения",
                    },
                    "manager_action_block": {"manager_checklist": ["Следующий шаг"]},
                },
            )

            first = build_deal_control_dashboard(db_path=db_path)["deals"][0]["checklist"]
            self.assertEqual(first["completed"], 0)
            self.assertEqual(first["total"], 5)
            self.assertEqual(first["items"][0]["text"], "Подтвердить ЛПР.")
            self.assertIn("Подтвердить бюджет клиента.", [item["text"] for item in first["items"]])
            self.assertIn("Подтвердить дату решения.", [item["text"] for item in first["items"]])
            self.assertIn("Зафиксировать в CRM следующий шаг.", [item["text"] for item in first["items"]])

            updated = save_checklist_item_completion(
                db_path=db_path,
                deal_id="101",
                item_id=first["items"][0]["id"],
                completed=True,
            )
            self.assertEqual(updated["completed"], 1)
            self.assertTrue(updated["items"][0]["completed"])

            projected = build_deal_control_dashboard(db_path=db_path)["deals"][0]["checklist"]
            self.assertEqual(projected["completed"], 1)
            self.assertEqual(projected["progress_percent"], 20)

    def test_scope_is_local_and_has_no_embedded_crm_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self.assertFalse(get_deal_control_scope(db_path)["configured"])
            scope = save_deal_control_scope(
                db_path, initial_deal_ids=["101", "101", "102"], manager_ids=["10"], pipeline_id="15"
            )
            self.assertEqual(scope["initial_deal_ids"], ["101", "102"])
            self.assertEqual(scope["manager_ids"], ["10"])
            self.assertEqual(scope["pipeline_id"], "15")
            self.assertEqual(scope["pipeline_ids"], ["15"])

    def test_sync_keeps_initial_deals_and_adds_only_target_manager_from_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(
                initial={"101": deal("101", manager_id="20")},
                pipeline=[deal("201", manager_id="10"), deal("202", manager_id="20"), deal("203", manager_id="10", closed="Y")],
            )
            with patch("api.deal_control.load_pipeline_stage_names", return_value={"C15:NEW": "Новая"}):
                result = refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
                )
            self.assertEqual([item["deal_id"] for item in result["deals"]], ["201", "101"])
            initial = next(item for item in result["deals"] if item["deal_id"] == "101")
            self.assertEqual(initial["manager_id"], "20")
            self.assertEqual(initial["manager_name"], "Петров Пётр")
            self.assertEqual(result["summary"]["active_deals"], 2)
            deal_list_calls = [call for call in client.calls if call[0] == "crm.deal.list"]
            self.assertEqual(len(deal_list_calls), 2)
            self.assertEqual(deal_list_calls[0][1]["filter"]["ID"], ["101"])

    def test_legacy_scope_pipeline_15_migrates_to_three_pipelines(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE deal_control_scope (
                        scope_key TEXT NOT NULL PRIMARY KEY,
                        initial_deal_ids_json TEXT NOT NULL,
                        manager_ids_json TEXT NOT NULL,
                        pipeline_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO deal_control_scope (
                        scope_key, initial_deal_ids_json, manager_ids_json, pipeline_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("active", "[]", '["10"]', "15", "2026-08-01T00:00:00+00:00"),
                )
                conn.commit()
            finally:
                conn.close()
            scope = get_deal_control_scope(db_path)
            self.assertEqual(scope["pipeline_id"], "15")
            self.assertEqual(scope["pipeline_ids"], ["15", "17", "47"])

    def test_sync_adds_allowed_17_47_stages_and_keeps_pipeline_15(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(
                db_path,
                initial_deal_ids=["101"],
                manager_ids=["10"],
                pipeline_id="15",
                pipeline_ids=["15", "17", "47"],
            )
            client = FakeBitrixClient(
                initial={"101": deal("101", manager_id="20")},
                pipeline=[
                    deal("201", manager_id="10"),
                    deal("301", manager_id="10", category_id="17", stage_id="C17:NEW"),
                    deal("302", manager_id="10", category_id="17", stage_id="C17:UC_3KRF2B"),
                    deal("303", manager_id="10", category_id="17", stage_id="C17:EXECUTING"),
                    deal("401", manager_id="10", category_id="47", stage_id="C47:PREPARATION"),
                    deal("402", manager_id="10", category_id="47", stage_id="C47:PREPAYMENT_INVOIC"),
                    deal("403", manager_id="10", category_id="47", stage_id="C47:EXECUTING"),
                    deal("404", manager_id="20", category_id="47", stage_id="C47:EXECUTING"),
                ],
            )
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                result = refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
                )
            active_ids = {item["deal_id"] for item in result["deals"]}
            self.assertEqual(active_ids, {"101", "201", "302", "303", "403"})
            self.assertTrue(all(item["is_active"] for item in result["deals"]))

    def test_sync_fetches_activities_only_for_active_deals_and_includes_reactivated_deals(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            upsert_deal_control_deal(
                db_path,
                deal_id="999",
                source="pipeline",
                title="Неактивная сделка",
                manager_id="10",
                manager_name="Иванов Иван",
                stage_id="C15:LOSE",
                stage_name="Проиграна",
                pipeline_id="15",
                amount="50000",
                currency_id="RUB",
                created_at_crm="2026-07-01T09:00:00+03:00",
                modified_at_crm="2026-07-19T09:00:00+03:00",
                is_active=False,
            )
            inactive_task = create_deal_control_task(
                db_path,
                deal_id="999",
                task_text="Старое поручение",
                touch_type=None,
                expected_result=None,
                due_at="2026-07-20T12:00:00+03:00",
            )
            inactive_status_before = inactive_task["crm_execution_status"]
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})

            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(
                    db_path=db_path,
                    client=client,
                    now=datetime(2026, 7, 20, 10, tzinfo=MSK),
                )

            activity_calls = [call for call in client.calls if call[0] == "crm.activity.list"]
            open_task_calls = [
                call for call in activity_calls
                if call[1]["filter"].get("PROVIDER_ID") == "CRM_TASKS_TASK"
            ]
            self.assertEqual(open_task_calls[-1][1]["filter"]["OWNER_ID"], ["101"])
            inactive_after_sync = next(
                item for item in list_deal_control_tasks(db_path) if item["id"] == inactive_task["id"]
            )
            self.assertEqual(inactive_after_sync["crm_execution_status"], inactive_status_before)

            client.calls.clear()
            client.pipeline = [deal("999", manager_id="10")]
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refreshed = refresh_deal_control(
                    db_path=db_path,
                    client=client,
                    now=datetime(2026, 7, 20, 11, tzinfo=MSK),
                )

            activity_calls = [call for call in client.calls if call[0] == "crm.activity.list"]
            open_task_calls = [
                call for call in activity_calls
                if call[1]["filter"].get("PROVIDER_ID") == "CRM_TASKS_TASK"
            ]
            completed_today_calls = [
                call for call in activity_calls
                if call[1]["filter"].get("COMPLETED") == "Y"
            ]
            task_history_calls = [
                call for call in activity_calls
                if "PROVIDER_ID" not in call[1]["filter"] and "COMPLETED" not in call[1]["filter"]
            ]
            self.assertEqual(set(open_task_calls[-1][1]["filter"]["OWNER_ID"]), {"101", "999"})
            self.assertEqual(set(completed_today_calls[-1][1]["filter"]["OWNER_ID"]), {"101", "999"})
            self.assertEqual(task_history_calls[-1][1]["filter"]["OWNER_ID"], ["999"])
            self.assertEqual({item["deal_id"] for item in refreshed["deals"]}, {"101", "999"})

    def test_sync_projects_existing_open_bitrix_tasks_and_clears_stale_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(
                initial={"101": deal("101", manager_id="10")},
                activities={
                    "101": [
                        {
                            "ID": "900", "OWNER_ID": "101", "PROVIDER_ID": "CRM_TASKS_TASK",
                            "SUBJECT": "Будущая задача", "DEADLINE": "2026-07-22T12:00:00+03:00",
                            "COMPLETED": "N",
                        },
                        {
                            "ID": "901", "OWNER_ID": "101", "PROVIDER_ID": "CRM_TASKS_TASK",
                            "SUBJECT": "Просроченная задача", "DEADLINE": "2026-07-19T12:00:00+03:00",
                            "COMPLETED": "N",
                        },
                        {
                            "ID": "902", "OWNER_ID": "101", "PROVIDER_ID": "CRM_TASKS_TASK",
                            "SUBJECT": "Закрытая задача", "DEADLINE": "2026-07-18T12:00:00+03:00",
                            "COMPLETED": "Y",
                        },
                        {
                            "ID": "903", "OWNER_ID": "101", "PROVIDER_ID": "CRM_CALL",
                            "SUBJECT": "Запланированный звонок", "DEADLINE": "2026-07-20T13:00:00+03:00",
                            "COMPLETED": "N",
                        },
                    ]
                },
            )
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                result = refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
                )
            projected = result["deals"][0]["bitrix_tasks"]
            self.assertEqual([item["activity_id"] for item in projected], ["901", "900"])
            self.assertEqual(result["deals"][0]["primary_bitrix_task"]["time_bucket"], "overdue")
            self.assertEqual(result["summary"]["tasks_total"], 1)
            self.assertEqual(result["summary"]["tasks_overdue"], 1)

            client.activities = {}
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                cleared = refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 11, tzinfo=MSK)
                )
            self.assertEqual(cleared["deals"][0]["bitrix_tasks"], [])
            self.assertIsNone(cleared["deals"][0]["primary_bitrix_task"])
            self.assertEqual(cleared["summary"]["tasks_total"], 0)

    def test_sync_marks_daily_communications_unavailable_when_recent_activity_query_fails(self):
        class RecentActivityFailureClient(FakeBitrixClient):
            def safe_list_all(self, method, payload=None):
                activity_filter = ((payload or {}).get("filter") or {})
                if activity_filter.get("COMPLETED") == "Y":
                    self.calls.append((method, payload or {}))
                    return {"ok": False, "items": [], "error": "temporary timeout"}
                return super().safe_list_all(method, payload)

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = RecentActivityFailureClient(initial={"101": deal("101", manager_id="10")})

            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                result = refresh_deal_control(
                    db_path=db_path,
                    client=client,
                    now=datetime(2026, 7, 20, 10, tzinfo=MSK),
                )

            self.assertFalse(result["deals"][0]["communications_today"]["available"])
            self.assertTrue(result["sync_errors"])

    def test_dashboard_reads_legacy_wrapped_bitrix_tasks_json(self):
        """DB may still hold `{tasks, scheduled_activities}` after a format rollback."""
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            upsert_deal_control_deal(
                db_path,
                deal_id="101",
                source="initial",
                title="Сделка 101",
                manager_id="10",
                manager_name="Иванов Иван",
                stage_id="C15:NEW",
                stage_name="Новая",
                pipeline_id="15",
                amount="120000",
                currency_id="RUB",
                created_at_crm="2026-07-19T09:00:00+03:00",
                modified_at_crm="2026-07-20T09:00:00+03:00",
                is_active=True,
            )
            legacy = {
                "tasks": [{
                    "activity_id": "901",
                    "subject": "Просроченная задача",
                    "deadline": "2026-07-19T12:00:00+03:00",
                    "time_bucket": "overdue",
                    "completed": False,
                    "provider_id": "CRM_TASKS_TASK",
                }],
                "scheduled_activities": [],
            }
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE deal_control_deals SET bitrix_tasks_json = ? WHERE deal_id = ?",
                    (dumps_json(legacy), "101"),
                )
            dashboard = build_deal_control_dashboard(
                db_path=db_path, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
            )
            projected = dashboard["deals"][0]["bitrix_tasks"]
            self.assertEqual([item["activity_id"] for item in projected], ["901"])
            self.assertEqual(dashboard["deals"][0]["primary_bitrix_task"]["time_bucket"], "overdue")
            self.assertEqual(dashboard["summary"]["tasks_overdue"], 1)

    def test_local_bitrix_completion_keeps_task_in_today_plan_and_can_be_reverted(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            upsert_deal_control_deal(
                db_path,
                deal_id="101",
                source="initial",
                title="Сделка 101",
                manager_id="10",
                manager_name="Иванов Иван",
                stage_id="C15:NEW",
                stage_name="Новая",
                pipeline_id="15",
                amount="120000",
                currency_id="RUB",
                created_at_crm="2026-07-19T09:00:00+03:00",
                modified_at_crm="2026-07-20T09:00:00+03:00",
                is_active=True,
            )
            save_deal_control_bitrix_tasks(
                db_path,
                deal_id="101",
                tasks=[{
                    "activity_id": "901",
                    "task_id": "501",
                    "responsible_id": "10",
                    "subject": "Позвонить клиенту",
                    "description": "",
                    "deadline": "2026-07-20T12:00:00+03:00",
                    "time_bucket": "today",
                    "completed": False,
                    "provider_id": "CRM_TASKS_TASK",
                }],
            )
            set_deal_control_bitrix_task_completion(
                db_path,
                deal_id="101",
                activity_id="901",
                completed=True,
                source_role="manager",
            )
            completed = build_deal_control_dashboard(
                db_path=db_path, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
            )
            task = completed["deals"][0]["primary_bitrix_task"]
            self.assertEqual(task["completion_state"], "local")
            self.assertEqual(task["time_bucket"], "today")
            self.assertEqual(completed["summary"]["tasks_completed_today"], 1)
            self.assertEqual(completed["summary"]["tasks_plan_today"], 1)

            set_deal_control_bitrix_task_completion(
                db_path,
                deal_id="101",
                activity_id="901",
                completed=False,
                source_role="rop",
            )
            reopened = build_deal_control_dashboard(
                db_path=db_path, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
            )
            self.assertEqual(reopened["deals"][0]["primary_bitrix_task"]["completion_state"], "open")
            self.assertEqual(reopened["summary"]["tasks_completed_today"], 0)

    def test_sync_keeps_tasks_completed_in_bitrix_today_and_ignores_legacy_assignments(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(
                initial={"101": deal("101", manager_id="10")},
                activities={"101": [{
                    "ID": "902",
                    "OWNER_ID": "101",
                    "PROVIDER_ID": "CRM_TASKS_TASK",
                    "ASSOCIATED_ENTITY_ID": "502",
                    "RESPONSIBLE_ID": "10",
                    "SUBJECT": "Закрытая сегодня задача",
                    "DEADLINE": "2026-07-20T09:00:00+03:00",
                    "LAST_UPDATED": "2026-07-20T10:15:00+03:00",
                    "COMPLETED": "Y",
                }]},
            )
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 11, tzinfo=MSK)
                )
            create_deal_control_task(
                db_path,
                deal_id="101",
                task_text="Старое локальное поручение",
                touch_type="Звонок",
                expected_result="Не влияет",
                due_at="2026-07-19T09:00:00+03:00",
            )
            dashboard = build_deal_control_dashboard(
                db_path=db_path, now=datetime(2026, 7, 20, 11, tzinfo=MSK)
            )
            item = dashboard["deals"][0]
            self.assertIsNone(item["current_task"])
            self.assertEqual(item["primary_bitrix_task"]["completion_state"], "bitrix")
            self.assertEqual(item["primary_bitrix_task"]["task_id"], "502")
            self.assertEqual(dashboard["summary"]["tasks_completed_today"], 1)
            self.assertEqual(dashboard["summary"]["tasks_overdue"], 1)

    def test_exact_deadline_bitrix_task_with_different_text_requires_rop_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            create_deal_control_task(
                db_path, deal_id="101", task_text="Провести звонок по направленному предложению",
                touch_type="звонок", expected_result="Получить решение",
                due_at="2026-07-21T11:00:00+03:00",
            )
            client.activities = {
                "101": [{
                    "ID": "910", "OWNER_ID": "101", "PROVIDER_ID": "CRM_TASKS_TASK",
                    "SUBJECT": "Связаться с заказчиком", "DESCRIPTION": "",
                    "DEADLINE": "2026-07-21T11:00:00+03:00", "COMPLETED": "N",
                }]
            }
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                result = refresh_deal_control(
                    db_path=db_path, client=client, now=datetime(2026, 7, 20, 10, tzinfo=MSK)
                )
            task = list_deal_control_tasks(db_path)[0]
            self.assertEqual(task["crm_execution_status"], "match_review")
            self.assertEqual(task["crm_match_activity_id"], "910")
            self.assertEqual(result["deals"][0]["primary_bitrix_task"]["activity_id"], "910")

    def test_closed_crm_task_is_not_claimed_as_client_result(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
                task = create_deal_control_task(
                    db_path,
                    deal_id="101",
                    task_text="Позвонить клиенту и подтвердить срок оплаты",
                    touch_type="звонок",
                    expected_result="Зафиксирован следующий шаг",
                    due_at="2026-07-20T15:00:00+03:00",
                )
                client.activities = {
                    "101": [{
                        "ID": "500", "OWNER_ID": "101", "SUBJECT": "Позвонить клиенту и подтвердить срок оплаты",
                        "DESCRIPTION": "", "DEADLINE": "2026-07-20T15:00:00+03:00", "COMPLETED": "Y",
                    }]
                }
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 16, tzinfo=MSK))
            saved = list_deal_control_tasks(db_path)[0]
            self.assertEqual(saved["crm_execution_status"], "crm_closed")
            self.assertEqual(saved["business_result_status"], "no_result")
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            self.assertEqual(history["crm_facts"][0]["fact_kind"], "crm_activity_completed")
            dashboard = build_deal_control_dashboard(
                db_path=db_path,
                now=datetime(2026, 7, 20, 16, tzinfo=MSK),
            )
            self.assertEqual(dashboard["summary"]["tasks_overdue"], 0)
            self.assertEqual(dashboard["summary"]["tasks_missing"], 1)
            self.assertIsNone(dashboard["deals"][0]["current_task"])

    def test_medium_match_requires_rop_review_and_rescheduling_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
                task = create_deal_control_task(
                    db_path, deal_id="101", task_text="Позвонить клиенту и подтвердить дату оплаты заказа",
                    touch_type=None, expected_result=None, due_at="2026-07-20T15:00:00+03:00",
                )
                client.activities = {
                    "101": [{
                        "ID": "501", "OWNER_ID": "101", "SUBJECT": "Позвонить подтвердить дату", "DESCRIPTION": "",
                        "DEADLINE": "2026-07-20T15:00:00+03:00", "COMPLETED": "Y",
                    }]
                }
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 16, tzinfo=MSK))
            saved = list_deal_control_tasks(db_path)[0]
            self.assertEqual(saved["crm_execution_status"], "match_review")
            confirmed = confirm_deal_control_task_crm_match(db_path, task_id=int(task["id"]))
            self.assertEqual(confirmed["crm_execution_status"], "crm_closed")
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 17, tzinfo=MSK))
            self.assertEqual(list_deal_control_tasks(db_path)[0]["crm_execution_status"], "crm_closed")
            update_deal_control_task(db_path, task_id=int(task["id"]), due_at="2026-07-22T12:00:00+03:00")
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            self.assertEqual(history["reschedules"][0]["previous_due_at"], "2026-07-20T15:00:00+03:00")

    def test_dashboard_marks_unclosed_past_task_overdue(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            create_deal_control_task(
                db_path, deal_id="101", task_text="Написать клиенту", touch_type=None,
                expected_result=None, due_at="2026-07-19T15:00:00+03:00",
            )
            result = build_deal_control_dashboard(db_path=db_path, now=datetime(2026, 7, 20, 10, tzinfo=MSK))
            self.assertEqual(result["summary"]["tasks_overdue"], 0)
            self.assertEqual(result["summary"]["tasks_missing"], 1)
            self.assertIsNone(result["deals"][0]["current_task"])

    def test_dashboard_splits_today_tomorrow_and_completed_today(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            completed = create_deal_control_task(
                db_path, deal_id="101", task_text="Отправить КП", touch_type="email",
                expected_result="КП отправлено", due_at="2026-07-20T10:00:00+03:00",
            )
            create_deal_control_task(
                db_path, deal_id="101", task_text="Получить обратную связь", touch_type="звонок",
                expected_result="Есть следующий шаг", due_at="2026-07-21T11:00:00+03:00",
            )
            with patch("storage.rop_db.utcish_now", return_value="2026-07-20T08:30:00+00:00"):
                update_deal_control_task(db_path, task_id=int(completed["id"]), local_status="completed")
            result = build_deal_control_dashboard(db_path=db_path, now=datetime(2026, 7, 20, 12, tzinfo=MSK))
            self.assertEqual(result["summary"]["tasks_tomorrow"], 0)
            self.assertEqual(result["summary"]["tasks_completed_today"], 0)
            self.assertEqual(result["summary"]["tasks_missing"], 1)
            self.assertEqual(len(result["deals"][0]["tasks"]), 2)

    def test_task_keeps_immutable_baseline_and_outcome_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={"C15:NEW": "Новая"}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            task = create_deal_control_task(
                db_path, deal_id="101", task_text="Получить решение по КП", touch_type="звонок",
                expected_result="Решение клиента и следующий шаг", due_at="2026-07-20T15:00:00+03:00",
            )
            saved = list_deal_control_tasks(db_path)[0]
            self.assertEqual(saved["baseline"]["deal_snapshot"]["stage_id"], "C15:NEW")
            with self.assertRaisesRegex(ValueError, "следующий шаг"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="confirmed_contact",
                    result_status="achieved", result_note="Клиент согласовал",
                    next_step_text=None, next_step_at=None, evidence_kind="manager_confirmation",
                    evidence_id=None, source_role="manager",
                )
            save_deal_control_task_outcome(
                db_path, task_id=int(task["id"]), contact_status="confirmed_contact",
                result_status="achieved", result_note="Клиент согласовал обсуждение",
                next_step_text="Отправить уточнённый расчёт", next_step_at="2026-07-21T12:00:00+03:00",
                evidence_kind="manager_confirmation", evidence_id=None, source_role="manager",
            )
            save_deal_control_task_outcome(
                db_path, task_id=int(task["id"]), contact_status="confirmed_contact",
                result_status="refused", result_note="РОП подтвердил отказ клиента",
                next_step_text=None, next_step_at=None, evidence_kind="rop_confirmation",
                evidence_id=None, source_role="rop",
            )
            saved = list_deal_control_tasks(db_path)[0]
            self.assertEqual(saved["local_status"], "completed")
            self.assertEqual(saved["latest_outcome"]["result_status"], "refused")
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            self.assertEqual([item["result_status"] for item in history["outcomes"]], ["refused", "achieved"])
            self.assertEqual(len([item for item in history["events"] if item["event_type"] == "outcome_recorded"]), 2)

    def test_outcome_requires_meaningful_contact_and_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            task = create_deal_control_task(
                db_path, deal_id="101", task_text="Позвонить клиенту", touch_type="звонок",
                expected_result="Получить ответ", due_at="2026-07-20T15:00:00+03:00",
            )

            with self.assertRaisesRegex(ValueError, "Сначала выполните действие"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="not_attempted",
                    result_status="pending", result_note=None, next_step_text=None, next_step_at=None,
                    evidence_kind=None, evidence_id=None, source_role="manager",
                )
            with self.assertRaisesRegex(ValueError, "попытки без ответа"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="attempt_no_contact",
                    result_status="pending", result_note="Не дозвонился", next_step_text=None, next_step_at=None,
                    evidence_kind=None, evidence_id=None, source_role="manager",
                )
            saved = save_deal_control_task_outcome(
                db_path, task_id=int(task["id"]), contact_status="attempt_no_contact",
                result_status="pending", result_note="Не дозвонился",
                next_step_text="Повторить звонок", next_step_at="2026-07-21T11:00:00+03:00",
                evidence_kind=None, evidence_id=None, source_role="manager",
            )
            self.assertEqual(saved["result_status"], "pending")

            with self.assertRaisesRegex(ValueError, "Confirmed contact or achieved"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="confirmed_contact",
                    result_status="pending", result_note="Клиент ответил",
                    next_step_text="Уточнить срок", next_step_at="2026-07-22T11:00:00+03:00",
                    evidence_kind="crm_activity", evidence_id="activity-1", source_role="manager",
                )
            with self.assertRaisesRegex(ValueError, "attempt_no_contact"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="attempt_no_contact",
                    result_status="achieved", result_note="Ответа не было",
                    next_step_text="Повторить звонок", next_step_at="2026-07-22T11:00:00+03:00",
                    evidence_kind="transcript", evidence_id=None, source_role="manager",
                )
            update_deal_control_task(db_path, task_id=int(task["id"]), local_status="completed")
            with self.assertRaisesRegex(ValueError, "Closed tasks"):
                save_deal_control_task_outcome(
                    db_path, task_id=int(task["id"]), contact_status="confirmed_contact",
                    result_status="achieved", result_note="Клиент подтвердил результат",
                    next_step_text="Зафиксировать итог", next_step_at="2026-07-22T11:00:00+03:00",
                    evidence_kind="manager_confirmation", evidence_id=None, source_role="manager",
                )

    def test_rop_reschedule_requires_reason_and_records_role(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            task = create_deal_control_task(
                db_path, deal_id="101", task_text="Уточнить решение", touch_type="звонок",
                expected_result="Получить срок", due_at="2026-07-20T15:00:00+03:00",
            )
            with self.assertRaisesRegex(ValueError, "причину переноса"):
                update_deal_control_task(
                    db_path, task_id=int(task["id"]), due_at="2026-07-21T12:00:00+03:00",
                    source_role="rop",
                )
            update_deal_control_task(
                db_path, task_id=int(task["id"]), due_at="2026-07-21T12:00:00+03:00",
                reschedule_reason="Клиент перенёс встречу", source_role="rop",
            )
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            self.assertEqual(history["reschedules"][0]["reason"], "Клиент перенёс встречу")
            self.assertEqual(history["reschedules"][0]["source_role"], "rop")

    def test_metrics_exclude_cancelled_tasks_and_keep_separate_count(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
            active = create_deal_control_task(
                db_path, deal_id="101", task_text="Позвонить клиенту", touch_type="звонок",
                expected_result="Получить ответ", due_at="2026-07-20T15:00:00+03:00",
            )
            cancelled = create_deal_control_task(
                db_path, deal_id="101", task_text="Старая задача", touch_type="звонок",
                expected_result=None, due_at="2026-07-19T15:00:00+03:00",
            )
            update_deal_control_task(db_path, task_id=int(cancelled["id"]), local_status="cancelled")

            metrics = get_deal_control_metrics(db_path)
            self.assertEqual(metrics["overall"]["tasks"], 1)
            self.assertEqual(metrics["cancelled_tasks"], 1)
            self.assertEqual(metrics["with_guidance"]["tasks"] + metrics["without_guidance"]["tasks"], 1)
            self.assertEqual(list_deal_control_tasks(db_path)[0]["id"], cancelled["id"])
            self.assertNotEqual(active["id"], cancelled["id"])

    def test_sync_records_attempt_once_and_detects_won_deal(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_deal_control_scope(db_path, initial_deal_ids=["101"], manager_ids=["10"], pipeline_id="15")
            client = FakeBitrixClient(initial={"101": deal("101", manager_id="10")})
            with patch("api.deal_control.load_pipeline_stage_names", return_value={"C15:NEW": "Новая", "C15:WON": "Успех"}):
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 9, tzinfo=MSK))
                with patch("storage.rop_db.utcish_now", return_value="2026-07-20T09:00:00+03:00"):
                    task = create_deal_control_task(
                        db_path, deal_id="101", task_text="Позвонить клиенту", touch_type="звонок",
                        expected_result="Получить решение", due_at="2026-07-20T15:00:00+03:00",
                    )
                client.activities = {
                    "101": [{
                        "ID": "700", "OWNER_ID": "101", "SUBJECT": "Позвонить клиенту",
                        "DEADLINE": "2026-07-20T15:00:00+03:00", "COMPLETED": "Y",
                    }]
                }
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 16, tzinfo=MSK))
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 20, 17, tzinfo=MSK))
                client.initial["101"] = {
                    **deal("101", manager_id="10", closed="Y"),
                    "STAGE_ID": "C15:WON",
                    "STAGE_SEMANTIC_ID": "S",
                    "DATE_MODIFY": "2026-07-21T10:00:00+03:00",
                }
                refresh_deal_control(db_path=db_path, client=client, now=datetime(2026, 7, 21, 10, tzinfo=MSK))
            history = list_deal_control_task_history(db_path, task_id=int(task["id"]))
            activity_facts = [item for item in history["crm_facts"] if item.get("fact_key") == "activity:700"]
            self.assertEqual(len(activity_facts), 1)
            self.assertEqual(activity_facts[0]["contact_class"], "attempt")
            reviewed = review_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_id=int(activity_facts[0]["id"]),
                review_status="confirmed",
            )
            self.assertEqual(reviewed["review_status"], "confirmed")
            self.assertEqual(reviewed["contact_class"], "attempt")
            self.assertTrue(any(item["fact_kind"] == "deal_won" for item in history["crm_facts"]))
            metrics = get_deal_control_metrics(db_path)
            self.assertEqual(metrics["overall"]["stage_progressed"], 1)
            self.assertEqual(metrics["overall"]["deals_won"], 1)
