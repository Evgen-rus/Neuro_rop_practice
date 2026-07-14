from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from api.jobs import _converted_lead_handoffs
from openai_api.llm.analyze_deal import build_prompt


class DealQualificationAndHandoffTests(unittest.TestCase):
    def test_deal_prompt_requires_qualification_assessment(self) -> None:
        prompt = build_prompt(
            "18683",
            "История сделки",
            "Транскрибация",
            "Диагностика",
            [(Path("qualification.md"), "Правила")],
            {"is_closed_lost": False},
        )

        self.assertIn("qualification_assessment", prompt)
        self.assertIn("budget_below_new_equipment_minimum", prompt)
        self.assertIn("Не предполага", prompt)

    def test_converted_lead_handoff_uses_local_related_deal(self) -> None:
        with patch(
            "run_rop_assistant.converted_lead_deals",
            return_value={"229607": {"id": "18683"}},
        ):
            handoffs = _converted_lead_handoffs(["229607"])

        self.assertEqual(handoffs, {"229607": "18683"})


if __name__ == "__main__":
    unittest.main()
