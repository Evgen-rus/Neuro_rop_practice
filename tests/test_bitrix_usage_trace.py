from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from bitrix.client import BitrixReadOnlyClient
from bitrix.usage_trace import append_trace_event, build_trace_event
from reliability.retry import RetryPolicy


NO_RETRY = RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0)
ZERO_DELAY_RETRY = RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0, jitter_ratio=0)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.ok = status_code < 400
        self.text = "sensitive response text"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("sensitive URL and response", response=self)


class BitrixUsageTraceTests(unittest.TestCase):
    def test_event_contains_metrics_but_no_payload_values_or_response(self) -> None:
        event = build_trace_event(
            run_id="run-1",
            method="crm.activity.list",
            payload={
                "filter": {"OWNER_ID": "secret-crm-id", "OWNER_TYPE_ID": 2, ">LAST_UPDATED": "secret-date"},
                "select": ["*"],
                "start": 50,
                "auth": "secret-webhook-token",
            },
            attempt=1,
            duration_ms=123.4567,
            ok=True,
            http_status=200,
            data={"result": [{"ID": "secret-result-id"}], "next": 100},
        )

        serialized = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["method"], "crm.activity.list")
        self.assertEqual(event["request_shape"]["filter_keys"], [">LAST_UPDATED", "OWNER_ID", "OWNER_TYPE_ID"])
        self.assertEqual(event["request_shape"]["page_start"], 50)
        self.assertEqual(event["item_count"], 1)
        self.assertFalse(event["empty"])
        self.assertNotIn("secret-crm-id", serialized)
        self.assertNotIn("secret-webhook-token", serialized)
        self.assertNotIn("secret-result-id", serialized)
        self.assertNotIn("auth", event["request_shape"]["payload_keys"])

    def test_append_writes_one_daily_json_line(self) -> None:
        event = build_trace_event(
            run_id="run-1",
            method="crm.activity.list",
            payload={"filter": {"OWNER_ID": "42"}},
            attempt=1,
            duration_ms=1,
            ok=True,
            http_status=200,
            data={"result": []},
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"BITRIX_USAGE_DAILY_DIR": directory}):
                append_trace_event(event)
            files = list(Path(directory).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            lines = files[0].read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        saved = json.loads(lines[0])
        self.assertTrue(saved["empty"])
        self.assertEqual(saved["item_count"], 0)

    def test_client_traces_each_retry_attempt_without_error_text(self) -> None:
        events: list[dict] = []
        responses = [
            FakeResponse(503, {}),
            FakeResponse(200, {"result": {"ID": "7"}}),
        ]
        with (
            patch("bitrix.client.requests.post", side_effect=responses),
            patch("bitrix.client.append_trace_event", side_effect=events.append),
        ):
            client = BitrixReadOnlyClient("https://secret.example/rest/hook", retry_policy=ZERO_DELAY_RETRY)
            result = client.call("crm.lead.get", {"id": "sensitive-id"})

        self.assertEqual(result["result"]["ID"], "7")
        self.assertEqual([event["attempt"] for event in events], [1, 2])
        self.assertEqual([event["ok"] for event in events], [False, True])
        self.assertEqual(events[0]["http_status"], 503)
        self.assertEqual(events[0]["error_type"], "HTTPError")
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("secret.example", serialized)
        self.assertNotIn("sensitive-id", serialized)
        self.assertNotIn("sensitive URL", serialized)

    def test_client_traces_transport_failure_without_leaking_exception(self) -> None:
        events: list[dict] = []
        with (
            patch(
                "bitrix.client.requests.post",
                side_effect=requests.Timeout("https://secret.example/rest/hook?id=42"),
            ),
            patch("bitrix.client.append_trace_event", side_effect=events.append),
        ):
            client = BitrixReadOnlyClient("https://secret.example/rest/hook", retry_policy=NO_RETRY)
            response = client.safe_call("crm.activity.get", {"id": "42"})

        self.assertFalse(response["ok"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_type"], "Timeout")
        self.assertIsNone(events[0]["http_status"])
        self.assertNotIn("secret.example", json.dumps(events, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
