from __future__ import annotations

import unittest
from pathlib import Path

from openai_api.llm.analyze_deal import (
    DEAL_ID_SECTION_MARKER,
    DEAL_PROMPT_CACHE_KEY,
    build_prompt,
    render_client_communication_profile_section,
    render_cost_section,
    render_report,
)
from openai_api.llm.llm_client import prompt_prefix_before
from openai_api.llm.validation import (
    AnalysisValidationError,
    validate_client_communication_profile,
)


def supported_profile() -> dict:
    return {
        "status": "supported",
        "primary_style": "D",
        "secondary_style": "C",
        "role_separation_confidence": "high",
        "profile_confidence": "medium",
        "evidence": [
            "Клиент последовательно просит коротко фиксировать результат и срок.",
            "Клиент запрашивает точные критерии сравнения вариантов.",
        ],
        "insufficient_reason": None,
        "recommended_communication": {
            "tone": "Коротко и по-деловому.",
            "structure": "Результат, критерии, срок и один следующий шаг.",
            "emphasize": ["Проверяемый результат", "Точные критерии"],
            "avoid": ["Длинное вступление"],
        },
    }


def insufficient_profile() -> dict:
    return {
        "status": "insufficient_evidence",
        "primary_style": None,
        "secondary_style": None,
        "role_separation_confidence": "low",
        "profile_confidence": "low",
        "evidence": [],
        "insufficient_reason": "Недостаточно уверенно отделена речь клиента.",
        "recommended_communication": {
            "tone": None,
            "structure": None,
            "emphasize": [],
            "avoid": [],
        },
    }


class DealClientCommunicationProfileTests(unittest.TestCase):
    def test_supported_mixed_profile_and_insufficient_profile_are_valid(self) -> None:
        validate_client_communication_profile(supported_profile())
        validate_client_communication_profile(insufficient_profile())

    def test_supported_profile_requires_distinct_styles_and_grounding(self) -> None:
        duplicate = supported_profile()
        duplicate["secondary_style"] = "D"
        with self.assertRaisesRegex(AnalysisValidationError, "secondary_style"):
            validate_client_communication_profile(duplicate)

        weak_roles = supported_profile()
        weak_roles["role_separation_confidence"] = "low"
        with self.assertRaisesRegex(AnalysisValidationError, "role_separation_confidence"):
            validate_client_communication_profile(weak_roles)

        no_evidence = supported_profile()
        no_evidence["evidence"] = []
        with self.assertRaisesRegex(AnalysisValidationError, "at least 2"):
            validate_client_communication_profile(no_evidence)

    def test_insufficient_profile_cannot_smuggle_style_or_adaptation(self) -> None:
        invalid = insufficient_profile()
        invalid["primary_style"] = "I"
        invalid["recommended_communication"]["tone"] = "Энергично."
        with self.assertRaisesRegex(AnalysisValidationError, "styles must be null"):
            validate_client_communication_profile(invalid)

    def test_disc_contract_is_in_shared_prefix_and_deal_facts_stay_dynamic(self) -> None:
        first = build_prompt(
            "7",
            "История первой сделки",
            "Транскрипт первой сделки",
            "Диагностика первой сделки",
            [(Path("okf.md"), "Общие правила")],
            {},
        )
        second = build_prompt(
            "18615",
            "История второй сделки",
            "Транскрипт второй сделки",
            "Диагностика второй сделки",
            [(Path("okf.md"), "Общие правила")],
            {},
        )
        first_prefix = prompt_prefix_before(first, DEAL_ID_SECTION_MARKER)
        second_prefix = prompt_prefix_before(second, DEAL_ID_SECTION_MARKER)

        self.assertEqual(first_prefix, second_prefix)
        self.assertIn("client_communication_profile", first_prefix)
        self.assertIn("Сначала отдели реплики и сообщения клиента", first_prefix)
        self.assertNotIn("История первой сделки", first_prefix)
        self.assertNotIn("Транскрипт первой сделки", first_prefix)
        self.assertEqual(DEAL_PROMPT_CACHE_KEY, "neuro-rop:full-deal:v2")

    def test_markdown_renders_disc_section_before_cost(self) -> None:
        supported = render_client_communication_profile_section(supported_profile())
        self.assertIn("## Коммуникационный профиль клиента (DISC)", supported)
        self.assertIn("Основной стиль: D", supported)
        self.assertIn("Вторичный стиль: C", supported)
        self.assertIn("Коротко и по-деловому.", supported)

        insufficient = render_client_communication_profile_section(insufficient_profile())
        self.assertIn("insufficient_evidence", insufficient)
        self.assertIn("Недостаточно уверенно отделена речь клиента.", insufficient)
        self.assertIn("Основной стиль: не определено", insufficient)

        missing = render_client_communication_profile_section(None)
        self.assertIn("профиль отсутствует в анализе", missing)

        report = render_report(
            {
                "deal_id": "101",
                "client_communication_profile": supported_profile(),
            },
            metadata={"estimated_cost": {"model": "test", "estimated_cost_usd": 0.1}},
        )
        disc_pos = report.index("## Коммуникационный профиль клиента (DISC)")
        cost_pos = report.index("## Стоимость анализа")
        self.assertLess(disc_pos, cost_pos)
        self.assertIn("Основной стиль: D", report)
        self.assertIn(render_cost_section({"estimated_cost": {"model": "test"}}).splitlines()[0], report)


if __name__ == "__main__":
    unittest.main()
