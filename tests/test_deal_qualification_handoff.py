from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from api.jobs import _converted_lead_handoffs
from openai_api.llm.analyze_deal import (
    DEAL_ID_SECTION_MARKER,
    HISTORY_SECTION_MARKER,
    build_prompt,
    deal_prompt_cache_markers,
    render_report,
    transcript_text_for_prompt,
)
from openai_api.llm.llm_client import prompt_prefix_before


class DealQualificationAndHandoffTests(unittest.TestCase):
    def test_deal_prompt_puts_global_okf_before_deal_specific_context(self) -> None:
        first = build_prompt("7", "history", "transcript", "diagnostics", [(Path("okf.md"), "rules")], {})
        second = build_prompt("18615", "other history", "other transcript", "other diagnostics", [(Path("okf.md"), "rules")], {})

        first_global = prompt_prefix_before(first, DEAL_ID_SECTION_MARKER)
        second_global = prompt_prefix_before(second, DEAL_ID_SECTION_MARKER)
        self.assertEqual(first_global, second_global)
        self.assertIn("### OKF FILE: okf.md", first_global)
        self.assertNotIn("history", first_global)

    def test_appended_call_reuses_previous_all_calls_boundary(self) -> None:
        old_aggregate = "header\n## Тексты звонков\n\n### Звонок 1: activity_id=1\n\ncall one\n"
        new_aggregate = old_aggregate + "\n### Звонок 2: activity_id=2\n\ncall two\n"
        aggregate_path = Path("deal_7_all_calls_transcript.md")
        old_text = transcript_text_for_prompt(aggregate_path, old_aggregate)
        new_text = transcript_text_for_prompt(aggregate_path, new_aggregate)
        old_prompt = build_prompt("7", "old history", old_text, "diagnostics", [(Path("okf.md"), "rules")], {})
        new_prompt = build_prompt("7", "new history", new_text, "diagnostics", [(Path("okf.md"), "rules")], {})

        old_all_calls_prefix = prompt_prefix_before(old_prompt, HISTORY_SECTION_MARKER)
        newest_call_marker = deal_prompt_cache_markers(new_text)[1]
        new_prior_calls_prefix = prompt_prefix_before(new_prompt, newest_call_marker)
        self.assertEqual(old_all_calls_prefix, new_prior_calls_prefix)
        self.assertEqual(len(deal_prompt_cache_markers(new_text)), 3)

    def test_dynamic_daily_checklist_is_after_cache_boundaries(self) -> None:
        first = build_prompt(
            "7",
            "same history",
            "same transcript",
            "same diagnostics",
            [(Path("okf.md"), "rules")],
            {},
            None,
            {"business_date": "2026-08-11", "revision": 1, "items": []},
        )
        second = build_prompt(
            "7",
            "same history",
            "same transcript",
            "same diagnostics",
            [(Path("okf.md"), "rules")],
            {},
            None,
            {"business_date": "2026-08-11", "revision": 9, "items": [{"id": "1"}]},
        )

        self.assertEqual(
            prompt_prefix_before(first, HISTORY_SECTION_MARKER),
            prompt_prefix_before(second, HISTORY_SECTION_MARKER),
        )
        self.assertEqual(deal_prompt_cache_markers("same transcript"), [DEAL_ID_SECTION_MARKER, HISTORY_SECTION_MARKER])
        self.assertIn("## CURRENT_DAILY_MANAGER_CHECKLIST", first)
        self.assertNotEqual(first, second)

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
        self.assertIn("decision_timing", prompt)
        self.assertIn("need_or_launch_timing", prompt)
        self.assertIn("deal_control_brief", prompt)
        self.assertIn("contact_questions", prompt)
        self.assertIn("call_script", prompt)
        self.assertIn("call_opening_variants", prompt)
        self.assertIn('"deal_context"', prompt)
        self.assertIn('"pressure_levers"', prompt)
        self.assertIn("описательную живую карту сделки", prompt)
        self.assertIn("Красавчик", prompt)
        self.assertIn("без канцелярита и мата", prompt)
        self.assertIn("История стадий Bitrix подтверждает движение карточки", prompt)
        self.assertIn("сообщения в чатах задач — внутренний рабочий контекст", prompt)
        self.assertIn("ближайшего незавершённого шага к деньгам", prompt)

    def test_deal_context_is_rendered_into_full_markdown(self) -> None:
        markdown = render_report({
            "deal_id": "18827",
            "deal_state": {"client": "Клиент", "amount": "240000", "stage": "КП", "summary": "КП отправлено"},
            "deal_context": {
                "current_truth": {
                    "client_profile": "Директор участвует в решении",
                    "current_need": "Оборудование для производства",
                    "desired_outcome": "Запуск к 1 сентября",
                    "current_status": "КП на рассмотрении",
                    "current_task": "Получить решение",
                    "next_checkpoint": "2026-08-17",
                    "next_step_owner": "client",
                },
                "critical_facts": [{
                    "fact_id": "launch_deadline", "category": "deadline", "fact": "С 1 сентября оборудование должно работать",
                    "status": "needs_confirmation", "importance": "high", "observed_at": None,
                    "source_type": "manager_comment", "evidence": ["Комментарий CRM"],
                }],
                "turning_points": [],
                "pain_points": [],
                "pressure_levers": [{
                    "lever_id": "launch_deadline", "type": "deadline", "title": "Срок запуска",
                    "fact": "С 1 сентября оборудование должно работать", "why_important": "Окно решения сокращается",
                    "business_consequence": "Можно не успеть к запуску", "basis_status": "needs_confirmation",
                    "status": "active", "ai_priority": 1, "evidence": ["Комментарий CRM"],
                }],
                "open_questions": ["Сохранился ли срок запуска?"],
                "source_conflicts": [],
            },
        })
        self.assertIn("## Живая карта сделки", markdown)
        self.assertIn("### Рычаги сделки", markdown)
        self.assertIn("С 1 сентября оборудование должно работать", markdown)

    def test_converted_lead_handoff_uses_local_related_deal(self) -> None:
        with patch(
            "run_rop_assistant.converted_lead_deals",
            return_value={"229607": {"id": "18683"}},
        ):
            handoffs = _converted_lead_handoffs(["229607"])

        self.assertEqual(handoffs, {"229607": "18683"})


if __name__ == "__main__":
    unittest.main()
