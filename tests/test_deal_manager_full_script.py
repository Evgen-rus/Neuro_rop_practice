from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_full_script as full_script_api
from openai_api.llm.deal_manager_full_script import (
    build_full_script_prompt,
    full_script_schema,
    validate_full_script,
)
from storage.rop_db import (
    get_deal_manager_call_script,
    get_deal_manager_full_script,
    save_deal_manager_call_script,
    save_deal_manager_full_script,
    save_deal_manager_quick_help,
)
from tests.test_deal_manager_quick_help import ANSWER, CONTEXT, DEAL
from tests.test_deal_manager_quick_help_storage import _seed_deal


SCRIPT = {
    "script_contract": "conversation_script_v1",
    "selected_strategy": "alternative",
    "conversation_goal": "Согласовать подтверждаемую дату следующего шага по КП.",
    "blocks": [
        {
            "block_id": "opening",
            "title": "Начало разговора",
            "objective": "Проверить, удобно ли клиенту говорить.",
            "suggested_phrases": ["Добрый день! Удобно две минуты по нашему КП?"],
            "listen_for": ["Готовность говорить"],
            "transition": "Коротко обозначить цель контакта.",
            "relevant_objection_ids": [],
        },
        {
            "block_id": "blocker",
            "title": "Что мешает решению",
            "objective": "Уточнить один текущий blocker.",
            "suggested_phrases": ["Какой вопрос сейчас мешает определить следующий шаг?"],
            "listen_for": ["Конкретный вопрос или участник согласования"],
            "transition": "Предложить действие только под названный blocker.",
            "relevant_objection_ids": ["technical_doubt"],
        },
        {
            "block_id": "agreement",
            "title": "Следующий шаг",
            "objective": "Получить конкретную договорённость.",
            "suggested_phrases": ["Какой следующий шаг и дату можем сейчас зафиксировать?"],
            "listen_for": ["Действие и дата"],
            "transition": "Повторить договорённость и завершить разговор.",
            "relevant_objection_ids": [],
        },
    ],
    "closing_agreement": "Повторить согласованные действие, ответственного и дату.",
    "relevant_tactic_ids": ["MT-CONTACT-002"],
}


class DealManagerFullScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        full_script_api._FULL_SCRIPT_JOBS.clear()

    def test_schema_prompt_and_validation_do_not_create_checklist_or_objections(self) -> None:
        prompt = build_full_script_prompt(
            analysis_projection=CONTEXT["analysis_projection"],
            situation_projection=CONTEXT["situation_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            checklist={"items": [{"id": "1", "text": "Дата решения", "completed": False}]},
            communication_pattern_context={"total_attempts": 2},
            quick_help=ANSWER,
            selected_strategy="alternative",
            relevant_tactics=ANSWER["lifehacks"],
            objection_handling={"items": [{"objection_id": "technical_doubt", "objection": "Нужно проверить", "manager_reply": "Проверим", "follow_up_question": "Что критично?", "next_step_goal": "Зафиксировать", "what_not_to_do": "Не обещать"}]},
        )
        self.assertIn("CURRENT_DAILY_CHECKLIST", prompt)
        self.assertIn("ASSISTANT_MODE", prompt)
        self.assertIn("PRESSURE_LEVER", prompt)
        self.assertIn("не создавай новый checklist", prompt)
        self.assertIn("не генерируй новые ответы", prompt)
        self.assertNotIn("checklist", full_script_schema()["properties"])
        self.assertNotIn("objection_handling", full_script_schema()["properties"])
        self.assertEqual(validate_full_script(
            SCRIPT, selected_strategy="alternative", allowed_objection_ids={"technical_doubt"},
        ), SCRIPT)

        call_prompt = build_full_script_prompt(
            analysis_projection=CONTEXT["analysis_projection"],
            situation_projection=CONTEXT["situation_projection"], deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"], checklist={"items": []},
            communication_pattern_context={"total_attempts": 2}, quick_help=ANSWER,
            selected_strategy="alternative", relevant_tactics=ANSWER["lifehacks"], script_mode="call",
            objection_handling={"items": [{"objection_id": "technical_doubt"}]},
        )
        self.assertIn("SCRIPT_MODE:\n\"call\"", call_prompt)
        self.assertIn("client_communication_profile", call_prompt)
        self.assertIn("телефонного звонка", call_prompt)
        self.assertIn("technical_doubt", call_prompt)

    def test_storage_is_idempotent_and_exact_context_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            quick_help = save_deal_manager_quick_help(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, question="Как продолжить разговор?", answer_json=ANSWER,
            )
            first = save_deal_manager_full_script(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="alternative", script_json=SCRIPT,
            )
            repeated = save_deal_manager_full_script(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="alternative", script_json={**SCRIPT, "conversation_goal": "Другой текст"},
            )
            self.assertEqual(first["id"], repeated["id"])
            self.assertEqual(repeated["content"], SCRIPT)
            self.assertIsNone(get_deal_manager_full_script(
                db_path, deal_id="101", source_report_id=report_id + 1,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="alternative",
            ))
            self.assertIsNone(get_deal_manager_full_script(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="primary",
            ))
            call_script = save_deal_manager_call_script(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="alternative", script_json=SCRIPT,
            )
            self.assertNotEqual(call_script["id"], 0)
            self.assertEqual(get_deal_manager_call_script(
                db_path, deal_id="101", source_report_id=report_id,
                situation_review_id=review_id, quick_help_id=int(quick_help["id"]),
                selected_strategy="alternative",
            )["content"], SCRIPT)

    def test_cached_script_is_returned_without_paid_confirmation(self) -> None:
        inputs = {
            "context": {"deal": {"deal_id": "101"}, "source_report_id": 17},
            "quick_help": {"id": 31},
            "quick_help_content": ANSWER,
            "situation_id": 21,
        }
        with patch.object(full_script_api, "_current_inputs", return_value=inputs), patch.object(
            full_script_api, "_cached_script", return_value={"id": 44, "content": SCRIPT},
        ):
            result = full_script_api.start_full_script_job(
                db_path=Path("state.sqlite"), deal_id="101", quick_help_id=31,
                selected_strategy="alternative", confirm_paid=False,
            )
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["reused"])
        self.assertEqual(result["script_id"], 44)

    def test_uncached_job_reads_checklist_with_named_deal_id_and_saves_script(self) -> None:
        inputs = {
            "context": {
                "deal": DEAL,
                "source_report_id": 17,
                "analysis_projection": CONTEXT["analysis_projection"],
                "situation_projection": CONTEXT["situation_projection"],
                "current_bitrix_task": CONTEXT["current_bitrix_task"],
            },
            "quick_help": {"id": 31},
            "quick_help_content": ANSWER,
            "situation_id": 21,
        }
        saved_script = {**SCRIPT, "selected_strategy": "primary"}
        storage_calls = []

        def storage_call(name, db_path, **kwargs):
            storage_calls.append((name, kwargs))
            if name == "get_deal_daily_checklist_analysis_projection":
                return {"tracked": True, "items": []}
            if name == "save_deal_manager_full_script":
                return {"id": 44}
            raise AssertionError(name)

        job = full_script_api.DealManagerFullScriptJob(
            job_id="job-1", deal_id="101", quick_help_id=31, selected_strategy="primary",
        )
        full_script_api._FULL_SCRIPT_JOBS[job.job_id] = job
        with patch.object(full_script_api, "_current_inputs", return_value=inputs), \
             patch.object(full_script_api, "_cached_script", return_value=None), \
             patch.object(full_script_api, "_storage_call", side_effect=storage_call), \
             patch.object(full_script_api, "_load_local_communications", return_value=[]), \
             patch.object(full_script_api, "generate_deal_manager_full_script", return_value=(saved_script, {})):
            full_script_api._run_full_script_job(job.job_id, Path("state.sqlite"))

        result = full_script_api.get_full_script_job(job.job_id)
        self.assertEqual(result["status"], "done")
        checklist_call = next(call for call in storage_calls if call[0] == "get_deal_daily_checklist_analysis_projection")
        self.assertEqual(checklist_call[1], {"deal_id": "101"})

    def test_workspace_exposes_existing_disc_labels_without_calculation(self) -> None:
        inputs = {
            "context": {
                "deal": DEAL,
                "source_report_id": 17,
                "analysis_projection": {"client_communication_profile": {
                    "status": "supported", "primary_style": "D", "secondary_style": "C",
                    "profile_confidence": "high",
                }},
            },
            "quick_help": {"id": 31},
            "quick_help_content": ANSWER,
            "situation_id": 21,
        }
        with patch.object(full_script_api, "_current_inputs", return_value=inputs), \
             patch.object(full_script_api, "_cached_script", return_value=None), \
             patch.object(full_script_api, "_storage_call", return_value={"items": []}):
            workspace = full_script_api.get_full_script_workspace(
                db_path=Path("state.sqlite"), deal_id="101", quick_help_id=31,
                selected_strategy="primary", script_mode="call",
            )
        self.assertEqual(workspace["disc_profile"], {
            "primary_style": "D", "secondary_style": "C", "profile_confidence": "high",
        })
        self.assertEqual(workspace["script_mode"], "call")

    def test_public_objections_are_allowlisted(self) -> None:
        projection = {
            "objection_handling": {
                "applicable": True,
                "summary": "Служебное резюме",
                "likely_objections": [{
                    "objection_type": "price", "probability": "high", "evidence": "internal",
                    "client_phrase": "Это дорого", "manager_reply": "Сверим состав предложения.",
                    "follow_up_question": "С чем сравниваете?", "next_step_goal": "Согласовать сравнение.",
                    "what_not_to_do": "Не давать скидку без основания.",
                }],
            },
        }
        result = full_script_api._public_objections(projection)
        self.assertEqual(result["items"][0]["objection"], "Это дорого")
        self.assertNotIn("objection_type", result["items"][0])
        self.assertNotIn("evidence", result["items"][0])
        self.assertEqual(result["items"][0]["objection_id"], "price")


if __name__ == "__main__":
    unittest.main()
