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

    def nurture_delta(self, *, processing_quality: str = "weak", attention: bool = True) -> dict:
        value = lead_delta(meaningful_contact=True, playbook="restore_no_contact_processing")
        value["lead_review"].update(
            {"final_verdict": "needs_nurture", "processing_quality": processing_quality}
        )
        if not attention:
            value["attention_required"] = False
            value["rop_action"] = None
        return value

    def test_needs_nurture_overrides_restore_no_contact(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043a\u043e \u043c\u043d\u0435 \u0447\u0435\u0440\u0435\u0437 \u043c\u0435\u0441\u044f\u0446."))
        result, audit = self.normalize(self.nurture_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "scheduled_nurture_followup")
        self.assertEqual(audit["normalization_reason"], "grounded_meaningful_contact_needs_nurture")

    def test_nurture_materializes_one_calm_task_without_three_call_cycle(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043e\u0441\u0435\u043d\u044c\u044e."))
        result, _audit = self.normalize(self.nurture_delta(), source)
        materialized = materialize_lead_attention_delta(result, today=date(2031, 2, 3))
        validate_lead_attention_delta(materialized)
        action = materialized["rop_action"]
        self.assertEqual(action["owner"], "\u041e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439 \u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440")
        self.assertNotIn("\u0442\u0440\u0438 \u043f\u043e\u043f\u044b\u0442", action["message_to_manager"].lower())
        self.assertIn("\u043e\u0441\u0435\u043d", action["message_to_manager"].lower())

    def test_nurture_keeps_client_confirmed_exact_date(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435, 2031-09-01."))
        result, _audit = self.normalize(self.nurture_delta(), source)
        action = materialize_lead_attention_delta(result, today=date(2031, 2, 3))["rop_action"]
        self.assertIn("2031-09-01", action["expected_crm_fact"])

    def test_nurture_vague_season_does_not_invent_client_date(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0414\u0430\u0432\u0430\u0439\u0442\u0435 \u043e\u0441\u0435\u043d\u044c\u044e."))
        result, _audit = self.normalize(self.nurture_delta(), source)
        action = materialize_lead_attention_delta(result, today=date(2031, 2, 3))["rop_action"]
        self.assertNotIn("2031-", action["expected_crm_fact"])
        self.assertIn("\u0431\u0435\u0437 \u0432\u044b\u0434\u0443\u043c\u0430\u043d\u043d\u043e\u0439 \u0442\u043e\u0447\u043d\u043e\u0439 \u0434\u0430\u0442\u044b", action["expected_crm_fact"])

    def test_nurture_without_task_requires_attention(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435."))
        result, _audit = self.normalize(self.nurture_delta(), source)
        self.assertTrue(result["attention_required"])

    def test_correctly_scheduled_nurture_has_no_attention(self) -> None:
        source = transcript(("nurture-1", "2031-02-03T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435."))
        result, _audit = self.normalize(
            self.nurture_delta(processing_quality="good", attention=False),
            source,
            history_text="source=lead:1 type=task id=task-1",
        )
        materialized = materialize_lead_attention_delta(result)
        validate_lead_attention_delta(materialized)
        self.assertFalse(materialized["attention_required"])
        self.assertIsNone(materialized["rop_action"])

    def test_old_busy_does_not_override_later_meaningful_nurture(self) -> None:
        source = transcript(
            ("busy-1", "2031-02-03T10:00:00+03:00", BUSY_BODY),
            ("nurture-2", "2031-02-04T10:00:00+03:00", "\u0412\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u0447\u0435\u0440\u0435\u0437 \u043c\u0435\u0441\u044f\u0446."),
        )
        result, _audit = self.normalize(self.nurture_delta(), source)
        self.assertEqual(result["lead_review"]["action_playbook"], "scheduled_nurture_followup")

    def test_confirmed_refusal_is_not_nurture(self) -> None:
        source = transcript(("refusal-1", "2031-02-03T10:00:00+03:00", "\u041c\u044b \u043f\u043e\u043a\u0443\u043f\u0430\u0442\u044c \u043d\u0435 \u0431\u0443\u0434\u0435\u043c, \u0432\u0435\u0440\u043d\u0438\u0442\u0435\u0441\u044c \u043f\u043e\u0437\u0436\u0435."))
        result, _audit = self.normalize(self.nurture_delta(), source)
        self.assertNotEqual(result["lead_review"]["action_playbook"], "scheduled_nurture_followup")

    def test_internal_comment_later_alone_is_not_nurture(self) -> None:
        result, _audit = self.normalize(
            self.nurture_delta(),
            "",
            history_text="source=lead:1 type=internal_comment id=comment-1 \u043f\u043e\u0437\u0436\u0435",
        )
        self.assertNotEqual(result["lead_review"]["action_playbook"], "scheduled_nurture_followup")


if __name__ == "__main__":
    unittest.main()
