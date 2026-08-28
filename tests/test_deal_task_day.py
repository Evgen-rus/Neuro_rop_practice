from __future__ import annotations

import unittest
from datetime import datetime

from api.deal_task_day import is_reschedule, task_results, task_totals
from setup import MSK_TZ


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ)


class DealTaskDayTests(unittest.TestCase):
    def test_first_deadline_is_not_a_reschedule(self) -> None:
        self.assertFalse(is_reschedule({"field": "DEADLINE", "to_value": "2026-08-19T18:00:00+03:00"}))
        self.assertTrue(is_reschedule({
            "field": "DEADLINE",
            "from_value": "2026-08-18T18:00:00+03:00",
            "to_value": "2026-08-19T18:00:00+03:00",
        }))

    def test_reschedule_then_completion_keeps_both_facts(self) -> None:
        tasks = [{
            "task_id": "9", "activity_id": "a9", "subject": "Согласовать КП",
            "deadline": "2026-08-19T18:00:00+03:00", "completed": True,
            "bitrix_completed_at": NOW.isoformat(),
        }]
        events = [
            {
                "id": 1, "event_type": "crm_task_history_observed",
                "occurred_at": "2026-08-18T12:00:00+03:00",
                "payload": {
                    "task_id": "9", "field": "DEADLINE",
                    "from_value": "2026-08-18T18:00:00+03:00",
                    "to_value": "2026-08-19T18:00:00+03:00",
                },
            },
            {
                "id": 2, "event_type": "crm_task_history_observed",
                "occurred_at": NOW.isoformat(),
                "payload": {"task_id": "9", "field": "STATUS", "to_value": "5"},
            },
        ]
        rows = task_results(tasks, events, NOW)
        self.assertEqual(len(rows), 1)
        task = rows[0]
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task["completed_today"])
        self.assertTrue(task["was_due"])
        self.assertEqual(len(task["reschedules"]), 1)
        self.assertEqual(task["reschedules"][0]["to_deadline"], "2026-08-19T18:00:00+03:00")
        self.assertEqual(task_totals([{"task_results": rows}]), {"tasks_completed": 1, "tasks_rescheduled": 1})


if __name__ == "__main__":
    unittest.main()
