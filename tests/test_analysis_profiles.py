from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage.rop_db import (
    create_daily_summary_run,
    create_analysis_profile,
    delete_analysis_profile,
    fail_orphaned_daily_summary_items,
    get_last_analysis_profile,
    get_daily_summary_run,
    list_analysis_profiles,
    prepare_daily_summary_items,
    update_daily_summary_item_progress,
    save_candidate_filter,
    set_last_analysis_profile,
    update_analysis_profile,
)


class AnalysisProfileStorageTests(unittest.TestCase):
    def test_profile_mojibake_is_repaired_in_storage_and_on_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            original = get_last_analysis_profile(db)
            correct_name = "Ежедневный контроль РОПа"
            correct_status = "спам"
            broken_name = correct_name.encode("utf-8").decode("latin-1")
            broken_status = correct_status.encode("utf-8").decode("latin-1")
            broken_profile = {**original["profile"]}
            broken_profile["lead"] = {
                **original["profile"]["lead"],
                "excluded_status_names": [broken_status],
            }
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE analysis_profiles SET name = ?, profile_json = ? WHERE id = ?",
                    (broken_name, json.dumps(broken_profile, ensure_ascii=False), original["id"]),
                )
                conn.commit()
            finally:
                conn.close()

            repaired = get_last_analysis_profile(db)
            self.assertEqual(repaired["name"], correct_name)
            self.assertEqual(repaired["profile"]["lead"]["excluded_status_names"], [correct_status])
            conn = sqlite3.connect(db)
            try:
                raw_name, raw_profile = conn.execute(
                    "SELECT name, profile_json FROM analysis_profiles WHERE id = ?",
                    (original["id"],),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(raw_name, correct_name)
            self.assertEqual(json.loads(raw_profile)["lead"]["excluded_status_names"], [correct_status])

            updated = update_analysis_profile(
                db,
                original["id"],
                name=broken_name,
                profile=broken_profile,
            )
            self.assertEqual(updated["name"], correct_name)
            self.assertEqual(updated["profile"]["lead"]["excluded_status_names"], [correct_status])
            gc.collect()

    def test_default_profile_migrates_compatible_legacy_filter_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            save_candidate_filter(
                db,
                {
                    "entity_type": "deal",
                    "pipeline_ids": ["15"],
                    "stage_ids": ["C15:NEW", "C15:4"],
                    "review_view": "reviewed",
                    "limit": 12,
                },
            )

            profile = get_last_analysis_profile(db)

            self.assertEqual(profile["name"], "Ежедневный контроль РОПа")
            self.assertEqual(profile["profile"]["deal"]["pipeline_ids"], ["15"])
            self.assertIn("C15:UC_TUYDP6", profile["profile"]["deal"]["stage_ids"])
            self.assertEqual(profile["profile"]["review_view"], "active")
            self.assertEqual(profile["profile"]["limits"]["workset"], 15)
            self.assertEqual(profile["profile"]["period_preset"], "today_and_previous_workday")
            imported = next(item for item in list_analysis_profiles(db) if item["name"] == "Импортированный фильтр кандидатов")
            self.assertEqual(imported["profile"]["deal"]["stage_ids"], ["C15:NEW", "C15:4"])
            self.assertEqual(imported["profile"]["review_view"], "reviewed")
            gc.collect()

    def test_named_profiles_version_and_last_selected_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            default = get_last_analysis_profile(db)
            second = create_analysis_profile(
                db,
                name="Утро за рабочий день",
                profile={"period_preset": "previous_workday", "limits": {"paid_per_run": 3}},
            )
            set_last_analysis_profile(db, second["id"])
            updated = update_analysis_profile(
                db,
                second["id"],
                name="Утренний отчёт",
                profile={**second["profile"], "limits": {**second["profile"]["limits"], "paid_per_run": 4}},
            )

            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["profile"]["period_preset"], "previous_workday")
            self.assertEqual(updated["profile"]["limits"]["paid_per_run"], 4)
            self.assertEqual(get_last_analysis_profile(db)["id"], second["id"])
            self.assertEqual(len(list_analysis_profiles(db)), 2)

            fallback_id = delete_analysis_profile(db, second["id"])
            self.assertEqual(fallback_id, default["id"])
            self.assertEqual(get_last_analysis_profile(db)["id"], default["id"])
            gc.collect()

    def test_legacy_calendar_periods_are_normalized_to_workdays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            yesterday = create_analysis_profile(
                db,
                name="Старый вчера",
                profile={"period_preset": "yesterday"},
            )
            combined = create_analysis_profile(
                db,
                name="Старый сегодня и вчера",
                profile={"period_preset": "today_and_yesterday"},
            )

            self.assertEqual(yesterday["profile"]["period_preset"], "previous_workday")
            self.assertEqual(combined["profile"]["period_preset"], "today_and_previous_workday")
            gc.collect()

    def test_daily_summary_keeps_immutable_profile_and_candidate_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            profile = get_last_analysis_profile(db)
            candidate = {
                "journey_key": "lead:7",
                "entity_type": "deal",
                "entity_id": "9",
                "origin_lead_id": "7",
                "lifecycle": "new",
                "analysis_freshness": "missing",
                "title": "Исходное название",
            }
            run = create_daily_summary_run(
                db,
                profile=profile,
                period={"preset": "today", "period_from": "2026-07-16T00:00:00+03:00"},
                scope={"deal": {"selected": 1}},
                candidates=[candidate],
                selected_journey_keys=["lead:7"],
                cost_preview={"paid_entity_limit": 1},
            )
            profile["profile"]["period_preset"] = "previous_workday"
            candidate["title"] = "Изменённое название"
            stored = get_daily_summary_run(db, run["id"])

            self.assertEqual(stored["profile_snapshot"]["period_preset"], "today_and_previous_workday")
            self.assertEqual(stored["items"][0]["candidate"]["title"], "Исходное название")
            self.assertEqual(stored["llm_required_count"], 1)
            self.assertEqual(stored["llm_allowed_count"], 1)
            gc.collect()

    def test_only_profile_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            default = get_last_analysis_profile(db)
            with self.assertRaisesRegex(ValueError, "единственный профиль"):
                delete_analysis_profile(db, default["id"])
            gc.collect()

    def test_daily_summary_persists_per_entity_progress_and_final_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            profile = get_last_analysis_profile(db)
            candidates = [
                {
                    "journey_key": f"lead:{entity_id}",
                    "entity_type": "lead",
                    "entity_id": entity_id,
                    "lifecycle": "new",
                    "analysis_freshness": "missing",
                    "title": f"Lead {entity_id}",
                }
                for entity_id in ("1", "2")
            ]
            run = create_daily_summary_run(
                db,
                profile=profile,
                period={"as_of": "2026-07-17"},
                scope={},
                candidates=candidates,
                selected_journey_keys=["lead:1", "lead:2"],
                cost_preview={"paid_entity_limit": 2},
            )
            prepare_daily_summary_items(db, run["id"], ["lead:1", "lead:2"])
            update_daily_summary_item_progress(
                db,
                run["id"],
                {"entity_type": "lead", "entity_id": "1", "stage": "done", "status": "done", "detail": "Готово"},
            )
            self.assertEqual(get_daily_summary_run(db, run["id"])["status"], "analyzing")
            update_daily_summary_item_progress(
                db,
                run["id"],
                {"entity_type": "lead", "entity_id": "2", "stage": "error", "status": "error", "detail": "Ошибка", "error": "failure"},
            )
            stored = get_daily_summary_run(db, run["id"])
            self.assertEqual(stored["status"], "completed_with_errors")
            self.assertEqual(stored["items"][0]["progress"]["detail"], "Готово")
            self.assertEqual(stored["items"][1]["error"], "failure")
            gc.collect()

    def test_orphaned_daily_summary_items_fail_after_server_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            profile = get_last_analysis_profile(db)
            candidates = [
                {
                    "journey_key": f"lead:{entity_id}",
                    "entity_type": "lead",
                    "entity_id": entity_id,
                    "lifecycle": "new",
                    "analysis_freshness": "fresh",
                    "title": f"Lead {entity_id}",
                }
                for entity_id in ("1", "2")
            ]
            run = create_daily_summary_run(
                db,
                profile=profile,
                period={"as_of": "2026-07-17"},
                scope={},
                candidates=candidates,
                selected_journey_keys=["lead:1", "lead:2"],
                cost_preview={"paid_entity_limit": 0},
            )
            prepare_daily_summary_items(db, run["id"], ["lead:1", "lead:2"])
            update_daily_summary_item_progress(
                db,
                run["id"],
                {"entity_type": "lead", "entity_id": "1", "stage": "done", "status": "done"},
            )

            updated = fail_orphaned_daily_summary_items(db, run["id"], active_job_ids=set())
            stored = get_daily_summary_run(db, run["id"])

            self.assertEqual(updated, 1)
            self.assertEqual(stored["status"], "completed_with_errors")
            self.assertEqual(stored["items"][0]["processing_status"], "done")
            self.assertEqual(stored["items"][1]["processing_status"], "error")
            self.assertIn("перезапуском сервера", stored["items"][1]["error"])
            gc.collect()


if __name__ == "__main__":
    unittest.main()
