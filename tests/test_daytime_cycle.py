from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
    list_automatic_analysis_items,
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
    def test_slots_cover_weekday_hours_evening_cycle_and_two_reports(self) -> None:
        pairs = [(item.hour, item.minute) for item in slot_times_for_day()]
        self.assertEqual(pairs[0], (7, 0))
        self.assertEqual(pairs[-1], (23, 0))
        self.assertIn((7, 0), pairs)
        self.assertIn((7, 30), pairs)
        self.assertIn((8, 0), pairs)
        self.assertIn((8, 30), pairs)
        self.assertIn((15, 30), pairs)
        self.assertIn((15, 45), pairs)
        self.assertIn((16, 0), pairs)
        self.assertIn((18, 0), pairs)
        self.assertIn((22, 0), pairs)
        self.assertIn((23, 0), pairs)
        self.assertNotIn((7, 55), pairs)
        self.assertNotIn((6, 30), pairs)
        self.assertNotIn((0, 0), pairs)
        self.assertNotIn((15, 50), pairs)
        self.assertNotIn((18, 30), pairs)
        self.assertEqual(pairs.count((15, 45)), 1)
        self.assertEqual(pairs.count((22, 0)), 1)
        self.assertEqual(pairs.count((23, 0)), 1)

    def test_next_slot_uses_1545_2200_and_2300(self) -> None:
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 15, 30, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 16, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 18, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 22, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 22, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 23, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 23, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 19, 7, 0, tzinfo=MSK_TZ),
        )

    def test_next_slot_stays_on_weekdays_and_keeps_evening_slots(self) -> None:
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 6, 50, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 7, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 7, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 7, 30, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 18, 7, 50, tzinfo=MSK_TZ)),
            datetime(2026, 8, 18, 8, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 21, 18, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 21, 22, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 21, 23, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 7, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 22, 12, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 7, 0, tzinfo=MSK_TZ),
        )
        self.assertEqual(
            next_scheduled_at(datetime(2026, 8, 23, 21, 0, tzinfo=MSK_TZ)),
            datetime(2026, 8, 24, 7, 0, tzinfo=MSK_TZ),
        )

    def test_publication_due_uses_moscow_day_and_catches_up_only_once(self) -> None:
        from api.daytime_cycle import DAY_END_REPORT_TIME, _planning_report_is_due

        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        self.assertFalse(_planning_report_is_due(due - timedelta(seconds=1), None))
        self.assertTrue(_planning_report_is_due(due, None))
        self.assertTrue(_planning_report_is_due(due.astimezone(timezone.utc), None))
        self.assertTrue(_planning_report_is_due(due.replace(hour=16), None))
        self.assertFalse(_planning_report_is_due(due, due.date()))
        self.assertFalse(_planning_report_is_due(due.replace(day=22), None))
        self.assertFalse(_planning_report_is_due(due.replace(hour=22), None, DAY_END_REPORT_TIME))
        self.assertTrue(_planning_report_is_due(due.replace(hour=23), None, DAY_END_REPORT_TIME))

    def test_scheduler_does_not_publish_planning_before_1545(self) -> None:
        from api.daytime_cycle import _scheduler_loop

        now = datetime(2026, 8, 18, 8, 13, tzinfo=MSK_TZ)
        with patch('api.daytime_cycle.datetime', wraps=datetime) as clock, \
             patch('api.daytime_cycle._stop_event') as stop, \
             patch('api.daytime_cycle._publish_planning_report', return_value={'status': 'success'}) as planning, \
             patch('api.daytime_cycle._publish_day_end_report', return_value={'status': 'success'}) as day_end, \
             patch('api.daytime_cycle._start_interval_cycle') as cycle:
            clock.now.return_value = now
            stop.is_set.return_value = False
            stop.wait.return_value = True
            _scheduler_loop()
        planning.assert_not_called()
        day_end.assert_not_called()
        cycle.assert_not_called()

    def test_scheduler_catches_up_reports_after_restart_without_starting_crm(self) -> None:
        from api.daytime_cycle import _scheduler_loop

        now = datetime(2026, 8, 18, 23, 10, tzinfo=MSK_TZ)
        with patch('api.daytime_cycle.datetime', wraps=datetime) as clock, \
             patch('api.daytime_cycle._stop_event') as stop, \
             patch('api.daytime_cycle._publish_planning_report', return_value={'status': 'success'}) as planning, \
             patch('api.daytime_cycle._publish_day_end_report', return_value={'status': 'success'}) as day_end, \
             patch('api.daytime_cycle._start_interval_cycle') as cycle:
            clock.now.return_value = now
            stop.is_set.return_value = False
            stop.wait.return_value = True
            _scheduler_loop()
        planning.assert_called_once_with(now)
        day_end.assert_called_once_with(now)
        cycle.assert_not_called()

    def test_slow_cycle_does_not_block_publication_or_spawn_another_worker(self) -> None:
        from api import daytime_cycle

        entered = threading.Event()
        release = threading.Event()

        def slow_cycle(**_kwargs):
            entered.set()
            release.wait(timeout=5)

        due = datetime(2026, 8, 18, 7, 30, tzinfo=MSK_TZ)
        with patch.object(daytime_cycle, '_cycle_thread', None), \
             patch.object(daytime_cycle, 'run_daytime_cycle', side_effect=slow_cycle) as cycle, \
             patch('api.daily_control.publish_planning_daily_control_report', return_value={'id': 1}) as publish:
            try:
                daytime_cycle._start_interval_cycle(due)
                self.assertTrue(entered.wait(timeout=2))
                daytime_cycle._start_interval_cycle(due)
                daytime_cycle._publish_planning_report(due.replace(hour=15, minute=45))
                cycle.assert_called_once()
                publish.assert_called_once()
            finally:
                release.set()
                if daytime_cycle._cycle_thread:
                    daytime_cycle._cycle_thread.join(timeout=2)


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
        self.assertEqual(
            set(result["phase_seconds"]),
            {"deal_control_sync", "manager_trajectory", "change_detection_and_enqueue", "tick_total"},
        )
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

    def test_running_analysis_does_not_block_the_next_bitrix_tick(self) -> None:
        calls: list[str] = []

        def refresh(**_kwargs):
            calls.append("sync")
            return {"sync_message": "CRM обновлена", "sync_errors": []}

        def collect(*_args, **_kwargs):
            return {"status": "success", "counts": {}, "errors": {}}

        def analyze(**_kwargs):
            return {
                "status": "running",
                "job_id": "long-job",
                "counts": {"checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 0, "error": 0},
            }

        first = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW,
            refresh_fn=refresh,
            collect_fn=collect,
            analyze_fn=analyze,
            make_client_fn=lambda: object(),
        )
        second = run_daytime_cycle(
            db_path=self.db_path,
            now=NOW + timedelta(minutes=30),
            refresh_fn=refresh,
            collect_fn=collect,
            analyze_fn=analyze,
            make_client_fn=lambda: object(),
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertEqual(calls, ["sync", "sync"])

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
             patch("api.daytime_cycle._publish_planning_report", return_value={"status": "success"}), \
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
             patch("api.daytime_cycle.threading.Thread.start"), \
             patch("api.daytime_cycle._summarize_decisions", return_value={
                 "checked": 1, "changed": 0, "full": 0, "mini": 0, "skip": 1, "error": 0,
             }), \
             patch.dict(os.environ, {"BITRIX_USAGE_DAILY_DIR": "", "BITRIX_TRACE_ALLOW_ENTITY_ID": ""}):
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
        self.assertEqual(options.storage_db_path, str(self.db_path))
        self.assertTrue(options.extra_env and "SPEND_DIARY_BATCH_PATH" in options.extra_env)
        self.assertNotIn("BITRIX_USAGE_DAILY_DIR", options.extra_env)
        self.assertEqual(payload["job_id"], "abc")
        items = list_automatic_analysis_items(self.db_path, int(payload["automatic_analysis_run_id"]))
        self.assertEqual(items[0]["sync_mode"], "full")
        self.assertEqual(items[0]["sync_reasons"], ["initial_or_unacknowledged"])

    def test_job_extra_env_copies_diagnostic_vars_and_entity_id(self) -> None:
        from api.daytime_cycle import _job_extra_env

        with patch.dict(
            os.environ,
            {
                "BITRIX_USAGE_DAILY_DIR": "C:/tmp/bitrix",
                "BITRIX_TRACE_RUN_ID": "diag-run1",
                "BITRIX_TRACE_ALLOW_ENTITY_ID": "1",
                "BITRIX_DENY_WRITE_METHODS": "1",
                "OPENAI_USAGE_TRACE_PATH": "C:/tmp/openai.jsonl",
            },
        ):
            extra = _job_extra_env("18507", batch_path="C:/tmp/batch.jsonl", automatic_run_id=9)
        self.assertEqual(extra["BITRIX_USAGE_DAILY_DIR"], "C:/tmp/bitrix")
        self.assertEqual(extra["BITRIX_TRACE_RUN_ID"], "diag-run1")
        self.assertEqual(extra["BITRIX_TRACE_ENTITY_ID"], "18507")
        self.assertEqual(extra["BITRIX_TRACE_COMPONENT"], "per_deal_context")
        self.assertEqual(extra["BITRIX_DENY_WRITE_METHODS"], "1")
        self.assertIn("SPEND_DIARY_BATCH_PATH", extra)

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


class PlanningReportSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "cycle.sqlite"
        init_db(self.db_path)
        _scope(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_1545_slot_publishes_planning_report_without_analysis(self) -> None:
        from api.daily_control import publish_planning_daily_control_report
        from storage.rop_db import list_daily_control_reports

        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        dashboard = {"deals": [], "managers": [], "sync_errors": []}
        with patch("api.daily_control._refresh_final_sources", return_value=dashboard) as refresh, \
             patch("api.jobs.start_analyze_job") as analyze:
            first = publish_planning_daily_control_report(db_path=self.db_path, now=due)
            second = publish_planning_daily_control_report(db_path=self.db_path, now=due)
        refresh.assert_called()
        analyze.assert_not_called()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["creation_kind"], "automatic_planning")
        self.assertEqual(first["business_date"], "2026-08-18")
        self.assertEqual(len(list_daily_control_reports(self.db_path)), 1)

    def test_scheduler_helper_does_not_start_the_30m_cycle(self) -> None:
        from api.daytime_cycle import _publish_planning_report

        due = datetime(2026, 8, 18, 15, 45, tzinfo=MSK_TZ)
        with patch(
            "api.daily_control.publish_planning_daily_control_report",
            return_value={"id": 9, "creation_kind": "automatic_planning", "cutoff_at": due.isoformat(), "source_status": "ok", "warnings": []},
        ), patch("api.daytime_cycle.run_daytime_cycle") as cycle:
            payload = _publish_planning_report(due)
        cycle.assert_not_called()
        self.assertEqual(payload["trigger"], "planning_report")
        self.assertEqual(payload["daily_control_report_id"], 9)

    def test_evening_cycle_uses_the_same_worker_without_forcing_llm(self) -> None:
        from api import daytime_cycle

        due = datetime(2026, 8, 18, 22, 0, tzinfo=MSK_TZ)
        with patch.object(daytime_cycle, "_cycle_thread", None), \
             patch.object(daytime_cycle, "run_daytime_cycle") as cycle:
            daytime_cycle._start_interval_cycle(due)
            if daytime_cycle._cycle_thread:
                daytime_cycle._cycle_thread.join(timeout=2)
        cycle.assert_called_once()
        self.assertEqual(cycle.call_args.kwargs["trigger"], "evening_22")
        self.assertEqual(cycle.call_args.kwargs["now"], due)

    def test_day_end_helper_does_not_start_analysis(self) -> None:
        from api.daytime_cycle import _publish_day_end_report

        due = datetime(2026, 8, 18, 23, 0, tzinfo=MSK_TZ)
        with patch(
            "api.daily_control.publish_day_end_daily_control_report",
            return_value={"id": 11, "creation_kind": "automatic_day_end", "cutoff_at": due.isoformat()},
        ), patch("api.daytime_cycle.run_daytime_cycle") as cycle:
            payload = _publish_day_end_report(due)
        cycle.assert_not_called()
        self.assertEqual(payload["trigger"], "day_end_report")
        self.assertEqual(payload["daily_control_report_id"], 11)


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

    def test_manual_and_scheduled_start_cannot_claim_the_same_deal(self) -> None:
        from api.jobs import AnalyzeOptions, start_analyze_job

        with patch("api.jobs.threading.Thread.start"):
            start_analyze_job(AnalyzeOptions(entity_type="deal", ids=["101"]))
            with self.assertRaisesRegex(ValueError, "сделки 101 уже выполняется") as error:
                start_analyze_job(
                    AnalyzeOptions(
                        entity_type="deal",
                        ids=["101"],
                        automatic_analysis_run_id=7,
                    )
                )
        self.assertIn("Дождитесь окончания текущего запуска", str(error.exception))

    def test_automatic_jobs_are_limited_to_two_workers_by_default(self) -> None:
        from api.jobs import AnalyzeOptions, get_job, start_analyze_job, wait_for_job
        from storage.rop_db import create_automatic_analysis_run

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "workers.sqlite"
            init_db(db_path)
            run = create_automatic_analysis_run(
                db_path,
                trigger="test",
                entity_ids=["101", "102", "103"],
            )
            entered = threading.Event()
            release = threading.Event()
            state_lock = threading.Lock()
            active = 0
            maximum = 0

            def fake_run_command(*_args, **_kwargs):
                nonlocal active, maximum
                with state_lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        entered.set()
                self.assertTrue(release.wait(timeout=3))
                with state_lock:
                    active -= 1

            with patch.dict(os.environ, {"ANALYSIS_WORKER_CONCURRENCY": "2"}), \
                 patch("api.jobs.run_command", side_effect=fake_run_command), \
                 patch("api.jobs._collect_group_results"):
                jobs = [
                    start_analyze_job(
                        AnalyzeOptions(
                            entity_type="deal",
                            ids=[entity_id],
                            automatic_analysis_run_id=int(run["id"]),
                            storage_db_path=str(db_path),
                        )
                    )
                    for entity_id in ("101", "102", "103")
                ]
                self.assertTrue(entered.wait(timeout=3))
                threading.Event().wait(0.1)
                self.assertEqual(maximum, 2)
                self.assertEqual(sum(get_job(str(job["job_id"]))["status"] == "running" for job in jobs), 2)
                release.set()
                for job in jobs:
                    result = wait_for_job(str(job["job_id"]), timeout_seconds=3, poll_seconds=0.02)
                    self.assertEqual(result["status"], "done")


if __name__ == "__main__":
    unittest.main()
