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
    quick_help_static_prompt,
    validate_quick_help,
)
from openai_api.llm.llm_client import ModelJsonParseError, ModelResponseIncompleteError


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
            if name == "get_current_deal_manager_quick_help":
                return None
            if name == "save_deal_manager_quick_help":
                calls.append((name, kwargs))
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
        self.assertEqual(len(calls), 1)
        self.assertEqual([item[1]["mode"] for item in calls], ["push"])
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

    def test_only_requested_mode_is_generated(self) -> None:
        seen = []

        def generate(**kwargs):
            job = next(iter(quick_help._QUICK_HELP_JOBS.values()))
            seen.append((kwargs["mode"], dict(job.saved_by_mode)))
            return (ANSWER, {})

        def save_call(name, db_path, **kwargs):
            if name == "get_current_deal_manager_quick_help":
                return None
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
                mode="reanimator",
                confirm_paid=True,
            )
        self.assertEqual(seen, [("reanimator", {})])

    def test_quick_help_does_not_auto_expand_channel_cards(self) -> None:
        def save_call(name, db_path, **kwargs):
            if name == "get_current_deal_manager_quick_help":
                return None
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
            if name == "list_deal_context_lever_priorities":
                return []
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
        self.assertIn("deal_context", result["context"])
        self.assertEqual(result["context"]["report"]["report_id"], 17)
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
        for rule in (
            "факты можно использовать наравне с CONTEXT, если они прямо ему не противоречат",
            "Если менеджер явно задал рычаг — используй его",
            "Сохраняй текущую ближайшую micro-conversion при добавлении нового аргумента или рычага",
            "CTA должен вести к подписанию, а не к скану договора, дате аванса",
            "Переводи выбранный рычаг в конкретную ценность для клиента",
            "alternative — другая логика аргументации той же ценности",
            "pattern_break — другая уместная техника закрытия той же micro-conversion",
            "Он не обязан быть жёстче",
            "Все три ведут к одной micro-conversion и отличаются стратегией",
            "Не делай все профили одинаково жёсткими только из-за режима push",
            "Но не считай факт недостаточно подтверждённым только потому, что его явно сообщил менеджер",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)
        self.assertNotIn("Опирайся только на переданные CONTEXT.", prompt)
        self.assertNotIn("Это PUSH-режим: уверенный, экспертный, предметный, коммерческий.", prompt)
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

    def test_push_template_generation_keeps_current_manager_facts_and_goal(self) -> None:
        question = (
            "Обычный срок 75 рабочих дней. Если согласуем до 31 августа, попадём в закупку "
            "1 сентября и сможем сделать за 60 рабочих дней. Клиенту важен срок. "
            "Хочу использовать это и закрыть на подписание."
        )
        kwargs = {
            "question": question,
            "analysis_projection": CONTEXT["analysis_projection"],
            "deal": DEAL,
            "current_bitrix_task": CONTEXT["current_bitrix_task"],
            "situation_projection": CONTEXT["situation_projection"],
            "communication_pattern_context": COMMUNICATION_CONTEXT,
            "mode": "push",
        }
        prompt = build_quick_help_prompt(**kwargs)
        template = quick_help_static_prompt("push")
        self.assertTrue(prompt.startswith(template))
        self.assertIn(question, prompt.split("MANAGER_QUESTION:\n", 1)[1])
        self.assertNotIn(question, template)
        self.assertIn("Этот запрос независим: не используй и не запрашивай историю прошлых ответов", template)
        with patch(
            "openai_api.llm.deal_manager_quick_help.call_structured_output_json",
            return_value=(PUSH_ANSWER, {}),
        ) as call:
            generate_deal_manager_quick_help(**kwargs, prompt_template=template)
        self.assertEqual(call.call_args.args[0], prompt)

    def test_ensure_reuses_only_requested_mode_and_does_not_call_llm(self) -> None:
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
                db_path=Path("state.sqlite"), deal_id="101", question="", mode="reanimator", confirm_paid=False,
            )

        self.assertTrue(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["saved_by_mode"], {"push": 40})
        self.assertEqual(second["saved_by_mode"], {"reanimator": 41})
        generate.assert_not_called()
        self.assertEqual(calls.count("get_current_deal_manager_quick_help"), 2)

    def test_chat_refinement_updates_only_selected_mode(self) -> None:
        saved = []

        def storage_call(name, db_path, **kwargs):
            if name == "get_current_deal_manager_quick_help":
                return None
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
        self.assertEqual([call.kwargs["mode"] for call in generate_mock.call_args_list], ["push"])
        self.assertEqual([item["mode"] for item in saved], ["push"])
        self.assertEqual({item["turn_id"] for item in saved}, {started["turn_id"]})
        self.assertTrue(started["turn_id"])
        self.assertEqual(saved[0]["origin"], "manager")
        self.assertEqual(saved[0]["question"], "Этот рычаг не подходит, давай через сроки")

    def test_lazy_counterpart_reuses_turn_for_same_manager_question(self) -> None:
        question = "Этот рычаг не подходит, давай через сроки"
        saved = []
        current_push = None

        def current_for_mode(db_path, context, mode):
            if mode == "push":
                return current_push
            return None

        def storage_call(name, db_path, **kwargs):
            nonlocal current_push
            if name != "save_deal_manager_quick_help":
                raise AssertionError(name)
            saved.append(kwargs)
            row = {"id": 60 + len(saved), "deal_id": "101", **kwargs}
            if kwargs["mode"] == "push":
                current_push = row
            return row

        with patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_current_for_mode", side_effect=current_for_mode), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", return_value=(ANSWER, {})), \
             patch.object(quick_help, "_storage_call", side_effect=storage_call), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            push = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question=question,
                mode="push", confirm_paid=True,
            )
            reanimator = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"), deal_id="101", question=question,
                mode="reanimator", confirm_paid=True,
            )

        self.assertEqual([item["mode"] for item in saved], ["push", "reanimator"])
        self.assertEqual(push["turn_id"], reanimator["turn_id"])
        self.assertEqual({item["turn_id"] for item in saved}, {push["turn_id"]})

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
        self.assertEqual(generated_modes, ["push"])

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

    def test_public_quick_help_error_hides_internal_details(self) -> None:
        incomplete = ModelResponseIncompleteError(
            "Structured output is incomplete: max_output_tokens",
            "секретный черновик",
            {"output_tokens": 4000},
        )
        parsed = ModelJsonParseError("Invalid JSON object", "{", {})
        self.assertEqual(quick_help.public_quick_help_error(incomplete), quick_help.INCOMPLETE_QUICK_HELP_ERROR)
        self.assertEqual(quick_help.public_quick_help_error(parsed), quick_help.FORMAT_QUICK_HELP_ERROR)
        self.assertEqual(
            quick_help.public_quick_help_error(ValueError("Сначала подтвердите текущую ситуацию сделки")),
            "Сначала подтвердите текущую ситуацию сделки",
        )
        paid = quick_help.public_quick_help_error(ValueError("Подтвердите платный AI-вызов для quick help"))
        self.assertEqual(paid, quick_help.GENERIC_QUICK_HELP_ERROR)
        self.assertNotIn("платн", paid.casefold())
        hidden = quick_help.public_quick_help_error(RuntimeError("Traceback secret key"))
        self.assertEqual(hidden, quick_help.GENERIC_QUICK_HELP_ERROR)
        self.assertNotIn("Traceback", hidden)
        self.assertNotIn("RuntimeError", hidden)

    def test_failed_quick_help_job_returns_human_error(self) -> None:
        incomplete = ModelResponseIncompleteError(
            "Structured output is incomplete: max_output_tokens",
            "",
            {},
        )
        with self.assertLogs(quick_help.logger, level="ERROR"), \
             patch.object(quick_help, "load_manager_screen_context", return_value=CONTEXT), \
             patch.object(quick_help, "_current_for_mode", return_value=None), \
             patch.object(quick_help, "_load_local_communications", return_value=[]), \
             patch.object(quick_help, "generate_deal_manager_quick_help", side_effect=incomplete), \
             patch.object(quick_help.threading, "Thread", ImmediateThread):
            started = quick_help.start_quick_help_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                question="",
                confirm_paid=True,
            )
        job = quick_help.get_quick_help_job(started["job_id"])
        self.assertEqual(job["status"], "error")
        self.assertEqual(job["error"], quick_help.INCOMPLETE_QUICK_HELP_ERROR)
        self.assertNotIn("ModelResponseIncompleteError", job["error"])
        self.assertNotIn("max_output_tokens", job["error"])

    def test_public_deal_context_projects_existing_qualification_blocks(self) -> None:
        analysis = {
            **CONTEXT["analysis_projection"],
            "qualification_assessment": {
                "bant": {
                    "budget": {"status": "missing"},
                    "need": {"status": "confirmed"},
                    "overall_status": "incomplete",
                    "missing_facts": ["Бюджет"],
                    "next_question": "Какой бюджет?",
                },
                "solution_fit": {"equipment_type": "labeler", "status": "needs_technical_data"},
                "commercial_fit": {"confirmed_budget_rub": None},
            },
            "money_path_diagnosis": {
                "stuck_point": "next_step",
                "why_money_is_at_risk": "Нет даты решения",
                "current_owner_of_next_step": "client",
                "next_required_fact": "Дата решения",
                "evidence": ["КП без ответа"],
            },
            "competitor_defense_checklist": {
                "applicable": False,
                "competitor_type": "not_applicable",
                "defense_points": [],
                "questions_to_client": [],
                "risk_if_not_defended": "Конкурент не заявлен",
            },
        }
        public = quick_help._public_deal_context(
            analysis,
            CONTEXT["situation_projection"],
            DEAL,
            CONTEXT["current_bitrix_task"],
            [],
        )
        self.assertEqual(public["bant"]["overall_status"], "incomplete")
        self.assertEqual(public["solution_fit"]["equipment_type"], "labeler")
        self.assertEqual(public["money_path"]["stuck_point"], "next_step")
        self.assertEqual(public["competitor"]["applicable"], False)
        self.assertEqual(public["deal_card"]["title"], "Тестовая сделка")
        self.assertEqual(public["journey"], [])

    def test_assistant_workspace_routes_are_present(self) -> None:
        from api.app import app

        methods = {route.path: route.methods for route in app.routes}
        self.assertEqual(methods["/api/deal-control/deals/{deal_id}/assistant-workspace"], {"GET"})
        self.assertEqual(
            methods["/api/deal-control/deals/{deal_id}/context/levers/{lever_id}/priority"],
            {"PUT"},
        )
        self.assertEqual(
            methods["/api/deal-control/deals/{deal_id}/assistant/communication-completed"],
            {"POST"},
        )


if __name__ == "__main__":
    unittest.main()
