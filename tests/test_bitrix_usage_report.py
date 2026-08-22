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
                    "attempt": 1,
                    "duration_ms": 20,
                    "ok": True,
                    "empty": False,
                },
            ],
            start=date(2026, 8, 22),
            end=date(2026, 8, 22),
        )
        self.assertEqual(report["physical_attempts"], 3)
        self.assertEqual(report["run_count"], 2)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["retries"], 1)
        self.assertEqual(report["methods"][0]["method"], "crm.activity.list")
        self.assertIn("Физических попыток: 3", render_table(report))


if __name__ == "__main__":
    unittest.main()
