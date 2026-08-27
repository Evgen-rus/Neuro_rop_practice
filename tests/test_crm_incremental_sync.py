from __future__ import annotations

import unittest
from typing import Any

from bitrix.customer_history import (
    activity_details_from_list,
    build_customer_history_bundle,
    fetch_contacts,
    fetch_entity_history,
    history_period,
    incremental_since,
)


class RecordingClient:
    def __init__(self, *, activity_items: list[dict[str, Any]] | None = None) -> None:
        self.activity_items = activity_items or []
        self.list_calls: list[tuple[str, dict[str, Any]]] = []
        self.call_calls: list[tuple[str, dict[str, Any]]] = []

    def safe_list_all(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        value = dict(payload or {})
        self.list_calls.append((method, value))
        if method == "crm.activity.list":
            return {"ok": True, "method": method, "payload": value, "items": list(self.activity_items)}
        return {"ok": True, "method": method, "payload": value, "items": []}

    def safe_call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.call_calls.append((method, dict(payload or {})))
        raise AssertionError(f"Unexpected safe_call: {method}")


class CrmIncrementalSyncTests(unittest.TestCase):
    def test_contact_details_use_batch_when_available(self) -> None:
        class BatchClient:
            def __init__(self) -> None:
                self.requests = []

            def safe_batch_call(self, requests_to_run):
                self.requests.append(requests_to_run)
                return {
                    key: {
                        "ok": True,
                        "method": method,
                        "payload": payload,
                        "response": {"result": {"ID": payload["id"]}},
                    }
                    for key, method, payload in requests_to_run
                }

        client = BatchClient()
        contacts = fetch_contacts(client, ["20", "10", "20"])

        self.assertEqual(set(contacts), {"10", "20"})
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len(client.requests[0]), 2)

    def test_activity_details_are_projected_from_list_without_api_call(self) -> None:
        activity = {"ID": "42", "FILES": [{"id": "7"}], "SUBJECT": "call"}

        details = activity_details_from_list([activity])

        self.assertEqual(details["42"]["response"]["result"], activity)
        self.assertEqual(details["42"]["method"], "crm.activity.list")
        self.assertTrue(details["42"]["reused_from_list"])

    def test_incremental_history_filters_by_last_updated_and_merges_snapshot(self) -> None:
        previous_history = {
            "activities": {
                "ok": True,
                "items": [
                    {
                        "ID": "1",
                        "SUBJECT": "old value",
                        "LAST_UPDATED": "2026-08-01T10:00:00+03:00",
                    }
                ],
            },
            "timeline_comments": [{"ok": True, "items": [{"ID": "10", "CREATED": "2026-08-01T10:00:00+03:00"}]}],
        }
        client = RecordingClient(
            activity_items=[
                {"ID": "1", "SUBJECT": "updated value", "LAST_UPDATED": "2999-08-08T10:00:00+03:00"},
                {"ID": "2", "SUBJECT": "new value", "LAST_UPDATED": "2999-08-08T11:00:00+03:00"},
            ]
        )

        history = fetch_entity_history(
            client,
            "deal",
            "7",
            history_period(365),
            previous_history=previous_history,
            updated_after="2026-08-08T09:55:00+03:00",
        )

        activities = {item["ID"]: item for item in history["activities"]["items"]}
        self.assertEqual(set(activities), {"1", "2"})
        self.assertEqual(activities["1"]["SUBJECT"], "updated value")
        activity_payload = next(payload for method, payload in client.list_calls if method == "crm.activity.list")
        self.assertEqual(activity_payload["filter"][">=LAST_UPDATED"], "2026-08-08T09:55:00+03:00")
        self.assertEqual(activity_payload["select"], ["*", "FILES", "COMMUNICATIONS"])
        self.assertEqual(client.call_calls, [])
        self.assertEqual(history["sync_mode"], "incremental")

    def test_first_history_fetch_is_full_and_has_no_update_filter(self) -> None:
        client = RecordingClient(activity_items=[])

        history = fetch_entity_history(client, "deal", "7", history_period(365))

        activity_payload = next(payload for method, payload in client.list_calls if method == "crm.activity.list")
        self.assertNotIn(">=LAST_UPDATED", activity_payload["filter"])
        self.assertNotIn(">LAST_UPDATED", activity_payload["filter"])
        self.assertEqual(history["sync_mode"], "full")

    def test_snapshot_overlap_is_fifteen_minutes(self) -> None:
        since = incremental_since({"generated_at": "2026-08-08T10:00:00+03:00"})
        self.assertEqual(since, "2026-08-08T09:45:00+03:00")

    def test_failed_snapshot_cursor_forces_full_retry_instead_of_advancing(self) -> None:
        since = incremental_since(
            {
                "generated_at": "2026-08-08T11:00:00+03:00",
                "sync": {"activity_cursor": None, "activity_sync_ok": False},
            }
        )
        self.assertIsNone(since)

    def test_preloaded_root_history_avoids_duplicate_root_requests(self) -> None:
        client = RecordingClient()
        root_response = {"ok": True, "response": {"result": {"ID": "1", "CONTACT_ID": "10"}}}
        contact_response = {"ok": True, "response": {"result": {"ID": "10"}}}
        root_history = {
            "entity_type": "lead",
            "entity_id": "1",
            "activities": {"ok": True, "items": []},
            "activity_details": {},
            "timeline_comments": [{"ok": True, "items": []}],
        }

        bundle = build_customer_history_bundle(
            client,
            root_type="lead",
            root_id="1",
            include_internal_context=False,
            root_response_override=root_response,
            root_history_override=root_history,
            preloaded_contacts={"10": contact_response},
        )

        self.assertIs(bundle["activities_by_entity"]["lead:1"], root_history)
        self.assertEqual(client.call_calls, [])
        root_activity_calls = [
            payload
            for method, payload in client.list_calls
            if method == "crm.activity.list" and payload.get("filter", {}).get("OWNER_TYPE_ID") == 1
        ]
        self.assertEqual(root_activity_calls, [])


if __name__ == "__main__":
    unittest.main()
