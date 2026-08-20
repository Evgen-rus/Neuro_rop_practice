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
    publish_daily_control_report,
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
    def test_overdue_control_and_two_zero_scores_are_red(self) -> None:
        status, label = classify_deal_status(_deal_row(current_task={"time_bucket": "overdue"}))
        self.assertEqual(status, "red")
        self.assertEqual(label, "Требует решения РОПа")
        status, _ = classify_deal_status(_deal_row(
            coaching={"report_id": 1, "communication_quality_audit": _audit(next_action=0, value=0)},
        ))
        self.assertEqual(status, "red")

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
                    "transcript": "разговор",
                }],
            })],
        })
        item = snapshot["deals"][0]["communications_today"]["items"][0]
        self.assertNotIn("text", item)
        self.assertNotIn("body", item)
        self.assertNotIn("transcript", item)
        self.assertFalse(item["content_available"])
        self.assertEqual(snapshot["deals"][0]["generic_question"], GENERIC_STATUS_QUESTION)

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
        self.assertEqual(saved["script"], live["script"])
        self.assertEqual(saved["ai_context"], live["ai_context"])

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

    def test_planning_report_uses_moscow_business_date_and_is_not_duplicated(self) -> None:
        _seed_deal(self.db_path)
        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        first = publish_planning_daily_control_report(db_path=self.db_path, now=due)
        second = publish_planning_daily_control_report(db_path=self.db_path, now=due.replace(minute=46))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["creation_kind"], "automatic_planning")
        self.assertEqual(first["business_date"], "2026-08-18")
        self.assertEqual(first["cutoff_at"][:16], "2026-08-18T15:45")

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
    def test_manager_is_forbidden_admin_and_rop_are_allowed(self) -> None:
        from api import app as api_app

        manager = {"id": 3, "login": "manager-3", "role": "manager", "manager_id": "10", "is_active": True}
        with patch.object(api_app, "auth_current_user", return_value=manager):
            with self.assertRaises(HTTPException) as error:
                api_app.daily_control_reports()
            self.assertEqual(error.exception.status_code, 403)
            with self.assertRaises(HTTPException) as error:
                api_app.daily_control_report_create()
            self.assertEqual(error.exception.status_code, 403)

        for role in ("admin", "rop"):
            user = {"id": 1, "login": f"{role}-1", "role": role, "manager_id": None, "is_active": True}
            with patch.object(api_app, "auth_current_user", return_value=user), \
                 patch.object(api_app, "daily_control_history_payload", return_value={"reports": []}), \
                 patch.object(api_app, "start_manual_daily_control_report", return_value={"status": "running"}):
                self.assertEqual(api_app.daily_control_reports(), {"reports": []})
                self.assertEqual(api_app.daily_control_report_create()["status"], "running")


if __name__ == "__main__":
    unittest.main()
