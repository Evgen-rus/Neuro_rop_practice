from __future__ import annotations

import json
import io
import unittest
from contextlib import redirect_stdout

from api.jobs import JobState, _apply_progress_event, parse_progress_event
from progress_events import PROGRESS_PREFIX, emit_progress


class JobProgressEventTests(unittest.TestCase):
    def test_parses_machine_progress_event(self) -> None:
        payload = {
            "entity_type": "lead",
            "entity_id": "42",
            "stage": "transcription",
            "status": "running",
            "detail": "Сегмент 2 из 5",
            "current": 2,
            "total": 5,
            "attempt": 1,
            "max_attempts": 3,
            "updated_at": "2026-07-17T12:00:00+03:00",
        }
        event = parse_progress_event(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False))
        self.assertEqual(event, payload)

    def test_ignores_malformed_or_non_entity_event(self) -> None:
        self.assertIsNone(parse_progress_event(PROGRESS_PREFIX + "not-json"))
        self.assertIsNone(parse_progress_event(PROGRESS_PREFIX + '{"stage":"crm_context"}'))

    def test_updates_one_entity_without_touching_another(self) -> None:
        job = JobState(job_id="job")
        job.entity_progress["lead:2"] = {"stage": "queued"}
        _apply_progress_event(
            job,
            {
                "entity_type": "lead",
                "entity_id": "1",
                "stage": "llm_analysis",
                "status": "running",
                "updated_at": "2026-07-17T12:00:00+03:00",
            },
        )
        self.assertEqual(job.entity_progress["lead:1"]["stage"], "llm_analysis")
        self.assertEqual(job.entity_progress["lead:2"]["stage"], "queued")

    def test_emitted_progress_is_ascii_safe_and_restores_unicode(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            emit_progress("lead", "42", "crm_context", detail="\u0421\u043e\u0431\u0438\u0440\u0430\u0435\u0442 CRM")

        line = output.getvalue().strip()
        self.assertTrue(line.isascii())
        self.assertIn("\\u0421", line)
        self.assertEqual(parse_progress_event(line)["detail"], "\u0421\u043e\u0431\u0438\u0440\u0430\u0435\u0442 CRM")


if __name__ == "__main__":
    unittest.main()
