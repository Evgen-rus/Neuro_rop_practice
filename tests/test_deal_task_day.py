from __future__ import annotations

import unittest
from datetime import datetime

from api.deal_task_day import is_reschedule, stamp, task_results, task_totals
from setup import MSK_TZ


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ)
UNIX_FROM = "1787896800"  # 2026-08-28T09:00:00+03:00
UNIX_TO = "1788242400"  # 2026-09-01T09:00:00+03:00


class DealTaskDayTests(unittest.TestCase):
    def test_first_deadline_is_not_a_reschedule(self) -> None:
        self.assertFalse(is_reschedule({"field": "DEADLINE", "to_value": "2026-08-19T18:00:00+03:00"}))
        self.assertTrue(is_reschedule({
            "field": "DEADLINE",
            "from_value": "2026-08-18T18:00:00+03:00",
            "to_value": "2026-08-19T18:00:00+03:00",
        }))

    def test_stamp_keeps_iso_and_dotted_dates(self) -> None:
        self.assertEqual(stamp("2026-08-18T18:00:00+03:00"), datetime(2026, 8, 18, 18, 0, tzinfo=MSK_TZ))
        self.assertEqual(stamp("18.08.2026"), datetime(2026, 8, 18, 0, 0, tzinfo=MSK_TZ))
        self.assertEqual(stamp("18.08.2026 18:00"), datetime(2026, 8, 18, 18, 0, tzinfo=MSK_TZ))
        self.assertIsNone(stamp("5"))
        self.assertIsNone(stamp("not-a-date"))

    def test_stamp_parses_unix_seconds_and_milliseconds(self) -> None:
        expected = datetime(2026, 8, 28, 9, 0, tzinfo=MSK_TZ)
        self.assertEqual(stamp(UNIX_FROM), expected)
        self.assertEqual(stamp(int(UNIX_FROM)), expected)
        self.assertEqual(stamp(int(UNIX_FROM) * 1000), expected)
        self.assertEqual(stamp(str(int(UNIX_FROM) * 1000)), expected)
        self.assertEqual(stamp(UNIX_TO), datetime(2026, 9, 1, 9, 0, tzinfo=MSK_TZ))

    def test_production_unix_deadline_is_a_reschedule(self) -> None:
        self.assertTrue(is_reschedule({
            "field": "DEADLINE",
            "from_value": UNIX_FROM,
            "to_value": UNIX_TO,
        }))
        self.assertFalse(is_reschedule({"field": "DEADLINE", "to_value": UNIX_TO}))
        self.assertFalse(is_reschedule({
            "field": "STATUS",
            "from_value": UNIX_FROM,
            "to_value": UNIX_TO,
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

    def test_unix_reschedule_restores_was_due_when_crm_deadline_is_already_future(self) -> None:
        cutoff = datetime(2026, 8, 28, 15, 45, tzinfo=MSK_TZ)
        tasks = [{
            "task_id": "9", "deadline": "2026-09-01T09:00:00+03:00",
            "completed": False, "time_bucket": "future",
        }]
        events = [{
            "id": 1, "event_type": "crm_task_history_observed",
            "occurred_at": "2026-08-28T12:00:00+03:00",
            "payload": {"task_id": "9", "field": "DEADLINE", "from_value": UNIX_FROM, "to_value": UNIX_TO},
        }]
        rows = task_results(tasks, events, cutoff)
        self.assertEqual(len(rows), 1)
        task = rows[0]
        self.assertTrue(task["was_due"])
        self.assertEqual(len(task["reschedules"]), 1)
        self.assertEqual(task["reschedules"][0]["from_deadline"], UNIX_FROM)
        self.assertEqual(task["reschedules"][0]["to_deadline"], UNIX_TO)
        self.assertEqual(task_totals([{"task_results": rows}]), {"tasks_completed": 0, "tasks_rescheduled": 1})

    def test_event_recorded_just_after_cutoff_is_kept_when_known_until_covers_collect(self) -> None:
        cutoff = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        collected = datetime(2026, 8, 18, 15, 46, tzinfo=MSK_TZ)
        events = [{
            "id": 1, "event_type": "crm_task_history_observed",
            "occurred_at": "2026-08-18T15:30:00+03:00",
            "recorded_at": collected.isoformat(),
            "payload": {
                "task_id": "9", "field": "DEADLINE",
                "from_value": "2026-08-18T18:00:00+03:00",
                "to_value": "2026-08-19T18:00:00+03:00",
            },
        }]
        tasks = [{"task_id": "9", "deadline": "2026-08-19T18:00:00+03:00", "completed": False}]
        dropped = task_results(tasks, events, cutoff)
        self.assertEqual(dropped[0]["reschedules"], [])
        kept = task_results(tasks, events, cutoff, recorded_through=collected)
        self.assertEqual(len(kept[0]["reschedules"]), 1)
        self.assertTrue(kept[0]["was_due"])


if __name__ == "__main__":
    unittest.main()
