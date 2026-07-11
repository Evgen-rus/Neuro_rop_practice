from __future__ import annotations

import unittest
from datetime import date

from openai_api.llm.attention_delta import materialize_lead_attention_delta, validate_lead_attention_delta
from openai_api.llm.lead_playbook_resolver import normalize_lead_action_playbook


def lead_delta(*, playbook: str = "restore_no_contact_processing", meaningful_contact: bool = False) -> dict:
    return {
        "entity_type": "lead",
        "entity_id": "fixture",
        "attention_required": True,
        "severity": "medium_high",
        "reason": "Нет подтверждённого содержательного контакта.",
        "rop_action": {
            "check": "Проверить результат доступной попытки связи.",
            "message_to_manager": "Зафиксируйте следующий шаг в CRM.",
            "expected_crm_fact": "CRM-след следующей попытки.",
            "deadline": None,
            "success_condition": "Есть проверяемый результат.",
            "owner": None,
            "evidence_ids": ["old-evidence"],
        },
        "memory_patch": {
            "confirmed_facts_add": [],
            "open_questions_add": [],
            "open_questions_resolve": [],
            "risks_add": [],
            "risks_resolve": [],
            "next_step": "Зафиксировать результат следующей попытки.",
        },
        "lead_review": {
            "qualification": "unknown",
            "lead_quality": "unknown",
            "processing_quality": "weak",
            "final_verdict": "bad_processing",
            "meaningful_contact": meaningful_contact,
            "action_playbook": playbook,
        },
    }


def transcript(*calls: tuple[str, str, str]) -> str:
    sections: list[str] = []
    for activity_id, when, body in calls:
        sections.append(
            f"### Звонок: activity_id={activity_id}\n\n"
            f"- Дата звонка: {when}\n\n"
            "```text\n"
            f"{body}\n"
            "```"
        )
    return "\n\n".join(sections)


BUSY_BODY = "Линия занята, абоненту неудобно сейчас говорить. Я голосовой ассистент."


class LeadPlaybookResolverTests(unittest.TestCase):
    def normalize(self, value: dict, transcript_text: str, history_text: str = "") -> tuple[dict, dict]:
        return normalize_lead_action_playbook(value, history_text=history_text, transcript_text=transcript_text)

    def test_exact_busy_normalizes_generic_no_contact_playbook(self) -> None:
        source = transcript(("call-1", "2031-02-03T10:00:00+03:00", BUSY_BODY))
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(audit["raw_action_playbook"], "restore_no_contact_processing")
        self.assertEqual(audit["normalized_action_playbook"], "retry_busy_number")
        self.assertEqual(audit["normalization_reason"], "latest_reliable_call_outcome_busy")
        self.assertEqual(audit["normalization_evidence_ids"], ["call-1"])
        materialized = materialize_lead_attention_delta(result, today=date(2031, 2, 3))
        validate_lead_attention_delta(materialized)
        self.assertEqual(materialized["lead_review"]["action_playbook"], "retry_busy_number")
        self.assertFalse(materialized["lead_review"]["meaningful_contact"])
        self.assertIn("10 минут", materialized["rop_action"]["message_to_manager"])
        self.assertNotIn("3 попыт", materialized["rop_action"]["message_to_manager"])

    def test_partial_context_keeps_exact_busy_without_manual_audit(self) -> None:
        source = transcript(("busy-1", "2031-02-03T10:00:00+03:00", BUSY_BODY))
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "retry_busy_number")
        self.assertEqual(audit["normalization_evidence_ids"], ["busy-1"])
        self.assertNotEqual(result["lead_review"]["action_playbook"], "manual_context_audit")

    def test_later_no_answer_prevents_busy_override(self) -> None:
        source = transcript(
            ("busy-1", "2031-02-03T10:00:00+03:00", BUSY_BODY),
            ("no-answer-2", "2031-02-03T11:00:00+03:00", "Абонент не отвечает."),
        )
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "restore_no_contact_processing")
        self.assertIsNone(audit["normalization_reason"])

    def test_meaningful_contact_after_busy_prohibits_busy_override(self) -> None:
        source = transcript(("busy-1", "2031-02-03T10:00:00+03:00", BUSY_BODY))
        result, audit = self.normalize(lead_delta(meaningful_contact=True, playbook="qualification_followup"), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "qualification_followup")
        self.assertIsNone(audit["normalization_reason"])

    def test_latest_invalid_number_has_priority(self) -> None:
        source = transcript(
            ("busy-1", "2031-02-03T10:00:00+03:00", BUSY_BODY),
            ("invalid-2", "2031-02-03T11:00:00+03:00", "Данный номер не существует."),
        )
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "verify_invalid_number")
        self.assertEqual(audit["normalization_reason"], "latest_reliable_call_outcome_invalid_number")
        self.assertEqual(audit["normalization_evidence_ids"], ["invalid-2"])

    def test_generic_no_contact_remains_generic_when_no_exact_outcome_exists(self) -> None:
        source = transcript(("call-1", "2031-02-03T10:00:00+03:00", "Короткая запись без результата."))
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "restore_no_contact_processing")
        self.assertIsNone(audit["normalization_reason"])

    def test_busy_word_in_internal_comment_cannot_trigger_normalization(self) -> None:
        result, audit = self.normalize(lead_delta(), "", history_text="Внутренний комментарий: занято")
        self.assertEqual(result["lead_review"]["action_playbook"], "restore_no_contact_processing")
        self.assertIsNone(audit["normalization_reason"])

    def test_normalization_requires_structured_evidence_id(self) -> None:
        source = "### Звонок без activity id\n\n- Дата звонка: 2031-02-03T10:00:00+03:00\n\n```text\n" + BUSY_BODY + "\n```"
        result, audit = self.normalize(lead_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "restore_no_contact_processing")
        self.assertEqual(audit["normalization_evidence_ids"], [])


if __name__ == "__main__":
    unittest.main()
