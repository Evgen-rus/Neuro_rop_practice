from __future__ import annotations

import unittest

from api.jobs import extract_summary_fields


class JobSummaryFieldsTests(unittest.TestCase):
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
