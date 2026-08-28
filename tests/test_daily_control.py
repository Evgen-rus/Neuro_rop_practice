from __future__ import annotations

import tempfile
import unittest
from functools import partial
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api.deal_control import build_deal_control_dashboard
from api.daily_control import (
    GENERIC_STATUS_QUESTION,
    build_daily_control_snapshot,
    build_direct_manager_question,
    classify_deal_status,
    compute_source_watermark,
    freshness_for_report,
    history_payload,
    project_deal_review_card,
    project_report_day_scope,
    publish_daily_control_report,
    publish_day_end_daily_control_report,
    publish_planning_daily_control_report,
    report_heading,
    report_payload,
)
from openai_api.llm.validation import _validate_deal_management_shapes
from setup import MSK_TZ
from openai_api.llm.deal_daily_quality import quality_event_signature
from storage.rop_db import (
    connect,
    get_daily_control_report,
    init_db,
    list_daily_control_reports,
    save_deal_control_communications_today,
    save_deal_control_scope,
    save_ui_report,
    upsert_deal_control_deal,
)


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ)
classify_deal_status = partial(classify_deal_status, now=NOW)
project_deal_review_card = partial(project_deal_review_card, now=NOW)


def _quality_event(**overrides):
    return {"event_id": "crm_activity:1", "channel": "email", "direction": "outgoing",
            "occurred_at": "2026-08-18T10:15:00+03:00", "content": "Отправляю условия поставки до пятницы.",
            "content_available": True, **overrides}


def _deal_row(**overrides):
    row = {
        "deal_id": "101",
        "title": "Сделка 101",
        "manager_id": "10",
        "manager_name": "Иванов Иван",
        "pipeline_id": "15",
        "pipeline_name": "Основные продажи",
        "stage_id": "C15:NEW",
        "stage_name": "КП отправлено",
        "amount": "100000",
        "currency_id": "RUB",
        "current_task": None,
        "primary_bitrix_task": None,
        "communications_today": {
            "date": "2026-08-18",
            "available": True,
            "completed": 1,
            "calls": 1,
            "messages": 0,
            "duration_seconds": 90,
            "items": [_quality_event()],
        },
        "coaching": {"report_id": 1, "communication_quality_audit": None},
    }
    row.update(overrides)
    return row


def _audit(*, next_action=1, value=1, data=1, status="assessed"):
    reasons = []
    if next_action == 0:
        reasons.append({"criterion": "next_action", "explanation": "Нет даты", "quote": "как решите"})
    return {
        "status": status,
        "scope_summary": "Учтён звонок 18.08.",
        "criteria": {
            "next_action": {"score": next_action if status == "assessed" else None},
            "value_development": {"score": value if status == "assessed" else None},
            "data_collection": {"score": data if status == "assessed" else None},
        },
        "zero_reasons": reasons if status == "assessed" else [],
        "summary_for_rop": "Клиент ждёт условия." if status == "assessed" else None,
        "insufficient_reason": None if status == "assessed" else "Нет содержательной коммуникации.",
        "daily_scope": {"version": 1, "business_date": "2026-08-18", "evaluated_through": NOW.isoformat(),
                        "event_signatures": {"crm_activity:1": quality_event_signature(_quality_event())}},
    }


def _seed_deal(db_path: Path, *, deal_id="101", manager_id="10", title="Сделка 101", audit=None, brief=None):
    save_deal_control_scope(
        db_path,
        initial_deal_ids=[deal_id],
        manager_ids=[manager_id],
        pipeline_id="15",
    )
    upsert_deal_control_deal(
        db_path,
        deal_id=deal_id,
        source="initial",
        title=title,
        manager_id=manager_id,
        manager_name="Иванов Иван" if manager_id == "10" else "Петров Пётр",
        stage_id="C15:NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="120000",
        currency_id="RUB",
        created_at_crm="2026-08-17T09:00:00+03:00",
        modified_at_crm="2026-08-18T09:00:00+03:00",
        is_active=True,
    )
    payload = {
        "deal_control_brief": {
            "current_situation": "КП отправлено, клиент сравнивает условия.",
            "what_to_check_now": "Подтвердить срок решения.",
            "missing_facts": ["Дата решения клиента"],
            **(brief or {}),
        },
        "manager_action_block": {"manager_checklist": ["Дата следующего шага"]},
    }
    if audit is not None:
        payload["communication_quality_audit"] = audit
    save_ui_report(db_path, entity_type="deal", entity_id=deal_id, report_json=payload)
    save_deal_control_communications_today(
        db_path,
        deal_id=deal_id,
        summary={
            "date": "2026-08-18",
            "available": True,
            "target": 3,
            "completed": 1,
            "calls": 1,
            "messages": 0,
            "duration_seconds": 90,
            "items": [
                {
                    "event_id": "crm_activity:1",
                    "channel": "call",
                    "direction": "outgoing",
                    "occurred_at": "2026-08-18T10:15:00+03:00",
                    "subject": "Звонок клиенту",
                    "duration_seconds": 90,
                    "contact_class": "attempt",
                    "text": "не должен попасть в snapshot",
                }
            ],
        },
    )


class ClassifierTests(unittest.TestCase):
    def test_legacy_open_checklist_cannot_turn_healthy_deal_yellow(self) -> None:
        deal = _deal_row(
            coaching={"report_id": 1, "communication_quality_audit": _audit()},
            communications_today={"date": "2026-08-18", "available": True, "completed": 0, "items": []},
        )
        baseline = classify_deal_status(deal)
        deal["checklist"] = {"completed": 0, "total": 5, "items": [{"text": "legacy marker"}]}
        self.assertEqual(classify_deal_status(deal), baseline)
        self.assertEqual(baseline[0], "green")
        self.assertNotIn("legacy marker", build_direct_manager_question(deal))

    def test_overdue_local_rop_task_does_not_force_red(self) -> None:
        status, label = classify_deal_status(_deal_row(
            current_task={"time_bucket": "overdue"},
            coaching={"report_id": 1, "communication_quality_audit": _audit()},
        ))
        self.assertEqual(status, "green")
        self.assertEqual(label, "В норме")

    def test_two_zero_scores_are_red(self) -> None:
        status, label = classify_deal_status(_deal_row(
            coaching={"report_id": 1, "communication_quality_audit": _audit(next_action=0, value=0)},
        ))
        self.assertEqual(status, "red")
        self.assertEqual(label, "Требует решения РОПа")

    def test_missing_next_step_or_no_analysis_is_yellow(self) -> None:
        status, label = classify_deal_status(_deal_row(
            coaching={"report_id": 1, "communication_quality_audit": _audit(next_action=0)},
        ))
        self.assertEqual(status, "yellow")
        self.assertEqual(label, "Нужна проверка")
        status, _ = classify_deal_status(_deal_row(coaching={"report_id": None}))
        self.assertEqual(status, "yellow")

    def test_healthy_deal_is_green(self) -> None:
        status, label = classify_deal_status(_deal_row(
            coaching={"report_id": 1, "communication_quality_audit": _audit()},
        ))
        self.assertEqual(status, "green")
        self.assertEqual(label, "В норме")


class DailyQualityTests(unittest.TestCase):
    def setUp(self):
        self.deal = _deal_row(
            primary_bitrix_task={"deadline": "2026-08-18T18:00:00+03:00", "completion_state": "open", "time_bucket": "today"},
            coaching={"report_id": 1, "communication_quality_audit": _audit()},
        )

    def quality(self, *, now=NOW):
        return project_deal_review_card(self.deal, now=now)["quality"]

    def empty_day(self):
        self.deal["communications_today"].update(items=[], completed=0, calls=0, messages=0)

    def test_due_today_without_work_has_system_zeros_without_analysis(self):
        self.empty_day()
        for coaching in (self.deal["coaching"], {}):
            with self.subTest(has_analysis=bool(coaching)):
                self.deal["coaching"] = coaching
                result = self.quality()
                self.assertEqual(result["status"], "no_work")
                self.assertEqual([v["score"] for v in result["criteria"].values()], [0, 0, 0])
                self.assertEqual(result["source"], "system")
                self.assertEqual(classify_deal_status(self.deal)[0], "red")

    def test_attempts_and_internal_actions_do_not_earn_points(self):
        self.deal["communications_today"]["items"] = [
            _quality_event(event_id=str(i), channel="call", call_outcome="no_answer") for i in range(3)
        ] + [_quality_event(channel=channel) for channel in ("task", "internal_comment", "stage_change")]
        self.deal["communications_today"]["calls_no_answer"] = 3
        card = project_deal_review_card(self.deal)
        self.assertEqual(card["quality"]["status"], "no_work")
        self.assertEqual(card["communications_today"]["calls_no_answer"], 3)

    def test_no_task_or_moved_task_does_not_require_work(self):
        self.empty_day()
        for task in (None, {"deadline": "2026-08-19T10:00:00+03:00", "time_bucket": "today"}):
            self.deal["primary_bitrix_task"] = task
            self.deal["day_scope"] = {"had_day_obligation": True, "task_buckets": ["today"]}
            result = self.quality()
            self.assertEqual(result["status"], "not_required")
            self.assertIsNone(result["confirmed_count"])

    def test_overdue_secondary_task_and_local_control_point_are_required(self):
        self.empty_day()
        self.deal["primary_bitrix_task"] = {"deadline": "2026-08-19T10:00:00+03:00"}
        self.deal["bitrix_tasks"] = [self.deal["primary_bitrix_task"], {"deadline": "2026-08-17T10:00:00+03:00"}]
        self.assertEqual(self.quality()["status"], "no_work")
        self.deal["bitrix_tasks"] = []
        self.deal["current_task"] = {"due_at": "2026-08-18T10:00:00+03:00", "local_status": "active"}
        self.assertEqual(self.quality()["status"], "no_work")

    def test_closing_crm_task_today_does_not_prove_work(self):
        self.empty_day()
        self.deal["primary_bitrix_task"].update(completed=True, bitrix_completed_at="2026-08-18T11:00:00+03:00")
        self.assertEqual(self.quality()["status"], "no_work")
        self.deal["primary_bitrix_task"]["bitrix_completed_at"] = "2026-08-17T11:00:00+03:00"
        self.assertEqual(self.quality()["status"], "not_required")

    def test_current_ai_audit_is_used_but_new_or_revised_event_waits(self):
        self.assertEqual(self.quality()["confirmed_count"], 3)
        self.assertEqual(self.quality()["source"], "ai")
        event = self.deal["communications_today"]["items"][0]
        for overrides in ({"event_id": "crm_activity:2"}, {"content": "Сроки изменились, свяжемся завтра."}):
            self.deal["communications_today"]["items"] = [{**event, **overrides}]
            self.assertEqual(self.quality()["status"], "pending_analysis")

    def test_publishing_old_or_legacy_analysis_today_cannot_make_it_current(self):
        audit = self.deal["coaching"]["communication_quality_audit"]
        self.deal["coaching"]["analysis_created_at"] = NOW.isoformat()
        audit["daily_scope"]["business_date"] = "2026-08-17"
        self.assertEqual(self.quality()["status"], "pending_analysis")
        audit.pop("daily_scope")
        self.assertEqual(self.quality()["status"], "pending_analysis")

    def test_business_day_rollover_uses_moscow_not_machine_timezone(self):
        self.empty_day()
        self.deal["communications_today"]["date"] = "2026-08-19"
        # 21:01 UTC is the next Moscow business day; yesterday's 3/3 is gone.
        result = self.quality(now=datetime.fromisoformat("2026-08-18T21:01:00+00:00"))
        self.assertEqual(result["business_date"], "2026-08-19")
        self.assertEqual(result["status"], "no_work")

    def test_unavailable_or_previous_day_sources_never_mean_zero_work(self):
        self.empty_day()
        for overrides in ({"available": False}, {"date": "2026-08-17"}, {"quality_sources_available": False}):
            baseline = deepcopy(self.deal["communications_today"])
            self.deal["communications_today"].update(overrides)
            self.assertEqual(self.quality()["status"], "missing")
            self.assertIsNone(self.quality()["confirmed_count"])
            self.deal["communications_today"] = baseline

    def test_new_call_waits_for_transcription_and_ai(self):
        self.deal["communications_today"]["items"] = [_quality_event(channel="call", call_outcome="connected", content_available=False)]
        self.assertEqual(self.quality()["status"], "pending_analysis")

    def test_snapshot_does_not_use_events_or_analysis_after_cutoff(self):
        self.deal["communications_today"]["items"] = [_quality_event(occurred_at="2026-08-18T17:00:00+03:00")]
        snapshot = build_daily_control_snapshot({"deals": [self.deal]}, cutoff_at=NOW)
        self.assertEqual(snapshot["deals"][0]["quality"]["status"], "no_work")


class DirectQuestionTests(unittest.TestCase):
    def test_uses_saved_analysis_field_when_present(self) -> None:
        question = build_direct_manager_question(_deal_row(
            coaching={"direct_manager_question": "Ты отправил договор? Клиент назвал правки?"},
        ))
        self.assertEqual(question, "Ты отправил договор? Клиент назвал правки?")

    def test_old_analysis_gets_deterministic_fallback(self) -> None:
        question = build_direct_manager_question(_deal_row(
            coaching={"direct_manager_question": "", "unknowns": ["дата решения"]},
            checklist={"items": [{"text": "Подтвердить дату решения.", "completed": False}]},
        ))
        self.assertIn("дата решения", question)
        self.assertTrue(question.endswith("?") or "?" in question)

    def test_optional_direct_question_is_valid_when_absent(self) -> None:
        errors: list[str] = []
        _validate_deal_management_shapes({
            "deal_control_brief": {
                "current_situation": "Ситуация",
                "rop_focus": "Фокус",
                "what_to_check_now": "Проверить шаг",
                "manager_coaching": "Сделай шаг",
                "contact_goal": "Получить дату",
                "call_script": "Добрый день",
                "strengths": [],
                "weaknesses": [],
                "known_facts": [],
                "missing_facts": [],
                "contact_questions": [],
                "call_opening_variants": [],
            },
            "client_communication_profile": {},
        }, errors)
        self.assertFalse(any("direct_manager_question" in item for item in errors), errors)


class SnapshotBuilderTests(unittest.TestCase):
    def test_does_not_copy_invented_communication_content(self) -> None:
        snapshot = build_daily_control_snapshot({
            "deals": [_deal_row(communications_today={
                "date": "2026-08-18",
                "available": True,
                "completed": 1,
                "calls": 1,
                "messages": 0,
                "duration_seconds": 90,
                "items": [{
                    "event_id": "crm_activity:1",
                    "channel": "call",
                    "direction": "outgoing",
                    "occurred_at": "2026-08-18T10:15:00+03:00",
                    "subject": "Звонок",
                    "duration_seconds": 90,
                    "text": "придуманный транскрипт",
                    "body": "тело письма",
                    "content": "сохранённый текст коммуникации",
                    "transcript": "разговор",
                }],
            })],
        })
        item = snapshot["deals"][0]["communications_today"]["items"][0]
        self.assertNotIn("text", item)
        self.assertNotIn("body", item)
        self.assertNotIn("content", item)
        self.assertNotIn("transcript", item)
        self.assertFalse(item["content_available"])
        self.assertEqual(snapshot["deals"][0]["generic_question"], GENERIC_STATUS_QUESTION)

    def test_old_snapshot_without_new_communication_fields_stays_readable(self) -> None:
        snapshot = build_daily_control_snapshot({
            "deals": [_deal_row(communications_today={
                "date": "2026-08-18",
                "available": True,
                "completed": 1,
                "calls": 1,
                "messages": 0,
                "duration_seconds": 90,
                "items": [{
                    "event_id": "crm_activity:1",
                    "channel": "call",
                    "direction": "outgoing",
                    "occurred_at": "2026-08-18T10:15:00+03:00",
                    "subject": "Звонок",
                    "duration_seconds": 90,
                }],
            })],
        })
        communications = snapshot["deals"][0]["communications_today"]
        self.assertEqual(communications["calls_total"], 1)
        self.assertEqual(communications["calls_connected"], 0)
        self.assertIsNone(communications["conversation_duration_seconds"])
        self.assertIsNone(communications["last_activity"])
        self.assertNotIn("content", communications["items"][0])

    def test_sanitize_keeps_new_summary_fields_without_bodies(self) -> None:
        snapshot = build_daily_control_snapshot({
            "deals": [_deal_row(communications_today={
                "date": "2026-08-18",
                "available": True,
                "completed": 2,
                "calls": 1,
                "messages": 1,
                "duration_seconds": 90,
                "calls_total": 1,
                "calls_connected": 1,
                "calls_no_answer": 0,
                "calls_unknown": 0,
                "emails": 0,
                "messenger_messages": 1,
                "conversation_duration_seconds": 70,
                "last_activity": {
                    "event_id": "crm_mirror:1",
                    "kind": "client_reply",
                    "label": "ответ клиента",
                    "occurred_at": "2026-08-18T12:00:00+03:00",
                    "content": "не должен попасть",
                },
                "items": [{
                    "event_id": "crm_mirror:1",
                    "channel": "whatsapp",
                    "direction": "incoming",
                    "occurred_at": "2026-08-18T12:00:00+03:00",
                    "content": "секрет",
                    "html": "<p>секрет</p>",
                    "call_outcome": None,
                    "content_available": True,
                }],
            })],
        })
        communications = snapshot["deals"][0]["communications_today"]
        self.assertEqual(communications["messenger_messages"], 1)
        self.assertEqual(communications["conversation_duration_seconds"], 70)
        self.assertEqual(communications["last_activity"]["kind"], "client_reply")
        self.assertNotIn("content", communications["last_activity"])
        self.assertNotIn("content", communications["items"][0])
        self.assertNotIn("html", communications["items"][0])
        self.assertTrue(communications["items"][0]["content_available"])

    def test_live_dashboard_review_matches_snapshot_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path, audit=_audit(next_action=0))
            dashboard = build_deal_control_dashboard(db_path=db_path, now=NOW)
            snapshot = build_daily_control_snapshot(dashboard)
            live = dashboard["deals"][0]["review"]
            saved = snapshot["deals"][0]
            self.assertEqual(saved["direct_question"], live["direct_question"])
            self.assertEqual(saved["quality"], live["quality"])
            self.assertEqual(saved["generic_question"], live["generic_question"])
            self.assertEqual(saved["status"], live["status"])
            self.assertEqual(saved["pipeline_id"], "15")
            self.assertEqual(saved["pipeline_name"], live["pipeline_name"])
        self.assertEqual(saved["script"], live["script"])
        self.assertEqual(saved["ai_context"], live["ai_context"])

    def test_review_card_preserves_pipeline_contract(self) -> None:
        card = project_deal_review_card(_deal_row())
        self.assertEqual(card["pipeline_id"], "15")
        self.assertEqual(card["pipeline_name"], "Основные продажи")
        self.assertEqual(card["stage_id"], "C15:NEW")

    def test_snapshot_keeps_frozen_bitrix_task_time_bucket(self) -> None:
        snapshot = build_daily_control_snapshot({
            "deals": [
                _deal_row(deal_id="101", primary_bitrix_task={"time_bucket": "today", "completion_state": "open"}),
                _deal_row(deal_id="102", title="Сделка 102", primary_bitrix_task=None),
            ],
        })
        buckets = {item["deal_id"]: item["bitrix_task_time_bucket"] for item in snapshot["deals"]}
        self.assertEqual(buckets["101"], "today")
        self.assertEqual(buckets["102"], "missing")

    def test_review_card_stores_open_bitrix_task_time_bucket(self) -> None:
        self.assertEqual(project_deal_review_card(_deal_row())["bitrix_task_time_bucket"], "missing")
        self.assertEqual(
            project_deal_review_card(_deal_row(primary_bitrix_task={
                "time_bucket": "tomorrow",
                "completion_state": "open",
            }))["bitrix_task_time_bucket"],
            "tomorrow",
        )
        self.assertEqual(
            project_deal_review_card(_deal_row(primary_bitrix_task={
                "time_bucket": "today",
                "completion_state": "bitrix",
            }))["bitrix_task_time_bucket"],
            "missing",
        )
        self.assertEqual(
            project_deal_review_card(_deal_row(primary_bitrix_task={
                "time_bucket": "overdue",
            }))["bitrix_task_time_bucket"],
            "overdue",
        )

    def test_review_script_is_manager_coaching_not_client_call_script(self) -> None:
        card = project_deal_review_card(_deal_row(coaching={
            "report_id": 1,
            "manager_coaching": "Уточни у клиента дату решения комиссии и зафиксируй её в CRM.",
            "script": "Добрый день, это компания Практик-М.",
            "communication_quality_audit": _audit(),
        }))
        self.assertEqual(card["script"], "Уточни у клиента дату решения комиссии и зафиксируй её в CRM.")
        self.assertEqual(card["ai_context"]["manager_coaching"], card["script"])

    def test_unavailable_communications_are_not_zero_activity(self) -> None:
        snapshot = build_daily_control_snapshot({
            "deals": [_deal_row(communications_today={"available": False, "completed": 0, "items": []})],
        })
        self.assertEqual(snapshot["team"]["no_movement"]["total"], 0)
        self.assertTrue(snapshot["deals"][0]["communications_today"]["unavailable"])
        self.assertTrue(any("недоступны" in item for item in snapshot["source_warnings"]))

    def test_missing_analysis_fields_are_empty_not_invented(self) -> None:
        card = project_deal_review_card(_deal_row(coaching={"report_id": None}))
        self.assertIn("Ожидает AI-оценки", card["attention_reason"])
        self.assertIsNone(card["summary_for_rop"])
        self.assertEqual(card["quality"]["status"], "pending_analysis")
        self.assertIsNone(card["quality"]["criteria"]["next_action"]["score"])
        self.assertFalse(card["ai_context"]["current_situation"])
        self.assertFalse(card["script"])

    def test_attention_reason_prefers_main_risk_description(self) -> None:
        card = project_deal_review_card(_deal_row(coaching={
            "report_id": 1,
            "current_situation": "КП отправлено, клиент сравнивает условия.",
            "main_risk_description": "Решение зависло у юриста, срок договора сгорает 25.08.",
            "communication_quality_audit": _audit(),
        }))
        self.assertEqual(card["attention_reason"], "Решение зависло у юриста, срок договора сгорает 25.08.")
        self.assertEqual(card["ai_context"]["current_situation"], "КП отправлено, клиент сравнивает условия.")

    def test_attention_reason_falls_back_to_situation_without_risk(self) -> None:
        card = project_deal_review_card(_deal_row(coaching={
            "report_id": 1,
            "current_situation": "КП отправлено, клиент сравнивает условия.",
            "communication_quality_audit": _audit(),
        }))
        self.assertEqual(card["attention_reason"], "КП отправлено, клиент сравнивает условия.")


class ReportDayScopeTests(unittest.TestCase):
    def test_scope_uses_all_open_tasks_and_only_completion_before_cutoff(self) -> None:
        row = _deal_row(
            communications_today={'date': '2026-08-18', 'available': False},
            bitrix_tasks=[
                {'time_bucket': 'overdue', 'completed': True, 'completion_state': 'bitrix', 'bitrix_completed_at': '2026-08-17T15:00:00+03:00'},
                {'time_bucket': 'today', 'completion_state': 'open'},
                {'time_bucket': 'future', 'completion_state': 'open'},
                {'time_bucket': 'today', 'completion_state': 'local', 'local_completed': True, 'local_completed_at': '2026-08-18T17:00:00+03:00'},
            ],
        )
        scope = project_report_day_scope(project_deal_review_card(row), NOW, source_deal=row)
        self.assertEqual(scope['task_buckets'], ['future', 'today'])
        self.assertEqual(scope['activity_kinds'], [])
        self.assertFalse(scope['legacy'])

    def test_old_snapshot_uses_its_day_and_does_not_count_future_or_undated_marks(self) -> None:
        row = _deal_row(
            communications_today={
                'date': '2026-08-18', 'available': True, 'calls': 2, 'messages': 1,
                'items': [
                    {'event_id': 'before', 'channel': 'call', 'occurred_at': '2026-08-18T12:00:00Z'},
                    {'event_id': 'after', 'channel': 'call', 'occurred_at': '2026-08-18T14:00:00Z'},
                    {'event_id': 'previous-day', 'channel': 'message', 'occurred_at': '2026-08-18T01:00:00+07:00'},
                ],
            },
            checklist={'items': [
                {'id': '1', 'completed': True, 'completed_at': '2026-08-18T17:00:00+03:00'},
                {'id': '2', 'completed': True},
            ]},
        )
        scope = project_report_day_scope(project_deal_review_card(row), NOW)
        self.assertEqual(scope['activity_kinds'], ['call'])
        self.assertEqual(scope['business_date'], '2026-08-18')
        self.assertTrue(scope['legacy'])
        row['communications_today']['date'] = '2026-08-17'
        self.assertEqual(project_report_day_scope(project_deal_review_card(row), NOW)['activity_kinds'], [])

    def test_completed_checklist_is_not_day_work(self) -> None:
        row = _deal_row(communications_today={}, checklist={'items': [
            {'id': '1', 'completed': True, 'completed_at': NOW.isoformat()},
        ]})
        scope = project_report_day_scope(project_deal_review_card(row), NOW)
        self.assertEqual(scope['activity_kinds'], [])
        self.assertFalse(scope['had_day_obligation'])


class DailyControlStorageTests(unittest.TestCase):
    def test_legacy_snapshot_api_omits_daily_fields_without_rewriting_history(self) -> None:
        from storage.rop_db import create_daily_control_report

        snapshot = build_daily_control_snapshot({"deals": [_deal_row()], "managers": []}, cutoff_at=NOW)
        snapshot["deals"][0]["checklist"] = {"completed": 1, "total": 2, "items": []}
        snapshot["managers"][0].update(checklist_completed=1, checklist_total=2)
        saved = create_daily_control_report(
            self.db_path, business_date=NOW.date().isoformat(), creation_kind="manual",
            started_at=NOW.isoformat(), cutoff_at=NOW.isoformat(), snapshot=snapshot,
            source_watermark="legacy", source_status="ok",
        )
        viewed = report_payload(saved["id"], {"role": "admin"}, db_path=self.db_path, now=NOW)
        self.assertNotIn("checklist", viewed["snapshot"]["deals"][0])
        self.assertNotIn("checklist_completed", viewed["snapshot"]["managers"][0])
        self.assertNotIn("checklist_total", viewed["snapshot"]["managers"][0])
        self.assertEqual(get_daily_control_report(self.db_path, saved["id"])["snapshot"], snapshot)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "daily.sqlite"
        init_db(self.db_path)
        self.audit_patch = patch("api.deal_control.COMMUNICATION_QUALITY_AUDIT_ENABLED", True)
        self.audit_patch.start()
        self.crm_refresh = patch(
            "api.daily_control._refresh_final_sources",
            side_effect=lambda **kwargs: build_deal_control_dashboard(
                db_path=kwargs.get("db_path", self.db_path),
                now=kwargs.get("now", NOW),
            ),
        ).start()

    def tearDown(self) -> None:
        self.crm_refresh.stop()
        self.audit_patch.stop()
        self.temp.cleanup()

    def test_manual_report_is_immutable_and_history_is_ordered(self) -> None:
        _seed_deal(self.db_path, audit=_audit(next_action=0))
        first = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW,
            cutoff_at=NOW,
            now=NOW,
            refresh=False,
        )
        second = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW.replace(hour=17),
            cutoff_at=NOW.replace(hour=17),
            now=NOW.replace(hour=17),
            refresh=False,
        )
        history = list_daily_control_reports(self.db_path)
        self.assertEqual([item["id"] for item in history], [second["id"], first["id"]])
        payload = history_payload(db_path=self.db_path, now=NOW.replace(hour=17))
        self.assertEqual(payload["latest_id"], second["id"])
        detail = report_payload(int(first["id"]), {"role": "admin"}, db_path=self.db_path, now=NOW)
        self.assertEqual(detail["freshness"]["state"], "historical")
        self.assertEqual(detail["next_id"], second["id"])
        self.assertIsNone(detail["previous_id"])

        stored = get_daily_control_report(self.db_path, int(first["id"]))
        stored["snapshot"]["deals"][0]["title"] = "изменено в памяти"
        reloaded = get_daily_control_report(self.db_path, int(first["id"]))
        self.assertEqual(reloaded["snapshot"]["deals"][0]["title"], "Сделка 101")

    def test_snapshot_freezes_existing_day_events_and_ignores_plans_and_late_evidence(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        def event(key, kind, payload, *, occurred=NOW.isoformat(), recorded=NOW.isoformat(), entity_id='101', entity_type='deal'):
            with patch('storage.rop_db.utcish_now', return_value=recorded):
                record_manager_trajectory_event(
                    self.db_path, entity_type=entity_type, entity_id=entity_id, manager_id='10',
                    event_type=kind, source='test', source_event_key=key, occurred_at=occurred, payload=payload,
                )

        event('call', 'crm_activity_observed', {'activity_kind': 'call', 'completed': True})
        event('message', 'crm_timeline_comment_observed', {'is_messenger_mirror': True})
        event('comment', 'crm_timeline_comment_observed', {'comment': 'Внутренняя заметка'})
        event('stage', 'crm_stage_history_observed', {'history_type_id': 2, 'stage_id': 'C15:PREPARATION'})
        event('task', 'crm_task_history_observed', {'field': 'STATUS', 'to_value': '5'})
        event('plan', 'crm_activity_planned', {'activity_kind': 'call', 'completed': False}, entity_id='102')
        event('creation', 'crm_stage_history_observed', {'history_type_id': 1}, entity_id='102')
        event('task-deadline', 'crm_task_history_observed', {'field': 'DEADLINE', 'to_value': 'tomorrow'}, entity_id='102')
        event('future', 'crm_timeline_comment_observed', {}, occurred='2026-08-18T17:00:00+03:00', entity_id='102')
        event('late', 'crm_timeline_comment_observed', {}, recorded='2026-08-18T17:00:00+03:00', entity_id='102')
        event('lead', 'crm_timeline_comment_observed', {}, entity_id='102', entity_type='lead')
        dashboard = {'deals': [_deal_row(deal_id=key, communications_today={}) for key in ('101', '102')]}
        first = publish_daily_control_report(db_path=self.db_path, creation_kind='manual', started_at=NOW, now=NOW, dashboard=dashboard)
        rows = {item['deal_id']: item for item in first['snapshot']['deals']}
        self.assertEqual(list(rows), ['101'])
        self.assertEqual(rows['101']['day_scope']['activity_kinds'], ['bitrix_task_completed', 'call', 'message'])
        self.assertNotIn('comment', rows['101']['day_scope']['activity_kinds'])
        self.assertNotIn('stage_change', rows['101']['day_scope']['activity_kinds'])
        self.assertEqual(first['snapshot']['team']['no_movement'], {'count': 0, 'total': 1})
        event('later-call', 'crm_activity_observed', {'activity_kind': 'call', 'completed': True}, entity_id='102')
        old = report_payload(first['id'], {'role': 'admin'}, db_path=self.db_path, now=NOW.replace(day=19))
        old_ids = [item['deal_id'] for item in old['snapshot']['deals']]
        self.assertEqual(old_ids, ['101'])
        self.assertEqual(get_daily_control_report(self.db_path, first['id'])['snapshot'], first['snapshot'])

    def test_legacy_report_never_gets_work_backfilled_from_current_database(self) -> None:
        from storage.rop_db import create_daily_control_report, record_manager_trajectory_event

        snapshot = build_daily_control_snapshot({'deals': [_deal_row()]})
        saved = create_daily_control_report(
            self.db_path, business_date='2026-08-18', creation_kind='manual',
            started_at=NOW.isoformat(), cutoff_at=NOW.isoformat(), snapshot=snapshot, source_watermark='old',
        )
        record_manager_trajectory_event(
            self.db_path, entity_type='deal', entity_id='101', manager_id='10',
            event_type='crm_timeline_comment_observed', source='test', source_event_key='newly-discovered',
            occurred_at=NOW.isoformat(), payload={},
        )
        viewed = report_payload(saved['id'], {'role': 'admin'}, db_path=self.db_path, now=NOW.replace(day=19))
        scope = viewed['snapshot']['deals'][0]['day_scope']
        self.assertEqual(scope['activity_kinds'], ['message'])
        self.assertTrue(scope['legacy'])
        self.assertEqual(scope['business_date'], '2026-08-18')
        stored = get_daily_control_report(self.db_path, saved['id'])
        self.assertNotIn('day_scope', stored['snapshot']['deals'][0])

    def test_work_event_changes_freshness_but_does_not_rewrite_snapshot(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        _seed_deal(self.db_path)
        first = publish_daily_control_report(db_path=self.db_path, creation_kind='manual', started_at=NOW, now=NOW)
        with patch('storage.rop_db.utcish_now', return_value=NOW.isoformat()):
            record_manager_trajectory_event(
                self.db_path, entity_type='deal', entity_id='101', manager_id='10',
                event_type='crm_activity_observed', source='test', source_event_key='later-call',
                occurred_at=NOW.isoformat(), payload={'activity_kind': 'call', 'completed': True},
            )
        self.assertEqual(freshness_for_report(first, db_path=self.db_path, now=NOW)['state'], 'stale')
        self.assertEqual(
            get_daily_control_report(self.db_path, first['id'])['snapshot']['deals'][0]['day_scope']['activity_kinds'],
            first['snapshot']['deals'][0]['day_scope']['activity_kinds'],
        )

    def test_planning_report_uses_moscow_business_date_and_is_not_duplicated(self) -> None:
        _seed_deal(self.db_path)
        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        first = publish_planning_daily_control_report(db_path=self.db_path, now=due)
        second = publish_planning_daily_control_report(db_path=self.db_path, now=due.replace(hour=16))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["creation_kind"], "automatic_planning")
        self.assertEqual(first["business_date"], "2026-08-18")
        self.assertEqual(first["cutoff_at"][:16], "2026-08-18T15:45")
        self.assertEqual(
            report_heading(first["creation_kind"], first["business_date"], first["cutoff_at"]),
            "ОТЧЕТ К ПЛАНЕРКЕ 18.08 15:45",
        )
        self.assertEqual(
            report_payload(int(first["id"]), {"role": "admin"}, db_path=self.db_path, now=due)["heading"],
            "ОТЧЕТ К ПЛАНЕРКЕ 18.08 15:45",
        )
        self.crm_refresh.assert_called()

    def test_day_end_report_refreshes_crm_without_llm_and_stays_independent(self) -> None:
        from storage.rop_db import create_automatic_analysis_run

        _seed_deal(self.db_path)
        due = datetime(2026, 8, 18, 23, 0, tzinfo=MSK_TZ)
        with patch("storage.rop_db.utcish_now", return_value=due.replace(hour=22).isoformat()):
            create_automatic_analysis_run(
                self.db_path, trigger="evening_22", status="done", business_date="2026-08-18",
            )
        planning = publish_planning_daily_control_report(db_path=self.db_path, now=due.replace(hour=15, minute=45))
        dashboard = {"deals": [_deal_row()], "managers": [], "sync_errors": []}
        with patch("api.jobs.start_analyze_job") as analyze:
            first = publish_day_end_daily_control_report(
                db_path=self.db_path, now=due, refresh_fn=lambda **_kwargs: dashboard, clock=lambda: due,
            )
            second = publish_day_end_daily_control_report(
                db_path=self.db_path, now=due, refresh_fn=lambda **_kwargs: dashboard, clock=lambda: due,
            )
        analyze.assert_not_called()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["id"], planning["id"])
        self.assertEqual(first["creation_kind"], "automatic_day_end")
        self.assertEqual(first["cutoff_at"][:16], "2026-08-18T23:00")
        self.assertEqual(
            report_heading(first["creation_kind"], first["business_date"], first["cutoff_at"]),
            "ОТЧЕТ ФИНАЛЬНЫЙ ЗА 18.08 23:00",
        )
        self.assertEqual(len(list_daily_control_reports(self.db_path)), 2)

    def test_morning_opens_previous_workday_final_report(self) -> None:
        from storage.rop_db import create_daily_control_report

        snapshot = {"deals": [], "managers": [], "team": {}}
        final = create_daily_control_report(
            self.db_path, business_date="2026-08-18", creation_kind="automatic_day_end",
            started_at="2026-08-18T23:00:00+03:00", cutoff_at="2026-08-18T23:00:00+03:00",
            snapshot=snapshot, source_watermark="final",
        )
        planning = create_daily_control_report(
            self.db_path, business_date="2026-08-18", creation_kind="automatic_planning",
            started_at="2026-08-18T15:45:00+03:00", cutoff_at="2026-08-18T15:45:00+03:00",
            snapshot=snapshot, source_watermark="planning",
        )
        morning = history_payload(db_path=self.db_path, now=datetime(2026, 8, 19, 8, 10, tzinfo=MSK_TZ))
        afternoon = history_payload(db_path=self.db_path, now=datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ))
        self.assertEqual(morning["default_id"], final["id"])
        self.assertFalse(morning["missing_morning_final"])
        self.assertEqual(afternoon["default_id"], planning["id"])
        self.assertEqual(afternoon["latest_id"], planning["id"])

    def test_reschedule_stays_in_today_and_keeps_both_task_facts(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        def event(key, payload, occurred=NOW.isoformat()):
            with patch("storage.rop_db.utcish_now", return_value=occurred):
                record_manager_trajectory_event(
                    self.db_path, entity_type="deal", entity_id="101", manager_id="10",
                    event_type="crm_task_history_observed", source="test", source_event_key=key,
                    occurred_at=occurred, payload=payload,
                )

        event("move", {
            "task_id": "9", "field": "DEADLINE",
            "from_value": "2026-08-18T18:00:00+03:00", "to_value": "2026-08-19T18:00:00+03:00",
        }, occurred="2026-08-18T12:00:00+03:00")
        event("done", {"task_id": "9", "field": "STATUS", "to_value": "5"})
        dashboard = {"deals": [_deal_row(
            bitrix_tasks=[{
                "task_id": "9", "activity_id": "a9", "subject": "Согласовать КП",
                "deadline": "2026-08-19T18:00:00+03:00", "completed": True,
                "bitrix_completed_at": NOW.isoformat(), "time_bucket": "tomorrow",
            }],
            communications_today={"date": "2026-08-18", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": []},
        )]}
        report = publish_daily_control_report(
            db_path=self.db_path, creation_kind="manual", started_at=NOW, now=NOW, dashboard=dashboard,
        )
        deal = report["snapshot"]["deals"][0]
        self.assertIn("bitrix_task_rescheduled", deal["day_scope"]["activity_kinds"])
        self.assertIn("bitrix_task_completed", deal["day_scope"]["activity_kinds"])
        task = deal["task_results"][0]
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["completed_today"])
        self.assertEqual(task["reschedules"][0]["from_deadline"], "2026-08-18T18:00:00+03:00")
        self.assertEqual(task["reschedules"][0]["to_deadline"], "2026-08-19T18:00:00+03:00")
        self.assertTrue(deal["day_scope"]["had_day_obligation"])
        self.assertTrue(deal["day_scope"]["untouched"])
        self.assertEqual(report["snapshot"]["team"]["tasks_completed"], 1)
        self.assertEqual(report["snapshot"]["team"]["tasks_rescheduled"], 1)

    def test_planning_keeps_reschedule_recorded_just_after_cutoff(self) -> None:
        from storage.rop_db import get_daily_control_report, record_manager_trajectory_event

        cutoff = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        collected = datetime(2026, 8, 18, 15, 46, tzinfo=MSK_TZ)
        dashboard = {"deals": [_deal_row(
            communications_today={
                "date": "2026-08-18", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": [],
            },
            bitrix_tasks=[{
                "task_id": "9", "deadline": "2026-08-19T18:00:00+03:00",
                "time_bucket": "tomorrow", "completion_state": "open",
            }],
        )], "managers": [], "sync_errors": []}

        def refresh_fn(**_kwargs):
            with patch("storage.rop_db.utcish_now", return_value=collected.isoformat()):
                record_manager_trajectory_event(
                    self.db_path, entity_type="deal", entity_id="101", manager_id="10",
                    event_type="crm_task_history_observed", source="test", source_event_key="move",
                    occurred_at="2026-08-18T15:30:00+03:00",
                    payload={
                        "task_id": "9", "field": "DEADLINE",
                        "from_value": "2026-08-18T18:00:00+03:00",
                        "to_value": "2026-08-19T18:00:00+03:00",
                    },
                )
            return dashboard

        report = publish_planning_daily_control_report(
            db_path=self.db_path, now=cutoff, refresh_fn=refresh_fn, clock=lambda: collected,
        )
        self.assertEqual([item["deal_id"] for item in report["snapshot"]["deals"]], ["101"])
        deal = report["snapshot"]["deals"][0]
        self.assertIn("bitrix_task_rescheduled", deal["day_scope"]["activity_kinds"])
        self.assertTrue(deal["task_results"][0]["reschedules"])
        self.assertTrue(deal["task_results"][0]["was_due"])
        self.assertTrue(deal["day_scope"]["had_day_obligation"])
        self.assertGreaterEqual(report["snapshot"]["team"]["tasks_rescheduled"], 1)

        with patch("storage.rop_db.utcish_now", return_value="2026-08-18T17:00:00+03:00"):
            record_manager_trajectory_event(
                self.db_path, entity_type="deal", entity_id="101", manager_id="10",
                event_type="crm_activity_observed", source="test", source_event_key="late-call",
                occurred_at="2026-08-18T15:40:00+03:00",
                payload={"activity_kind": "call", "completed": True},
            )
        frozen = get_daily_control_report(self.db_path, report["id"])
        self.assertEqual(frozen["snapshot"], report["snapshot"])
        self.assertNotIn("call", frozen["snapshot"]["deals"][0]["day_scope"]["activity_kinds"])

    def test_source_preparation_is_frozen_and_manual_regeneration_uses_finished_batch(self) -> None:
        from storage.rop_db import create_automatic_analysis_run, finish_automatic_analysis_run

        _seed_deal(self.db_path)
        due = NOW.replace(hour=15, minute=45)
        run = create_automatic_analysis_run(self.db_path, trigger='test', business_date=due.date().isoformat())
        first = publish_planning_daily_control_report(db_path=self.db_path, now=due)
        self.assertEqual(first['snapshot']['source_preparation']['status'], 'running')
        self.assertEqual(first['source_status'], 'partial')
        self.assertTrue(any('ещё выполнялся' in item for item in first['warnings']))
        finish_automatic_analysis_run(self.db_path, run['id'], status='done')
        with patch('api.jobs.start_analyze_job') as analyze:
            second = publish_daily_control_report(
                db_path=self.db_path, creation_kind='manual', started_at=due,
                now=due.replace(hour=8), refresh=True,
                refresh_fn=lambda **_kwargs: {'deals': [], 'managers': []},
            )
        analyze.assert_not_called()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(second['snapshot']['source_preparation']['status'], 'done')
        self.assertIsNotNone(second['snapshot']['source_preparation']['finished_at'])
        frozen = get_daily_control_report(self.db_path, first['id'])
        self.assertEqual(frozen['snapshot']['source_preparation']['status'], 'running')
        detail = report_payload(second['id'], {'role': 'admin'}, db_path=self.db_path, now=due)
        self.assertEqual(detail['snapshot']['source_preparation'], second['snapshot']['source_preparation'])

    def test_old_or_missing_batch_does_not_claim_todays_sources_are_ready(self) -> None:
        from storage.rop_db import create_automatic_analysis_run

        for status in (None, 'done'):
            with self.subTest(status=status):
                if status:
                    create_automatic_analysis_run(self.db_path, trigger='test', status=status, business_date='2026-08-17')
                report = publish_daily_control_report(
                    db_path=self.db_path, creation_kind='manual', started_at=NOW, now=NOW,
                    dashboard={'deals': [], 'managers': []},
                )
                self.assertEqual(report['source_status'], 'partial')
                self.assertTrue(any('за сегодня не подтверждено' in warning for warning in report['warnings']))

    def test_latest_report_becomes_stale_after_source_changes(self) -> None:
        _seed_deal(self.db_path)
        report = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW,
            cutoff_at=NOW,
            now=NOW,
            refresh=False,
        )
        fresh = freshness_for_report(report, db_path=self.db_path, now=NOW)
        self.assertEqual(fresh["state"], "current")
        upsert_deal_control_deal(
            self.db_path,
            deal_id="101",
            source="initial",
            title="Сделка 101",
            manager_id="10",
            manager_name="Иванов Иван",
            stage_id="C15:NEW",
            stage_name="Новая",
            pipeline_id="15",
            amount="150000",
            currency_id="RUB",
            created_at_crm="2026-08-17T09:00:00+03:00",
            modified_at_crm="2026-08-18T18:00:00+03:00",
            is_active=True,
        )
        stale = freshness_for_report(report, db_path=self.db_path, now=NOW)
        self.assertEqual(stale["state"], "stale")
        self.assertNotEqual(stale["live_watermark"], report["source_watermark"])
        self.assertNotEqual(compute_source_watermark(self.db_path, now=NOW), report["source_watermark"])

    def test_reports_ignore_legacy_checklist_in_snapshot_and_freshness(self) -> None:
        _seed_deal(self.db_path)
        first = publish_daily_control_report(
            db_path=self.db_path, creation_kind="manual", started_at=NOW,
            cutoff_at=NOW, now=NOW, refresh=False,
        )
        self.assertNotIn("checklist", first["snapshot"]["deals"][0])
        self.assertNotIn("checklist_completed", first["snapshot"]["managers"][0])
        self.assertNotIn("checklist_total", first["snapshot"]["managers"][0])
        watermark = compute_source_watermark(self.db_path, now=NOW)
        # Simulate orphaned tables from an older version, not a runtime storage API.
        with connect(self.db_path) as conn:
            conn.execute("CREATE TABLE deal_daily_checklists (id INTEGER, revision INTEGER)")
            conn.execute("INSERT INTO deal_daily_checklists VALUES (1, 99)")
        self.assertEqual(compute_source_watermark(self.db_path, now=NOW), watermark)
        frozen = get_daily_control_report(self.db_path, int(first["id"]))
        self.assertEqual(frozen["snapshot"], first["snapshot"])


    def test_old_analysis_without_direct_question_still_renders(self) -> None:
        _seed_deal(self.db_path)
        report = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW,
            cutoff_at=NOW,
            now=NOW,
            refresh=False,
        )
        question = report["snapshot"]["deals"][0]["direct_question"]
        self.assertTrue(question)
        self.assertIn("Ты", question)

    def test_daily_control_has_no_checklist_write_route(self) -> None:
        from api.app import app

        paths = [getattr(route, "path", "") for route in app.routes]
        self.assertTrue(any(path.startswith("/api/daily-control/reports") for path in paths))
        self.assertFalse(any("/checklist/" in path for path in paths))


class DailySnapshotSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "daily.sqlite"
        init_db(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self, dashboard, *, kind="manual", now=NOW):
        return publish_daily_control_report(
            db_path=self.db_path, creation_kind=kind, started_at=now, cutoff_at=now, now=now, dashboard=dashboard,
        )

    def test_heading_uses_kind_date_and_cutoff_time(self) -> None:
        planning = datetime(2026, 8, 27, 15, 45, tzinfo=MSK_TZ)
        final = datetime(2026, 8, 27, 23, 0, tzinfo=MSK_TZ)
        self.assertEqual(report_heading("automatic_planning", "2026-08-27", planning), "ОТЧЕТ К ПЛАНЕРКЕ 27.08 15:45")
        self.assertEqual(report_heading("automatic_day_end", "2026-08-27", final), "ОТЧЕТ ФИНАЛЬНЫЙ ЗА 27.08 23:00")
        self.assertEqual(report_heading("manual", "2026-08-27", NOW), "ОТЧЕТ ВРУЧНУЮ 27.08 16:00")

    def test_future_deal_with_internal_activity_stays_out_unless_client_contact(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        def event(key, kind, payload, entity_id="101"):
            with patch("storage.rop_db.utcish_now", return_value=NOW.isoformat()):
                record_manager_trajectory_event(
                    self.db_path, entity_type="deal", entity_id=entity_id, manager_id="10",
                    event_type=kind, source="test", source_event_key=key, occurred_at=NOW.isoformat(), payload=payload,
                )

        event("stage", "crm_stage_history_observed", {"history_type_id": 2, "stage_id": "C15:PREPARATION"})
        event("comment", "crm_timeline_comment_observed", {"comment": "Внутренняя заметка"})
        future_task = {"time_bucket": "future", "completion_state": "open"}
        report = self._publish({"deals": [
            _deal_row(deal_id="101", communications_today={}, primary_bitrix_task=future_task, bitrix_tasks=[future_task]),
            _deal_row(
                deal_id="102", title="Сделка 102",
                communications_today={
                    "date": "2026-08-18", "available": True, "calls": 1, "messages": 0, "completed": 1,
                    "items": [{"event_id": "c1", "channel": "call", "occurred_at": "2026-08-18T10:00:00+03:00"}],
                },
                primary_bitrix_task=future_task, bitrix_tasks=[future_task],
            ),
        ]})
        self.assertEqual([item["deal_id"] for item in report["snapshot"]["deals"]], ["102"])
        self.assertEqual(report["snapshot"]["team"]["deals_total"], 1)
        self.assertEqual(report["snapshot"]["team"]["calls"], 1)

    def test_closed_today_task_stays_in_snapshot_even_if_primary_looks_missing(self) -> None:
        report = self._publish({"deals": [_deal_row(
            communications_today={"date": "2026-08-18", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": []},
            primary_bitrix_task=None,
            bitrix_tasks=[{
                "task_id": "9", "time_bucket": "today", "completed": True,
                "completion_state": "bitrix", "bitrix_completed_at": "2026-08-18T12:00:00+03:00",
                "deadline": "2026-08-18T18:00:00+03:00",
            }],
        )]})
        self.assertEqual(len(report["snapshot"]["deals"]), 1)
        deal = report["snapshot"]["deals"][0]
        self.assertTrue(deal["day_scope"]["had_day_obligation"])
        self.assertTrue(deal["day_scope"]["untouched"])
        self.assertEqual(deal["bitrix_task_time_bucket"], "missing")

    def test_reschedule_of_future_task_does_not_add_the_deal(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        with patch("storage.rop_db.utcish_now", return_value=NOW.isoformat()):
            record_manager_trajectory_event(
                self.db_path, entity_type="deal", entity_id="101", manager_id="10",
                event_type="crm_task_history_observed", source="test", source_event_key="move",
                occurred_at="2026-08-18T12:00:00+03:00",
                payload={
                    "task_id": "9", "field": "DEADLINE",
                    "from_value": "2026-08-25T18:00:00+03:00", "to_value": "2026-08-26T18:00:00+03:00",
                },
            )
        report = self._publish({"deals": [_deal_row(
            communications_today={},
            bitrix_tasks=[{
                "task_id": "9", "deadline": "2026-08-26T18:00:00+03:00",
                "time_bucket": "future", "completion_state": "open",
            }],
        )]})
        self.assertEqual(report["snapshot"]["deals"], [])
        self.assertEqual(report["snapshot"]["team"]["deals_total"], 0)

    def test_unix_deadline_reschedule_keeps_deal_when_crm_deadline_is_already_future(self) -> None:
        from storage.rop_db import record_manager_trajectory_event

        cutoff = datetime(2026, 8, 28, 15, 45, tzinfo=MSK_TZ)
        with patch("storage.rop_db.utcish_now", return_value="2026-08-28T12:00:00+03:00"):
            record_manager_trajectory_event(
                self.db_path, entity_type="deal", entity_id="101", manager_id="10",
                event_type="crm_task_history_observed", source="test", source_event_key="move",
                occurred_at="2026-08-28T12:00:00+03:00",
                payload={
                    "task_id": "9", "field": "DEADLINE",
                    "from_value": "1787896800", "to_value": "1788242400",
                },
            )
        report = self._publish({"deals": [_deal_row(
            communications_today={
                "date": "2026-08-28", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": [],
            },
            bitrix_tasks=[{
                "task_id": "9", "deadline": "2026-09-01T09:00:00+03:00",
                "time_bucket": "future", "completion_state": "open",
            }],
        )]}, now=cutoff)
        self.assertEqual([item["deal_id"] for item in report["snapshot"]["deals"]], ["101"])
        deal = report["snapshot"]["deals"][0]
        self.assertIn("bitrix_task_rescheduled", deal["day_scope"]["activity_kinds"])
        self.assertTrue(deal["task_results"][0]["reschedules"])
        self.assertTrue(deal["task_results"][0]["was_due"])
        self.assertTrue(deal["day_scope"]["had_day_obligation"])
        self.assertGreaterEqual(report["snapshot"]["team"]["tasks_rescheduled"], 1)

    def test_unavailable_comms_keep_obligation_and_do_not_add_future_without_contact(self) -> None:
        report = self._publish({"deals": [
            _deal_row(
                deal_id="101",
                communications_today={"date": "2026-08-18", "available": False, "completed": 0, "items": []},
                primary_bitrix_task={"time_bucket": "today", "completion_state": "open"},
                bitrix_tasks=[{"time_bucket": "today", "completion_state": "open"}],
            ),
            _deal_row(
                deal_id="102", title="Сделка 102",
                communications_today={"date": "2026-08-18", "available": False, "completed": 0, "items": []},
                primary_bitrix_task={"time_bucket": "future", "completion_state": "open"},
                bitrix_tasks=[{"time_bucket": "future", "completion_state": "open"}],
            ),
        ]})
        self.assertEqual([item["deal_id"] for item in report["snapshot"]["deals"]], ["101"])
        deal = report["snapshot"]["deals"][0]
        self.assertTrue(deal["day_scope"]["had_day_obligation"])
        self.assertFalse(deal["day_scope"]["untouched"])
        self.assertEqual(report["snapshot"]["team"]["no_movement"]["total"], 0)

    def test_day_end_keeps_planning_obligation_and_adds_later_contact(self) -> None:
        planning_due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        final_due = datetime(2026, 8, 18, 23, 0, tzinfo=MSK_TZ)
        planning = publish_daily_control_report(
            db_path=self.db_path, creation_kind="automatic_planning",
            started_at=planning_due, cutoff_at=planning_due, now=planning_due,
            dashboard={"deals": [_deal_row(
                communications_today={"date": "2026-08-18", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": []},
                primary_bitrix_task={"time_bucket": "today", "completion_state": "open"},
                bitrix_tasks=[{"task_id": "9", "time_bucket": "today", "completion_state": "open", "deadline": "2026-08-18T18:00:00+03:00"}],
            )], "managers": [], "sync_errors": []},
        )
        self.assertEqual([item["deal_id"] for item in planning["snapshot"]["deals"]], ["101"])
        self.assertTrue(planning["snapshot"]["deals"][0]["day_scope"]["had_day_obligation"])
        final = publish_day_end_daily_control_report(
            db_path=self.db_path, now=final_due,
            refresh_fn=lambda **_kwargs: {"deals": [
                _deal_row(
                    communications_today={"date": "2026-08-18", "available": True, "completed": 0, "calls": 0, "messages": 0, "items": []},
                    primary_bitrix_task={"time_bucket": "tomorrow", "completion_state": "open"},
                    bitrix_tasks=[{
                        "task_id": "9", "time_bucket": "tomorrow", "completion_state": "open",
                        "deadline": "2026-08-19T18:00:00+03:00",
                    }],
                ),
                _deal_row(
                    deal_id="202", title="Сделка 202",
                    communications_today={
                        "date": "2026-08-18", "available": True, "calls": 1, "completed": 1,
                        "items": [{"event_id": "late", "channel": "call", "occurred_at": "2026-08-18T18:10:00+03:00"}],
                    },
                    primary_bitrix_task={"time_bucket": "future", "completion_state": "open"},
                    bitrix_tasks=[{"time_bucket": "future", "completion_state": "open"}],
                ),
            ], "managers": [], "sync_errors": []},
            clock=lambda: final_due,
        )
        ids = [item["deal_id"] for item in final["snapshot"]["deals"]]
        self.assertEqual(sorted(ids), ["101", "202"])
        rows = {item["deal_id"]: item for item in final["snapshot"]["deals"]}
        self.assertTrue(rows["101"]["day_scope"]["had_day_obligation"])
        self.assertTrue(rows["101"]["day_scope"]["untouched"])
        self.assertIn("call", rows["202"]["day_scope"]["activity_kinds"])
        self.assertEqual(final["snapshot"]["team"]["deals_total"], 2)


class DailyControlAccessTests(unittest.TestCase):
    def test_manager_is_forbidden_rop_can_read_only_admin_can_generate(self) -> None:
        from api import app as api_app

        manager = {"id": 3, "login": "manager-3", "role": "manager", "manager_id": "10", "is_active": True}
        with patch.object(api_app, "auth_current_user", return_value=manager):
            with self.assertRaises(HTTPException) as error:
                api_app.daily_control_reports()
            self.assertEqual(error.exception.status_code, 403)
            with self.assertRaises(HTTPException) as error:
                api_app.daily_control_report_create()
            self.assertEqual(error.exception.status_code, 403)

        rop = {"id": 2, "login": "rop-1", "role": "rop", "manager_id": None, "is_active": True}
        with patch.object(api_app, "auth_current_user", return_value=rop), \
             patch.object(api_app, "daily_control_history_payload", return_value={"reports": []}):
            self.assertEqual(api_app.daily_control_reports(), {"reports": []})
            with self.assertRaises(HTTPException) as error:
                api_app.daily_control_report_create()
            self.assertEqual(error.exception.status_code, 403)

        admin = {"id": 1, "login": "admin-1", "role": "admin", "manager_id": None, "is_active": True}
        with patch.object(api_app, "auth_current_user", return_value=admin), \
             patch.object(api_app, "daily_control_history_payload", return_value={"reports": []}), \
             patch.object(api_app, "start_manual_daily_control_report", return_value={"status": "running"}):
            self.assertEqual(api_app.daily_control_reports(), {"reports": []})
            self.assertEqual(api_app.daily_control_report_create()["status"], "running")


if __name__ == "__main__":
    unittest.main()
