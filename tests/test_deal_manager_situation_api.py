from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_situation as situation
from openai_api.config import ANALYSIS_MODEL, ANALYSIS_REASONING_EFFORT
from openai_api.llm.deal_manager_situation import (
    MANAGER_MODEL,
    MANAGER_REASONING_EFFORT,
    build_situation_prompt,
    compact_analysis_projection,
    generate_deal_manager_situation,
    situation_schema,
)
from openai_api.llm.llm_client import call_structured_output_json
from storage import rop_db as storage


REPORT = {
    "id": 17,
    "report_json": {
        "deal_state": {"summary": "КП отправлено"},
        "deal_control_brief": {
            "current_situation": "Клиент получил КП",
            "missing_facts": ["Дата решения не подтверждена"],
        },
        "main_risk": {"description": "Нет подтверждённого следующего шага"},
        "private_extra": {"secret": "не передавать"},
    },
}
DEAL = {
    "deal_id": "101",
    "title": "Тестовая сделка",
    "stage_name": "КП",
    "manager_name": "Иванов",
    "primary_bitrix_task": {
        "activity_id": "500",
        "subject": "Позвонить клиенту",
        "deadline": "2026-08-03T15:00:00+03:00",
        "completed": False,
        "description": "Получить решение",
    },
}


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def dispatcher(*, saved=None, state=None, calls=None):
    calls = calls if calls is not None else []

    def call(name, db_path, **kwargs):
        calls.append((name, kwargs))
        if name == "get_latest_ui_report":
            return REPORT
        if name == "list_deal_control_deals":
            return [DEAL]
        if name == "get_deal_manager_situation_state":
            return state or {"status": "pending", "state": "pending", "is_current": False}
        if name == "get_latest_deal_manager_situation_review":
            return None
        if name == "save_deal_manager_situation_confirmation":
            return saved or {"id": 21, "status": "confirmed", "deal_id": "101"}
        if name == "save_deal_manager_situation_refined_projection":
            return saved or {"id": 22, "status": "refined", "deal_id": "101"}
        raise AssertionError(f"unexpected storage call: {name}")

    return call


class DealManagerSituationTests(unittest.TestCase):
    def setUp(self) -> None:
        situation._SITUATION_JOBS.clear()
        trace_patch = patch("openai_api.llm.llm_client.append_usage_trace")
        trace_patch.start()
        self.addCleanup(trace_patch.stop)

    def test_prompt_is_sectioned_and_analysis_is_allow_listed(self) -> None:
        projection = compact_analysis_projection(REPORT["report_json"])
        prompt = build_situation_prompt(
            analysis_projection=projection,
            deal=DEAL,
            current_bitrix_task=DEAL["primary_bitrix_task"],
            previous_manager_projection={"current_situation": "Старая версия"},
            manager_context="Менеджер уточнил: клиент просил вернуться в пятницу",
        )
        self.assertIn("CONFIRMED_ANALYSIS_CONTEXT", prompt)
        self.assertIn("CURRENT_BITRIX_TASK", prompt)
        self.assertIn("PREVIOUS_MANAGER_PROJECTION", prompt)
        self.assertIn("NEW_MANAGER_CONTEXT", prompt)
        self.assertIn("Менеджер уточнил", prompt)
        self.assertIn("КП отправлено", prompt)
        self.assertNotIn("private_extra", prompt)
        self.assertNotIn("не передавать", prompt)
        self.assertEqual(MANAGER_MODEL, ANALYSIS_MODEL)
        self.assertEqual(MANAGER_REASONING_EFFORT, ANALYSIS_REASONING_EFFORT)
        self.assertEqual(situation_schema()["properties"]["questions"]["maxItems"], 4)

    def test_generate_caches_confirmed_context_but_not_previous_or_new_context(self) -> None:
        result = {
            "current_situation": "Клиент изучает КП",
            "what_to_check_now": "Уточнить дату решения",
            "rop_focus": "Зафиксировать следующий шаг",
            "manager_coaching": "Задать один конкретный вопрос",
            "known": ["КП отправлено"],
            "unknowns": ["Дата решения"],
            "contact_goal": "Получить дату решения",
            "questions": ["Когда будет принято решение?"],
            "script": "Подскажите, когда будет принято решение по КП?",
            "script_variants": [],
            "crm_checklist": ["Дата решения"],
            "script_channel": "звонок",
        }
        with patch(
            "openai_api.llm.deal_manager_situation.call_structured_output_json",
            return_value=(result, {}),
        ) as call:
            generate_deal_manager_situation(
                analysis_projection=compact_analysis_projection(REPORT["report_json"]),
                deal=DEAL,
                current_bitrix_task=DEAL["primary_bitrix_task"],
                previous_manager_projection={"current_situation": "Старая версия"},
                manager_context="Новый контекст",
            )
        prefix = call.call_args.kwargs["stable_prefix"]
        self.assertIn("CURRENT_BITRIX_TASK", prefix)
        self.assertNotIn("PREVIOUS_MANAGER_PROJECTION:\n", prefix)
        self.assertEqual(call.call_args.kwargs["prompt_cache_key"], "neuro-rop:deal-manager-situation:v1")

    def test_confirm_uses_canonical_storage_and_never_calls_llm(self) -> None:
        calls = []
        with patch.object(situation, "_storage_call", side_effect=dispatcher(calls=calls)) as storage_call, \
             patch.object(situation, "generate_deal_manager_situation") as generate:
            result = situation.confirm_deal_manager_situation(
                db_path=Path("state.sqlite"),
                deal_id="101",
            )

        generate.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["situation"]["status"], "confirmed")
        self.assertEqual(result["situation"]["manager_projection"]["current_situation"], "Клиент получил КП")
        save_name, save_kwargs = calls[-1]
        self.assertEqual(save_name, "save_deal_manager_situation_confirmation")
        self.assertEqual(save_kwargs["source_report_id"], 17)
        self.assertIsNone(save_kwargs["manager_context"])
        storage_call.assert_called()

    def test_refine_passes_previous_projection_and_saves_only_complete_result(self) -> None:
        calls = []
        current = {
            "id": 21,
            "deal_id": "101",
            "status": "confirmed",
            "manager_projection": {"current_situation": "КП отправлено", "manager_action": "Уточнить срок"},
        }
        refined = {
            "current_situation": "Клиент изучает КП",
            "what_to_check_now": "Получить дату следующего контакта",
            "rop_focus": "Не оставлять КП без контрольной даты",
            "manager_coaching": "Спросить о критерии и сроке решения",
            "known": ["КП отправлено"],
            "unknowns": ["Дата решения"],
            "contact_goal": "Получить дату следующего шага",
            "questions": ["Когда вернётесь к решению?"],
            "script": "Добрый день! Подскажите, когда вернётесь к решению по КП?",
            "script_variants": [],
            "crm_checklist": ["Дата следующего шага"],
            "script_channel": "звонок",
        }

        def refine_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            if name == "get_latest_ui_report":
                return REPORT
            if name == "list_deal_control_deals":
                return [DEAL]
            if name == "get_deal_manager_situation_state":
                return current
            if name == "get_latest_deal_manager_situation_review":
                return {"refined_coaching": current["manager_projection"]}
            if name == "save_deal_manager_situation_refined_projection":
                return {"id": 22, "status": "refined", "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(situation, "_storage_call", side_effect=refine_call), \
             patch.object(storage, "get_deal_manager_situation_state", return_value=current, create=True), \
             patch.object(storage, "get_latest_deal_manager_situation_review", return_value={"refined_coaching": current["manager_projection"]}, create=True), \
             patch.object(situation, "generate_deal_manager_situation", return_value=(refined, {"model": "gpt-5.6-luna", "raw_output_text": "secret"})), \
             patch.object(situation.threading, "Thread", ImmediateThread):
            started = situation.start_situation_refine_job(
                db_path=Path("state.sqlite"),
                deal_id="101",
                context="Клиент попросил вернуться после внутреннего согласования",
                confirm_paid=True,
            )

        self.assertEqual(started["stage"], "done")
        job = situation.get_situation_job(started["job_id"])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["stage"], "done")
        save_name, save_kwargs = calls[-1]
        self.assertEqual(save_name, "save_deal_manager_situation_refined_projection")
        self.assertEqual(save_kwargs["refined_coaching"], refined)
        self.assertEqual(save_kwargs["source_report_id"], 17)
        self.assertEqual(save_kwargs["manager_context"], "Клиент попросил вернуться после внутреннего согласования")
        self.assertNotIn("raw_output_text", save_kwargs["model_meta"])

    def test_refine_requires_paid_confirmation_but_allows_pending_situation(self) -> None:
        with self.assertRaisesRegex(ValueError, "платный"):
            situation.start_situation_refine_job(
                db_path=Path("state.sqlite"), deal_id="101", context="Уточнение", confirm_paid=False
            )
        refined = {
            "current_situation": "Ситуация дополнена менеджером",
            "what_to_check_now": "Проверить дату решения",
            "rop_focus": "Зафиксировать следующий шаг",
            "manager_coaching": "Сменить повторяющийся сценарий контакта",
            "known": ["КП отправлено"],
            "unknowns": ["Дата решения"],
            "contact_goal": "Получить дату решения",
            "questions": ["Когда будет принято решение?"],
            "script": "Подскажите, когда вернётесь с решением?",
            "script_variants": [],
            "crm_checklist": ["Дата следующего контакта"],
            "script_channel": "звонок",
        }
        calls = []

        def pending_call(name, db_path, **kwargs):
            calls.append((name, kwargs))
            if name == "get_latest_ui_report":
                return REPORT
            if name == "list_deal_control_deals":
                return [DEAL]
            if name == "get_deal_manager_situation_state":
                return {"status": "pending", "state": "pending", "is_current": False}
            if name == "save_deal_manager_situation_refined_projection":
                return {"id": 23, "action": "context_added", "deal_id": "101"}
            raise AssertionError(name)

        with patch.object(situation, "_storage_call", side_effect=pending_call), \
             patch.object(situation, "generate_deal_manager_situation", return_value=(refined, {})), \
             patch.object(situation.threading, "Thread", ImmediateThread):
            started = situation.start_situation_refine_job(
                db_path=Path("state.sqlite"), deal_id="101", context="Уточнение", confirm_paid=True
            )
        self.assertEqual(started["status"], "done")
        self.assertEqual(calls[-1][0], "save_deal_manager_situation_refined_projection")

    def test_only_one_active_refine_job_per_deal(self) -> None:
        active = situation.DealManagerSituationJob(
            job_id="active", deal_id="101", context="контекст", status="running", stage="llm"
        )
        situation._SITUATION_JOBS["active"] = active
        with patch.object(situation, "_storage_call", side_effect=dispatcher()), \
             patch.object(storage, "get_deal_manager_situation_state", return_value={"id": 21, "status": "confirmed"}, create=True), \
             patch.object(storage, "get_latest_deal_manager_situation_review", return_value=None, create=True):
            result = situation.start_situation_refine_job(
                db_path=Path("state.sqlite"), deal_id="101", context="Новый контекст", confirm_paid=True
            )
        self.assertEqual(result["job_id"], "active")

    def test_structured_client_forwards_manager_reasoning_effort(self) -> None:
        response = SimpleNamespace(
            id="resp_situation",
            output_text=json.dumps(
                {
                    "current_situation": "Клиент изучает КП",
                    "what_to_check_now": "Зафиксировать дату",
                    "rop_focus": "Контроль следующего шага",
                    "manager_coaching": "Уточнить срок",
                    "known": [],
                    "unknowns": ["Дата решения"],
                    "contact_goal": "Получить дату",
                    "questions": ["Когда будет решение?"],
                    "script": "Когда будет решение по КП?",
                    "script_variants": [],
                    "crm_checklist": [],
                    "script_channel": "звонок",
                },
                ensure_ascii=False,
            ),
            usage={"input_tokens": 10, "output_tokens": 20},
        )
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response) as create:
            parsed, metadata = call_structured_output_json(
                "prompt",
                schema=situation_schema(),
                schema_name="deal_manager_situation",
                model="gpt-5.6-luna",
                reasoning_effort="xhigh",
            )
        self.assertEqual(parsed["what_to_check_now"], "Зафиксировать дату")
        self.assertEqual(metadata["reasoning_effort"], "xhigh")
        self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "xhigh"})


if __name__ == "__main__":
    unittest.main()
