from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import deal_manager_quick_help as quick_help
from api import deal_call_transcript as transcript_api
from api import deal_communication_content as content_api
from bitrix.customer_history import build_history_sections
from bitrix.deals.communication_history import include_source_lead_communications
from bitrix.deals.communication_timeline import load_bundle


def fixture() -> tuple[dict, dict]:
    call = {"ID": "700", "TYPE_ID": "2", "OWNER_ID": "202", "COMPLETED": "Y", "DIRECTION": "2",
            "START_TIME": "2026-08-27T10:00:00+03:00", "END_TIME": "2026-08-27T10:02:00+03:00"}
    email = {"ID": "701", "TYPE_ID": "4", "OWNER_ID": "202", "COMPLETED": "Y", "DIRECTION": "1",
             "START_TIME": "2026-08-27T10:10:00+03:00", "DESCRIPTION": "<p>Полный ответ клиента</p>"}
    source = {
        "lead_id": "202", "activities": {"ok": True, "items": [call, email]}, "activity_details": {},
        "timeline_comments": [{"ok": True, "items": [
            {"ID": "702", "CREATED": "2026-08-27T11:00:00+03:00", "COMMENT": "[img]https://example/whatsapp.png[/img] Клиент:\nСообщение лида"},
        ]}],
    }
    return ({"bundle_type": "customer_history_bundle", "client_touchpoints": [], "internal_context": [],
             "normalized_communications": []},
            {"deal_id": "101", "deal": {"item": {"ID": "101", "LEAD_ID": "202"}}, "source_lead": source})


class SourceLeadCommunicationTests(unittest.TestCase):
    def test_enriches_once_without_mutating_inputs_or_adopting_tasks(self):
        bundle, context = fixture()
        source = context["source_lead"]
        source["activities"]["items"].append({"ID": "703", "TYPE_ID": "6", "PROVIDER_ID": "CRM_TASKS_TASK"})
        original = copy.deepcopy((bundle, context))
        result = include_source_lead_communications(bundle, context, deal_id="101")
        self.assertEqual((bundle, context), original)
        self.assertEqual({e["channel"] for e in result["normalized_communications"]}, {"call", "email", "whatsapp"})
        self.assertTrue(all(e["entity_type"] == "lead" and e["entity_id"] == "202" for e in result["normalized_communications"]))
        self.assertNotIn("tasks_by_entity", result)
        self.assertEqual(include_source_lead_communications(result, context, deal_id="101"), result)

    def test_deduplicates_call_already_bound_to_deal(self):
        bundle, context = fixture()
        call = context["source_lead"]["activities"]["items"][0]
        history = {"entity_type": "deal", "entity_id": "101", "activities": {"items": [call]}}
        bundle.update(build_history_sections({"activities_by_entity": {"deal:101": history}}))
        result = include_source_lead_communications(bundle, context, deal_id="101")
        self.assertEqual(sum(e["channel"] == "call" for e in result["normalized_communications"]), 1)
        self.assertEqual(sum(e["id"] == "700" for e in result["client_touchpoints"]), 1)

    def test_requires_matching_deal_and_explicit_source_lead(self):
        for mutation in ("wrong_deal", "no_lead", "wrong_lead"):
            with self.subTest(mutation=mutation):
                bundle, context = fixture()
                if mutation == "wrong_deal":
                    context["deal"]["item"]["ID"] = "999"
                elif mutation == "no_lead":
                    context["deal"]["item"].pop("LEAD_ID")
                else:
                    context["source_lead"]["lead_id"] = "999"
                self.assertEqual(include_source_lead_communications(bundle, context, deal_id="101"), bundle)

    def test_refreshes_existing_source_lead_message_without_duplicating_it(self):
        bundle, context = fixture()
        saved = include_source_lead_communications(bundle, context, deal_id="101")
        context["source_lead"]["activities"]["items"][1]["DESCRIPTION"] = "Уточнённый ответ клиента"
        refreshed = include_source_lead_communications(saved, context, deal_id="101")
        emails = [e for e in refreshed["normalized_communications"] if e["channel"] == "email"]
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["content"], "Уточнённый ответ клиента")
        self.assertEqual(next(e for e in refreshed["unified_timeline"] if e["id"] == "701")["text"], "Уточнённый ответ клиента")

    def test_honors_disabled_internal_context(self):
        bundle, context = fixture()
        bundle["include_internal_context"] = False
        result = include_source_lead_communications(bundle, context, deal_id="101")
        self.assertEqual(result["internal_context"], [])
        self.assertEqual({e["channel"] for e in result["normalized_communications"]}, {"call", "email"})

    def test_old_workspace_supports_quick_help_text_transcript_and_local_timeline(self):
        bundle, context = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            history_path = raw / "deal_101_customer_history_bundle.json"
            history_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            (raw / "deal_101_context.json").write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
            transcripts = root / "transcripts"
            transcripts.mkdir()
            (transcripts / "call_700_transcript.txt").write_text("Сохранённый разговор", encoding="utf-8")
            with patch.object(quick_help, "deal_workspace_dir", return_value=root), \
                 patch.object(transcript_api, "deal_workspace_dir", return_value=root):
                events = quick_help._load_local_communications("101")
                self.assertEqual(len(events), 3)
                self.assertEqual(content_api.get_deal_communication_content("101", "crm_activity:701")["text"], "Полный ответ клиента")
                mirror = next(e for e in events if e["channel"] == "whatsapp")
                self.assertEqual(content_api.get_deal_communication_content("101", mirror["event_id"])["text"], "Сообщение лида")
                self.assertEqual(transcript_api.get_deal_call_transcript("101", "crm_activity:700")["text"], "Сохранённый разговор")
                self.assertEqual(load_bundle(history_path)["normalized_communications"], events)
            self.assertEqual(json.loads(history_path.read_text(encoding="utf-8")), bundle)


if __name__ == "__main__":
    unittest.main()
