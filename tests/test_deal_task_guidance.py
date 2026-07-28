from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.deal_task_guidance import (
    _GUIDANCE_JOBS,
    get_task_guidance_job,
    start_task_guidance_job,
)
from openai_api.llm.deal_task_guidance import (
    MAX_GUIDANCE_OUTPUT_TOKENS,
    build_deal_task_guidance_prompt,
    compact_analysis_context,
    deal_task_guidance_schema,
)
from storage.rop_db import (
    create_deal_control_task,
    list_deal_control_tasks,
    save_deal_control_task_guidance,
    save_ui_report,
    update_deal_control_task,
    upsert_deal_control_deal,
)


GUIDANCE = {
    "task_focus": "Получить решение по КП",
    "expected_outcome": "Зафиксированы решение клиента и следующий шаг",
    "known_facts": ["КП отправлено"],
    "missing_facts": ["Дата решения не подтверждена"],
    "contact_goal": "Получить решение или согласованную дату решения",
    "contact_questions": ["Когда вы сможете принять решение по КП?"],
    "ready_text": "Добрый день! Возвращаюсь к направленному КП.",
    "crm_checklist": ["Позиция клиента", "Дата следующего шага"],
}


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def add_deal(db_path: Path) -> None:
    upsert_deal_control_deal(
        db_path,
        deal_id="101",
        source="initial",
        title="Тестовая сделка",
        manager_id="10",
        manager_name="Иванов Иван",
        stage_id="C15:NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="120000",
        currency_id="RUB",
        created_at_crm="2026-07-20T09:00:00+03:00",
        modified_at_crm="2026-07-20T10:00:00+03:00",
        is_active=True,
    )


def add_report(db_path: Path, *, summary: str = "КП отправлено") -> int:
    return save_ui_report(
        db_path,
        entity_type="deal",
        entity_id="101",
        report_json={
            "deal_state": {"summary": summary},
            "deal_control_brief": {
                "current_situation": summary,
                "known_facts": ["КП отправлено"],
                "missing_facts": ["Дата решения не подтверждена"],
            },
            "private_extra": {"must_not_enter_prompt": True},
        },
    )


class DealTaskGuidanceTests(unittest.TestCase):
    def setUp(self):
        _GUIDANCE_JOBS.clear()

    def test_prompt_uses_task_and_only_compact_analysis_fields(self):
        task = {
            "id": 7,
            "task_text": "Позвонить клиенту",
            "touch_type": "Звонок",
            "expected_result": "Получить решение",
            "due_at": "2026-07-20T16:00:00+03:00",
        }
        report = {
            "analysis": {
                "deal_state": {"summary": "КП отправлено"},
                "deal_control_brief": {"known_facts": ["КП отправлено"]},
                "private_extra": {"secret": "not included"},
            }
        }
        prompt = build_deal_task_guidance_prompt(
            task=task,
            deal={"deal_id": "101", "title": "Тестовая сделка"},
            report_json=report,
        )
        self.assertIn("Позвонить клиенту", prompt)
        self.assertIn("КП отправлено", prompt)
        self.assertNotIn("private_extra", prompt)
        self.assertNotIn("secret", prompt)
        self.assertIn("ready_text — до 1200 знаков", prompt)
        self.assertEqual(MAX_GUIDANCE_OUTPUT_TOKENS, 5000)
        self.assertEqual(deal_task_guidance_schema()["properties"]["known_facts"]["maxItems"], 4)
        self.assertEqual(set(deal_task_guidance_schema()["required"]), set(GUIDANCE))
        self.assertNotIn("private_extra", compact_analysis_context(report))

    def test_guidance_becomes_stale_only_when_task_input_or_report_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            add_deal(db_path)
            report_id = add_report(db_path)
            task = create_deal_control_task(
                db_path,
                deal_id="101",
                task_text="Позвонить клиенту",
                touch_type="Звонок",
                expected_result="Получить решение",
                due_at="2026-07-20T16:00:00+03:00",
            )
            save_deal_control_task_guidance(
                db_path,
                task_id=int(task["id"]),
                task_revision=1,
                source_report_id=report_id,
                guidance=GUIDANCE,
            )
            current = list_deal_control_tasks(db_path)[0]
            self.assertFalse(current["guidance"]["is_stale"])

            update_deal_control_task(
                db_path,
                task_id=int(task["id"]),
                business_result_status="client_fact",
            )
            self.assertFalse(list_deal_control_tasks(db_path)[0]["guidance"]["is_stale"])

            update_deal_control_task(
                db_path,
                task_id=int(task["id"]),
                expected_result="Получить решение и дату оплаты",
            )
            self.assertTrue(list_deal_control_tasks(db_path)[0]["guidance"]["is_stale"])

            changed = list_deal_control_tasks(db_path)[0]
            save_deal_control_task_guidance(
                db_path,
                task_id=int(task["id"]),
                task_revision=int(changed["guidance_revision"]),
                source_report_id=report_id,
                guidance=GUIDANCE,
            )
            self.assertFalse(list_deal_control_tasks(db_path)[0]["guidance"]["is_stale"])
            add_report(db_path, summary="Получен новый ответ клиента")
            self.assertTrue(list_deal_control_tasks(db_path)[0]["guidance"]["is_stale"])

    def test_explicit_paid_confirmation_and_background_job(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            add_deal(db_path)
            add_report(db_path)
            task = create_deal_control_task(
                db_path,
                deal_id="101",
                task_text="Позвонить клиенту",
                touch_type="Звонок",
                expected_result="Получить решение",
                due_at="2026-07-20T16:00:00+03:00",
            )
            with self.assertRaisesRegex(ValueError, "Подтвердите платный"):
                start_task_guidance_job(db_path=db_path, task_id=int(task["id"]), confirm_paid=False)

            metadata = {
                "model": "test-model",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "raw_output_text": "must not be stored",
            }
            with (
                patch("api.deal_task_guidance.threading.Thread", ImmediateThread),
                patch(
                    "api.deal_task_guidance.generate_deal_task_guidance",
                    return_value=(GUIDANCE, metadata),
                ),
            ):
                started = start_task_guidance_job(
                    db_path=db_path,
                    task_id=int(task["id"]),
                    confirm_paid=True,
                )
            job = get_task_guidance_job(started["job_id"])
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["stage"], "done")
            saved = list_deal_control_tasks(db_path)[0]["guidance"]
            self.assertEqual(saved["content"]["contact_goal"], GUIDANCE["contact_goal"])
            self.assertNotIn("raw_output_text", saved["model_meta"])

    def test_guidance_requires_existing_full_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            add_deal(db_path)
            task = create_deal_control_task(
                db_path,
                deal_id="101",
                task_text="Позвонить клиенту",
                touch_type="Звонок",
                expected_result="Получить решение",
                due_at="2026-07-20T16:00:00+03:00",
            )
            with self.assertRaisesRegex(ValueError, "Сначала проведите полный анализ"):
                start_task_guidance_job(db_path=db_path, task_id=int(task["id"]), confirm_paid=True)


if __name__ == "__main__":
    unittest.main()
