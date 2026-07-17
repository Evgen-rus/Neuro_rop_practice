from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from api.app import DailySummaryStartRequest, daily_summary_start


class DailySummaryStartTests(unittest.TestCase):
    def test_frontend_separates_new_and_backlog_sections(self) -> None:
        frontend_source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

        self.assertIn("Новые случаи", frontend_source)
        self.assertIn("Накопившиеся и вернувшиеся в контроль", frontend_source)
        self.assertIn("candidate.lifecycle === 'reactivation' ? 'вернулся в контроль'", frontend_source)

    def test_fresh_card_reuses_report_without_starting_pipeline_or_llm(self) -> None:
        run = {
            "id": 7,
            "status": "draft",
            "llm_allowed_count": 0,
            "items": [
                {
                    "selected": True,
                    "journey_key": "lead:42",
                    "entity_type": "lead",
                    "entity_id": "42",
                    "candidate": {"analysis_freshness": "fresh"},
                }
            ],
            "profile_snapshot": {"analysis": {}},
        }
        with (
            patch("api.app.get_daily_summary_run", return_value=run),
            patch("api.app.prepare_daily_summary_items") as prepare_items,
            patch("api.app.get_latest_ui_report", return_value={"id": 17}) as latest_report,
            patch("api.app.complete_daily_summary_item") as complete_item,
            patch("api.app.start_analyze_job") as start_job,
            patch("api.app.attach_job_to_daily_summary") as attach_job,
        ):
            result = daily_summary_start(7, DailySummaryStartRequest(confirm_paid=False))

        prepare_items.assert_called_once_with(ANY, 7, ["lead:42"])
        latest_report.assert_called_once_with(ANY, entity_type="lead", entity_id="42")
        complete_item.assert_called_once_with(
            ANY,
            7,
            entity_type="lead",
            entity_id="42",
            report_id=17,
        )
        start_job.assert_not_called()
        attach_job.assert_not_called()
        self.assertEqual(result["started_count"], 1)
        self.assertEqual(result["reused_count"], 1)
        self.assertEqual(result["jobs"], [])


if __name__ == "__main__":
    unittest.main()
