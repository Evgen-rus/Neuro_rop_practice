from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api.app as api_app
import api.jobs as jobs
from storage.rop_db import (
    connect,
    get_candidate_review_states,
    get_lead_workflow_state,
    get_ui_report,
    init_db,
    list_rop_decisions,
    save_rop_decision,
    save_ui_report,
    upsert_candidate_review_state,
    upsert_lead_workflow_state,
)


class LeadWorkflowStorageTests(unittest.TestCase):
    def test_round_trip_and_lead_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            first = upsert_lead_workflow_state(
                db_path,
                lead_id="101",
                source_report_id=None,
                manager_review_text="Разбор",
                manager_message_options=["Вариант 1", "Вариант 2", "Вариант 3"],
                manager_full_review_text="Разбор целиком",
                manager_task_text="Задача",
                review_completed=True,
                task_completed=False,
                control_mode="days",
                control_days=3,
                control_date=None,
                control_completed=False,
                final_decision=None,
            )
            self.assertEqual(first["lead_id"], "101")
            self.assertTrue(first["review_completed"])
            self.assertEqual(first["control_days"], 3)
            self.assertEqual(first["manager_message_options"], ["Вариант 1", "Вариант 2", "Вариант 3"])
            self.assertEqual(first["manager_full_review_text"], "Разбор целиком")
            self.assertIsNone(get_lead_workflow_state(db_path, "202"))

    def test_report_snapshots_are_optional_and_decoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            old_id = save_ui_report(db_path, entity_type="lead", entity_id="1", report_json={"lead_state": {}})
            new_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="2",
                report_json={"lead_state": {}},
                report_meta={"stage_name": "Новый"},
                technical_log={"status": "done"},
                model_context={"history_text": "История", "transcript_text": "Транскрипт", "transcript_used": True},
            )
            self.assertIsNone(get_ui_report(db_path, old_id)["report_meta"])
            self.assertEqual(get_ui_report(db_path, new_id)["report_meta"]["stage_name"], "Новый")
            self.assertEqual(get_ui_report(db_path, new_id)["technical_log"]["status"], "done")
            self.assertEqual(get_ui_report(db_path, new_id)["model_context"]["transcript_text"], "Транскрипт")


class LeadWorkflowApiTests(unittest.TestCase):
    def test_review_link_returns_only_the_selected_saved_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="501",
                report_json={
                    "deal_state": {"summary": "Нужно подтвердить срок закупки."},
                    "rop_manager_message_block": {"message_to_manager": "Уточнить срок у клиента."},
                },
                technical_log={"internal": "not for review"},
                model_context={"history_text": "not for review"},
            )
            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                full = api_app.report_detail(report_id)
                share_token = full["share_token"]
                review = api_app.review_report(share_token)

            self.assertEqual(review["id"], report_id)
            self.assertEqual(review["entity_type"], "deal")
            self.assertEqual(review["entity_id"], "501")
            self.assertEqual(review["report_json"]["deal_state"]["summary"], "Нужно подтвердить срок закупки.")
            self.assertNotIn("technical_log", review)
            self.assertNotIn("model_context", review)
            self.assertNotIn("share_token", review)

    def test_control_toggle_syncs_candidate_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="77",
                report_json={
                    "lead_state": {"summary": "Нужна проверка"},
                    "rop_manager_message_block": {
                        "manager_review_text": "Хорошо проведён первый контакт. Теперь важно согласовать следующий шаг.",
                        "message_to_manager": "До 2026-07-24 позвонить клиенту и зафиксировать результат в CRM.",
                        "deadline": "2026-07-24",
                    },
                    "manager_action_block": {
                        "primary_text": {"text": "Клиентский вариант 1"},
                        "backup_texts": [{"text": "Клиентский вариант 2"}, {"text": "Клиентский вариант 3"}],
                    },
                },
            )
            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                control = api_app.save_lead_workflow(
                    "77",
                    api_app.LeadWorkflowRequest(
                        source_report_id=report_id,
                        manager_full_review_text="Ручная редакция всего разбора",
                        review_completed=True,
                        task_completed=True,
                        control_mode="date",
                        control_date="2026-07-24",
                    ),
                )
                self.assertEqual(control["status_label"], "На контроле")
                self.assertEqual(control["manager_full_review_text"], "Ручная редакция всего разбора")
                review = get_candidate_review_states(db_path, entity_type="lead", entity_ids=["77"])["77"]
                self.assertEqual(review["state"], "snoozed")
                self.assertEqual(review["next_control_date"], "2026-07-24")

                active = api_app.save_lead_workflow(
                    "77",
                    api_app.LeadWorkflowRequest(control_mode=None, control_date=None, control_days=None),
                )
                self.assertEqual(active["status_label"], "В работе")
                review = get_candidate_review_states(db_path, entity_type="lead", entity_ids=["77"])["77"]
                self.assertEqual(review["state"], "active")
                self.assertEqual(review["decision"], "Снят с контроля")

    def test_legacy_report_uses_client_texts_and_builds_review_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="88",
                report_json={
                    "lead_state": {},
                    "manager_quality": {"what_done_well": ["собраны исходные параметры"], "missed_points": []},
                    "rop_manager_message_block": {
                        "message_to_manager": "Позвонить клиенту.",
                        "why_it_matters": "Нужно подтвердить актуальность.",
                    },
                    "manager_action_block": {
                        "primary_text": {"text": "Клиентский вариант 1"},
                        "backup_texts": [{"text": "Клиентский вариант 2"}, {"text": "Клиентский вариант 3"}],
                    },
                },
            )
            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                workflow = api_app.lead_workflow("88", report_id=report_id)
            self.assertEqual(
                workflow["manager_message_options"],
                ["Клиентский вариант 1", "Клиентский вариант 2", "Клиентский вариант 3"],
            )
            self.assertIn("собраны исходные параметры", workflow["manager_review_text"])
            self.assertIn("Вариант 1 — Деловой и прямой", workflow["manager_full_review_text"])
            self.assertIn("«Клиентский вариант 1»", workflow["manager_full_review_text"])
            self.assertNotIn("final_decision", workflow)

    def test_confirmed_correct_closure_builds_review_without_client_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="230059",
                report_json={
                    "lead_state": {"summary": "Закрытие подтверждено."},
                    "closure_review": {
                        "verdict": "confirmed_correct",
                        "client_contact_required": False,
                        "manager_task_required": False,
                    },
                    "rop_manager_message_block": {
                        "manager_review_text": "Лид закрыт корректно; дополнительных действий не требуется.",
                        "message_to_manager": "Дополнительная задача менеджеру не требуется.",
                    },
                    "manager_action_block": {"primary_text": None, "backup_texts": []},
                },
            )

            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                workflow = api_app.lead_workflow("230059", report_id=report_id)

            self.assertEqual(workflow["manager_message_options"], [])
            self.assertNotIn("Вариант 1", workflow["manager_full_review_text"])
            self.assertEqual(workflow["manager_task_text"], "Дополнительная задача менеджеру не требуется.")

    def test_confirmed_correct_missing_crm_return_keeps_task_and_hides_client_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="230411",
                report_json={
                    "lead_state": {"summary": "Проект отложен."},
                    "closure_review": {
                        "verdict": "confirmed_correct",
                        "client_contact_required": False,
                        "manager_task_required": False,
                    },
                    "qualification_assessment": {
                        "lead_category": {"value": "C"},
                        "lead_route": {
                            "controlled_return_required": True,
                            "controlled_return_status": "missing_in_crm",
                            "recommended_return_date": "2027-01-21",
                        },
                    },
                    "rop_manager_message_block": {
                        "manager_review_text": "Клиенту сейчас писать не нужно; оформи возврат в CRM.",
                        "message_to_manager": "До 2026-07-24 создай в CRM задачу возврата на 2027-01-21.",
                        "deadline": "2026-07-24",
                    },
                    "manager_action_block": {"primary_text": None, "backup_texts": []},
                },
            )

            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                workflow = api_app.lead_workflow("230411", report_id=report_id)

            self.assertEqual(workflow["manager_message_options"], [])
            self.assertIn("создай в CRM задачу возврата", workflow["manager_task_text"])
            self.assertFalse(workflow["task_completed"])

    def test_legacy_report_preserves_manually_edited_client_texts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="89",
                report_json={
                    "rop_manager_message_block": {
                        "manager_message_options": ["Старый вариант 1", "Старый вариант 2", "Старый вариант 3"],
                    },
                    "manager_action_block": {
                        "primary_text": {"text": "Клиентский вариант 1"},
                        "backup_texts": [{"text": "Клиентский вариант 2"}, {"text": "Клиентский вариант 3"}],
                    },
                },
            )
            edited_options = ["Ручной вариант 1", "Ручной вариант 2", "Ручной вариант 3"]
            upsert_lead_workflow_state(
                db_path,
                lead_id="89",
                source_report_id=report_id,
                manager_review_text="Ручной разбор",
                manager_message_options=edited_options,
                manager_task_text="Задача",
                review_completed=False,
                task_completed=False,
                control_mode=None,
                control_days=2,
                control_date=None,
                control_completed=False,
                final_decision=None,
            )

            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                workflow = api_app.lead_workflow("89", report_id=report_id)

            self.assertEqual(workflow["manager_review_text"], "Ручной разбор")
            self.assertEqual(workflow["manager_message_options"], edited_options)

    def test_new_report_refreshes_workflow_texts_and_review_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            old_report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="90",
                report_json={"lead_state": {}},
            )
            upsert_lead_workflow_state(
                db_path,
                lead_id="90",
                source_report_id=old_report_id,
                manager_review_text="Старый разбор",
                manager_message_options=["Старый 1", "Старый 2", "Старый 3"],
                manager_task_text="Старая задача",
                review_completed=True,
                task_completed=True,
                control_mode="days",
                control_days=2,
                control_date=None,
                control_completed=False,
                final_decision=None,
            )
            new_report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="90",
                report_json={
                    "rop_manager_message_block": {
                        "manager_review_text": "Новый разбор",
                        "message_to_manager": "Новая задача",
                    },
                    "manager_action_block": {
                        "primary_text": {"text": "Новый 1"},
                        "backup_texts": [{"text": "Новый 2"}, {"text": "Новый 3"}],
                    },
                },
            )

            with patch.object(api_app, "DEFAULT_DB_PATH", db_path):
                workflow = api_app.lead_workflow("90", report_id=new_report_id)

            self.assertEqual(workflow["source_report_id"], new_report_id)
            self.assertEqual(workflow["manager_review_text"], "Новый разбор")
            self.assertEqual(workflow["manager_message_options"], ["Новый 1", "Новый 2", "Новый 3"])
            self.assertIn("Вариант 3 — Спокойный и консультативный", workflow["manager_full_review_text"])
            self.assertIn("«Новый 3»", workflow["manager_full_review_text"])
            self.assertEqual(workflow["manager_task_text"], "Новая задача")
            self.assertFalse(workflow["review_completed"])
            self.assertFalse(workflow["task_completed"])
            self.assertEqual(workflow["control_mode"], "days")

    def test_one_time_migration_reactivates_no_attention_and_keeps_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.db"
            report_id = save_ui_report(db_path, entity_type="lead", entity_id="99", report_json={"lead_state": {}})
            upsert_lead_workflow_state(
                db_path,
                lead_id="99",
                source_report_id=report_id,
                manager_review_text="Старый разбор",
                manager_message_options=None,
                manager_task_text="Старая задача",
                review_completed=True,
                task_completed=True,
                control_mode=None,
                control_days=None,
                control_date=None,
                control_completed=True,
                final_decision="no_attention",
            )
            upsert_candidate_review_state(
                db_path,
                entity_type="lead",
                entity_id="99",
                state="reviewed",
                report_id=report_id,
                decision="Не требует внимания",
            )
            save_rop_decision(db_path, report_id=report_id, decision="Не требует внимания")
            with connect(db_path) as conn:
                conn.execute(
                    "DELETE FROM local_migrations WHERE migration_id = ?",
                    ("2026-07-22-reactivate-lead-no-attention",),
                )

            init_db(db_path)

            workflow = get_lead_workflow_state(db_path, "99")
            review = get_candidate_review_states(db_path, entity_type="lead", entity_ids=["99"])["99"]
            self.assertIsNone(workflow["final_decision"])
            self.assertEqual(review["state"], "active")
            self.assertIsNone(review["next_control_date"])
            self.assertEqual(list_rop_decisions(db_path, report_id)[0]["decision"], "Не требует внимания")


class LeadReportSnapshotTests(unittest.TestCase):
    def test_communication_summary_separates_attempt_contact_and_internal_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lead_root = root / "reports" / "rop_assistant" / "leads" / "lead_5"
            transcript_dir = lead_root / "transcripts"
            transcript_dir.mkdir(parents=True)
            (transcript_dir / "call_10_client_transcript.json").write_text(
                json.dumps(
                    {
                        "text": (
                            "Менеджер: Добрый день, уточню параметры проекта. "
                            "Клиент: Да, проект актуален, предложение получили. "
                            "Давайте вернёмся к согласованию бюджета на следующей неделе."
                        )
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            bundle = {
                "normalized_communications": [
                    {
                        "event_id": "crm_activity:10",
                        "occurred_at": "2026-07-18T10:00:00+03:00",
                        "channel": "call",
                        "direction": "outgoing",
                        "source_label": "Активность Bitrix",
                        "call_id": "10",
                        "duration_seconds": 95.0,
                        "contact_class": "attempt",
                    },
                    {
                        "event_id": "crm_activity:11",
                        "occurred_at": "2026-07-19T10:00:00+03:00",
                        "channel": "call",
                        "direction": "outgoing",
                        "source_label": "Активность Bitrix",
                        "call_id": "11",
                        "duration_seconds": 0.0,
                        "contact_class": "attempt",
                    },
                    {
                        "event_id": "crm_timeline_comment:12",
                        "occurred_at": "2026-07-20T10:00:00+03:00",
                        "channel": "internal_comment",
                        "direction": "internal",
                        "source_label": "Комментарий CRM",
                        "content": "Менеджер сообщил, что КП отправлено.",
                        "contact_class": "internal_information",
                    },
                ]
            }
            with patch.object(jobs, "PROJECT_ROOT", root):
                summary = jobs.build_lead_communication_summary("5", bundle)

            self.assertEqual(summary["last_attempt"]["event_id"], "crm_activity:11")
            self.assertEqual(summary["last_attempt"]["contact_class"], "attempt")
            self.assertIn("короче 20 секунд", summary["last_attempt"]["classification_reason"])
            self.assertEqual(summary["last_confirmed_contact"]["event_id"], "crm_activity:10")
            self.assertTrue(summary["last_confirmed_contact"]["has_transcript"])
            self.assertEqual(summary["last_internal_information"]["event_id"], "crm_timeline_comment:12")

    def test_metadata_uses_local_bundle_and_russian_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lead_dir = root / "reports" / "rop_assistant" / "leads" / "lead_5" / "raw"
            lead_dir.mkdir(parents=True)
            (root / "crm_pipeline_map.json").write_text(
                json.dumps({"lead_pipeline": {"stages": [{"status_id": "NEW", "name": "Новый"}]}}, ensure_ascii=False),
                encoding="utf-8",
            )
            bundle = {
                "generated_at": "2026-07-20T10:00:00+07:00",
                "lead": {"response": {"result": {"ID": "5", "TITLE": "Лид 5", "STATUS_ID": "NEW", "ASSIGNED_BY_ID": "9", "DATE_CREATE": "2026-07-13T10:30:00+03:00", "DATE_MODIFY": "2026-07-20T11:45:00+03:00"}}},
                "client_touchpoints": [{"event_type": "call", "when": "2026-07-19", "subject": "Звонок", "text": "Обсудили задачу"}],
                "tasks_and_control": [{"event_type": "task", "when": "2026-07-21", "subject": "Перезвонить", "completed": False}],
            }
            (lead_dir / "lead_5_customer_history_bundle.json").write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(jobs, "PROJECT_ROOT", root):
                metadata = jobs.build_lead_report_meta("5")
            self.assertEqual(metadata["stage_name"], "Новый")
            self.assertEqual(metadata["lead_created_at"], "2026-07-13T10:30:00+03:00")
            self.assertEqual(metadata["lead_modified_at"], "2026-07-20T11:45:00+03:00")
            self.assertEqual(metadata["last_contact"]["type"], "Звонок")
            self.assertEqual(metadata["current_task"]["subject"], "Перезвонить")

    def test_model_context_snapshot_reads_only_factual_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "history.md"
            transcript_path = root / "transcript.md"
            history_path.write_text("История CRM", encoding="utf-8")
            transcript_path.write_text("Транскрипт звонка", encoding="utf-8")
            snapshot = jobs.build_model_context_snapshot(
                {
                    "input_files": {
                        "history": str(history_path),
                        "transcript": str(transcript_path),
                        "knowledge": ["не должен попасть в snapshot"],
                    }
                }
            )
            self.assertEqual(snapshot["history_text"], "История CRM")
            self.assertEqual(snapshot["transcript_text"], "Транскрипт звонка")
            self.assertTrue(snapshot["transcript_used"])


if __name__ == "__main__":
    unittest.main()
