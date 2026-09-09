from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.ai_spend import build_ai_spend_day, build_ai_spend_summary
from openai_api.llm.usage_trace import append_usage_trace
from openai_api.spend_diary import (
    DIR_ENV,
    display_kind_label,
    format_rub_ui,
    model_label,
    paid_calls_label,
    record_paid_call,
)
from setup import MSK_TZ


NOW = datetime(2026, 9, 9, 16, 40, tzinfo=MSK_TZ)
TODAY = NOW.date()


def _admin() -> dict[str, object]:
    return {"id": 1, "login": "admin", "role": "admin", "manager_id": None, "is_active": True}


def _user(role: str) -> dict[str, object]:
    return {"id": 2, "login": role, "role": role, "manager_id": "10", "is_active": True}


class AiSpendProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {DIR_ENV: str(self.dir)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_summary_sums_several_events_and_groups_by_day(self) -> None:
        yesterday = NOW.replace(day=8, hour=11)
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=12.5,
            estimated_cost_usd=0.16,
            entity_type="deal",
            entity_id="18421",
            model="gpt-5.6-terra",
            now=yesterday,
        )
        record_paid_call(
            kind="deal_manager_quick_help_push",
            estimated_cost_rub=1.82,
            estimated_cost_usd=0.024,
            entity_type="deal",
            entity_id="18421",
            model="gpt-5.6-luna",
            now=NOW,
        )
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=31.4,
            estimated_cost_usd=0.42,
            entity_type="deal",
            entity_id="19003",
            model="gpt-5.6-terra",
            now=NOW,
        )
        summary = build_ai_spend_summary(now=NOW)
        self.assertEqual(summary["today"]["paid_calls"], 2)
        self.assertEqual(summary["today"]["estimated_cost_rub"], 33.22)
        self.assertEqual(summary["today"]["estimated_cost_rub_label"], "~33,22 ₽")
        self.assertEqual(summary["last_7_days"]["estimated_cost_rub"], 45.72)
        self.assertEqual(summary["last_30_days"]["estimated_cost_rub"], 45.72)
        self.assertEqual([item["date"] for item in summary["days"]], ["2026-09-09", "2026-09-08"])
        self.assertEqual(summary["days"][0]["label"], "09 сентября")
        self.assertEqual(summary["days"][1]["paid_calls"], 1)
        self.assertIn("не счёт OpenAI", summary["disclaimer"])

    def test_llm_and_transcription_are_both_counted(self) -> None:
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=20,
            estimated_cost_usd=0.27,
            entity_type="deal",
            entity_id="18421",
            model="gpt-5.6-terra",
            now=NOW,
        )
        record_paid_call(
            kind="transcription",
            estimated_cost_rub=4.16,
            estimated_cost_usd=0.055,
            entity_type="deal",
            entity_id="18421",
            model="gpt-4o-mini-transcribe",
            duration_seconds=83.2,
            now=NOW,
        )
        day = build_ai_spend_day(TODAY)
        kinds = {item["kind"] for item in day["events"]}
        self.assertEqual(kinds, {"full_deal_analysis", "transcription"})
        self.assertEqual(day["paid_calls"], 2)
        self.assertEqual(day["estimated_cost_rub"], 24.16)
        transcribe = next(item for item in day["events"] if item["kind"] == "transcription")
        self.assertEqual(transcribe["kind_label"], "Транскрибация")
        self.assertEqual(transcribe["duration_seconds"], 83.2)
        self.assertEqual(transcribe["model_label"], "GPT-4o Mini Transcribe")

    def test_event_without_cost_is_listed_but_not_summed(self) -> None:
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=10,
            estimated_cost_usd=0.13,
            entity_type="deal",
            entity_id="1",
            now=NOW,
        )
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=None,
            estimated_cost_usd=None,
            entity_type="deal",
            entity_id="2",
            model="unknown-model",
            now=NOW,
        )
        day = build_ai_spend_day(TODAY)
        self.assertEqual(len(day["events"]), 2)
        self.assertEqual(day["paid_calls"], 1)
        self.assertEqual(day["estimated_cost_rub"], 10.0)
        unpaid = next(item for item in day["events"] if item["entity_id"] == "2")
        self.assertEqual(unpaid["estimated_cost_rub_label"], "оценка недоступна")

    def test_broken_jsonl_line_does_not_break_the_day(self) -> None:
        record_paid_call(
            kind="deal_manager_quick_help_push",
            estimated_cost_rub=1.5,
            entity_type="deal",
            entity_id="18421",
            now=NOW,
        )
        path = self.dir / "2026-09-09.events.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8") + "{not-json\n" + '{"kind":"oops"}\n',
            encoding="utf-8",
        )
        day = build_ai_spend_day(TODAY)
        self.assertEqual(day["skipped_lines"], 1)
        self.assertEqual(day["paid_calls"], 1)
        self.assertEqual(day["estimated_cost_rub"], 1.5)

    def test_missing_logs_return_zero_totals(self) -> None:
        summary = build_ai_spend_summary(now=NOW)
        self.assertEqual(summary["today"]["estimated_cost_rub"], 0.0)
        self.assertEqual(summary["today"]["estimated_cost_rub_label"], "~0 ₽")
        self.assertEqual(summary["today"]["paid_calls"], 0)
        self.assertEqual(summary["days"], [])
        day = build_ai_spend_day(TODAY)
        self.assertEqual(day["events"], [])
        self.assertEqual(day["skipped_lines"], 0)

    def test_usage_trace_is_not_double_counted(self) -> None:
        usage_path = self.dir / "usage.jsonl"
        daily_dir = self.dir / "usage_daily"
        with patch.dict(
            os.environ,
            {
                "OPENAI_USAGE_TRACE_PATH": str(usage_path),
                "OPENAI_USAGE_DAILY_DIR": str(daily_dir),
            },
        ):
            append_usage_trace(
                {
                    "requested_at": NOW.isoformat(),
                    "call_type": "full_deal_analysis",
                    "model": "gpt-5.6-terra",
                    "estimated_cost_rub": 31.4,
                    "estimated_cost_usd": 0.4187,
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 50},
                        "output_tokens_details": {"reasoning_tokens": 80},
                    },
                },
                entity_type="deal",
                entity_id="18421",
            )
        summary = build_ai_spend_summary(now=NOW)
        self.assertEqual(summary["today"]["paid_calls"], 1)
        self.assertEqual(summary["today"]["estimated_cost_rub"], 31.4)
        day = build_ai_spend_day(TODAY)
        event = day["events"][0]
        self.assertEqual(event["kind_label"], "Полный анализ")
        self.assertEqual(event["model_label"], "GPT-5.6 Terra")
        self.assertEqual(event["input_tokens"], 1000)
        self.assertEqual(event["cached_input_tokens"], 400)
        self.assertEqual(event["cache_write_tokens"], 50)
        self.assertEqual(event["output_tokens"], 200)
        self.assertEqual(event["reasoning_tokens"], 80)
        self.assertEqual(event["cache_hit_percent"], 40.0)

    def test_batch_jsonl_is_not_added_on_top_of_daily_events(self) -> None:
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=10,
            entity_type="deal",
            entity_id="1",
            now=NOW,
        )
        batch_dir = self.dir / "batches"
        batch_dir.mkdir()
        (batch_dir / "extra.jsonl").write_text(
            json.dumps({"kind": "full_deal_analysis", "estimated_cost_rub": 99, "at": NOW.isoformat()}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary = build_ai_spend_summary(now=NOW)
        self.assertEqual(summary["today"]["paid_calls"], 1)
        self.assertEqual(summary["today"]["estimated_cost_rub"], 10.0)

    def test_human_labels_and_rub_format(self) -> None:
        self.assertEqual(display_kind_label("full_deal_analysis"), "Полный анализ")
        self.assertEqual(display_kind_label("deal_manager_quick_help_push"), "Quick Help")
        self.assertEqual(model_label("gpt-5.6-terra"), "GPT-5.6 Terra")
        self.assertEqual(format_rub_ui(1940), "~1 940 ₽")
        self.assertEqual(format_rub_ui(31.4), "~31,40 ₽")
        self.assertEqual(paid_calls_label(1), "1 платный вызов")
        self.assertEqual(paid_calls_label(37), "37 платных вызовов")


class AiSpendApiAccessTests(unittest.TestCase):
    def test_admin_receives_summary(self) -> None:
        from api import app as api_app

        payload = {"today": {"estimated_cost_rub": 12}}
        with patch.object(api_app, "auth_current_user", return_value=_admin()), patch.object(
            api_app, "build_ai_spend_summary", return_value=payload,
        ):
            self.assertEqual(api_app.ai_spend_summary_get(), payload)

    def test_rop_gets_403(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value=_user("rop")):
            with self.assertRaises(HTTPException) as raised:
                api_app.ai_spend_summary_get()
        self.assertEqual(raised.exception.status_code, 403)

        with patch.object(api_app, "auth_current_user", return_value=_user("rop")):
            with self.assertRaises(HTTPException) as raised:
                api_app.ai_spend_day_get(date_=TODAY)
        self.assertEqual(raised.exception.status_code, 403)

    def test_manager_gets_403(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "auth_current_user", return_value=_user("manager")):
            with self.assertRaises(HTTPException) as raised:
                api_app.ai_spend_summary_get()
        self.assertEqual(raised.exception.status_code, 403)

        with patch.object(api_app, "auth_current_user", return_value=_user("manager")):
            with self.assertRaises(HTTPException) as raised:
                api_app.ai_spend_day_get(date_=TODAY)
        self.assertEqual(raised.exception.status_code, 403)

    def test_http_endpoints_require_admin_and_hide_prompts(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "authenticate_request", return_value=_user("manager")):
            forbidden = TestClient(api_app.app).get("/api/admin/ai-spend/summary")
        self.assertEqual(forbidden.status_code, 403)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {DIR_ENV: directory}, clear=False,
        ), patch.object(api_app, "authenticate_request", return_value=_admin()):
            record_paid_call(
                kind="full_deal_analysis",
                estimated_cost_rub=8,
                entity_type="deal",
                entity_id="18421",
                now=NOW,
            )
            response = TestClient(api_app.app).get("/api/admin/ai-spend/day?date=2026-09-09")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("prompt", dumped)
        self.assertNotIn("response", dumped)
        self.assertEqual(body["events"][0]["kind_label"], "Полный анализ")


if __name__ == "__main__":
    unittest.main()
