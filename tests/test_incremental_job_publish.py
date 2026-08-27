from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.jobs import JobState, _collect_results, _publish_deal_result
from progress_events import compact_decision_status
from storage.rop_db import (
    create_automatic_analysis_run,
    get_latest_automatic_analysis_run,
    get_or_create_ui_report_for_analysis_run,
    init_db,
    interrupt_running_automatic_analysis_runs,
    list_automatic_analysis_items,
    list_ui_reports,
    save_analysis_run,
)


def _analysis() -> dict:
    return {
        "deal_state": {"summary": "В работе"},
        "rop_manager_message_block": {
            "message_to_manager": "Позвонить клиенту",
            "success_condition": "Получен подтверждённый следующий шаг",
            "deadline": "2026-08-10",
        },
        "manager_action_block": {"recommended_channel": "phone"},
    }


def _ready(job: JobState, entity_id: str, *, run_id: int, decision: str) -> None:
    job.entity_progress[f"deal:{entity_id}"] = {
        "entity_type": "deal",
        "entity_id": entity_id,
        "publish_ready": True,
        "analysis_run_id": run_id,
        "decision_status": decision,
        "status": "error" if decision == "error" else "done",
        "stage": "error" if decision == "error" else "done",
    }


class IncrementalJobPublishTests(unittest.TestCase):
    def test_compact_decision_status_maps_engine_values(self) -> None:
        self.assertEqual(compact_decision_status("FIRST_FULL_ANALYSIS"), "full")
        self.assertEqual(compact_decision_status("FULL_LLM_ANALYSIS"), "full")
        self.assertEqual(compact_decision_status("INCREMENTAL_LLM_ANALYSIS"), "full")
        self.assertEqual(compact_decision_status("MINI_RECOMMENDATION_NO_LLM"), "mini")
        self.assertEqual(compact_decision_status("SKIPPED_NO_CHANGES"), "skip")
        self.assertEqual(compact_decision_status("ERROR"), "error")

    def test_final_collect_is_idempotent_and_keeps_published_report_after_later_error(self) -> None:
        job = JobState(job_id="batch")
        _ready(job, "101", run_id=73, decision="full")
        _ready(job, "202", run_id=80, decision="error")
        created_ids: list[int] = []
        created_flags: list[bool] = []

        def create(*_args, **kwargs):
            created_ids.append(int(kwargs["analysis_run_id"]))
            created = len(created_ids) == 1
            created_flags.append(created)
            return {"id": 15}, created

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            analysis_path.write_text(
                json.dumps({"analysis_run_id": 73, "analysis": _analysis()}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths_101 = {
                "analysis_json": analysis_path,
                "report_md": root / "deal_101.md",
                "error_json": root / "error.json",
            }
            paths_202 = {
                "analysis_json": root / "missing.json",
                "report_md": root / "missing.md",
                "error_json": root / "error-202.json",
            }

            def analysis_paths(_entity_type: str, entity_id: str):
                return paths_101 if entity_id == "101" else paths_202

            materialize_calls: list[int] = []

            def materialize(*_args, **_kwargs):
                materialize_calls.append(1)
                return {"id": 9}

            with patch("api.jobs.analysis_paths", side_effect=analysis_paths), \
                 patch("api.jobs.get_or_create_ui_report_for_analysis_run", side_effect=create), \
                 patch("api.jobs.get_automatic_analysis_item", return_value=None), \
                 patch("api.jobs.apply_deal_recommendation_feedback"), \
                 patch("api.jobs.apply_deal_daily_checklist_update"), \
                 patch("api.jobs.materialize_deal_recommendation_from_report", side_effect=materialize):
                _publish_deal_result(job, "101", allow_raise=False)
                _publish_deal_result(job, "202", allow_raise=False)
                _collect_results(job, "deal", ["101", "202"])

        self.assertEqual(created_ids, [73, 73])
        self.assertEqual(created_flags, [True, False])
        self.assertEqual(len(materialize_calls), 1)
        self.assertEqual(job.report_ids, [15])
        by_id = {item["entity_id"]: item for item in job.results}
        self.assertEqual(by_id["101"]["report_id"], 15)
        self.assertIsNone(by_id["202"]["report_id"])

    def test_mini_and_skip_do_not_create_report_from_stale_analysis_file(self) -> None:
        job = JobState(job_id="skip-job")
        _ready(job, "101", run_id=90, decision="skip")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            analysis_path.write_text(
                json.dumps({"analysis_run_id": 11, "analysis": _analysis()}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths = {
                "analysis_json": analysis_path,
                "report_md": root / "deal_101.md",
                "error_json": root / "error.json",
            }
            with patch("api.jobs.analysis_paths", return_value=paths), \
                 patch("api.jobs.get_or_create_ui_report_for_analysis_run") as create, \
                 patch("api.jobs.get_latest_ui_report", return_value={"id": 8, "report_json": _analysis()}), \
                 patch("api.jobs.materialize_deal_recommendation_from_report") as materialize:
                _collect_results(job, "deal", ["101"])
        create.assert_not_called()
        materialize.assert_not_called()
        self.assertEqual(job.results[0]["report_id"], 8)

    def test_get_or_create_ui_report_returns_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="101",
                status="FULL_LLM_ANALYSIS",
            )
            first, created_first = get_or_create_ui_report_for_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="101",
                analysis_run_id=run_id,
                report_json=_analysis(),
            )
            second, created_second = get_or_create_ui_report_for_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="101",
                analysis_run_id=run_id,
                report_json=_analysis(),
            )
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(list_ui_reports(db_path, limit=10)), 1)

    def test_interrupt_running_automatic_analysis_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            create_automatic_analysis_run(
                db_path,
                trigger="interval_30m",
                entity_ids=["101"],
                status="running",
            )
            changed = interrupt_running_automatic_analysis_runs(db_path)
            items = list_automatic_analysis_items(db_path, 1)
            self.assertEqual(changed, 1)
            self.assertEqual(items[0]["entity_id"], "101")
            latest = get_latest_automatic_analysis_run(db_path)
            self.assertEqual(latest["status"], "interrupted")
            self.assertIsNotNone(latest["finished_at"])

    def test_restart_requeues_only_unfinished_automatic_items(self) -> None:
        from api.daytime_cycle import resume_automatic_analysis_runs
        from storage.rop_db import update_automatic_analysis_item

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite"
            init_db(db_path)
            run = create_automatic_analysis_run(
                db_path,
                trigger="interval_30m",
                entity_ids=["101", "102"],
                status="running",
                spend_batch_path=str(Path(directory) / "batch.jsonl"),
            )
            update_automatic_analysis_item(
                db_path,
                int(run["id"]),
                entity_type="deal",
                entity_id="101",
                decision_status="skip",
                processing_status="done",
            )
            with patch("api.daytime_cycle._launch_automatic_run_jobs", return_value=["job-102"]) as launch:
                resumed = resume_automatic_analysis_runs(db_path)
            self.assertEqual(resumed, 1)
            self.assertEqual(launch.call_args.kwargs["deal_ids"], ["102"])
            latest = get_latest_automatic_analysis_run(db_path)
            self.assertEqual(latest["status"], "running")
            items = list_automatic_analysis_items(db_path, int(run["id"]))
            self.assertEqual(items[0]["processing_status"], "done")
            self.assertEqual(items[1]["processing_status"], "retry")

    def test_restart_rebuilds_missing_human_spend_block_from_batch_events(self) -> None:
        from api.daytime_cycle import resume_automatic_analysis_runs
        from storage.rop_db import update_automatic_analysis_item

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "rop.sqlite"
            spend_dir = root / "spend"
            batch_path = spend_dir / "batches" / "run.jsonl"
            batch_path.parent.mkdir(parents=True)
            batch_path.write_text(
                json.dumps(
                    {
                        "kind": "full_deal_analysis",
                        "entity_type": "deal",
                        "entity_id": "101",
                        "estimated_cost_rub": 12,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            init_db(db_path)
            run = create_automatic_analysis_run(
                db_path,
                trigger="interval_30m",
                entity_ids=["101"],
                status="running",
                business_date="2026-08-18",
                spend_batch_path=str(batch_path),
            )
            update_automatic_analysis_item(
                db_path,
                int(run["id"]),
                entity_type="deal",
                entity_id="101",
                decision_status="full",
                processing_status="done",
            )
            with patch.dict(os.environ, {"SPEND_DIARY_DIR": str(spend_dir)}):
                self.assertEqual(resume_automatic_analysis_runs(db_path), 0)
            diary = spend_dir / "daily_spend-placeholder"
            human_files = list(spend_dir.glob("*.txt"))
            self.assertEqual(len(human_files), 1, diary)
            text = human_files[0].read_text(encoding="utf-8")
            self.assertIn("Сделка 101 — полный анализ, ~12 ₽", text)
            self.assertIn("За этот запуск: ~12 ₽", text)


if __name__ == "__main__":
    unittest.main()
