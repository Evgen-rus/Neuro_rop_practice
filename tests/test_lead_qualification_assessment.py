from __future__ import annotations

import copy
import unittest
from pathlib import Path

from openai_api.llm.analyze_lead import build_prompt, render_report
from openai_api.llm.validation import (
    AnalysisValidationError,
    normalize_analysis_for_validation,
    validate_lead_analysis,
)


def qualification_assessment() -> dict:
    return {
        "bant": {
            "budget": {"status": "confirmed", "evidence": ["Клиент подтвердил бюджет 1,2 млн ₽."]},
            "authority": {"status": "confirmed", "evidence": ["В разговоре участвует директор."]},
            "need": {"status": "confirmed", "evidence": ["Нужен этикетировщик для новой линии."]},
            "timeframe": {"status": "confirmed", "evidence": ["Запуск запланирован в августе."]},
            "overall_status": "confirmed",
            "missing_facts": [],
            "next_question": None,
        },
        "solution_fit": {
            "equipment_type": "labeler",
            "status": "compatible",
            "reason_code": None,
            "evidence": ["Круглая тара 0,5 л соответствует ограничениям OKF."],
            "missing_facts": [],
        },
        "commercial_fit": {
            "new_equipment_budget_status": "sufficient",
            "confirmed_budget_rub": 1_200_000,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": None,
            "evidence": ["Клиент назвал бюджет 1,2 млн ₽ на новое оборудование."],
        },
    }


def lead_analysis() -> dict:
    return {
        "lead_id": "42",
        "lead_state": {
            "summary": "Есть подтверждённый проект.",
            "client": "ООО Тест",
            "need": "Этикетировщик",
            "status": "IN_PROCESS",
            "qualification": "A",
            "qualification_reason": "BANT и применимость подтверждены.",
        },
        "qualification_assessment": qualification_assessment(),
        "activity_summary": {"meaningful_contact": True, "summary": "Проведён предметный разговор."},
        "rop_manager_message_block": {
            "check_for_rop": "Проверить подготовку расчёта.",
            "why_it_matters": "Клиент готов перейти к КП.",
            "message_to_manager": "Подготовьте расчёт и зафиксируйте срок ответа клиента.",
            "expected_crm_update": "В CRM зафиксирован срок ответа по КП.",
            "deadline": "2026-07-15",
            "success_condition": "КП и дата ответа внесены в CRM.",
            "evidence": ["Клиент запросил расчёт."],
        },
        "main_risk": {"risk_level": "medium", "risk_type": "follow_up", "description": "Нужен расчёт."},
        "loss_diagnosis": {
            "lead_quality": "good",
            "processing_quality": "good",
            "source_signal": "good_source",
            "call_attempt_quality": "enough",
            "next_step_quality": "clear",
            "final_verdict": "ready_for_deal",
            "evidence": ["Клиент подтвердил интерес к расчёту."],
        },
        "manager_quality": {"what_done_well": [], "missed_points": [], "critical_mistake": None},
        "call_attempt_recommendation": {
            "applicable": False,
            "contact_status": "meaningful_contact",
            "attempts_found": "Есть содержательный контакт.",
            "recommendation_fit": "not_applicable",
            "recommendation_gap": "Нет данных для оценки.",
            "next_call_plan": [],
            "rop_control": "Контролировать расчёт.",
        },
        "manager_action_block": {
            "recommended_channel": "email",
            "channel_reason": "Нужно направить расчёт.",
            "goal": "Согласовать следующий шаг.",
            "primary_text": {"type": "email", "subject": "Расчёт", "text": "Направляем расчёт."},
            "backup_texts": [],
            "manager_checklist": [],
        },
        "rop_action": {"required": True, "text": "Проверить срок ответа."},
        "memory_update": {
            "change_summary": "Подтверждён проект.",
            "facts_confirmed_add": [],
            "open_questions_update": [],
            "next_actions_update": [],
            "risks_update": [],
        },
    }


class LeadQualificationAssessmentTests(unittest.TestCase):
    def test_full_lead_prompt_requires_independent_assessments(self) -> None:
        prompt = build_prompt(
            "42",
            "История",
            "Транскрибация",
            "Диагностика",
            [(Path("qualification.md"), "Правила")],
        )

        self.assertIn("Сначала заполни qualification_assessment", prompt)
        self.assertIn("budget_below_new_equipment_minimum", prompt)
        self.assertIn("не предполагай бюджет", prompt)

    def test_confirmed_bant_compatible_solution_and_sufficient_budget_is_valid(self) -> None:
        analysis = lead_analysis()

        validate_lead_analysis(analysis)

        self.assertEqual(analysis["lead_state"]["qualification"], "A")
        self.assertEqual(analysis["loss_diagnosis"]["final_verdict"], "ready_for_deal")

    def test_incomplete_bant_keeps_data_gap_and_one_question(self) -> None:
        analysis = lead_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["bant"]["authority"] = {"status": "missing", "evidence": []}
        assessment["bant"]["overall_status"] = "incomplete"
        assessment["bant"]["missing_facts"] = ["Кто принимает окончательное решение."]
        assessment["bant"]["next_question"] = "Кто принимает окончательное решение по закупке?"
        assessment["solution_fit"] = {
            "equipment_type": "labeler",
            "status": "needs_technical_data",
            "reason_code": None,
            "evidence": ["Клиент запросил этикетировщик без параметров тары."],
            "missing_facts": ["Размеры и форма тары."],
        }
        assessment["commercial_fit"] = {
            "new_equipment_budget_status": "unknown",
            "confirmed_budget_rub": None,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "unknown",
            "evidence": [],
        }
        analysis["lead_state"]["qualification"] = "B"
        analysis["loss_diagnosis"]["final_verdict"] = "data_gap"

        validate_lead_analysis(analysis)

        self.assertEqual(assessment["bant"]["next_question"], "Кто принимает окончательное решение по закупке?")

    def test_postponed_need_uses_nurture_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["lead_state"]["qualification"] = "C"
        analysis["loss_diagnosis"]["final_verdict"] = "needs_nurture"

        validate_lead_analysis(analysis)

    def test_confirmed_technical_stop_factor_uses_technical_mismatch(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["solution_fit"] = {
            "equipment_type": "labeler",
            "status": "not_compatible",
            "reason_code": "technical_mismatch",
            "evidence": ["Клиенту нужна круглая тара 15 л."],
            "missing_facts": [],
        }
        analysis["lead_state"]["qualification"] = "D"
        analysis["loss_diagnosis"]["final_verdict"] = "technical_mismatch"

        validate_lead_analysis(analysis)

    def test_confirmed_budget_below_minimum_uses_budget_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["commercial_fit"] = {
            "new_equipment_budget_status": "below_minimum",
            "confirmed_budget_rub": 600_000,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "budget_below_new_equipment_minimum",
            "evidence": ["Клиент назвал бюджет 600 тыс. ₽ на новое оборудование."],
        }
        analysis["lead_state"]["qualification"] = "D"
        analysis["loss_diagnosis"]["final_verdict"] = "budget_below_new_equipment_minimum"

        validate_lead_analysis(analysis)

    def test_unnamed_budget_does_not_receive_budget_d_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["commercial_fit"] = {
            "new_equipment_budget_status": "unknown",
            "confirmed_budget_rub": None,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "unknown",
            "evidence": [],
        }
        analysis["lead_state"]["qualification"] = "B"
        analysis["loss_diagnosis"]["final_verdict"] = "data_gap"

        validate_lead_analysis(analysis)

        self.assertNotEqual(
            analysis["loss_diagnosis"]["final_verdict"], "budget_below_new_equipment_minimum"
        )

    def test_d_requires_one_matching_machine_reason(self) -> None:
        analysis = lead_analysis()
        analysis["lead_state"]["qualification"] = "D"
        analysis["loss_diagnosis"]["final_verdict"] = "technical_mismatch"

        with self.assertRaises(AnalysisValidationError):
            validate_lead_analysis(analysis)

    def test_old_json_without_assessment_renders_safely(self) -> None:
        analysis = lead_analysis()
        del analysis["qualification_assessment"]

        report = render_report(analysis)

        self.assertIn("## Квалификация и применимость\n\nНет данных", report)

    def test_legacy_normalization_is_explicit_and_new_contract_stays_required(self) -> None:
        analysis = lead_analysis()
        del analysis["qualification_assessment"]

        with self.assertRaises(AnalysisValidationError):
            validate_lead_analysis(copy.deepcopy(analysis))

        changes = normalize_analysis_for_validation(analysis, allow_legacy_qualification_assessment=True)
        validate_lead_analysis(analysis)

        self.assertIn({"path": "qualification_assessment", "action": "added_legacy_fallback"}, changes)

    def test_unknown_reason_code_is_not_normalized(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["solution_fit"]["reason_code"] = "unsupported_code"

        with self.assertRaises(AnalysisValidationError):
            validate_lead_analysis(analysis)


if __name__ == "__main__":
    unittest.main()
