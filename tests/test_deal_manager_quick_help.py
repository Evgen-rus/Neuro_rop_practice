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
    project_locked_move,
    project_quick_help_for_material,
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
ANSWER_V2 = {
    "answer_contract": "strategy_v2",
    "situation_summary": "Клиент получил КП, но следующий шаг ещё не согласован.",
    "next_action": "Напишите клиенту один короткий вопрос о решении.",
    "expected_result": "Цель — получить ответ и согласовать дату следующего шага.",
    "client_messages": {
        "primary": "Добрый день! Подскажите, какой следующий шаг по КП вам удобнее согласовать?",
        "alternative": "Добрый день! Какой вопрос по КП сейчас мешает определить дату решения?",
        "pattern_break": "Добрый день! Ответьте, пожалуйста, одним словом: обсуждаем, переносим или закрываем?",
    },
    "lifehacks": [{
        "tactic_id": "MT-CONTACT-002",
        "title": "Смена канала связи",
        "action": "Перейти с безрезультатных звонков на короткое сообщение.",
        "why_relevant": "История показывает несколько попыток без контакта.",
        "conditions": "Использовать только доступный клиенту канал.",
    }],
    "fallback_action": "Если ответа не будет, зафиксируйте паузу и следующий допустимый момент возврата.",
}
ANSWER = {
    **ANSWER_V2,
    "answer_contract": "strategy_v3",
    "mode": "reanimator",
    "pressure_lever": {
        "title": "Низкое усилие ответа",
        "rationale": "Несколько исходящих попыток без подтверждённого контакта, поэтому сейчас важнее вернуть клиента в диалог.",
    },
    "strategy_labels": {
        "primary": "Через короткий вопрос",
        "alternative": "Через уточнение blocker",
        "pattern_break": "Через выбор из трёх",
    },
    "client_messages": {
        "primary": "Добрый день! Подскажите, какой следующий шаг по КП вам удобнее согласовать?",
        "alternative": "Добрый день! Какой вопрос по КП сейчас мешает определить дату решения?",
        "pattern_break": "Добрый день! Ответьте, пожалуйста, одним словом: обсуждаем, переносим или закрываем?",
    },
}
PUSH_ANSWER = {
    **ANSWER,
    "mode": "push",
    "pressure_lever": {
        "title": "Отстройка через надёжность",
        "rationale": "Клиент сравнивает решение с конкурентом, ключевой вопрос — стабильная работа, а не цена.",
    },
    "strategy_labels": {
        "primary": "Через надёжность",
        "alternative": "Через сроки",
        "pattern_break": "Через согласование",
    },
    "next_action": "Отправьте экспертное письмо с подтверждением надёжности узлов и одним следующим шагом.",
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
        self.assertIn("зачем клиенту ответить", prompt)
        self.assertIn("Не придумывай выгоду", prompt)
        self.assertIn("pressure_lever", prompt)
        self.assertIn("strategy_labels", prompt)
        self.assertLess(prompt.index("MANAGER_TACTICS"), prompt.index("SITUATION_CONTEXT"))
        self.assertLess(prompt.index("CURRENT_BITRIX_TASK"), prompt.index("MANAGER_QUESTION"))
        self.assertNotIn("old_quick_help_answer", prompt)
        self.assertFalse(quick_help_schema()["additionalProperties"])
        self.assertNotIn("call_scripts", quick_help_schema()["properties"])
        self.assertNotIn("crm_checklist", quick_help_schema()["properties"])
        self.assertEqual(quick_help_schema()["properties"]["lifehacks"]["maxItems"], 3)
        self.assertIn("MT-CONTACT-001", prompt)
        self.assertIn("MT-OBJECTION-001", prompt)
        self.assertIn("MT-CLOSE-001", prompt)
        self.assertIn("MT-TRUST-001", prompt)
        self.assertIn("сделай blocker конкретным", prompt)
        self.assertIn("главным препятствием", prompt)
        self.assertEqual(validate_quick_help(ANSWER), ANSWER)
        self.assertEqual(validate_quick_help(ANSWER_V2), ANSWER_V2)

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
        self.assertEqual(kwargs["prompt_cache_key"], "neuro-rop:deal-manager-quick-help:v7")
        prefixes = kwargs["cache_prefixes"]
        self.assertEqual(len(prefixes), 2)
        self.assertIn("MANAGER_TACTICS", prefixes[0])
        self.assertNotIn("SITUATION_CONTEXT", prefixes[0])
        self.assertIn("CURRENT_BITRIX_TASK", prefixes[1])
        self.assertIn("COMMUNICATION_PATTERN_CONTEXT", prefixes[1])
        self.assertNotIn("MANAGER_QUESTION", prefixes[1])
        self.assertTrue(call.call_args.args[0].startswith(prefixes[0]))
        self.assertIsNone(kwargs.get("stable_prefix"))

    def test_validation_rejects_missing_strategy_variant(self) -> None:
        invalid = {**ANSWER, "client_messages": {"primary": "Текст", "alternative": "Другой текст"}}
        with self.assertRaisesRegex(ValueError, "client_messages"):
            validate_quick_help(invalid)

    def test_validation_rejects_duplicate_strategy_variants_and_summary_list(self) -> None:
        duplicate = {**ANSWER, "client_messages": {"primary": "Одна фраза", "alternative": "Одна фраза", "pattern_break": "Одна фраза"}}
        with self.assertRaisesRegex(ValueError, "разные варианты"):
            validate_quick_help(duplicate)
        listed = {**ANSWER, "next_action": "Сначала напишите.\nПотом позвоните."}
        with self.assertRaisesRegex(ValueError, "без списка"):
            validate_quick_help(listed)

    def test_locked_move_keeps_only_the_selected_strategy(self) -> None:
        locked = project_locked_move(ANSWER, "alternative")
        self.assertEqual(locked["mode"], "reanimator")
        self.assertEqual(locked["selected_strategy"], "alternative")
        self.assertEqual(locked["selected_client_message"], ANSWER["client_messages"]["alternative"])
        self.assertEqual(locked["strategy_label"], "Через уточнение blocker")
        projected = project_quick_help_for_material(ANSWER, "alternative")
        self.assertEqual(projected["client_messages"], {"alternative": ANSWER["client_messages"]["alternative"]})
        self.assertNotIn("primary", projected["client_messages"])
        self.assertNotIn("pattern_break", projected["strategy_labels"])

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
             patch("api.deal_manager_full_script.expand_and_save_strategy_materials") as expand, \
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
        expand.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertEqual([item[1]["mode"] for item in calls], ["push", "reanimator"])
        self.assertEqual({item[1]["turn_id"] for item in calls}, {job["turn_id"]})
        name, kwargs = calls[0]
        self.assertEqual(name, "save_deal_manager_quick_help")
        self.assertEqual(kwargs["question"], "Что сказать клиенту после паузы?")
        self.assertEqual(kwargs["situation_review_id"], 21)
        self.assertEqual(kwargs["origin"], "manager")
        self.assertNotIn("raw_output_text", kwargs["model_meta"])
        communication_context = generate.call_args.kwargs["communication_pattern_context"]
        self.assertEqual(communication_context["window_days"], 30)
        self.assertEqual(communication_context["recent_events"], [])

    def test_first_mode_is_saved_before_second_mode_runs(self) -> None:
        seen = []

        def generate(**kwargs):
            job = next(iter(quick_help._QUICK_HELP_JOBS.values()))
            seen.append((kwargs["mode"], dict(job.saved_by_mode)))
            return (ANSWER, {})

        def save_call(name, db_path, **kwargs):
            if name == "save_deal_manager_quick_help":
                return {"id": 40 + len(seen) - 1, "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", side_effect=generate), \
             patch.object(quick_help, "_storage_call", side_effect=save_call), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="Что сказать клиенту после паузы?",
                confirm_paid=True,
            )
        self.assertEqual(seen[0], ("push", {}))
        self.assertEqual(seen[1][0], "reanimator")
        self.assertEqual(seen[1][1].get("push"), 40)

    def test_quick_help_does_not_auto_expand_channel_cards(self) -> None:
        def save_call(name, db_path, **kwargs):
            if name == "save_deal_manager_quick_help":
                return {"id": 31, "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", return_value=(ANSWER, {})), \
             patch.object(quick_help, "_storage_call", side_effect=save_call), \
             patch("api.deal_manager_full_script.expand_and_save_strategy_materials") as expand, \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            started = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="Что сказать клиенту после паузы?",
                confirm_paid=True,
            )
        self.assertEqual(started["status"], "done")
        self.assertEqual(started["quick_help_id"], 31)
        self.assertEqual(started["detail"], "Пакет рекомендации готов")
        expand.assert_not_called()

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
                db_path=Path("state.sqlite"), deal_id="101", question="x" * 4001, confirm_paid=True
            )
        with self.assertRaisesRegex(ValueError, "от 1 до 100"):
            quick_help.list_quick_help_history(
                db_path=Path("state.sqlite"), deal_id="101", limit=101
            )

    def test_assistant_workspace_combines_help_crm_history_and_context(self) -> None:
        entry = {
            "id": 31,
            "deal_id": "101",
            "source_report_id": 17,
            "situation_review_id": 21,
            "mode": "reanimator",
            "origin": "manager",
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
        self.assertEqual(result["disc_profile"], {
            "primary_style": "C",
            "secondary_style": None,
            "profile_confidence": "low",
        })
        self.assertEqual(result["context"]["last_communication"]["occurred_at"], "2026-08-04T16:10:00+03:00")
        self.assertEqual(result["current_by_mode"]["reanimator"]["id"], 31)
        self.assertIsNone(result["current_by_mode"]["push"])
        self.assertEqual(
            [item["kind"] for item in result["timeline"]],
            ["communication_completed", "assistant_request", "communication"],
        )

    def test_push_contract_requires_lever_labels_and_mode(self) -> None:
        prompt = build_quick_help_prompt(
            question="",
            analysis_projection=CONTEXT["analysis_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            situation_projection=CONTEXT["situation_projection"],
            communication_pattern_context=COMMUNICATION_CONTEXT,
            mode="push",
        )
        self.assertIn("режиме Дожим", prompt)
        self.assertIn("один приоритетный рычаг", prompt)
        self.assertIn("линзу только из подтверждаемых фактов", prompt)
        self.assertIn("один экспертный аргумент через выбранный рычаг", prompt)
        self.assertIn("один закрывающий шаг", prompt)
        self.assertIn("сделай blocker конкретным", prompt)
        self.assertIn("главным препятствием", prompt)
        self.assertIn("MT-OBJECTION-001", prompt)
        self.assertIn("MT-TRUST-001", prompt)
        self.assertNotIn("мягкий режим восстановления контакта", prompt)
        self.assertEqual(validate_quick_help(PUSH_ANSWER, expected_mode="push"), PUSH_ANSWER)
        with self.assertRaisesRegex(ValueError, "mode не соответствует"):
            validate_quick_help(PUSH_ANSWER, expected_mode="reanimator")
        with patch(
            "openai_api.llm.deal_manager_quick_help.call_structured_output_json",
            return_value=(PUSH_ANSWER, {}),
        ) as call:
            generate_deal_manager_quick_help(
                question="",
                analysis_projection=CONTEXT["analysis_projection"],
                deal=DEAL,
                current_bitrix_task=CONTEXT["current_bitrix_task"],
                situation_projection=CONTEXT["situation_projection"],
                communication_pattern_context=COMMUNICATION_CONTEXT,
                mode="push",
            )
        self.assertEqual(call.call_args.kwargs["prompt_cache_key"], "neuro-rop:deal-manager-push:v4")
        prefixes = call.call_args.kwargs["cache_prefixes"]
        self.assertIn("MANAGER_TACTICS", prefixes[0])
        self.assertNotIn("MANAGER_QUESTION", prefixes[1])

    def test_ensure_reuses_current_modes_and_does_not_call_llm(self) -> None:
        calls: list[str] = []

        def storage_call(name, db_path, **kwargs):
            calls.append(name)
            if name == "get_current_deal_manager_quick_help":
                mode = kwargs["mode"]
                return {"id": 40 if mode == "push" else 41, "mode": mode}
            raise AssertionError(name)

        with patch.object(quick_help, "load_manager_screen_context", return_value={**CONTEXT, "deal": DEAL}), \
             patch.object(quick_help, "_storage_call", side_effect=storage_call), \
             patch.object(quick_help, "generate_deal_manager_quick_help") as generate:
            first = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question="", confirm_paid=True,
            )
            second = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question="", confirm_paid=False,
            )

        self.assertTrue(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["saved_by_mode"], {"push": 40, "reanimator": 41})
        generate.assert_not_called()
        self.assertEqual(calls.count("get_current_deal_manager_quick_help"), 4)

    def test_chat_refinement_updates_both_modes_with_shared_turn(self) -> None:
        saved = []

        def storage_call(name, db_path, **kwargs):
            if name == "save_deal_manager_quick_help":
                saved.append(kwargs)
                return {"id": 50 + len(saved), "deal_id": "101"}
            raise AssertionError(name)

        def generate(**kwargs):
            return (PUSH_ANSWER if kwargs["mode"] == "push" else ANSWER, {})

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", side_effect=generate) as generate_mock, \
             patch.object(quick_help, "_storage_call", side_effect=storage_call), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            started = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="Этот рычаг не подходит, давай через сроки",
                mode="push",
                confirm_paid=True,
            )

        self.assertEqual(started["status"], "done")
        self.assertEqual([call.kwargs["mode"] for call in generate_mock.call_args_list], ["push", "reanimator"])
        self.assertEqual([item["mode"] for item in saved], ["push", "reanimator"])
        self.assertEqual({item["turn_id"] for item in saved}, {started["turn_id"]})
        self.assertTrue(started["turn_id"])
        self.assertEqual(saved[0]["origin"], "manager")
        self.assertEqual(saved[0]["question"], "Этот рычаг не подходит, давай через сроки")
        self.assertEqual(saved[1]["question"], "Этот рычаг не подходит, давай через сроки")

    def test_new_situation_context_allows_fresh_ensure(self) -> None:
        generated_modes: list[str] = []

        def current_for_mode(db_path, context, mode):
            if int(context["source_report_id"]) == 17:
                return {"id": 40 if mode == "push" else 41, "mode": mode}
            return None

        def storage_call(name, db_path, **kwargs):
            if name == "save_deal_manager_quick_help":
                generated_modes.append(kwargs["mode"])
                return {"id": 90 + len(generated_modes), "deal_id": "101"}
            raise AssertionError(name)

        def generate(**kwargs):
            return (PUSH_ANSWER if kwargs["mode"] == "push" else ANSWER, {})

        with patch.object(quick_help, "load_manager_screen_context", return_value={**CONTEXT, "deal": DEAL, "source_report_id": 18, "situation_id": 22}), \
             patch.object(quick_help, "_current_for_mode", side_effect=current_for_mode), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", side_effect=generate), \
             patch.object(quick_help, "_storage_call", side_effect=storage_call), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            started = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question="", confirm_paid=True,
            )

        self.assertFalse(started["reused"])
        self.assertEqual(generated_modes, ["push", "reanimator"])

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
