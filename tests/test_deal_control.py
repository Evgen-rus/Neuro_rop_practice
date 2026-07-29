from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from api.deal_control import build_deal_control_dashboard, refresh_deal_control
from storage.rop_db import (
    confirm_deal_control_task_crm_match,
    create_deal_control_task,
    get_deal_control_metrics,
    get_deal_control_scope,
    list_deal_control_task_history,
    list_deal_control_tasks,
    review_deal_control_task_crm_fact,
    save_deal_control_scope,
    save_deal_control_task_outcome,
    update_deal_control_task,
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
            ids = ((payload or {}).get("filter") or {}).get("ID")
            if ids:
                return [self.initial[item] for item in ids if item in self.initial]
            return list(self.pipeline)
        if method == "user.get":
            return [
                {"ID": "10", "LAST_NAME": "Иванов", "NAME": "Иван"},
                {"ID": "20", "LAST_NAME": "Петров", "NAME": "Пётр"},
            ]
        raise AssertionError(method)

    def safe_list_all(self, method, payload=None):
        if method != "crm.activity.list":
            raise AssertionError(method)
        owner_ids = ((payload or {}).get("filter") or {}).get("OWNER_ID") or []
        owner_ids = owner_ids if isinstance(owner_ids, list) else [owner_ids]
        result = []
        for owner_id in owner_ids:
            result.extend(self.activities.get(str(owner_id), []))
        return {"ok": True, "items": result}


def deal(deal_id: str, *, manager_id: str, closed: str = "N") -> dict:
    return {
        "ID": deal_id,
        "TITLE": f"Сделка {deal_id}",
        "ASSIGNED_BY_ID": manager_id,
        "CATEGORY_ID": "15",
        "STAGE_ID": "C15:NEW",
        "CLOSED": closed,
        "OPPORTUNITY": "120000",
        "CURRENCY_ID": "RUB",
        "DATE_CREATE": "2026-07-19T09:00:00+03:00",
        "DATE_MODIFY": "2026-07-20T09:00:00+03:00",
    }


class DealControlTests(unittest.TestCase):
    def test_scope_is_local_and_has_no_embedded_crm_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self.assertFalse(get_deal_control_scope(db_path)["configured"])
            scope = save_deal_control_scope(
                db_path, initial_deal_ids=["101", "101", "102"], manager_ids=["10"], pipeline_id="15"
            )
            self.assertEqual(scope["initial_deal_ids"], ["101", "102"])
            self.assertEqual(scope["manager_ids"], ["10"])

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
            self.assertEqual(dashboard["summary"]["tasks_overdue"], 1)
            self.assertEqual(dashboard["deals"][0]["current_task"]["time_bucket"], "overdue")

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
            self.assertEqual(result["summary"]["tasks_overdue"], 1)
            self.assertEqual(result["deals"][0]["current_task"]["view_status"], "overdue")

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
            self.assertEqual(result["summary"]["tasks_tomorrow"], 1)
            self.assertEqual(result["summary"]["tasks_completed_today"], 1)
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
