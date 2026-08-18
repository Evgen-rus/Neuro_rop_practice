from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.usage_trace import append_usage_trace
from openai_api.spend_diary import (
    BATCH_ENV,
    DIR_ENV,
    day_total_rub,
    format_rub,
    human_diary_path,
    kind_label,
    record_paid_call,
    render_cycle_text,
    write_cycle_block,
)
from setup import MSK_TZ


NOW = datetime(2026, 8, 18, 15, 50, tzinfo=MSK_TZ)


class SpendDiaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.env = patch.dict(os.environ, {DIR_ENV: str(self.dir), BATCH_ENV: ""}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_adhoc_line_is_brief_and_has_running_total(self) -> None:
        record_paid_call(
            kind="deal_manager_quick_help_push",
            estimated_cost_rub=3.5,
            estimated_cost_usd=0.0467,
            entity_type="deal",
            entity_id="18507",
            now=NOW,
        )
        text = human_diary_path(NOW).read_text(encoding="utf-8")
        self.assertIn("Дневник трат OpenAI", text)
        self.assertIn("это не счёт OpenAI", text)
        self.assertIn("15:50  Quick Help · сделка 18507 · ~3.50 ₽  (сегодня ~3.50 ₽)", text)

    def test_entity_id_newlines_and_prompt_text_are_not_written(self) -> None:
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=12,
            entity_type="deal",
            entity_id="18507\nсекрет клиента",
            now=NOW,
        )
        text = human_diary_path(NOW).read_text(encoding="utf-8")
        events = (self.dir / "2026-08-18.events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("секрет клиента", text)
        self.assertNotIn("секрет клиента", events)
        self.assertNotIn("\n18507", text)

    def test_batch_env_does_not_write_adhoc_line(self) -> None:
        batch = self.dir / "batch.jsonl"
        with patch.dict(os.environ, {BATCH_ENV: str(batch)}):
            record_paid_call(
                kind="full_deal_analysis",
                estimated_cost_rub=12,
                entity_type="deal",
                entity_id="18507",
                now=NOW,
            )
        self.assertFalse(human_diary_path(NOW).exists())
        payload = json.loads(batch.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["entity_id"], "18507")
        self.assertEqual(payload["estimated_cost_rub"], 12.0)

    def test_cycle_block_matches_brief_format_and_skip_is_one_line(self) -> None:
        events = [
            {"kind": "full_deal_analysis", "entity_id": "18507", "estimated_cost_rub": 12},
            {"kind": "full_deal_analysis", "entity_id": "18412", "estimated_cost_rub": 9},
        ]
        text = render_cycle_text(
            started=NOW,
            counts={
                "checked": 24,
                "full": 2,
                "mini": 3,
                "skip": 19,
                "full_ids": ["18507", "18412"],
                "mini_ids": ["18301"],
            },
            events=events,
            today_rub=84,
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "18.08.2026 15:50 МСК",
                    "Проверено сделок: 24",
                    "FULL: 2 | MINI: 3 | без изменений: 19",
                    "",
                    "Сделка 18507 — полный анализ, ~12 ₽",
                    "Сделка 18412 — полный анализ, ~9 ₽",
                    "Сделка 18301 — мини, без LLM",
                    "Остальные 19 — без изменений, LLM не вызывался",
                    "",
                    "За этот запуск: ~21 ₽",
                    "За сегодня: ~84 ₽",
                    "",
                ]
            ),
        )

    def test_all_skip_is_still_one_line(self) -> None:
        text = render_cycle_text(
            started=NOW,
            counts={"checked": 24, "full": 0, "mini": 0, "skip": 24},
            events=[],
            today_rub=0,
        )
        self.assertIn("24 — без изменений, LLM не вызывался", text)
        self.assertNotIn("Остальные", text)

    def test_retries_are_summed_into_the_deal_line_and_day_total(self) -> None:
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=8,
            entity_type="deal",
            entity_id="18507",
            now=NOW,
        )
        record_paid_call(
            kind="full_deal_analysis",
            estimated_cost_rub=7,
            entity_type="deal",
            entity_id="18507",
            now=NOW,
        )
        self.assertEqual(day_total_rub(NOW), 15.0)
        text = render_cycle_text(
            started=NOW,
            counts={"checked": 1, "full": 1, "mini": 0, "skip": 0, "full_ids": ["18507"]},
            events=[
                {"kind": "full_deal_analysis", "entity_id": "18507", "estimated_cost_rub": 8},
                {"kind": "full_deal_analysis", "entity_id": "18507", "estimated_cost_rub": 7},
            ],
            today_rub=15,
        )
        self.assertIn("Сделка 18507 — полный анализ, ~15 ₽", text)
        self.assertIn("За этот запуск: ~15 ₽", text)

    def test_usage_trace_success_is_copied_into_the_diary(self) -> None:
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
                    "requested_at": "2026-08-18T12:50:00+00:00",
                    "call_type": "deal_manager_followups",
                    "model": "gpt-5.4",
                    "estimated_cost_rub": 2.25,
                    "estimated_cost_usd": 0.03,
                },
                entity_type="deal",
                entity_id="42",
            )
        diary = human_diary_path(NOW).read_text(encoding="utf-8")
        self.assertIn("фоллоуапы · сделка 42 · ~2.25 ₽", diary)

    def test_cycle_write_appends_to_the_same_day_file(self) -> None:
        write_cycle_block(
            started=NOW,
            counts={"checked": 2, "full": 0, "mini": 0, "skip": 2},
            events=[],
            now=NOW,
        )
        write_cycle_block(
            started=NOW.replace(hour=16),
            counts={"checked": 2, "full": 0, "mini": 0, "skip": 2},
            events=[],
            now=NOW.replace(hour=16),
        )
        text = human_diary_path(NOW).read_text(encoding="utf-8")
        self.assertEqual(text.count("Дневник трат OpenAI"), 1)
        self.assertEqual(text.count("Проверено сделок: 2"), 2)

    def test_kind_labels_cover_paid_helpers(self) -> None:
        self.assertEqual(kind_label("deal_manager_full_script_call"), "полный скрипт")
        self.assertEqual(kind_label("attention_delta_compact"), "compact")
        self.assertEqual(kind_label("transcription_voice"), "транскрибация голоса")
        self.assertEqual(format_rub(12.0), "~12 ₽")
        self.assertEqual(format_rub(3.5), "~3.50 ₽")


if __name__ == "__main__":
    unittest.main()
