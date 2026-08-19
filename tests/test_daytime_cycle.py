from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api.daytime_cycle import (
    next_scheduled_at,
    run_daytime_cycle,
    slot_times_for_day,
    start_daytime_cycle,
    stop_daytime_cycle,
)
from setup import MSK_TZ
from storage.rop_db import (
    init_db,
    save_analysis_run,
    save_deal_control_scope,
    upsert_deal_control_deal,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=MSK_TZ)


def _scope(db_path: Path) -> None:
    save_deal_control_scope(
        db_path,
        initial_deal_ids=["101"],
        manager_ids=["10"],
        pipeline_id="15",
    )
    upsert_deal_control_deal(
        db_path,
        deal_id="101",
        source="initial",
        title="Сделка",
        manager_id="10",
        manager_name="Менеджер",
        stage_id="NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="100",
        currency_id="RUB",
        created_at_crm=NOW.isoformat(),
        modified_at_crm=NOW.isoformat(),
        is_active=True,
    )


class DaytimeCycleScheduleTests(unittest.TestCase):
    def test_slots_cover_weekday_hours_and_planning_prep_once(self) -> None:
        pairs = [(item.hour, item.minute) for item in slot_times_for_day()]
        self.assertEqual(pairs[0], (8, 0))
        self.assertEqual(pairs[-1], (18, 0))
        self.assertIn((8, 0), pairs)
        self.assertIn((8, 30), pairs)
        self.assertIn((15, 50), pairs)
        self.assertIn((18, 0), pairs)
        self.assertNotIn((7, 30), pairs)
        self.assertNotIn((0, 0), pairs)
        self.assertNotIn((18, 30), pairs)
        self.assertEqual(pairs.count((15, 50)), 1)

    def test_next_slot_uses_1550_before_planning(self) -> None:
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 15, 40, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 15, 50, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 15, 50, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 15, 30, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 15, 50, tzinfo=MSK_TZ),
        )

    def test_next_slot_stays_on_weekdays_between_eight_and_eighteen(self) -> None:
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 7, 50, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 8, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 18, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 19, 8, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 21, 18, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 8, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 22, 12, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 8, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 23, 21, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 8, 0, tzinfo=MSK_TZ),
        )


class DaytimeCycleRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cycle.sqlite"
        init_db(self.db_path)
        _scope(self.db_path)
        self.spend_env = patch.dict(
            os.environ,
            {"SPEND_DIARY_DIR": str(Path(self.temp.name) / "spend")},
            clear=False,
        )
        self.spend_env.start()

    def tearDown(self) -> None:
        stop_daytime_cycle()
        self.spend_env.stop()
        self.temp.cleanup()

    def test_cycle_goes_sync_then_trajectory_then_change_detection_without_force_llm(self) -> None:
        calls: list[str] = []

        def refresh(**_kwargs):
            calls.append("sync")
            return {"sync_message": "CRM обновлена", "sync_errors": []}

        def collect(*_args, **_kwargs):
            calls.append("trajectory")
            return {"status": "success", "counts": {"activities": 2, "stage_changes": 1}, "errors": {}}

        def analyze(**kwargs):
            calls.append("analyze")
            self.assertEqual(kwargs["deal_ids"], ["101"])
            return {
                "status": "done",
                "job_id": "job-1",
                "busy_ids": [],
                "counts": {"checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0},
            }

        result = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW,
            trigger="interval_30m",
            refresh_fn=refresh,
            collect_fn=collect,
            analyze_fn=analyze,
            make_client_fn=lambda: object(),
        )
        self.assertEqual(calls, ["sync", "trajectory", "analyze"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["decisions"]["skip"], 1)
        self.assertEqual(result["decisions"]["full"], 0)
        diary = (Path(self.temp.name) / "spend" / f"{NOW.date().isoformat()}.txt").read_text(encoding="utf-8")
        self.assertIn("1 — без изменений, LLM не вызывался", diary)
        self.assertIn("За этот запуск: ~0 ₽", diary)

    def test_error_in_sync_does_not_block_trajectory_or_next_logic(self) -> None:
        calls: list[str] = []

        def refresh(**_kwargs):
            calls.append("sync")
            raise RuntimeError("bitrix down")

        def collect(*_args, **_kwargs):
            calls.append("trajectory")
            return {"status": "success", "counts": {"activities": 0, "stage_changes": 0}, "errors": {}}

        def analyze(**_kwargs):
            calls.append("analyze")
            return {
                "status": "done",
                "counts": {"checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0},
            }

        result = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW,
            refresh_fn=refresh,
            collect_fn=collect,
            analyze_fn=analyze,
            make_client_fn=lambda: object(),
        )
        self.assertEqual(calls, ["sync", "trajectory", "analyze"])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any("Bitrix sync" in item for item in result["errors"]))

    def test_overlapping_runs_are_skipped(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_refresh(**_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return {"sync_message": "CRM обновлена", "sync_errors": []}

        def noop_collect(*_args, **_kwargs):
            return {"status": "success", "counts": {}, "errors": {}}

        def noop_analyze(**_kwargs):
            return {"status": "done", "counts": {"checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0}}

        first_result: dict = {}

        def first_run() -> None:
            first_result.update(
                run_daytime_cycle(
                    db_path=self.db_path,
                    now=NOW,
                    refresh_fn=slow_refresh,
                    collect_fn=noop_collect,
                    analyze_fn=noop_analyze,
                    make_client_fn=lambda: object(),
                )
            )

        worker = threading.Thread(target=first_run)
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        second = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW,
            refresh_fn=lambda **_kwargs: self.fail("second run must not sync"),
            collect_fn=lambda *_args, **_kwargs: self.fail("second run must not collect"),
            analyze_fn=lambda **_kwargs: self.fail("second run must not analyze"),
        )
        self.assertEqual(second["status"], "skipped_locked")
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_result.get("status"), "success")

    def test_unconfigured_scope_does_not_call_bitrix(self) -> None:
        empty = Path(self.temp.name) / "empty.sqlite"
        init_db(empty)
        called = []
        result = run_daytime_cycle(
            db_path=empty,
            now=NOW,
            refresh_fn=lambda **_kwargs: called.append("sync"),
            collect_fn=lambda *_args, **_kwargs: called.append("collect"),
            analyze_fn=lambda **_kwargs: called.append("analyze"),
        )
        self.assertEqual(result["status"], "skipped_unconfigured")
        self.assertEqual(called, [])

    def test_scheduler_survives_a_failed_tick(self) -> None:
        due = datetime.now(MSK_TZ) + timedelta(milliseconds=50)
        later = datetime.now(MSK_TZ) + timedelta(hours=2)
        calls = {"count": 0}

        def boom(**_kwargs):
            calls["count"] += 1
            raise RuntimeError("tick failed")

        with patch("api.daytime_cycle.next_scheduled_at", side_effect=[due, later, later]), \
             patch("api.daytime_cycle.run_daytime_cycle", side_effect=boom), \
             patch("api.daytime_cycle.logger.exception"), \
             patch.dict("os.environ", {"DAYTIME_CYCLE_ENABLED": "true"}):
            start_daytime_cycle()
            deadline = datetime.now(MSK_TZ) + timedelta(seconds=2)
            while calls["count"] < 1 and datetime.now(MSK_TZ) < deadline:
                threading.Event().wait(0.02)
            stop_daytime_cycle(timeout=1)
            self.assertGreaterEqual(calls["count"], 1)

    def test_analyze_work_pool_does_not_force_llm(self) -> None:
        from api.daytime_cycle import _analyze_work_pool
        from api.jobs import AnalyzeOptions

        with patch("api.jobs.busy_analyze_entity_ids", return_value=set()), \
             patch("api.jobs.start_analyze_job") as start, \
             patch("api.jobs.wait_for_job", return_value={"status": "done"}), \
             patch("api.daytime_cycle._summarize_decisions", return_value={
                 "checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0,
             }):
            start.return_value = {"job_id": "abc"}
            payload = _analyze_work_pool(
                db_path=self.db_path,
                deal_ids=["101"],
                started_at=NOW.isoformat(),
            )
        options = start.call_args.args[0]
        self.assertIsInstance(options, AnalyzeOptions)
        self.assertFalse(options.force_llm)
        self.assertTrue(options.analyze)
        self.assertIsNotNone(options.automatic_analysis_run_id)
        self.assertTrue(options.extra_env and "SPEND_DIARY_BATCH_PATH" in options.extra_env)
        self.assertEqual(payload["job_id"], "abc")

    def test_analyze_work_pool_skips_deals_with_running_jobs(self) -> None:
        from api.daytime_cycle import _analyze_work_pool

        with patch("api.jobs.busy_analyze_entity_ids", return_value={"101"}), \
             patch("api.jobs.start_analyze_job") as start:
            payload = _analyze_work_pool(
                db_path=self.db_path,
                deal_ids=["101"],
                started_at=NOW.isoformat(),
            )
        start.assert_not_called()
        self.assertEqual(payload["status"], "skipped_busy")
        self.assertEqual(payload["busy_ids"], ["101"])

    def test_locked_cycle_does_not_create_a_new_automatic_run(self) -> None:
        from storage.rop_db import get_latest_automatic_analysis_run

        started = threading.Event()
        release = threading.Event()

        def slow_refresh(**_kwargs):
            started.set()
            self.assertTrue(release.wait(timeout=2))
            return {"sync_message": "CRM обновлена", "sync_errors": []}

        def noop_collect(*_args, **_kwargs):
            return {"status": "success", "counts": {}, "errors": {}}

        def noop_analyze(**_kwargs):
            return {"status": "done", "counts": {"checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0}}

        worker = threading.Thread(
            target=lambda: run_daytime_cycle(
                db_path=self.db_path,
                now=NOW,
                refresh_fn=slow_refresh,
                collect_fn=noop_collect,
                analyze_fn=noop_analyze,
                make_client_fn=lambda: object(),
            )
        )
        worker.start()
        self.assertTrue(started.wait(timeout=2))
        second = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW,
            refresh_fn=lambda **_kwargs: self.fail("second run must not sync"),
            collect_fn=lambda *_args, **_kwargs: self.fail("second run must not collect"),
            analyze_fn=lambda **_kwargs: self.fail("second run must not analyze"),
        )
        self.assertEqual(second["status"], "skipped_locked")
        self.assertIsNone(get_latest_automatic_analysis_run(self.db_path))
        release.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_decision_summary_uses_latest_run_and_does_not_treat_skip_as_full(self) -> None:
        from api.daytime_cycle import _summarize_decisions

        save_analysis_run(
            self.db_path,
            entity_type="deal",
            entity_id="101",
            status="FULL_LLM_ANALYSIS",
            fingerprint="old",
        )
        save_analysis_run(
            self.db_path,
            entity_type="deal",
            entity_id="101",
            status="SKIPPED_NO_CHANGES",
            fingerprint="new",
        )
        counts = _summarize_decisions(
            db_path=self.db_path,
            deal_ids=["101", "202"],
            created_at_from=(NOW - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(counts["checked"], 2)
        self.assertEqual(counts["full"], 0)
        self.assertEqual(counts["skip"], 1)
        self.assertEqual(counts["error"], 1)
        self.assertEqual(counts["changed"], 0)


class JobWaitTests(unittest.TestCase):
    def tearDown(self) -> None:
        from api import jobs
        with jobs._LOCK:
            jobs._JOBS.clear()

    def test_wait_for_job_returns_finished_status(self) -> None:
        from api.jobs import JobState, _JOBS, _LOCK, wait_for_job

        job = JobState(job_id="wait-1", status="running")
        with _LOCK:
            _JOBS["wait-1"] = job

        def finish() -> None:
            threading.Event().wait(0.05)
            with _LOCK:
                _JOBS["wait-1"].status = "done"

        threading.Thread(target=finish).start()
        result = wait_for_job("wait-1", timeout_seconds=1, poll_seconds=0.02)
        self.assertEqual(result["status"], "done")


if __name__ == "__main__":
    unittest.main()
