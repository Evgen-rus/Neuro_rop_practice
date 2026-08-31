from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import app as app_api
from api import deal_communication_content as content_api
from api import deal_communication_thread as thread_api


class DealCommunicationContentTests(unittest.TestCase):
    def test_reads_full_email_description_from_raw_activity(self) -> None:
        bundle = {
            "normalized_communications": [{
                "event_id": "crm_activity:81",
                "source_ids": ["81"],
                "channel": "email",
                "source_type": "crm_activity",
                "content": "Короткий фрагмент...",
            }],
            "activities_by_entity": {
                "deal:101": {
                    "activities": {
                        "items": [{
                            "ID": "81",
                            "DESCRIPTION": "<p>Полный текст письма</p><p>Вторая строка</p>",
                        }],
                    },
                    "activity_details": {},
                },
            },
        }
        with patch.object(content_api, "load_local_communication_bundle", return_value=bundle):
            result = content_api.get_deal_communication_content(
                "101",
                "crm_activity:81",
                db_path=Path("unused.sqlite"),
            )
        self.assertEqual(result["text"], "Полный текст письма\nВторая строка")
        self.assertFalse(result["is_excerpt"])
        self.assertEqual(result["channel"], "email")

    def test_reads_full_mirrored_messenger_comment(self) -> None:
        bundle = {
            "normalized_communications": [{
                "event_id": "crm_mirror:abc",
                "source_ids": ["501"],
                "channel": "whatsapp",
                "source_type": "crm_timeline_comment",
                "content": "Короткий фрагмент...",
            }],
            "timeline_comments_by_entity": {
                "deal:101": [{
                    "items": [{
                        "ID": "501",
                        "COMMENT": (
                            "[img]https://example/whatsapp.png[/img] Клиент:\n"
                            "Полный текст сообщения\nПродолжение"
                        ),
                    }],
                }],
            },
        }
        with patch.object(content_api, "load_local_communication_bundle", return_value=bundle):
            result = content_api.get_deal_communication_content(
                "101",
                "crm_mirror:abc",
                db_path=Path("unused.sqlite"),
            )
        self.assertEqual(result["text"], "Полный текст сообщения\nПродолжение")
        self.assertFalse(result["is_excerpt"])
        self.assertEqual(result["channel"], "whatsapp")

    def test_uses_daily_saved_fragment_when_bundle_is_not_updated(self) -> None:
        rows = [{
            "deal_id": "101",
            "communications_today": {
                "items": [{
                    "event_id": "crm_activity:90",
                    "channel": "message",
                    "content": "Сохранённый текст сообщения",
                }],
            },
        }]
        with patch.object(content_api, "load_local_communication_bundle", return_value={}), \
             patch.object(content_api.storage, "list_deal_control_deals", return_value=rows):
            result = content_api.get_deal_communication_content(
                "101",
                "crm_activity:90",
                db_path=Path("state.sqlite"),
            )
        self.assertEqual(result["text"], "Сохранённый текст сообщения")
        self.assertTrue(result["is_excerpt"])

    def test_rejects_internal_comment_and_foreign_event(self) -> None:
        bundle = {
            "normalized_communications": [{
                "event_id": "internal_comment:7",
                "source_ids": ["7"],
                "channel": "internal_comment",
                "content": "Внутренний секрет",
            }],
        }
        with patch.object(content_api, "load_local_communication_bundle", return_value=bundle), \
             patch.object(content_api.storage, "list_deal_control_deals", return_value=[]):
            with self.assertRaises(content_api.DealCommunicationContentNotFound):
                content_api.get_deal_communication_content(
                    "101",
                    "internal_comment:7",
                    db_path=Path("state.sqlite"),
                )
            with self.assertRaises(content_api.DealCommunicationContentNotFound):
                content_api.get_deal_communication_content(
                    "101",
                    "crm_activity:999",
                    db_path=Path("state.sqlite"),
                )

    def test_http_endpoint_checks_scope_and_maps_missing_to_404(self) -> None:
        payload = {
            "deal_id": "101",
            "event_id": "crm_activity:81",
            "channel": "email",
            "text": "Письмо",
            "is_excerpt": False,
            "truncated": False,
        }
        with patch.object(app_api, "require_deal") as require, patch.object(
            app_api,
            "get_deal_communication_content",
            return_value=payload,
        ) as read:
            result = app_api.deal_communication_content_get("101", "crm_activity:81")
        self.assertEqual(result, payload)
        require.assert_called_once_with("101", action="open")
        read.assert_called_once_with("101", "crm_activity:81", db_path=app_api.DEFAULT_DB_PATH)

        with patch.object(app_api, "require_deal"), patch.object(
            app_api,
            "get_deal_communication_content",
            side_effect=content_api.DealCommunicationContentNotFound("Нет текста"),
        ):
            with self.assertRaises(HTTPException) as error:
                app_api.deal_communication_content_get("101", "crm_activity:81")
        self.assertEqual(error.exception.status_code, 404)

    def test_thread_uses_anchor_moscow_day_and_keeps_conversation_isolated(self) -> None:
        conversation = "conversation:v1:contact-a"
        events = [
            {
                "event_id": "crm_activity:2", "source_ids": ["2"],
                "occurred_at": "2026-08-27T11:00:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "whatsapp", "direction": "incoming",
                "participant_role": "client", "participant_name": "Клиент",
                "contact_class": "confirmed_contact", "source_type": "crm_activity",
                "content": "Да", "conversation_key": conversation,
                "conversation_scope": "contact",
            },
            {
                "event_id": "crm_activity:1", "source_ids": ["1"],
                "occurred_at": "2026-08-27T09:00:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "whatsapp", "direction": "outgoing",
                "participant_role": "employee", "participant_name": "Менеджер",
                "contact_class": "attempt", "source_type": "crm_activity",
                "content": "Добрый день", "conversation_key": conversation,
                "conversation_scope": "contact",
            },
            {
                "event_id": "crm_activity:3", "source_ids": ["3"],
                "occurred_at": "2026-08-27T10:00:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "telegram", "direction": "incoming",
                "participant_role": "client", "contact_class": "confirmed_contact",
                "content": "Другой канал", "conversation_key": "conversation:v1:telegram",
            },
            {
                "event_id": "crm_activity:4", "source_ids": ["4"],
                "occurred_at": "2026-08-27T10:30:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "whatsapp", "direction": "incoming",
                "participant_role": "client", "contact_class": "confirmed_contact",
                "content": "Другой контакт", "conversation_key": "conversation:v1:contact-b",
            },
            {
                "event_id": "crm_activity:5", "source_ids": ["5"],
                "occurred_at": "2026-08-28T00:15:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "whatsapp", "direction": "incoming",
                "participant_role": "client", "contact_class": "confirmed_contact",
                "content": "Другой день", "conversation_key": conversation,
            },
            {
                "event_id": "internal_comment:6", "source_ids": ["6"],
                "occurred_at": "2026-08-27T10:45:00+03:00",
                "entity_key": "deal:101", "entity_keys": ["deal:101"],
                "channel": "internal_comment", "direction": "internal",
                "participant_role": "employee", "contact_class": "internal_information",
                "content": "Внутренняя запись", "conversation_key": conversation,
            },
        ]
        bundle = {
            "deal": {"item": {"ID": "101"}},
            "normalized_communications": events,
            "activities_by_entity": {
                "deal:101": {
                    "activities": {"items": [
                        {"ID": str(index), "DESCRIPTION": event["content"]}
                        for index, event in enumerate(events, start=1)
                    ]},
                    "activity_details": {},
                },
            },
        }
        with patch.object(thread_api, "load_local_communication_bundle", return_value=bundle):
            result = thread_api.get_deal_communication_thread(
                "101",
                "crm_activity:2",
                db_path=Path("unused.sqlite"),
            )
        self.assertEqual(result["date"], "2026-08-27")
        self.assertEqual(
            [item["event_id"] for item in result["messages"]],
            ["crm_activity:1", "crm_activity:2"],
        )
        self.assertEqual(result["messages"][0]["participant_role"], "manager")
        self.assertNotIn("Другой канал", str(result))
        self.assertNotIn("Другой контакт", str(result))
        self.assertNotIn("Внутренняя запись", str(result))

    def test_thread_http_endpoint_checks_scope(self) -> None:
        payload = {
            "deal_id": "101",
            "anchor_event_id": "crm_activity:2",
            "messages": [],
        }
        with patch.object(app_api, "require_deal") as require, patch.object(
            app_api,
            "get_deal_communication_thread",
            return_value=payload,
        ) as read:
            result = app_api.deal_communication_thread_get("101", "crm_activity:2")
        self.assertEqual(result, payload)
        require.assert_called_once_with("101", action="open")
        read.assert_called_once_with(
            "101",
            "crm_activity:2",
            db_path=app_api.DEFAULT_DB_PATH,
        )


if __name__ == "__main__":
    unittest.main()
