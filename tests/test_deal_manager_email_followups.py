from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_followups as followups_api
from openai_api.llm.deal_manager_email import build_email_prompt, validate_email
from openai_api.llm.deal_manager_followups import build_followups_prompt, validate_followups
from storage.rop_db import (
    get_deal_manager_email_script,
    get_deal_manager_followups,
    save_deal_manager_email_script,
    save_deal_manager_followups,
    save_deal_manager_quick_help,
)
from tests.test_deal_manager_quick_help import ANSWER, COMMUNICATION_CONTEXT, CONTEXT, DEAL, ImmediateThread
from tests.test_deal_manager_quick_help_storage import _seed_deal


EMAIL = {
    "email_contract": "manager_email_v1", "selected_strategy": "primary",
    "subject": "Следующий шаг по согласованию", "greeting": "Добрый день!",
    "context": "Возвращаюсь к вопросу согласования следующего шага.",
    "questions": ["Какой вопрос сейчас требует уточнения?", "Кто участвует в согласовании?"],
    "value_point": "Это поможет подготовить точный ответ без лишних итераций.",
    "call_to_action": "Подскажите, пожалуйста, какой вариант удобнее обсудить.",
    "closing": "Буду на связи.",
}

FOLLOWUPS = {
    "followups_contract": "followup_plan_v1",
    "context_summary": "Клиент получил КП, но дата решения не подтверждена.",
    "items": [
        {
            "item_id": f"idea_{index}", "concern_or_scenario": title,
            "basis_status": basis, "evidence_summary": evidence,
            "followup_type": kind, "idea": idea,
            "why_it_may_help": "Снижает неопределённость и даёт содержательный повод ответить.",
            "suggested_channel": "Email", "timing": "До следующего согласованного контакта.",
            "target_micro_conversion": "Получить один конкретный вопрос или дату следующего шага.",
            "caution": "Сначала проверить, что материал доступен; не выдавать гипотезу за страх клиента.",
        }
        for index, (title, basis, evidence, kind, idea) in enumerate([
            ("Нет даты решения", "confirmed", "В ситуации явно отсутствует дата решения.", "checklist", "Предложить идею чек-листа критериев согласования."),
            ("Возможное сравнение подрядчиков", "inferred", "Этап допускает сравнение, но прямого подтверждения нет.", "article", "Предложить идею статьи о критериях выбора подрядчика."),
            ("Если клиент молчит", "generic", "Это условный сценарий, а не установленный факт.", "useful_tip", "Предложить короткую полезную памятку по следующему шагу."),
        ], start=1)
    ],
}


class DealManagerEmailFollowupsTests(unittest.TestCase):
    def setUp(self) -> None:
        followups_api._FOLLOWUPS_JOBS.clear()

    def test_email_contract_and_prompt_use_disc_without_making_it_evidence(self) -> None:
        prompt = build_email_prompt(
            analysis_projection=CONTEXT["analysis_projection"], situation_projection=CONTEXT["situation_projection"],
            deal=DEAL, current_bitrix_task=CONTEXT["current_bitrix_task"],
            communication_pattern_context=COMMUNICATION_CONTEXT, quick_help=ANSWER, selected_strategy="primary",
        )
        self.assertIn("client_communication_profile", prompt)
        self.assertIn("ASSISTANT_MODE", prompt)
        self.assertIn("PRESSURE_LEVER", prompt)
        self.assertIn("LOCKED_MOVE", prompt)
        self.assertIn("тот же человек", prompt)
        self.assertIn(ANSWER["client_messages"]["primary"], prompt)
        self.assertNotIn(ANSWER["client_messages"]["pattern_break"], prompt)
        self.assertIn("не выводи из профиля факты или страхи", prompt)
        self.assertEqual(validate_email(EMAIL, selected_strategy="primary"), EMAIL)
        with self.assertRaisesRegex(ValueError, "1 до 4"):
            validate_email({**EMAIL, "questions": ["1", "2", "3", "4", "5"]}, selected_strategy="primary")

    def test_followups_contract_separates_confirmed_inferred_and_generic(self) -> None:
        prompt = build_followups_prompt(
            analysis_projection=CONTEXT["analysis_projection"], situation_projection=CONTEXT["situation_projection"],
            deal=DEAL, current_bitrix_task=CONTEXT["current_bitrix_task"], communication_pattern_context=COMMUNICATION_CONTEXT,
        )
        self.assertIn("confirmed", prompt)
        self.assertIn("DISC не доказывает", prompt)
        self.assertEqual(validate_followups(FOLLOWUPS), FOLLOWUPS)
        with self.assertRaisesRegex(ValueError, "от 3 до 5"):
            validate_followups({**FOLLOWUPS, "items": FOLLOWUPS["items"][:2]})

    def test_storage_is_exact_context_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            quick_help = save_deal_manager_quick_help(
                db_path, deal_id="101", source_report_id=report_id, situation_review_id=review_id,
                question="Что написать?", answer_json=ANSWER,
            )
            email = save_deal_manager_email_script(
                db_path, deal_id="101", source_report_id=report_id, situation_review_id=review_id,
                quick_help_id=int(quick_help["id"]), selected_strategy="primary", script_json=EMAIL,
            )
            repeated = save_deal_manager_email_script(
                db_path, deal_id="101", source_report_id=report_id, situation_review_id=review_id,
                quick_help_id=int(quick_help["id"]), selected_strategy="primary", script_json={**EMAIL, "subject": "Другое"},
            )
            self.assertEqual(email["id"], repeated["id"])
            self.assertIsNone(get_deal_manager_email_script(
                db_path, deal_id="101", source_report_id=report_id + 1, situation_review_id=review_id,
                quick_help_id=int(quick_help["id"]), selected_strategy="primary",
            ))
            saved_followups = save_deal_manager_followups(
                db_path, deal_id="101", source_report_id=report_id, situation_review_id=review_id, followups_json=FOLLOWUPS,
            )
            self.assertEqual(get_deal_manager_followups(
                db_path, deal_id="101", source_report_id=report_id, situation_review_id=review_id,
            )["id"], saved_followups["id"])

    def test_followups_job_requires_paid_confirmation_and_saves_safe_result(self) -> None:
        with patch.object(followups_api, "_inputs", return_value={"context": CONTEXT, "situation_id": 21}), \
             patch.object(followups_api, "_cached", return_value=None), \
             patch.object(followups_api.threading, "Thread", ImmediateThread), \
             patch.object(followups_api, "generate_deal_manager_followups", return_value=(FOLLOWUPS, {"model": "test", "raw_output_text": "secret"})), \
             patch.object(followups_api, "_load_local_communications", return_value=[]), \
             patch.object(followups_api, "_storage_call", return_value={"id": 44}) as storage:
            with self.assertRaisesRegex(ValueError, "платный"):
                followups_api.start_followups_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=False)
            result = followups_api.start_followups_job(db_path=Path("state.sqlite"), deal_id="101", confirm_paid=True)
        self.assertEqual(result["status"], "done")
        saved = storage.call_args.kwargs
        self.assertEqual(saved["followups_json"], FOLLOWUPS)
        self.assertNotIn("raw_output_text", saved["model_meta"])


if __name__ == "__main__":
    unittest.main()
