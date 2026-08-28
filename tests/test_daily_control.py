from __future__ import annotations

import tempfile
import unittest
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
    report_payload,
)
from openai_api.llm.validation import _validate_deal_management_shapes
from setup import MSK_TZ
from storage.rop_db import (
    get_daily_control_report,
    init_db,
    list_daily_control_reports,
    save_deal_control_communications_today,
    save_deal_control_scope,
    save_deal_daily_checklist_item_completion,
    save_ui_report,
    upsert_deal_control_deal,
)


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ)


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
            "items": [],
        },
        "checklist": {"completed": 1, "total": 2, "items": []},
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
    def test_overdue_local_rop_task_does_not_force_red(self) -> None:
        status, label = classify_deal_status(_deal_row(
            current_task={"time_bucket": "overdue"},
            coaching={"report_id": 1, "communication_quality_audit": _audit()},
            checklist={"completed": 2, "total": 2, "items": []},
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
            checklist={"completed": 2, "total": 2, "items": []},
        ))
        self.assertEqual(status, "green")
        self.assertEqual(label, "В норме")


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
        self.assertIn("Ты подтвердил", question)
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
        self.assertEqual(card["attention_reason"], "Нет данных")
        self.assertIsNone(card["summary_for_rop"])
        self.assertEqual(card["quality"]["insufficient_reason"], "Нет данных")
        self.assertEqual(card["quality"]["criteria"]["next_action"]["verdict"], "Нет данных")
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

    def test_completed_checklist_is_work_but_not_a_crm_task_completion(self) -> None:
        row = _deal_row(communications_today={}, checklist={'items': [
            {'id': '1', 'completed': True, 'completed_at': NOW.isoformat()},
        ]})
        scope = project_report_day_scope(project_deal_review_card(row), NOW)
        self.assertEqual(scope['activity_kinds'], ['checklist_completed'])


class DailyControlStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "daily.sqlite"
        init_db(self.db_path)
        self.audit_patch = patch("api.deal_control.COMMUNICATION_QUALITY_AUDIT_ENABLED", True)
        self.audit_patch.start()

    def tearDown(self) -> None:
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
        self.assertEqual(rows['101']['day_scope']['activity_kinds'], ['bitrix_task_completed', 'call', 'comment', 'message', 'stage_change'])
        self.assertEqual(rows['102']['day_scope']['activity_kinds'], [])
        self.assertEqual(first['snapshot']['team']['no_movement'], {'count': 0, 'total': 1})
        event('later-call', 'crm_activity_observed', {'activity_kind': 'call', 'completed': True}, entity_id='102')
        old = report_payload(first['id'], {'role': 'admin'}, db_path=self.db_path, now=NOW.replace(day=19))
        old_rows = {item['deal_id']: item for item in old['snapshot']['deals']}
        self.assertEqual(old_rows['102']['day_scope']['activity_kinds'], [])
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
        self.assertEqual(scope['activity_kinds'], ['call'])
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
                event_type='crm_timeline_comment_observed', source='test', source_event_key='comment',
                occurred_at=NOW.isoformat(), payload={},
            )
        self.assertEqual(freshness_for_report(first, db_path=self.db_path, now=NOW)['state'], 'stale')
        self.assertNotIn('comment', get_daily_control_report(self.db_path, first['id'])['snapshot']['deals'][0]['day_scope']['activity_kinds'])

    def test_planning_report_uses_moscow_business_date_and_is_not_duplicated(self) -> None:
        _seed_deal(self.db_path)
        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        first = publish_planning_daily_control_report(db_path=self.db_path, now=due)
        second = publish_planning_daily_control_report(db_path=self.db_path, now=due.replace(hour=16))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["creation_kind"], "automatic_planning")
        self.assertEqual(first["business_date"], "2026-08-18")
        self.assertEqual(first["cutoff_at"][:16], "2026-08-18T15:45")

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
            communications_today={},
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
        self.assertEqual(report["snapshot"]["team"]["tasks_completed"], 1)
        self.assertEqual(report["snapshot"]["team"]["tasks_rescheduled"], 1)

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

    def test_report_uses_manager_checklist_and_stays_frozen_after_later_marks(self) -> None:
        _seed_deal(self.db_path)
        first = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW,
            cutoff_at=NOW,
            now=NOW,
            refresh=False,
        )
        deal = first["snapshot"]["deals"][0]
        self.assertGreaterEqual(deal["checklist"]["total"], 1)
        open_item = next(item for item in deal["checklist"]["items"] if not item["completed"])
        save_deal_daily_checklist_item_completion(
            self.db_path,
            deal_id="101",
            item_id=open_item["id"],
            completed=True,
            business_date="2026-08-18",
        )
        frozen = get_daily_control_report(self.db_path, int(first["id"]))
        frozen_item = next(item for item in frozen["snapshot"]["deals"][0]["checklist"]["items"] if item["id"] == open_item["id"])
        self.assertFalse(frozen_item["completed"])
        later = publish_daily_control_report(
            db_path=self.db_path,
            creation_kind="manual",
            started_at=NOW.replace(hour=18),
            cutoff_at=NOW.replace(hour=18),
            now=NOW.replace(hour=18),
            refresh=False,
        )
        later_item = next(item for item in later["snapshot"]["deals"][0]["checklist"]["items"] if item["id"] == open_item["id"])
        self.assertTrue(later_item["completed"])
        self.assertEqual(later["snapshot"]["managers"][0]["checklist_completed"], later["snapshot"]["deals"][0]["checklist"]["completed"])

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
        self.assertFalse(any("daily-control" in path and "checklist" in path for path in paths))


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
