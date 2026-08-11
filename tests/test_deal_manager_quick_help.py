from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_quick_help as quick_help
from api import deal_manager_situation as situation
from openai_api.llm.deal_manager_quick_help import (
    build_quick_help_prompt,
    generate_deal_manager_quick_help,
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
    "analysis_projection": {
        "deal_state": {"summary": "КП отправлено"},
        "client_communication_profile": {
            "status": "tentative",
            "primary_style": "C",
            "secondary_style": None,
            "role_separation_confidence": "medium",
            "profile_confidence": "low",
            "evidence": ["Клиент просит точные критерии сравнения."],
            "insufficient_reason": None,
            "recommended_communication": {
                "tone": "Спокойно и предметно.",
                "structure": "Факты, критерии и следующий шаг.",
                "emphasize": ["Критерии"],
                "avoid": ["Общие обещания"],
            },
        },
    },
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
    "answer_contract": "strategy_v1",
    "situation_summary": "Клиент получил КП, но следующий шаг ещё не согласован.",
    "next_action": "Напишите клиенту один короткий вопрос о решении.",
    "expected_result": "Цель — получить ответ и согласовать дату следующего шага.",
    "client_messages": {
        "primary": "Добрый день! Подскажите, какой следующий шаг по КП вам удобнее согласовать?",
        "alternative": "Добрый день! Какой вопрос по КП сейчас мешает определить дату решения?",
        "pattern_break": "Добрый день! Ответьте, пожалуйста, одним словом: обсуждаем, переносим или закрываем?",
    },
    "call_scripts": {
        "primary": "Добрый день! Предлагаю за минуту согласовать следующий шаг по КП.",
        "alternative": "Добрый день! Какой критерий по КП нужно уточнить, чтобы определить дату решения?",
        "pattern_break": "Добрый день! Давайте коротко выберем: продолжаем, переносим или закрываем вопрос?",
    },
    "recommended_strategy": "pattern_break",
    "recommended_channel": "message",
    "fallback_action": "Если ответа не будет, зафиксируйте паузу и следующий допустимый момент возврата.",
    "crm_checklist": ["Ответ клиента", "Дата следующего шага"],
}
COMMUNICATION_CONTEXT = {
    "window_days": 30,
    "max_recent_events": 10,
    "total_attempts": 5,
    "confirmed_contacts": 0,
    "attempts_by_channel": {"call": 3, "message": 2},
    "consecutive_attempts_without_contact": 5,
    "last_confirmed_contact": None,
    "recent_events": [],
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

    def test_only_one_active_quick_help_job_per_deal(self) -> None:
        active = quick_help.DealManagerQuickHelpJob(
            job_id="active",
            deal_id="101",
            question="Первый вопрос",
            situation_id=21,
            status="running",
            stage="llm",
        )
        quick_help._QUICK_HELP_JOBS[active.job_id] = active
        with patch.object(
            quick_help,
            "load_manager_screen_context",
            return_value={"situation_id": 21},
        ):
            result = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="Второй вопрос",
                confirm_paid=True,
            )
        self.assertEqual(result["job_id"], "active")

    def test_schema_and_prompt_are_strict_and_history_free(self) -> None:
        prompt = build_quick_help_prompt(
            question="Что сказать клиенту после паузы?",
            analysis_projection=CONTEXT["analysis_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            situation_projection=CONTEXT["situation_projection"],
            communication_pattern_context=COMMUNICATION_CONTEXT,
        )
        self.assertIn("Понял ситуацию", prompt)
        self.assertIn("Что сказать клиенту", prompt)
        self.assertIn("SITUATION_CONTEXT", prompt)
        self.assertIn("client_communication_profile", prompt)
        self.assertIn("COMMUNICATION_PATTERN_CONTEXT", prompt)
        self.assertIn("primary_style", prompt)
        self.assertIn("одна главная ближайшая micro-conversion", prompt)
        self.assertIn("не делай fallback сложнее основного касания", prompt)
        self.assertIn("при активном техническом обсуждении", prompt)
        self.assertIn("зачем клиенту отвечать", prompt)
        self.assertIn("Не придумывай выгоду", prompt)
        self.assertLess(prompt.index("CURRENT_BITRIX_TASK"), prompt.index("MANAGER_QUESTION"))
        self.assertNotIn("old_quick_help_answer", prompt)
        self.assertFalse(quick_help_schema()["additionalProperties"])
        self.assertEqual(quick_help_schema()["properties"]["crm_checklist"]["maxItems"], 4)
        self.assertEqual(
            quick_help_schema()["properties"]["recommended_strategy"]["enum"],
            ["primary", "alternative", "pattern_break"],
        )
        self.assertEqual(validate_quick_help(ANSWER), ANSWER)

    def test_generate_marks_reusable_deal_context_before_dynamic_question(self) -> None:
        with patch(
            "openai_api.llm.deal_manager_quick_help.call_structured_output_json",
            return_value=(ANSWER, {}),
        ) as call:
            generate_deal_manager_quick_help(
                question="Что сказать клиенту?",
                analysis_projection=CONTEXT["analysis_projection"],
                deal=DEAL,
                current_bitrix_task=CONTEXT["current_bitrix_task"],
                situation_projection=CONTEXT["situation_projection"],
                communication_pattern_context=COMMUNICATION_CONTEXT,
            )
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["prompt_cache_key"], "neuro-rop:deal-manager-quick-help:v3")
        self.assertIn("CURRENT_BITRIX_TASK", kwargs["stable_prefix"])
        self.assertIn("COMMUNICATION_PATTERN_CONTEXT", kwargs["stable_prefix"])
        self.assertNotIn("MANAGER_QUESTION", kwargs["stable_prefix"])
        self.assertTrue(call.call_args.args[0].startswith(kwargs["stable_prefix"]))

    def test_validation_rejects_missing_strategy_variant(self) -> None:
        invalid = {**ANSWER, "client_messages": {"primary": "Текст", "alternative": "Другой текст"}}
        with self.assertRaisesRegex(ValueError, "client_messages"):
            validate_quick_help(invalid)

    def test_validation_rejects_duplicate_strategy_variants_and_summary_list(self) -> None:
        duplicate = {**ANSWER, "call_scripts": {"primary": "Одна фраза", "alternative": "Одна фраза", "pattern_break": "Одна фраза"}}
        with self.assertRaisesRegex(ValueError, "разные варианты"):
            validate_quick_help(duplicate)
        listed = {**ANSWER, "next_action": "Сначала напишите.\nПотом позвоните."}
        with self.assertRaisesRegex(ValueError, "без списка"):
            validate_quick_help(listed)

    def test_communication_pattern_context_is_bounded_and_fact_only(self) -> None:
        events = [
            {
                "occurred_at": f"2026-08-{day:02d}T10:00:00+03:00",
                "channel": "call" if day % 2 else "message",
                "direction": "outgoing",
                "contact_class": "attempt",
                "preview": "Чувствительный текст не должен попасть в prompt",
            }
            for day in range(1, 12)
        ]
        events.extend([
            {
                "occurred_at": "2026-07-10T10:00:00+03:00",
                "channel": "call",
                "direction": "incoming",
                "contact_class": "confirmed_contact",
            },
            {
                "occurred_at": "2026-08-10T12:00:00+03:00",
                "channel": "message",
                "direction": "outgoing",
                "contact_class": "internal_information",
            },
        ])
        result = quick_help.build_communication_pattern_context(
            events,
            now=datetime.fromisoformat("2026-08-11T18:00:00+03:00"),
        )
        self.assertEqual(result["total_attempts"], 11)
        self.assertEqual(result["consecutive_attempts_without_contact"], 11)
        self.assertEqual(len(result["recent_events"]), 10)
        self.assertEqual(result["last_confirmed_contact"]["occurred_at"], "2026-07-10T10:00:00+03:00")
        self.assertNotIn("preview", str(result))

    def test_quick_help_requires_confirmed_situation_and_saves_safe_answer(self) -> None:
        calls = []

        def save_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            if name == "save_deal_manager_quick_help":
                return {"id": 31, "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", return_value=(ANSWER, {"model": "gpt-5.6-luna", "raw_output_text": "secret"})) as generate, \
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
        communication_context = generate.call_args.kwargs["communication_pattern_context"]
        self.assertEqual(communication_context["window_days"], 30)
        self.assertEqual(communication_context["recent_events"], [])

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

    def test_assistant_workspace_combines_help_crm_history_and_context(self) -> None:
        entry = {
            "id": 31,
            "deal_id": "101",
            "question": "Что делать после отправки КП?",
            "content": ANSWER,
            "created_at": "2026-08-05T11:20:00+03:00",
        }

        def storage_call(name, db_path, **kwargs):
            if name == "list_deal_manager_quick_help":
                return [entry]
            if name == "list_deal_manager_assistant_events":
                return [{
                    "id": 7,
                    "event_type": "communication_completed",
                    "created_at": "2026-08-05T11:40:00+03:00",
                }]
            raise AssertionError(name)

        communications = [{
            "event_id": "crm_activity:500",
            "occurred_at": "2026-08-04T16:10:00+03:00",
            "channel": "whatsapp",
            "direction": "outgoing",
            "source_label": "CRM/WhatsApp",
            "subject": "КП отправлено",
            "preview": "КП отправлено клиенту",
            "contact_class": "attempt",
        }]
        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_storage_call", side_effect=storage_call), \
             patch.object(quick_help, "_load_local_communications", return_value=communications):
            result = quick_help.get_manager_assistant_workspace(
                db_path=Path("state.sqlite"), deal_id="101"
            )

        self.assertTrue(result["started"])
        self.assertEqual(result["entries"][0]["id"], 31)
        self.assertEqual(result["context"]["stage"], "КП")
        self.assertEqual(result["context"]["current_task"], "Позвонить клиенту")
        self.assertEqual(result["context"]["main_risk"], "Нет даты решения")
        self.assertEqual(result["context"]["last_communication"]["occurred_at"], "2026-08-04T16:10:00+03:00")
        self.assertEqual(
            [item["kind"] for item in result["timeline"]],
            ["communication_completed", "assistant_request", "communication"],
        )

    def test_communication_completed_uses_confirmed_deal_context(self) -> None:
        calls = []

        def storage_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            return {"id": 8, **kwargs}

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT) as load, \
             patch.object(quick_help, "_storage_call", side_effect=storage_call):
            result = quick_help.record_manager_communication_completed(
                db_path=Path("state.sqlite"), deal_id="101", quick_help_id=31
            )

        load.assert_called_once_with(Path("state.sqlite"), "101", require_confirmed_situation=True)
        self.assertEqual(result["event_type"], "communication_completed")
        self.assertEqual(calls[0][1]["quick_help_id"], 31)

    def test_assistant_workspace_routes_are_present(self) -> None:
        from api.app import app

        methods = {route.path: route.methods for route in app.routes}
        self.assertEqual(methods["/api/deal-control/deals/{deal_id}/assistant-workspace"], {"GET"})
        self.assertEqual(
            methods["/api/deal-control/deals/{deal_id}/assistant/communication-completed"],
            {"POST"},
        )


if __name__ == "__main__":
    unittest.main()
