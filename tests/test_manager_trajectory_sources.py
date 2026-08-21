from __future__ import annotations

import unittest
from datetime import datetime

from api.manager_trajectory_sources import (
    ACTIVITY_SELECT_V3,
    BUSINESS_FIELD_SELECT,
    business_snapshot,
    collect_presence_snapshots,
    collect_stage_history_facts,
    collect_task_history_facts,
    collect_timeline_comment_facts,
    normalize_activity_payload,
    fetch_activity_facts,
)
from setup import MSK_TZ


class FakeClient:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, dict]] = []
        self.call_calls: list[tuple[str, dict]] = []

    def safe_list_all(self, method: str, payload: dict) -> dict:
        self.list_calls.append((method, payload))
        if method == "crm.activity.list":
            return {
                "ok": True,
                "items": [{
                    "ID": "a1",
                    "OWNER_TYPE_ID": "2",
                    "OWNER_ID": "101",
                    "TYPE_ID": "4",
                    "PROVIDER_ID": "CRM_EMAIL",
                    "RESPONSIBLE_ID": "10",
                    "AUTHOR_ID": "11",
                    "EDITOR_ID": "12",
                    "SUBJECT": "Тема",
                    "DESCRIPTION": "Текст письма",
                    "DIRECTION": "2",
                    "COMPLETED": "Y",
                    "START_TIME": "2026-08-21T10:00:00+03:00",
                    "LAST_UPDATED": "2026-08-21T10:01:00+03:00",
                    "RESULT_STATUS": "1",
                    "COMMUNICATIONS": [{"VALUE": "client@example.test"}],
                    "FILES": [],
                }],
            }
        if method == "crm.timeline.comment.list":
            return {
                "ok": True,
                "items": [{
                    "ID": "c1",
                    "CREATED": "2026-08-21T10:02:00+03:00",
                    "AUTHOR_ID": "10",
                    "COMMENT": "Комментарий",
                }],
            }
        if method == "crm.stagehistory.list":
            return {
                "ok": True,
                "items": [{
                    "ID": "s1",
                    "TYPE_ID": "2",
                    "OWNER_ID": "101",
                    "CREATED_TIME": "2026-08-21T10:03:00+03:00",
                    "CATEGORY_ID": "15",
                    "STAGE_ID": "C15:NEW",
                    "STAGE_SEMANTIC_ID": "P",
                }],
            }
        if method == "user.get":
            return {
                "ok": True,
                "items": [{
                    "ID": "10",
                    "NAME": "Александр",
                    "LAST_NAME": "Тестовый",
                    "IS_ONLINE": "Y",
                    "LAST_ACTIVITY_DATE": "2026-08-21T10:04:00+03:00",
                    "LAST_LOGIN": "2026-08-21T08:00:00+03:00",
                }],
            }
        if method == "task.ctasklogitem.list":
            return {
                "ok": True,
                "items": [{
                    "ID": "h1", "TASK_ID": "700", "FIELD": "DEADLINE",
                    "FROM_VALUE": "2026-08-20", "TO_VALUE": "2026-08-21",
                    "USER_ID": "10", "CREATED_DATE": "2026-08-21T10:05:00+03:00",
                }],
            }
        raise AssertionError(f"Unexpected list method: {method}")

    def safe_call(self, method: str, payload: dict) -> dict:
        self.call_calls.append((method, payload))
        if method == "task.ctasklogitem.list":
            return {
                "ok": True,
                "response": {"result": [{
                    "ID": "h1",
                    "TASK_ID": "700",
                    "FIELD": "DEADLINE",
                    "FROM_VALUE": "2026-08-20",
                    "TO_VALUE": "2026-08-21",
                    "USER_ID": "10",
                    "CREATED_DATE": "2026-08-21T10:05:00+03:00",
                }]},
            }
        raise AssertionError(f"Unexpected call method: {method}")


class ManagerTrajectorySourcesTests(unittest.TestCase):
    def test_activity_selection_and_normalization_keep_content_and_provenance(self) -> None:
        self.assertIn("SUBJECT", ACTIVITY_SELECT_V3)
        self.assertIn("DESCRIPTION", ACTIVITY_SELECT_V3)
        self.assertIn("AUTHOR_ID", ACTIVITY_SELECT_V3)
        payload = normalize_activity_payload({
            "ID": "1",
            "OWNER_ID": "20",
            "OWNER_TYPE_ID": "2",
            "TYPE_ID": "4",
            "PROVIDER_ID": "CRM_EMAIL",
            "SUBJECT": "Тема",
            "DESCRIPTION": "Текст",
            "COMPLETED": "Y",
            "START_TIME": "2026-08-21T10:00:00+03:00",
        })
        self.assertEqual(payload["activity_id"], "1")
        self.assertEqual(payload["subject"], "Тема")
        self.assertEqual(payload["description"], "Текст")
        self.assertTrue(payload["completed"])
        self.assertEqual(payload["activity_kind"], "email")

    def test_fetch_activity_facts_uses_manager_filter_and_event_key(self) -> None:
        client = FakeClient()
        result = fetch_activity_facts(
            client,
            ["10"],
            datetime(2026, 8, 21, 0, 0, tzinfo=MSK_TZ),
            datetime(2026, 8, 22, 0, 0, tzinfo=MSK_TZ),
        )
        self.assertFalse(result["errors"])
        self.assertEqual(len(result["facts"]), 1)
        fact = result["facts"][0]
        self.assertEqual(fact["source_event_key"], "crm_activity:a1:2026-08-21T10:01:00+03:00")
        self.assertEqual(fact["manager_id"], "10")
        self.assertEqual(client.list_calls[0][1]["select"], ACTIVITY_SELECT_V3)

    def test_business_snapshot_only_includes_explicit_uf_fields(self) -> None:
        self.assertIn("OPPORTUNITY", BUSINESS_FIELD_SELECT["deal"])
        snapshot = business_snapshot(
            {
                "ID": "101",
                "TITLE": "Сделка",
                "OPPORTUNITY": "1000",
                "UF_CRM_NEED": "нужно",
                "UF_CRM_SECRET": "не брать",
            },
            "deal",
            custom_allowlist=["UF_CRM_NEED", "UF_CRM_SECRET", "COMMENTS"],
        )
        self.assertEqual(snapshot["fields"]["OPPORTUNITY"], "1000")
        self.assertEqual(snapshot["fields"]["UF_CRM_NEED"], "нужно")
        self.assertEqual(snapshot["fields"]["UF_CRM_SECRET"], "не брать")
        self.assertNotIn("COMMENTS", snapshot["fields"])
        self.assertEqual(snapshot["field_labels"]["OPPORTUNITY"], "Сумма")

    def test_timeline_comments_are_deduplicated_and_keep_manager_author(self) -> None:
        client = FakeClient()
        result = collect_timeline_comment_facts(
            client,
            [{"entity_type": "deal", "entity_id": "101", "manager_id": "10"},
             {"entity_type": "deal", "entity_id": "101", "manager_id": "10"}],
            ["10"],
        )
        self.assertFalse(result["errors"])
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["facts"][0]["source_event_key"], "crm_timeline_comment:deal:101:c1")
        self.assertEqual(result["facts"][0]["payload"]["author_id"], "10")

    def test_task_history_uses_task_ctasklogitem_list(self) -> None:
        client = FakeClient()
        result = collect_task_history_facts(
            client,
            [{
                "provider_id": "CRM_TASKS_TASK",
                "associated_entity_id": "700",
                "entity_type": "deal",
                "entity_id": "101",
                "responsible_id": "10",
            }],
            ["10"],
        )
        self.assertFalse(result["errors"])
        self.assertEqual(len(result["facts"]), 1)
        self.assertTrue(any(method == "task.ctasklogitem.list" for method, _ in client.list_calls))
        fact = result["facts"][0]
        self.assertEqual(fact["source_event_key"], "task_history:700:h1")
        self.assertEqual(fact["payload"]["field"], "DEADLINE")
        self.assertEqual(fact["manager_id"], "10")

    def test_stage_history_and_presence_are_read_only_facts(self) -> None:
        client = FakeClient()
        stages = collect_stage_history_facts(client, [{"entity_type": "deal", "entity_id": "101", "manager_id": "10"}])
        presence = collect_presence_snapshots(client, ["10"])
        self.assertFalse(stages["errors"])
        self.assertFalse(presence["errors"])
        self.assertEqual(stages["facts"][0]["payload"]["stage_id"], "C15:NEW")
        self.assertEqual(presence["facts"][0]["payload"]["is_online"], True)
        self.assertEqual(presence["facts"][0]["manager_id"], "10")
        self.assertTrue(any(method == "crm.stagehistory.list" for method, _ in client.list_calls))
        self.assertTrue(any(method == "user.get" for method, _ in client.list_calls))


if __name__ == "__main__":
    unittest.main()
