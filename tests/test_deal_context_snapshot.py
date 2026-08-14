from __future__ import annotations

import unittest

from openai_api.llm.validation import _validate_deal_context


def deal_context() -> dict:
    return {
        "current_truth": {
            "client_profile": "Директор принимает решение",
            "current_need": "Оборудование для производства",
            "desired_outcome": "Запуск к 1 сентября",
            "current_status": "КП передано директору",
            "current_task": "Получить решение",
            "next_checkpoint": "2026-08-17",
            "next_step_owner": "client",
        },
        "critical_facts": [{
            "fact_id": "launch_deadline",
            "category": "deadline",
            "fact": "С 1 сентября оборудование должно работать на производстве",
            "status": "needs_confirmation",
            "importance": "high",
            "observed_at": "2026-08-06",
            "source_type": "manager_comment",
            "evidence": ["Комментарий CRM от 06.08"],
        }],
        "turning_points": [{
            "turning_point_id": "quote_sent",
            "occurred_at": "2026-08-07",
            "title": "КП отправлено",
            "what_happened": "Клиент получил КП",
            "impact": "Следующий шаг перешёл к директору",
            "status": "active",
            "evidence": ["Подтверждение клиента"],
        }],
        "pain_points": [{
            "pain_id": "decision_delay",
            "title": "Нет решения директора",
            "description": "КП находится на внутреннем согласовании",
            "status": "active",
            "impact": "Срок запуска под риском",
            "evidence": ["Разговор с клиентом"],
        }],
        "pressure_levers": [{
            "lever_id": "launch_deadline",
            "type": "deadline",
            "title": "Срок запуска",
            "fact": "С 1 сентября оборудование должно работать",
            "why_important": "Окно решения сокращается",
            "business_consequence": "Можно не успеть к запуску",
            "basis_status": "needs_confirmation",
            "status": "active",
            "ai_priority": 1,
            "evidence": ["Комментарий CRM"],
        }],
        "open_questions": ["Сохранился ли срок запуска?"],
        "source_conflicts": [],
    }


class DealContextSnapshotTests(unittest.TestCase):
    def test_valid_context_passes(self) -> None:
        errors: list[str] = []
        _validate_deal_context(deal_context(), errors)
        self.assertEqual(errors, [])

    def test_duplicate_ai_priority_is_rejected(self) -> None:
        value = deal_context()
        value["pressure_levers"].append({
            **value["pressure_levers"][0],
            "lever_id": "budget",
            "title": "Бюджет",
        })
        errors: list[str] = []
        _validate_deal_context(value, errors)
        self.assertTrue(any("duplicate ai_priority 1" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
