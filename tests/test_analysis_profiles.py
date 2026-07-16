from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from storage.rop_db import (
    create_daily_summary_run,
    create_analysis_profile,
    delete_analysis_profile,
    get_last_analysis_profile,
    get_daily_summary_run,
    list_analysis_profiles,
    save_candidate_filter,
    set_last_analysis_profile,
    update_analysis_profile,
)


class AnalysisProfileStorageTests(unittest.TestCase):
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
                name="Утро за вчера",
                profile={"period_preset": "yesterday", "limits": {"paid_per_run": 3}},
            )
            set_last_analysis_profile(db, second["id"])
            updated = update_analysis_profile(
                db,
                second["id"],
                name="Утренний отчёт",
                profile={**second["profile"], "limits": {**second["profile"]["limits"], "paid_per_run": 4}},
            )

            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["profile"]["period_preset"], "yesterday")
            self.assertEqual(updated["profile"]["limits"]["paid_per_run"], 4)
            self.assertEqual(get_last_analysis_profile(db)["id"], second["id"])
            self.assertEqual(len(list_analysis_profiles(db)), 2)

            fallback_id = delete_analysis_profile(db, second["id"])
            self.assertEqual(fallback_id, default["id"])
            self.assertEqual(get_last_analysis_profile(db)["id"], default["id"])
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
            profile["profile"]["period_preset"] = "yesterday"
            candidate["title"] = "Изменённое название"
            stored = get_daily_summary_run(db, run["id"])

            self.assertEqual(stored["profile_snapshot"]["period_preset"], "today_and_yesterday")
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


if __name__ == "__main__":
    unittest.main()
