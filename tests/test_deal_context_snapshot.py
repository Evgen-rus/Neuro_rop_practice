from __future__ import annotations

import unittest

from openai_api.llm.validation import _validate_deal_context, normalize_analysis_for_validation


def deal_context() -> dict:
    return {
        "deal_card": {
            "company": "Завод",
            "equipment": "Этикетировщик ЭН00.05",
            "manufacturing_days": "25",
            "amount": "240000",
            "responsible": "Иванов",
        },
        "current_truth": {
            "client_profile": "Директор принимает решение",
            "current_need": "Оборудование для производства",
            "desired_outcome": "Запуск к 1 сентября",
            "current_status": "КП передано директору",
            "current_task": "Получить решение",
            "next_checkpoint": "2026-08-17",
            "next_step_owner": "client",
        },
        "decision_path": {
            "decision_maker": "Директор",
            "influencers": ["Главный инженер"],
            "approval_path": "Инженер проверяет, директор утверждает",
            "current_step_owner": "client",
            "basis_status": "needs_confirmation",
            "evidence": ["Комментарий менеджера про директора"],
        },
        "commitments": [{
            "commitment_id": "quote_review",
            "party": "client",
            "promise": "Директор рассмотрит КП до пятницы",
            "due_at": "2026-08-15",
            "status": "open",
            "basis_status": "needs_confirmation",
            "evidence": ["Сообщение клиента"],
        }],
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
        "journey": [{
            "entry_id": "lead_converted",
            "occurred_at": "2026-07-01",
            "title": "Лид стал сделкой",
            "what_happened": "После квалификации создана сделка",
            "learned": ["Нужен этикетировщик"],
            "missing": ["ЛПР не подтверждён"],
            "status": "past",
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
        }, {
            "lever_id": "director_approval",
            "type": "authority",
            "title": "Решение директора",
            "fact": "КП лежит у директора",
            "why_important": "Без ЛПР сделка не двинется к договору",
            "business_consequence": "Потеря окна запуска",
            "basis_status": "inferred",
            "status": "active",
            "ai_priority": 2,
            "evidence": ["Статус согласования в CRM"],
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

    def test_single_lever_is_rejected(self) -> None:
        value = deal_context()
        value["pressure_levers"] = value["pressure_levers"][:1]
        errors: list[str] = []
        _validate_deal_context(value, errors)
        self.assertTrue(any("at least 2 items" in error for error in errors))

    def test_turning_point_current_status_is_normalized_to_active(self) -> None:
        context = deal_context()
        context["turning_points"][0]["status"] = "current"
        analysis = {"deal_context": context}

        changes = normalize_analysis_for_validation(analysis)

        self.assertEqual(analysis["deal_context"]["turning_points"][0]["status"], "active")
        self.assertEqual(
            changes,
            [
                {
                    "path": "deal_context.turning_points[0].status",
                    "action": "enum_alias",
                    "from": "current",
                    "to": "active",
                }
            ],
        )
        errors: list[str] = []
        _validate_deal_context(analysis["deal_context"], errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
