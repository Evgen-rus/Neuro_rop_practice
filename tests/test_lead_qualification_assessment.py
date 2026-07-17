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
from api.jobs import extract_summary_fields


def qualification_assessment() -> dict:
    def bant_item(label: str, evidence: str, *, purchase_window: str | None = None) -> dict:
        item = {
            "label": label,
            "status": "confirmed",
            "summary": evidence,
            "evidence": [evidence],
            "missing_facts": [],
            "next_question_or_action": None,
        }
        if purchase_window is not None:
            item["purchase_window"] = purchase_window
        return item

    return {
        "bant": {
            "budget": bant_item("Бюджет и финансовая готовность", "Клиент подтвердил бюджет 1,2 млн ₽."),
            "authority": bant_item("ЛПР и влияние на решение", "В разговоре участвует директор."),
            "need": bant_item("Актуальная потребность", "Нужен этикетировщик для новой линии."),
            "timeframe": bant_item(
                "Срок покупки или запуска",
                "Покупка запланирована в течение 60 дней.",
                purchase_window="up_to_60_days",
            ),
            "overall_status": "confirmed",
            "missing_facts": [],
            "next_question": None,
        },
        "solution_fit": {
            "equipment_type": "labeler",
            "status": "compatible",
            "technical_data_status": "sufficient",
            "reason_code": None,
            "evidence": ["Круглая тара 0,5 л соответствует ограничениям OKF."],
            "missing_facts": [],
            "next_question_or_action": None,
        },
        "commercial_fit": {
            "new_equipment_budget_status": "sufficient",
            "budget_named": True,
            "applies_to_new_equipment": True,
            "confirmed_budget_rub": 1_200_000,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": None,
            "evidence": ["Клиент назвал бюджет 1,2 млн ₽ на новое оборудование."],
            "missing_facts": [],
            "next_question_or_action": None,
        },
        "lead_category": {
            "value": "A",
            "reason": "Полный BANT, применимость и бюджет подтверждены.",
            "reason_codes": [],
            "bant_factors": ["Все четыре критерия BANT подтверждены."],
            "technical_factors": ["Решение технически применимо."],
            "budget_factors": ["Бюджет нового оборудования составляет 1,2 млн ₽."],
            "missing_facts": [],
            "next_step": "Перевести лид в обычную сделку и подготовить расчёт.",
        },
        "lead_route": {
            "current_route": "ordinary_deal",
            "recommended_route": "ordinary_deal",
            "status": "allowed",
            "reason": "Полный BANT подтверждён.",
            "controlled_return_required": False,
            "controlled_return_date": None,
            "evidence": ["Все четыре критерия BANT подтверждены."],
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
            "route_quality": "correct",
            "final_verdict": "ready_for_deal",
            "evidence": ["Клиент подтвердил интерес к расчёту."],
        },
        "manager_quality": {"what_done_well": [], "missed_points": [], "critical_mistake": None},
        "call_attempt_recommendation": {
            "applicable": False,
            "contact_status": "meaningful_contact",
            "cycle_status": "not_applicable",
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
    @staticmethod
    def make_bant_incomplete(analysis: dict, name: str = "authority") -> None:
        assessment = analysis["qualification_assessment"]
        item = assessment["bant"][name]
        item.update(
            {
                "status": "not_confirmed",
                "summary": "Критерий не подтверждён имеющимися данными.",
                "evidence": [],
                "missing_facts": ["Нужно подтвердить критерий."],
                "next_question_or_action": "Подтвердите этот критерий, пожалуйста.",
            }
        )
        assessment["bant"]["overall_status"] = "incomplete"
        assessment["bant"]["missing_facts"] = ["Нужно подтвердить критерий."]
        assessment["bant"]["next_question"] = "Подтвердите этот критерий, пожалуйста."

    @staticmethod
    def set_category(analysis: dict, value: str, *, reason_codes: list[str] | None = None) -> None:
        analysis["lead_state"]["qualification"] = value
        analysis["qualification_assessment"]["lead_category"].update(
            {
                "value": value,
                "reason": f"Основание категории {value} подтверждено.",
                "reason_codes": reason_codes or [],
            }
        )

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
        analysis["loss_diagnosis"]["call_attempt_quality"] = "not_applicable"

        validate_lead_analysis(analysis)

        self.assertEqual(analysis["lead_state"]["qualification"], "A")
        self.assertEqual(analysis["loss_diagnosis"]["final_verdict"], "ready_for_deal")

    def test_incomplete_bant_keeps_data_gap_and_one_question(self) -> None:
        analysis = lead_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["bant"]["authority"] = {
            "label": "ЛПР и влияние на решение",
            "status": "not_confirmed",
            "summary": "ЛПР в коммуникациях не установлен.",
            "evidence": [],
            "missing_facts": ["Кто принимает окончательное решение."],
            "next_question_or_action": "Кто принимает окончательное решение по закупке?",
        }
        assessment["bant"]["overall_status"] = "incomplete"
        assessment["bant"]["missing_facts"] = ["Кто принимает окончательное решение."]
        assessment["bant"]["next_question"] = "Кто принимает окончательное решение по закупке?"
        assessment["solution_fit"] = {
            "equipment_type": "labeler",
            "status": "needs_technical_data",
            "technical_data_status": "insufficient",
            "reason_code": None,
            "evidence": ["Клиент запросил этикетировщик без параметров тары."],
            "missing_facts": ["Размеры и форма тары."],
            "next_question_or_action": "Какие размеры и форма тары?",
        }
        assessment["commercial_fit"] = {
            "new_equipment_budget_status": "unknown",
            "budget_named": False,
            "applies_to_new_equipment": "unknown",
            "confirmed_budget_rub": None,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "unknown",
            "evidence": [],
            "missing_facts": ["Бюджет нового оборудования."],
            "next_question_or_action": "Какой бюджет предусмотрен на новое оборудование?",
        }
        analysis["lead_state"]["qualification"] = "B"
        assessment["lead_category"] = {
            "value": "B",
            "reason": "Проект реален, но ЛПР и технические параметры не подтверждены.",
            "reason_codes": [],
            "bant_factors": ["Потребность и срок подтверждены, ЛПР не установлен."],
            "technical_factors": ["Не хватает параметров тары."],
            "budget_factors": ["Бюджет нового оборудования не назван."],
            "missing_facts": ["ЛПР", "Параметры тары", "Бюджет"],
            "next_step": "Уточнить ЛПР, параметры тары и бюджет одним контактом.",
        }
        assessment["lead_route"] = {
            "current_route": "clarification",
            "recommended_route": "clarification",
            "status": "allowed",
            "reason": "Для ОП2 не подтверждено больше одного критерия.",
            "controlled_return_required": False,
            "controlled_return_date": None,
            "evidence": ["ЛПР и бюджет не подтверждены."],
        }
        analysis["loss_diagnosis"]["route_quality"] = "correct"
        analysis["loss_diagnosis"]["final_verdict"] = "data_gap"

        validate_lead_analysis(analysis)

        self.assertEqual(assessment["bant"]["next_question"], "Кто принимает окончательное решение по закупке?")

    def test_postponed_need_uses_nurture_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["lead_state"]["qualification"] = "C"
        assessment = analysis["qualification_assessment"]
        assessment["bant"]["timeframe"]["purchase_window"] = "months_3_to_12"
        assessment["lead_category"].update(
            {
                "value": "C",
                "reason": "Потребность отложена на срок от 3 до 12 месяцев.",
                "next_step": "Поставить датированную задачу возврата.",
            }
        )
        assessment["lead_route"].update(
            {
                "current_route": "auto_reminder",
                "recommended_route": "auto_reminder",
                "controlled_return_required": True,
                "controlled_return_date": "2026-10-15",
            }
        )
        analysis["loss_diagnosis"]["final_verdict"] = "needs_nurture"

        validate_lead_analysis(analysis)

    def test_confirmed_technical_stop_factor_uses_technical_mismatch(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["solution_fit"] = {
            "equipment_type": "labeler",
            "status": "not_compatible",
            "technical_data_status": "sufficient",
            "reason_code": "technical_mismatch",
            "evidence": ["Клиенту нужна круглая тара 15 л."],
            "missing_facts": [],
            "next_question_or_action": None,
        }
        analysis["lead_state"]["qualification"] = "D"
        analysis["qualification_assessment"]["lead_category"].update(
            {"value": "D", "reason_codes": ["technical_mismatch"], "reason": "Подтверждена несовместимость."}
        )
        analysis["qualification_assessment"]["lead_route"].update(
            {"current_route": "disqualified", "recommended_route": "disqualified"}
        )
        analysis["loss_diagnosis"]["final_verdict"] = "technical_mismatch"

        validate_lead_analysis(analysis)

    def test_confirmed_budget_below_minimum_uses_budget_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["commercial_fit"] = {
            "new_equipment_budget_status": "below_minimum",
            "budget_named": True,
            "applies_to_new_equipment": True,
            "confirmed_budget_rub": 600_000,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "budget_below_new_equipment_minimum",
            "evidence": ["Клиент назвал бюджет 600 тыс. ₽ на новое оборудование."],
            "missing_facts": [],
            "next_question_or_action": None,
        }
        analysis["lead_state"]["qualification"] = "D"
        analysis["qualification_assessment"]["lead_category"].update(
            {
                "value": "D",
                "reason_codes": ["budget_below_new_equipment_minimum"],
                "reason": "Явно назван бюджет нового оборудования ниже минимума.",
            }
        )
        analysis["qualification_assessment"]["lead_route"].update(
            {"current_route": "disqualified", "recommended_route": "disqualified"}
        )
        analysis["loss_diagnosis"]["final_verdict"] = "budget_below_new_equipment_minimum"

        validate_lead_analysis(analysis)

    def test_missing_technical_data_is_b_not_d(self) -> None:
        analysis = lead_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["solution_fit"].update(
            {
                "status": "needs_technical_data",
                "technical_data_status": "insufficient",
                "reason_code": None,
                "missing_facts": ["Размер и форма тары."],
                "next_question_or_action": "Какие размер и форма тары?",
            }
        )
        self.set_category(analysis, "B")
        analysis["loss_diagnosis"]["final_verdict"] = "data_gap"

        validate_lead_analysis(analysis)

        self.assertEqual(assessment["lead_category"]["value"], "B")

    def test_timeframe_over_twelve_months_is_d(self) -> None:
        analysis = lead_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["bant"]["timeframe"]["purchase_window"] = "over_12_months"
        self.set_category(analysis, "D", reason_codes=["timeframe_over_12_months"])
        assessment["lead_route"].update(
            {"current_route": "disqualified", "recommended_route": "disqualified", "status": "allowed"}
        )
        analysis["loss_diagnosis"]["final_verdict"] = "timeframe_over_12_months"

        validate_lead_analysis(analysis)

    def test_unfinished_no_contact_cycle_is_unknown_with_next_question(self) -> None:
        analysis = lead_analysis()
        for name in ("budget", "authority", "need", "timeframe"):
            self.make_bant_incomplete(analysis, name)
            analysis["qualification_assessment"]["bant"][name]["status"] = "unknown"
        analysis["qualification_assessment"]["bant"]["timeframe"]["purchase_window"] = "unknown"
        self.set_category(analysis, "unknown")
        category = analysis["qualification_assessment"]["lead_category"]
        category["missing_facts"] = ["Нет содержательного контакта."]
        category["next_step"] = "Продолжить существующий цикл дозвона и задать вопрос о потребности."
        analysis["qualification_assessment"]["lead_route"].update(
            {
                "current_route": "clarification",
                "recommended_route": "clarification",
                "status": "needs_clarification",
            }
        )
        analysis["activity_summary"]["meaningful_contact"] = False
        analysis["call_attempt_recommendation"]["cycle_status"] = "in_progress"
        analysis["loss_diagnosis"].update(
            {"call_attempt_quality": "not_enough", "route_quality": "needs_clarification", "final_verdict": "data_gap"}
        )

        validate_lead_analysis(analysis)

    def test_completed_no_contact_cycle_is_e(self) -> None:
        analysis = lead_analysis()
        self.set_category(analysis, "E", reason_codes=["call_cycle_completed_no_contact"])
        assessment = analysis["qualification_assessment"]
        assessment["lead_route"].update(
            {
                "current_route": "disqualified",
                "recommended_route": "disqualified",
                "status": "allowed",
                "reason": "Цикл дозвона по действующей рекомендации завершён.",
                "evidence": ["Зафиксированы обе волны попыток и альтернативный канал."],
            }
        )
        analysis["activity_summary"]["meaningful_contact"] = False
        analysis["call_attempt_recommendation"]["cycle_status"] = "completed"
        analysis["loss_diagnosis"]["final_verdict"] = "no_contact_after_full_cycle"

        validate_lead_analysis(analysis)

    def test_incomplete_no_contact_cycle_cannot_be_e(self) -> None:
        analysis = lead_analysis()
        self.set_category(analysis, "E", reason_codes=["call_cycle_completed_no_contact"])
        analysis["qualification_assessment"]["lead_route"].update(
            {
                "current_route": "disqualified",
                "recommended_route": "disqualified",
                "status": "allowed",
                "evidence": ["В истории есть только часть рекомендованных попыток."],
            }
        )
        analysis["activity_summary"]["meaningful_contact"] = False
        analysis["call_attempt_recommendation"]["cycle_status"] = "in_progress"
        analysis["loss_diagnosis"]["final_verdict"] = "no_contact_after_full_cycle"

        with self.assertRaises(AnalysisValidationError):
            validate_lead_analysis(analysis)

    def test_spam_and_invalid_contact_are_e(self) -> None:
        for reason in ("spam", "invalid_contact"):
            with self.subTest(reason=reason):
                analysis = lead_analysis()
                self.set_category(analysis, "E", reason_codes=[reason])
                analysis["qualification_assessment"]["lead_route"].update(
                    {
                        "current_route": "disqualified",
                        "recommended_route": "disqualified",
                        "status": "allowed",
                        "evidence": [f"Подтверждено основание: {reason}."],
                    }
                )
                analysis["loss_diagnosis"]["final_verdict"] = "bad_lead"

                validate_lead_analysis(analysis)

    def test_leasing_recommendation_does_not_change_category(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["lead_category"]["next_step"] = (
            "При необходимости обсудить лизинг после подтверждённой категории A."
        )

        validate_lead_analysis(analysis)

        self.assertEqual(analysis["lead_state"]["qualification"], "A")

    def test_ordinary_deal_with_incomplete_bant_is_marked_violation(self) -> None:
        analysis = lead_analysis()
        self.make_bant_incomplete(analysis)
        self.set_category(analysis, "B")
        analysis["qualification_assessment"]["lead_route"].update(
            {
                "current_route": "ordinary_deal",
                "recommended_route": "op2",
                "status": "violation",
                "reason": "Обычная сделка требует полного BANT.",
            }
        )
        analysis["loss_diagnosis"].update({"route_quality": "violation", "final_verdict": "data_gap"})

        validate_lead_analysis(analysis)

    def test_op2_allows_exactly_one_unconfirmed_bant_criterion(self) -> None:
        analysis = lead_analysis()
        self.make_bant_incomplete(analysis)
        self.set_category(analysis, "B")
        analysis["qualification_assessment"]["lead_route"].update(
            {
                "current_route": "op2",
                "recommended_route": "op2",
                "status": "allowed",
                "reason": "В ОП2 допустим один неподтверждённый критерий BANT.",
            }
        )
        analysis["loss_diagnosis"]["final_verdict"] = "data_gap"

        validate_lead_analysis(analysis)

    def test_unnamed_budget_does_not_receive_budget_d_verdict(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["commercial_fit"] = {
            "new_equipment_budget_status": "unknown",
            "budget_named": False,
            "applies_to_new_equipment": "unknown",
            "confirmed_budget_rub": None,
            "new_equipment_minimum_rub": 1_000_000,
            "reason_code": "unknown",
            "evidence": [],
            "missing_facts": ["Бюджет нового оборудования."],
            "next_question_or_action": "Какой бюджет предусмотрен на новое оборудование?",
        }
        analysis["lead_state"]["qualification"] = "B"
        analysis["qualification_assessment"]["bant"]["budget"].update(
            {
                "status": "unknown",
                "summary": "Бюджет не назван.",
                "evidence": [],
                "missing_facts": ["Бюджет нового оборудования."],
                "next_question_or_action": "Какой бюджет предусмотрен?",
            }
        )
        analysis["qualification_assessment"]["bant"]["overall_status"] = "incomplete"
        analysis["qualification_assessment"]["bant"]["missing_facts"] = ["Бюджет нового оборудования."]
        analysis["qualification_assessment"]["bant"]["next_question"] = "Какой бюджет предусмотрен?"
        analysis["qualification_assessment"]["lead_category"].update(
            {"value": "B", "reason": "Проект реален, но бюджет не подтверждён."}
        )
        analysis["qualification_assessment"]["lead_route"].update(
            {"current_route": "op2", "recommended_route": "op2"}
        )
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

    def test_old_json_without_new_fields_keeps_api_summary_and_frontend_fallback(self) -> None:
        analysis = lead_analysis()
        del analysis["qualification_assessment"]["lead_category"]
        del analysis["qualification_assessment"]["lead_route"]

        summary = extract_summary_fields(analysis, "lead")
        frontend_source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        self.assertEqual(summary["lead_category"], "A")
        self.assertIsNone(summary["lead_route_status"])
        self.assertIn("asString(leadCategory.value) || asString(leadState.qualification)", frontend_source)

    def test_new_lead_category_ui_is_not_rendered_for_deals(self) -> None:
        frontend_source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        self.assertIn("isLead && hasQualificationAssessment", frontend_source)
        self.assertIn("!isLead && hasQualificationAssessment", frontend_source)

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

    def test_non_rejection_category_reason_codes_are_cleared_before_validation(self) -> None:
        analysis = lead_analysis()
        analysis["qualification_assessment"]["lead_category"]["reason_codes"] = [
            "incomplete_bant",
            "insufficient_technical_data",
        ]

        changes = normalize_analysis_for_validation(analysis)
        validate_lead_analysis(analysis)

        self.assertEqual(analysis["qualification_assessment"]["lead_category"]["reason_codes"], [])
        self.assertIn(
            {
                "path": "qualification_assessment.lead_category.reason_codes",
                "action": "cleared_non_rejection_reason_codes",
                "category": "A",
                "removed_items": 2,
            },
            changes,
        )

    def test_lead_prompt_requires_empty_reason_codes_for_non_rejection_categories(self) -> None:
        prompt = build_prompt("1", "История", "Транскрипция", "Диагностика", [])

        self.assertIn("Для категорий A, B, C и unknown поле lead_category.reason_codes всегда должно быть пустым", prompt)


if __name__ == "__main__":
    unittest.main()
