from __future__ import annotations

import unittest

from bitrix.client import BitrixReadOnlyClient


class FakeBatchClient(BitrixReadOnlyClient):
    def __init__(self, response: dict) -> None:
        super().__init__("https://example.test/rest/1/token")
        self.response = response
        self.batch_payloads: list[dict] = []
        self.tail_calls: list[tuple[str, dict]] = []

    def safe_call(self, method: str, payload: dict | None = None) -> dict:
        self.assert_batch_method(method)
        self.batch_payloads.append(dict(payload or {}))
        return self.response

    def safe_list_all(self, method: str, payload: dict | None = None) -> dict:
        self.tail_calls.append((method, dict(payload or {})))
        return {"ok": True, "method": method, "payload": payload or {}, "items": [{"ID": "tail"}]}

    @staticmethod
    def assert_batch_method(method: str) -> None:
        if method != "batch":
            raise AssertionError(method)


class BitrixBatchListTests(unittest.TestCase):
    def test_encodes_nested_payload_and_finishes_paginated_tail(self) -> None:
        client = FakeBatchClient({
            "ok": True,
            "response": {
                "result": {
                    "result": {
                        "r0": [{"ID": "first"}],
                        "r1": {"items": [{"ID": "second"}]},
                    },
                    "result_error": {},
                    "result_next": {"r0": 50},
                },
            },
        })

        result = client.safe_batch_list([
            (
                "deal:1",
                "crm.timeline.comment.list",
                {
                    "order": {"CREATED": "ASC"},
                    "filter": {"ENTITY_ID": "1"},
                    "select": ["ID", "CREATED"],
                },
            ),
            ("deal:2", "crm.stagehistory.list", {"filter": {"OWNER_ID": "2"}}),
        ])

        commands = client.batch_payloads[0]["cmd"]
        self.assertEqual(set(commands), {"r0", "r1"})
        self.assertIn("filter%5BENTITY_ID%5D=1", commands["r0"])
        self.assertIn("select%5B%5D=ID", commands["r0"])
        self.assertEqual([item["ID"] for item in result["deal:1"]["items"]], ["first", "tail"])
        self.assertEqual(result["deal:2"]["items"][0]["ID"], "second")
        self.assertEqual(client.tail_calls[0][1]["start"], 50)

    def test_reports_item_and_outer_errors_without_losing_other_results(self) -> None:
        client = FakeBatchClient({
            "ok": True,
            "response": {
                "result": {
                    "result": {"r0": []},
                    "result_error": {
                        "r1": {"error": "ACCESS_DENIED", "error_description": "denied"},
                    },
                    "result_next": {},
                },
            },
        })
        result = client.safe_batch_list([
            ("ok", "crm.timeline.comment.list", {}),
            ("bad", "crm.stagehistory.list", {}),
        ])
        self.assertTrue(result["ok"]["ok"])
        self.assertFalse(result["bad"]["ok"])
        self.assertEqual(result["bad"]["error"], "denied")

        failed = FakeBatchClient({"ok": False, "error": "batch unavailable"})
        result = failed.safe_batch_list([("one", "crm.timeline.comment.list", {})])
        self.assertFalse(result["one"]["ok"])
        self.assertEqual(result["one"]["items"], [])

    def test_rejects_duplicate_logical_keys(self) -> None:
        client = FakeBatchClient({"ok": True, "response": {"result": {}}})
        with self.assertRaises(ValueError):
            client.safe_batch_list([
                ("same", "crm.timeline.comment.list", {}),
                ("same", "crm.stagehistory.list", {}),
            ])


if __name__ == "__main__":
    unittest.main()
