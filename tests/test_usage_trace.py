from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.usage_trace import append_usage_trace, build_daily_usage_line, build_usage_trace_event


class UsageTraceTests(unittest.TestCase):
    def metadata(self) -> dict:
        return {
            "requested_at": "2026-08-08T09:00:00+00:00",
            "call_type": "full_deal_analysis",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "low",
            "prompt_cache": {
                "mode": "explicit",
                "prompt_cache_key": "neuro-rop:full-deal:v1",
                "breakpoint_count": 1,
                "ttl": "30m",
            },
            "request_fingerprint": {
                "prompt": {"chars": 100, "bytes_utf8": 120, "sha256_16": "abc123"},
                "stable_prefix": {"chars": 40, "bytes_utf8": 45, "sha256_16": "def456"},
                "cache_prefixes": [
                    {"chars": 20, "bytes_utf8": 22, "sha256_16": "first"},
                    {"chars": 40, "bytes_utf8": 45, "sha256_16": "def456"},
                ],
            },
            "usage": {
                "input_tokens": 2_000,
                "output_tokens": 300,
                "total_tokens": 2_300,
                "input_tokens_details": {"cached_tokens": 1_200, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 100},
            },
            "estimated_cost": {"estimated_cost_usd": 0.01, "estimated_cost_rub": 0.75},
            "latency_seconds": 1.25,
            "response_id": "resp_test",
            "raw_output_text": "must not be traced",
            "prompt": "must not be traced",
        }

    def test_event_contains_usage_but_no_prompt_or_response_text(self) -> None:
        event = build_usage_trace_event(
            self.metadata(),
            status="success",
            entity_type="deal",
            entity_id="123",
        )
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertEqual(event["cached_input_tokens"], 1_200)
        self.assertEqual(event["entity_id"], "123")
        self.assertEqual(event["prompt_sha256_16"], "abc123")
        self.assertEqual(event["stable_prefix_sha256_16"], "def456")
        self.assertEqual([prefix["chars"] for prefix in event["cache_prefixes"]], [20, 40])
        self.assertNotIn("must not be traced", serialized)
        self.assertNotIn("raw_output_text", event)
        self.assertNotIn("prompt", event)

    def test_append_writes_one_utf8_json_line_to_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            daily_dir = Path(directory) / "daily"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_TRACE_PATH": str(path), "OPENAI_USAGE_DAILY_DIR": str(daily_dir)},
            ):
                append_usage_trace(self.metadata(), entity_type="deal", entity_id="123")

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["call_type"], "full_deal_analysis")
            daily_lines = (daily_dir / "2026-08-08.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(daily_lines), 1)
            self.assertTrue(daily_lines[0].startswith("2026-08-08 12:00:00 MSK | "))

    def test_daily_usage_is_one_safe_human_readable_line(self) -> None:
        event = build_usage_trace_event(
            self.metadata(),
            status="success",
            entity_type="deal",
            entity_id="123\n456",
        )
        filename, line = build_daily_usage_line(event)

        self.assertEqual(filename, "2026-08-08.log")
        self.assertNotIn("\n", line)
        self.assertIn("entity=deal:123 456", line)
        self.assertIn("input=2000", line)
        self.assertIn("cached=1200", line)
        self.assertIn("cache_write=0", line)
        self.assertIn("output=300", line)
        self.assertIn("cost=$0.0100/0.75 ₽", line)

    def test_trace_write_failure_does_not_escape(self) -> None:
        with patch("pathlib.Path.open", side_effect=OSError("denied")):
            append_usage_trace(self.metadata())


if __name__ == "__main__":
    unittest.main()
