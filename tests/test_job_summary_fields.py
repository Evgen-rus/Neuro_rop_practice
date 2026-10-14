from __future__ import annotations

import unittest

from api.jobs import extract_summary_fields


class JobSummaryFieldsTests(unittest.TestCase):
    def test_lead_recommendation_uses_manager_assignment_but_deal_keeps_review(self) -> None:
        analysis = {"rop_manager_message_block": {
            "check_for_rop": "Проверить запись звонка.",
            "message_to_manager": "Уточни срок покупки и зафиксируй ответ клиента в CRM.",
        }}
        self.assertEqual(extract_summary_fields(analysis, "lead")["recommended_action"],
                         analysis["rop_manager_message_block"]["message_to_manager"])
        self.assertEqual(extract_summary_fields(analysis, "deal")["recommended_action"],
                         analysis["rop_manager_message_block"]["check_for_rop"])

    def test_legacy_lead_without_assignment_keeps_review_fallback(self) -> None:
        summary = extract_summary_fields({"rop_manager_message_block": {"check_for_rop": "Уточнить срок."}}, "lead")
        self.assertEqual(summary["recommended_action"], "Уточнить срок.")

    def test_deal_priority_is_not_used_as_risk_fallback(self) -> None:
        summary = extract_summary_fields(
            {
                "deal_state": {"summary": "Сделка активна"},
                "priority_recommendation": {"priority": "high"},
            },
            "deal",
        )

        self.assertIsNone(summary["risk_level"])

    def test_deal_risk_comes_from_main_risk(self) -> None:
        summary = extract_summary_fields(
            {
                "main_risk": {
                    "risk_level": "medium_high",
                    "description": "Не подтверждён следующий шаг",
                },
                "priority_recommendation": {"priority": "low"},
            },
            "deal",
        )

        self.assertEqual(summary["risk_level"], "medium_high")


if __name__ == "__main__":
    unittest.main()
