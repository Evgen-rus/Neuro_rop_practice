from __future__ import annotations

import copy
import unittest

from bitrix.customer_history import build_normalized_communications


def response(item: dict) -> dict:
    return {"ok": True, "response": {"result": item}}


class NormalizedCommunicationsTests(unittest.TestCase):
    def test_normalization_is_additive_and_handles_lead_calls(self) -> None:
        bundle = {
            "lead": response({"ID": "42", "NAME": "Иван", "LAST_NAME": "Петров"}),
            "contacts": {},
            "client_touchpoints": [
                {
                    "when": "2026-07-17T13:35:12+03:00",
                    "event_type": "call",
                    "entity_type": "lead",
                    "entity_id": "42",
                    "entity_key": "lead:42",
                    "id": "623261",
                    "subject": "Исходящий звонок",
                    "text": "",
                    "direction": "2",
                    "raw": {
                        "ID": "623261",
                        "START_TIME": "2026-07-17T13:35:12+03:00",
                        "END_TIME": "2026-07-17T13:35:12+03:00",
                        "COMPLETED": "Y",
                    },
                }
            ],
            "internal_context": [],
        }
        original = copy.deepcopy(bundle)

        events = build_normalized_communications(bundle)

        self.assertEqual(bundle, original)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "crm_activity:623261")
        self.assertEqual(events[0]["contact_class"], "attempt")
        self.assertEqual(events[0]["duration_seconds"], 0.0)
        self.assertFalse(events[0]["has_transcript"])

    def test_wazzup_client_messages_are_direct_contacts_and_duplicates_collapse(self) -> None:
        text = "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Владимирович Олег:\nНормально, место есть."
        bundle = {
            "lead": response({"ID": "42"}),
            "contacts": {
                "7": response({"ID": "7", "NAME": "Владимирович", "LAST_NAME": "Олег"})
            },
            "client_touchpoints": [],
            "internal_context": [
                {
                    "when": "2026-07-20T09:47:15+03:00",
                    "category": "timeline_comment",
                    "event_type": "internal_comment",
                    "entity_type": "lead",
                    "entity_id": "42",
                    "entity_key": "lead:42",
                    "id": "100",
                    "text": text,
                },
                {
                    "when": "2026-07-20T09:47:15+03:00",
                    "category": "timeline_comment",
                    "event_type": "internal_comment",
                    "entity_type": "contact",
                    "entity_id": "7",
                    "entity_key": "contact:7",
                    "id": "101",
                    "text": text,
                },
            ],
        }

        events = build_normalized_communications(bundle)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["channel"], "whatsapp")
        self.assertEqual(events[0]["direction"], "incoming")
        self.assertEqual(events[0]["participant_role"], "client")
        self.assertEqual(events[0]["contact_class"], "confirmed_contact")
        self.assertEqual(events[0]["source_ids"], ["100", "101"])

    def test_unmatched_mirrored_message_is_uncertain_attempt_and_internal_chat_stays_internal(self) -> None:
        bundle = {
            "deal": response({"ID": "9"}),
            "contacts": {"7": response({"ID": "7", "NAME": "Дмитрий"})},
            "client_touchpoints": [],
            "internal_context": [
                {
                    "when": "2026-07-16T10:51:38+03:00",
                    "category": "timeline_comment",
                    "entity_type": "deal",
                    "entity_id": "9",
                    "entity_key": "deal:9",
                    "id": "200",
                    "text": "[img]https://static.wazzup24.com/images/bitrix/max.png[/img] Александр Пахомов:\nНаправляем предварительное КП.",
                },
                {
                    "when": "2026-07-16T11:00:00+03:00",
                    "category": "internal_im_chat",
                    "entity_type": "deal",
                    "entity_id": "9",
                    "entity_key": "deal:9",
                    "id": "300",
                    "author": "РОП",
                    "text": "Автор: РОП\nПроверь следующий шаг.",
                },
            ],
        }

        events = build_normalized_communications(bundle)

        self.assertEqual(events[0]["channel"], "max")
        self.assertEqual(events[0]["direction"], "unknown")
        self.assertEqual(events[0]["participant_role"], "unknown")
        self.assertEqual(events[0]["contact_class"], "attempt")
        self.assertEqual(events[1]["channel"], "internal_chat")
        self.assertEqual(events[1]["contact_class"], "internal_information")
        self.assertEqual(events[1]["evidence_level"], "reported")


if __name__ == "__main__":
    unittest.main()
