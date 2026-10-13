import copy
import unittest

from test_lead_qualification_assessment import lead_analysis
from api.jobs import extract_lead_qualification_summary
from openai_api.llm.analyze_lead import build_prompt, render_report, validate_lead_analysis_for_crm_state
from openai_api.llm.lead_autobant import AUTOBANT_SCHEMA
from openai_api.llm.lead_requirements import TECHNICAL_ITEMS, requirements_contract
from openai_api.llm.validation import AnalysisValidationError, normalize_analysis_for_validation, validate_lead_analysis


def autobant_analysis():
    analysis = lead_analysis()
    assessment = analysis["qualification_assessment"]
    autobant = copy.deepcopy(AUTOBANT_SCHEMA)
    evidence = ["06.10.2026, разговор: клиент подтвердил параметры и получение материалов."]
    autobant["products"] = [{"id": "lab_round", "evidence": evidence}]
    for key, value in (("timeline", "le60"), ("authority", "final"), ("need", "concrete"),
                       ("production", "launched"), ("tz", "yes")):
        autobant[key].update(value=value, evidence=evidence)
    assessment["autobant"] = autobant
    assessment.update(requirements_contract())
    technical = assessment["technical_requirements"]
    technical.update(required_now=True, readiness="ready", reason="Перед расчётом требуется полное ТЗ.")
    technical["equipment"] = [{"type": "labeler", "items": [
        {"key": key, "label": label, "status": "collected", "summary": "Параметр подтверждён в разговоре.",
         "evidence": evidence, "next_question": None} for key, label in TECHNICAL_ITEMS["labeler"].items()
    ]}]
    assessment["stage_review"].update(
        current_stage_id="BANT", current_stage_name="Идентификация BANT", verdict="confirmed_correct",
        reason="Квалификация соответствует текущей стадии.", evidence=evidence,
    )
    assessment["commercial_fit"]["new_equipment_minimum_rub"] = 720_000
    return analysis


class LeadRequirementsTests(unittest.TestCase):
    def test_new_a_uses_product_price_below_legacy_million_and_server_result(self):
        analysis = autobant_analysis()
        fit = analysis["qualification_assessment"]["commercial_fit"]
        fit["confirmed_budget_rub"] = 750_000
        analysis["qualification_assessment"]["autobant_result"] = {"category": "E"}
        validate_lead_analysis(analysis)
        result = analysis["qualification_assessment"]["autobant_result"]
        self.assertEqual(result["category"], "A")
        self.assertEqual(result["reference_price_rub"], 800_000)
        summary = extract_lead_qualification_summary(analysis)
        self.assertEqual((summary["technical_collected_count"], summary["technical_total_count"]), (8, 8))
        self.assertFalse(summary["requires_attention"])

    def test_full_tz_cannot_hide_missing_or_duplicate_items(self):
        for mutation in ("partial", "duplicate", "no_evidence", "false_ready"):
            with self.subTest(mutation=mutation):
                analysis = autobant_analysis()
                technical = analysis["qualification_assessment"]["technical_requirements"]
                items = technical["equipment"][0]["items"]
                if mutation == "partial":
                    items[0].update(status="partial", next_question="Какие размеры этикетки?")
                    technical["readiness"] = "incomplete"
                elif mutation == "duplicate":
                    items[1] = copy.deepcopy(items[0])
                elif mutation == "no_evidence":
                    items[0]["evidence"] = []
                else:
                    items[0].update(status="missing", evidence=[], next_question="Пришлите фото.")
                with self.assertRaises(AnalysisValidationError):
                    validate_lead_analysis(analysis)

    def test_missing_tz_is_informational_early_but_attention_before_calculation(self):
        analysis = autobant_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["autobant"]["tz"]["value"] = "partial"
        assessment["lead_category"]["value"] = "B"
        analysis["lead_state"]["qualification"] = "B"
        technical = assessment["technical_requirements"]
        technical["readiness"] = "incomplete"
        technical["equipment"][0]["items"][1].update(
            status="missing", summary="Фото только обещано, передача не подтверждена.", evidence=[],
            next_question="Когда сможете передать фото и размеры?",
        )
        for required in (False, True):
            technical["required_now"] = required
            validate_lead_analysis(analysis)
            summary = extract_lead_qualification_summary(analysis)
            self.assertEqual(summary["requires_attention"], required)
            self.assertEqual(summary["technical_collected_count"], 7)

    def test_stage_review_bound_to_crm_and_evidence(self):
        analysis = autobant_analysis()
        crm = {"status_id": analysis["closure_review"]["crm_status_id"],
               "status_name": analysis["closure_review"]["crm_status_name"],
               "status_semantic_id": analysis["closure_review"]["crm_status_semantic_id"], "is_closed_lost": False}
        review = analysis["qualification_assessment"]["stage_review"]
        review.update(current_stage_id=crm["status_id"], current_stage_name=crm["status_name"])
        validate_lead_analysis_for_crm_state(analysis, crm, require_autobant=True)
        review["current_stage_id"] = "OTHER"
        with self.assertRaisesRegex(AnalysisValidationError, "stage_review"):
            validate_lead_analysis_for_crm_state(analysis, crm, require_autobant=True)
        review["evidence"] = []
        with self.assertRaisesRegex(AnalysisValidationError, "supporting evidence"):
            validate_lead_analysis(analysis)

    def test_budget_bands_use_new_category_grounds_and_c_return(self):
        for budget, category in ((390_000, "E"), (400_000, "C"), (560_000, "B")):
            with self.subTest(budget=budget):
                analysis = autobant_analysis()
                assessment = analysis["qualification_assessment"]
                assessment["commercial_fit"].update(
                    confirmed_budget_rub=budget, new_equipment_budget_status="below_minimum",
                    reason_code="budget_below_new_equipment_minimum",
                )
                reasons = ["autobant_budget_far_below_reference"] if category == "E" else []
                assessment["lead_category"].update(value=category, reason_codes=reasons)
                analysis["lead_state"]["qualification"] = category
                if category == "C":
                    assessment["lead_route"].update(
                        current_route="auto_reminder", controlled_return_required=True,
                        controlled_return_status="missing_in_crm", recommended_return_date="2026-12-01",
                        status="violation",
                    )
                    analysis["loss_diagnosis"]["route_quality"] = "violation"
                validate_lead_analysis(analysis)
                if category == "C":
                    assessment["lead_route"]["controlled_return_required"] = False
                    with self.assertRaisesRegex(AnalysisValidationError, "controlled return"):
                        validate_lead_analysis(analysis)

    def test_missing_contact_does_not_prove_completed_cycle(self):
        analysis = autobant_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["autobant"]["screening"] = [{"code": "e2", "evidence": ["Клиент не ответил."]}]
        assessment["lead_category"].update(value="E", reason_codes=["autobant_e2"])
        analysis["lead_state"]["qualification"] = "E"
        analysis["call_attempt_recommendation"]["cycle_status"] = "in_progress"
        with self.assertRaisesRegex(AnalysisValidationError, "completed call cycle"):
            validate_lead_analysis(analysis)

    def test_malformed_checklist_is_a_validation_error_not_a_crash(self):
        for value in (None, [], {"required_now": True, "reason": "Расчёт", "equipment": [None]}):
            with self.subTest(value=value):
                analysis = autobant_analysis()
                analysis["qualification_assessment"]["technical_requirements"] = value
                with self.assertRaises(AnalysisValidationError):
                    validate_lead_analysis(analysis)

    def test_invalid_model_result_and_extreme_budget_cannot_bypass_validation(self):
        analysis = autobant_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["autobant"]["version"] = "other"
        assessment["autobant_result"] = {"minimum_budget_rub": []}
        with self.assertRaises(AnalysisValidationError):
            validate_lead_analysis(analysis)
        analysis = autobant_analysis()
        analysis["qualification_assessment"]["commercial_fit"]["confirmed_budget_rub"] = 10 ** 500
        with self.assertRaisesRegex(AnalysisValidationError, "finite non-negative"):
            validate_lead_analysis(analysis)

    def test_legacy_reports_are_readable_but_new_generation_requires_regulation(self):
        legacy = lead_analysis()
        validate_lead_analysis(legacy)
        self.assertNotIn("regulation_version", extract_lead_qualification_summary(legacy))
        with self.assertRaisesRegex(AnalysisValidationError, "requires.*autobant"):
            validate_lead_analysis_for_crm_state(legacy, {}, require_autobant=True)

    def test_unknown_reference_is_not_normalized_to_legacy_budget_threshold(self):
        analysis = autobant_analysis()
        assessment = analysis["qualification_assessment"]
        assessment["autobant"]["products"] = []
        assessment["commercial_fit"].update(new_equipment_minimum_rub=None, new_equipment_budget_status="unknown")
        normalize_analysis_for_validation(analysis)
        self.assertIsNone(assessment["commercial_fit"]["new_equipment_minimum_rub"])

    def test_prompt_and_markdown_include_tz_and_stage_without_changing_control_scope(self):
        prompt = build_prompt("42", "История", "Разговор", "Ограничения", [])
        self.assertIn("обещание", prompt)
        self.assertIn('"technical_requirements"', prompt)
        self.assertIn("перед предметным расчётом", prompt)
        analysis = autobant_analysis()
        validate_lead_analysis(analysis)
        markdown = render_report(analysis, {})
        self.assertIn("## АвтоBANT 3.0", markdown)
        self.assertIn("## Полнота ТЗ", markdown)
        self.assertIn("Полная / пустая тара при обкатке", markdown)
        self.assertIn("## Соответствие стадии", markdown)


if __name__ == "__main__":
    unittest.main()
