from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_companion as companion_api
from openai_api.config import COMPANION_MAX_OUTPUT_TOKENS
from openai_api.llm.deal_manager_companion import (
    build_companion_prompt,
    generate_deal_manager_companion,
    validate_companion,
)
from storage.rop_db import get_deal_manager_companion, save_deal_manager_companion
from tests.test_deal_manager_quick_help import CONTEXT, DEAL, ImmediateThread
from tests.test_deal_manager_quick_help_storage import _seed_deal


LAST_CONTACT = {
    "event_id": "crm_activity:77",
    "channel": "call",
    "direction": "outgoing",
    "occurred_at": "2026-08-19T14:38:00+03:00",
    "duration_seconds": 504,
    "subject": "Звонок клиенту",
    "contact_class": "confirmed_contact",
    "content_available": True,
    "content_kind": "transcript",
    "content_excerpt": "Клиент: КП у директора, вернёмся 25 августа.",
}

EMAIL_WITHOUT_BODY = {
    **LAST_CONTACT,
    "channel": "email",
    "content_available": False,
    "content_kind": None,
    "content_excerpt": None,
    "subject": "Re: КП",
}

COMPANION = {
    "companion_contract": "companion_message_v1",
    "understood": ["Директор получил КП", "Решение пока не принято", "Следующий контакт 25.08"],
    "message_text": "Илья, спасибо за разговор.\nКП у директора, пока ожидаем рассмотрения.\nСледующий шаг: вторник, 25 августа.",
    "insufficient_reason": None,
}

SKIP_JOB = {
    "status": "done",
    "entity_progress": {"deal:101": {"entity_id": "101", "stage": "skipped"}},
}

FULL_JOB = {
    "status": "done",
    "entity_progress": {"deal:101": {"entity_id": "101", "stage": "analyzed"}},
}


class CompanionContractTests(unittest.TestCase):
    def test_prompt_is_post_call_message_not_followups(self) -> None:
        prompt = build_companion_prompt(
            analysis_projection=CONTEXT["analysis_projection"],
            situation_projection=CONTEXT["situation_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            last_contact=companion_api._prompt_last_contact(LAST_CONTACT),
        )
        self.assertIn("состоявшегося разговора", prompt)
        self.assertIn("Нельзя придумывать факты", prompt)
        self.assertIn("LAST_CONTACT", prompt)
        self.assertNotIn("3–5 действительно разных вариантов", prompt)
        self.assertNotIn("MANAGER_NOTE", prompt)
        self.assertEqual(validate_companion(COMPANION)["message_text"], COMPANION["message_text"])

    def test_rewrite_note_is_in_prompt_and_does_not_invent_facts(self) -> None:
        prompt = build_companion_prompt(
            analysis_projection=CONTEXT["analysis_projection"],
            situation_projection=CONTEXT["situation_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            last_contact=companion_api._prompt_last_contact(LAST_CONTACT),
            previous_message=COMPANION["message_text"],
            manager_note="Не пиши про вторник. Клиент сам наберёт.",
        )
        self.assertIn("MANAGER_NOTE", prompt)
        self.assertIn("Не пиши про вторник", prompt)
        self.assertIn("PREVIOUS_MESSAGE", prompt)
        self.assertIn("перепиши PREVIOUS_MESSAGE", prompt)

    def test_insufficient_clears_message(self) -> None:
        result = validate_companion({
            **COMPANION,
            "message_text": "нельзя использовать",
            "insufficient_reason": "Нет подтверждённого итога разговора",
        })
        self.assertEqual(result["message_text"], "")
        self.assertEqual(result["insufficient_reason"], "Нет подтверждённого итога разговора")

    def test_uses_configured_output_token_limit(self) -> None:
        with patch(
            "openai_api.llm.deal_manager_companion.call_structured_output_json",
            return_value=(COMPANION, {}),
        ) as call:
            generate_deal_manager_companion(
                analysis_projection=CONTEXT["analysis_projection"],
                situation_projection=CONTEXT["situation_projection"],
                deal=DEAL,
                current_bitrix_task=CONTEXT["current_bitrix_task"],
                last_contact=LAST_CONTACT,
            )
        self.assertEqual(call.call_args.kwargs["max_output_tokens"], COMPANION_MAX_OUTPUT_TOKENS)
        self.assertEqual(call.call_args.kwargs["call_type"], "deal_manager_companion")


class CompanionJobTests(unittest.TestCase):
    def setUp(self) -> None:
        companion_api._COMPANION_JOBS.clear()

    def tearDown(self) -> None:
        companion_api._COMPANION_JOBS.clear()

    def test_paid_confirm_required_even_without_local_contact(self) -> None:
        with patch.object(companion_api, "find_last_contact", return_value=None), \
             patch.object(companion_api, "start_analyze_job") as analyze, \
             patch.object(companion_api, "generate_deal_manager_companion") as generate:
            with self.assertRaisesRegex(ValueError, "платный"):
                companion_api.start_companion_job(
                    db_path=Path("state.sqlite"), deal_id="101", confirm_paid=False,
                )
        analyze.assert_not_called()
        generate.assert_not_called()

    def test_missing_contact_after_bitrix_refresh_skips_companion_llm(self) -> None:
        with patch.object(companion_api, "find_last_contact", return_value=None), \
             patch.object(companion_api, "busy_analyze_entity_ids", return_value=set()), \
             patch.object(companion_api, "start_analyze_job", return_value={"job_id": "job-1"}) as start, \
             patch.object(companion_api, "wait_for_job", return_value=SKIP_JOB), \
             patch.object(companion_api, "generate_deal_manager_companion") as generate, \
             patch.object(companion_api.threading, "Thread", ImmediateThread):
            result = companion_api.start_companion_job(
                db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True,
            )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["missing_reason"], "Нет данных")
        self.assertTrue(result["analysis_started"])
        self.assertFalse(start.call_args.args[0].force_llm)
        generate.assert_not_called()

    def test_analyze_job_runs_before_looking_for_contact(self) -> None:
        order: list[str] = []

        def fake_start(options):
            order.append("analyze")
            return {"job_id": "job-1"}

        def fake_find(deal_id, **kwargs):
            order.append("contact")
            return LAST_CONTACT

        with patch.object(companion_api, "find_last_contact", side_effect=fake_find), \
             patch.object(companion_api, "busy_analyze_entity_ids", return_value=set()), \
             patch.object(companion_api, "start_analyze_job", side_effect=fake_start), \
             patch.object(companion_api, "wait_for_job", return_value=SKIP_JOB), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=None), \
             patch.object(companion_api, "generate_deal_manager_companion", return_value=(COMPANION, {"model": "test"})), \
             patch.object(companion_api.threading, "Thread", ImmediateThread), \
             patch.object(companion_api, "_storage_call", return_value={"id": 7}):
            companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        self.assertEqual(order[:2], ["analyze", "contact"])

    def test_workspace_get_does_not_start_job(self) -> None:
        with patch.object(companion_api, "find_last_contact", return_value=LAST_CONTACT), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=None), \
             patch.object(companion_api, "start_analyze_job") as analyze:
            payload = companion_api.get_companion_workspace(db_path=Path("state.sqlite"), deal_id="101")
        self.assertEqual(payload["last_contact"]["event_id"], "crm_activity:77")
        self.assertIsNone(payload["companion"])
        analyze.assert_not_called()

    def test_skip_still_builds_text_without_forcing_full_llm(self) -> None:
        generate = patch.object(
            companion_api, "generate_deal_manager_companion", return_value=(COMPANION, {"model": "test", "raw_output_text": "secret"}),
        ).start()
        self.addCleanup(patch.stopall)
        with patch.object(companion_api, "find_last_contact", return_value=LAST_CONTACT), \
             patch.object(companion_api, "busy_analyze_entity_ids", return_value=set()), \
             patch.object(companion_api, "start_analyze_job", return_value={"job_id": "job-1"}) as start, \
             patch.object(companion_api, "wait_for_job", return_value=SKIP_JOB), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=None), \
             patch.object(companion_api.threading, "Thread", ImmediateThread), \
             patch.object(companion_api, "_storage_call", return_value={"id": 44}) as storage:
            with self.assertRaisesRegex(ValueError, "платный"):
                companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=False)
            result = companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["analysis_decision"], "skip")
        self.assertTrue(result["analysis_started"])
        generate.assert_called_once()
        options = start.call_args.args[0]
        self.assertFalse(options.force_llm)
        self.assertTrue(options.transcribe_audio)
        self.assertTrue(options.analyze)
        saved = storage.call_args.kwargs
        self.assertEqual(saved["companion_json"], COMPANION)
        self.assertNotIn("raw_output_text", saved["model_meta"])

    def test_full_decision_still_goes_through_change_aware_job(self) -> None:
        generate = patch.object(
            companion_api, "generate_deal_manager_companion", return_value=(COMPANION, {"model": "test"}),
        ).start()
        self.addCleanup(patch.stopall)
        with patch.object(companion_api, "find_last_contact", return_value=LAST_CONTACT), \
             patch.object(companion_api, "busy_analyze_entity_ids", return_value=set()), \
             patch.object(companion_api, "start_analyze_job", return_value={"job_id": "job-full"}) as start, \
             patch.object(companion_api, "wait_for_job", return_value=FULL_JOB), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=None), \
             patch.object(companion_api.threading, "Thread", ImmediateThread), \
             patch.object(companion_api, "_storage_call", return_value={"id": 51}):
            result = companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        self.assertEqual(result["analysis_decision"], "full")
        self.assertFalse(start.call_args.args[0].force_llm)
        generate.assert_called_once()

    def test_same_event_and_report_reuses_cache_without_second_companion_llm(self) -> None:
        generate = patch.object(
            companion_api, "generate_deal_manager_companion", return_value=(COMPANION, {"model": "test"}),
        ).start()
        self.addCleanup(patch.stopall)
        cached = {"id": 9, "content": COMPANION}
        with patch.object(companion_api, "find_last_contact", return_value=LAST_CONTACT), \
             patch.object(companion_api, "_run_analyze", return_value=(SKIP_JOB, True)), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=cached), \
             patch.object(companion_api.threading, "Thread", ImmediateThread):
            result = companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        self.assertTrue(result["reused"])
        self.assertEqual(result["companion_id"], 9)
        generate.assert_not_called()

    def test_missing_body_is_not_invented_in_prompt(self) -> None:
        captured: dict[str, object] = {}

        def fake_generate(**kwargs):
            captured["last_contact"] = kwargs["last_contact"]
            return (
                {
                    "companion_contract": "companion_message_v1",
                    "understood": [],
                    "message_text": "",
                    "insufficient_reason": "Нет текста письма",
                },
                {"model": "test"},
            )

        with patch.object(companion_api, "find_last_contact", return_value=EMAIL_WITHOUT_BODY), \
             patch.object(companion_api, "_run_analyze", return_value=(SKIP_JOB, True)), \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value=None), \
             patch.object(companion_api.threading, "Thread", ImmediateThread), \
             patch.object(companion_api, "generate_deal_manager_companion", side_effect=fake_generate), \
             patch.object(companion_api, "_storage_call", return_value={"id": 12}):
            result = companion_api.start_companion_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        last_contact = captured["last_contact"]
        self.assertFalse(last_contact["content_available"])
        self.assertNotIn("content_excerpt", last_contact)
        self.assertNotIn("text", last_contact)
        self.assertNotIn("transcript", last_contact)
        self.assertEqual(result["missing_reason"], "Нет данных")

    def test_manager_note_rewrites_without_starting_analyze_job(self) -> None:
        captured: dict[str, object] = {}

        def fake_generate(**kwargs):
            captured["note"] = kwargs["manager_note"]
            captured["previous"] = kwargs["previous_message"]
            return (COMPANION, {"model": "test"})

        with patch.object(companion_api, "find_last_contact", return_value=LAST_CONTACT), \
             patch.object(companion_api, "start_analyze_job") as analyze, \
             patch.object(companion_api, "_load_context", return_value=CONTEXT), \
             patch.object(companion_api, "_cached", return_value={"id": 9, "content": COMPANION}), \
             patch.object(companion_api.threading, "Thread", ImmediateThread), \
             patch.object(companion_api, "generate_deal_manager_companion", side_effect=fake_generate), \
             patch.object(companion_api, "_storage_call", return_value={"id": 10}):
            result = companion_api.start_companion_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                confirm_paid=True,
                regenerate=True,
                manager_note="Не пиши про вторник. Клиент сам наберёт.",
            )
        self.assertEqual(result["status"], "done")
        self.assertFalse(result["analysis_started"])
        analyze.assert_not_called()
        self.assertEqual(captured["note"], "Не пиши про вторник. Клиент сам наберёт.")
        self.assertIn("Илья, спасибо за разговор", str(captured["previous"]))

    def test_storage_is_keyed_by_event_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, _review_id = _seed_deal(db_path)
            first = save_deal_manager_companion(
                db_path, deal_id="101", source_report_id=report_id, last_event_id="crm_activity:77",
                companion_json=COMPANION,
            )
            repeated = save_deal_manager_companion(
                db_path, deal_id="101", source_report_id=report_id, last_event_id="crm_activity:77",
                companion_json={**COMPANION, "message_text": "Другой текст.\nСледующий шаг тот же."},
            )
            self.assertEqual(first["id"], repeated["id"])
            self.assertIn("Другой текст", repeated["content"]["message_text"])
            self.assertIsNone(get_deal_manager_companion(
                db_path, deal_id="101", source_report_id=report_id, last_event_id="crm_activity:99",
            ))


if __name__ == "__main__":
    unittest.main()
