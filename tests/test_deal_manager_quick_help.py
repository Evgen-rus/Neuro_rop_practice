from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_quick_help as quick_help
from api import deal_manager_situation as situation
from openai_api.llm.deal_manager_quick_help import (
    build_quick_help_prompt,
    quick_help_schema,
    validate_quick_help,
)


DEAL = {
    "deal_id": "101",
    "title": "Тестовая сделка",
    "stage_name": "КП",
    "primary_bitrix_task": {
        "activity_id": "500",
        "subject": "Позвонить клиенту",
        "deadline": "2026-08-03T15:00:00+03:00",
        "completed": False,
    },
}
CONTEXT = {
    "deal": DEAL,
    "analysis_projection": {"deal_state": {"summary": "КП отправлено"}},
    "current_bitrix_task": DEAL["primary_bitrix_task"],
    "source_report_id": 17,
    "situation_id": 21,
    "situation_status": "confirmed",
    "situation_projection": {
        "current_situation": "Клиент получил КП",
        "what_blocks_progress": "Нет даты решения",
        "manager_action": "Уточнить критерий решения",
        "next_step": "Получить дату следующего шага",
        "facts_to_clarify": ["Дата решения"],
    },
}
ANSWER = {
    "problem_summary": "Нет подтверждённого следующего шага.",
    "diagnosis": "Что сейчас мешает: менеджер возвращается к КП без новой проверяемой причины.",
    "recommended_action": "Задать один вопрос о критерии и сроке решения.",
    "action_steps": ["Назвать цель звонка", "Спросить о критерии", "Зафиксировать дату"],
    "client_message": "Добрый день! Подскажите, какой вопрос по КП сейчас нужно уточнить?",
    "call_script": "Добрый день! Хочу понять, что нужно уточнить по КП, чтобы согласовать следующий шаг.",
    "facts_to_clarify": ["Критерий решения", "Дата решения"],
    "crm_checklist": ["Ответ клиента", "Дата следующего шага"],
}


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class DealManagerQuickHelpTests(unittest.TestCase):
    def setUp(self) -> None:
        quick_help._QUICK_HELP_JOBS.clear()

    def test_schema_and_prompt_are_strict_and_history_free(self) -> None:
        prompt = build_quick_help_prompt(
            question="Что сказать клиенту после паузы?",
            analysis_projection=CONTEXT["analysis_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            situation_projection=CONTEXT["situation_projection"],
        )
        self.assertIn("Что сейчас мешает", prompt)
        self.assertIn("Что сказать клиенту", prompt)
        self.assertIn("SITUATION_CONTEXT", prompt)
        self.assertNotIn("old_quick_help_answer", prompt)
        self.assertFalse(quick_help_schema()["additionalProperties"])
        for field in ("action_steps", "facts_to_clarify", "crm_checklist"):
            self.assertEqual(quick_help_schema()["properties"][field]["maxItems"], 4)
        self.assertEqual(validate_quick_help(ANSWER), ANSWER)

    def test_quick_help_requires_confirmed_situation_and_saves_safe_answer(self) -> None:
        calls = []

        def save_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            if name == "save_deal_manager_quick_help":
                return {"id": 31, "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "generate_deal_manager_quick_help", return_value=(ANSWER, {"model": "gpt-5.6-luna", "raw_output_text": "secret"})), \
             patch.object(quick_help, "_storage_call", side_effect=save_call), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            started = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="Что сказать клиенту после паузы?",
                confirm_paid=True,
            )

        self.assertEqual(started["stage"], "done")
        job = quick_help.get_quick_help_job(started["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["quick_help_id"], 31)
        name, kwargs = calls[0]
        self.assertEqual(name, "save_deal_manager_quick_help")
        self.assertEqual(kwargs["question"], "Что сказать клиенту после паузы?")
        self.assertEqual(kwargs["answer_json"], ANSWER)
        self.assertEqual(kwargs["situation_review_id"], 21)
        self.assertNotIn("raw_output_text", kwargs["model_meta"])

    def test_history_has_limit_cursor_and_does_not_bypass_situation_gate(self) -> None:
        calls = []

        def list_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            self.assertEqual(name, "list_deal_manager_quick_help")
            return [{"id": 30, "question": "Первый вопрос", "answer_json": ANSWER}]

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_storage_call", side_effect=list_call):
            result = quick_help.list_quick_help_history(
                db_path=Path("state.sqlite"), deal_id="101", limit=20, before_id=31
            )

        self.assertEqual(result["items"][0]["id"], 30)
        self.assertEqual(calls[0][1], {"deal_id": "101", "limit": 20, "before_id": 31})
        with patch.object(quick_help, "load_manager_screen_context", side_effect=ValueError("Сначала подтвердите")):
            with self.assertRaisesRegex(ValueError, "подтвердите"):
                quick_help.list_quick_help_history(db_path=Path("state.sqlite"), deal_id="101")

    def test_paid_confirmation_and_question_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "платный"):
            quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question="Помоги", confirm_paid=False
            )
        with self.assertRaisesRegex(ValueError, "от 1 до 4000"):
            quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question=" ", confirm_paid=True
            )
        with self.assertRaisesRegex(ValueError, "от 1 до 100"):
            quick_help.list_quick_help_history(
                db_path=Path("state.sqlite"), deal_id="101", limit=101
            )


if __name__ == "__main__":
    unittest.main()
