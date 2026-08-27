from __future__ import annotations

import unittest
from datetime import date

from scripts.bitrix_usage_report import build_report, render_table


class BitrixUsageReportTests(unittest.TestCase):
    def test_builds_privacy_safe_method_aggregate(self) -> None:
        report = build_report(
            [
                {
                    "run_id": "one",
                    "method": "crm.activity.list",
                    "attempt": 1,
                    "duration_ms": 125.5,
                    "ok": True,
                    "empty": False,
                },
                {
                    "run_id": "one",
                    "method": "crm.activity.list",
                    "attempt": 2,
                    "duration_ms": 10,
                    "ok": False,
                    "empty": True,
                },
                {
                    "run_id": "two",
                    "method": "batch",
                    "component": "manager_trajectory_timeline",
                    "attempt": 1,
                    "duration_ms": 20,
                    "ok": True,
                    "empty": False,
                    "batch_cmd_count": 4,
                    "batch_cmd_methods": ["crm.timeline.comment.list"],
                },
            ],
            start=date(2026, 8, 22),
            end=date(2026, 8, 22),
        )
        self.assertEqual(report["physical_attempts"], 3)
        self.assertEqual(report["physical_http"], 3)
        self.assertEqual(report["logical_commands"], 6)
        self.assertEqual(report["batch_http"], 1)
        self.assertEqual(report["batch_cmds"], 4)
        self.assertEqual(report["run_count"], 2)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["retries"], 1)
        self.assertEqual(report["methods"][0]["method"], "crm.activity.list")
        self.assertEqual(report["methods"][0]["physical_http"], 2)
        self.assertIn("Физических попыток: 3", render_table(report))
        filtered = build_report(
            [
                {
                    "run_id": "one",
                    "method": "crm.activity.list",
                    "component": "deal_control",
                    "attempt": 1,
                    "duration_ms": 1,
                    "ok": True,
                    "empty": False,
                    "item_count": 5,
                },
                {
                    "run_id": "two",
                    "method": "crm.deal.list",
                    "component": "deal_control",
                    "attempt": 1,
                    "duration_ms": 1,
                    "ok": True,
                    "empty": False,
                    "item_count": 9,
                },
            ],
            start=date(2026, 8, 22),
            end=date(2026, 8, 22),
            run_id="two",
        )
        self.assertEqual(filtered["physical_http"], 1)
        self.assertEqual(filtered["item_count"], 9)
        self.assertEqual(filtered["components"][0]["component"], "deal_control")


if __name__ == "__main__":
    unittest.main()
