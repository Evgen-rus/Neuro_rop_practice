from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openai_api.llm.validation import _validate_daily_checklist_update
from storage.rop_db import (
    apply_deal_daily_checklist_update,
    connect,
    get_deal_daily_checklist_analysis_projection,
    get_or_create_deal_daily_checklist,
    save_deal_daily_checklist_item_completion,
    upsert_deal_control_deal,
)


class DailyDealChecklistTests(unittest.TestCase):
    def _save_deal(self, db_path: Path) -> None:
        upsert_deal_control_deal(
            db_path,
            deal_id="101",
            source="initial",
            title="Сделка 101",
            manager_id="10",
            manager_name="Иванов Иван",
            stage_id="C15:NEW",
            stage_name="Новая",
            pipeline_id="15",
            amount="120000",
            currency_id="RUB",
            created_at_crm="2026-08-10T09:00:00+03:00",
            modified_at_crm="2026-08-11T09:00:00+03:00",
            is_active=True,
        )

    def test_same_day_ai_delta_keeps_manager_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            first = get_or_create_deal_daily_checklist(
                db_path,
                deal_id="101",
                business_date="2026-08-11",
                seed_items=[
                    {"text": "Получить актуальный договор.", "source": "missing"},
                    {"text": "Уточнить дату решения.", "source": "focus"},
                ],
                source_report_id=1,
            )
            completed_id = first["items"][0]["id"]
            after_manager = save_deal_daily_checklist_item_completion(
                db_path,
                deal_id="101",
                item_id=completed_id,
                completed=True,
                business_date="2026-08-11",
            )

            updated = apply_deal_daily_checklist_update(
                db_path,
                deal_id="101",
                source_report_id=2,
                update={
                    "business_date": "2026-08-11",
                    "base_revision": after_manager["revision"],
                    "add": [{"text": "Подтвердить срок согласования.", "reason": "Появился новый срок."}],
                    "retire": [],
                    "reopen": [],
                },
            )

            self.assertIsNotNone(updated)
            by_id = {item["id"]: item for item in updated["items"]}
            self.assertTrue(by_id[completed_id]["completed"])
            self.assertIn("Подтвердить срок согласования.", [item["text"] for item in updated["items"]])

    def test_first_daily_version_can_preserve_legacy_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            checklist = get_or_create_deal_daily_checklist(
                db_path,
                deal_id="101",
                business_date="2026-08-11",
                seed_items=[{
                    "text": "Получить актуальный договор.",
                    "source": "missing",
                    "completed": True,
                    "completed_at": "2026-08-11T10:00:00+03:00",
                    "completed_by": "manager",
                }],
                source_report_id=1,
            )

            self.assertEqual(checklist["completed"], 1)
            self.assertTrue(checklist["items"][0]["completed"])
            self.assertEqual(checklist["items"][0]["completed_at"], "2026-08-11T10:00:00+03:00")

    def test_new_day_hides_completed_and_carries_only_open_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            first = get_or_create_deal_daily_checklist(
                db_path,
                deal_id="101",
                business_date="2026-08-10",
                seed_items=[
                    {"text": "Получить договор.", "source": "missing"},
                    {"text": "Уточнить срок.", "source": "focus"},
                ],
                source_report_id=1,
            )
            save_deal_daily_checklist_item_completion(
                db_path,
                deal_id="101",
                item_id=first["items"][0]["id"],
                completed=True,
                business_date="2026-08-10",
            )

            next_day = get_or_create_deal_daily_checklist(
                db_path,
                deal_id="101",
                business_date="2026-08-11",
                source_report_id=1,
            )

            self.assertEqual([item["text"] for item in next_day["items"]], ["Уточнить срок."])
            self.assertFalse(next_day["items"][0]["completed"])
            self.assertEqual(next_day["items"][0]["change_kind"], "carried")

    def test_stale_ai_delta_cannot_retire_or_reopen_after_manager_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            first = get_or_create_deal_daily_checklist(
                db_path,
                deal_id="101",
                business_date="2026-08-11",
                seed_items=[{"text": "Получить договор.", "source": "missing"}],
                source_report_id=1,
            )
            item_id = first["items"][0]["id"]
            stale_revision = first["revision"]
            save_deal_daily_checklist_item_completion(
                db_path,
                deal_id="101",
                item_id=item_id,
                completed=True,
                business_date="2026-08-11",
            )

            updated = apply_deal_daily_checklist_update(
                db_path,
                deal_id="101",
                source_report_id=2,
                update={
                    "business_date": "2026-08-11",
                    "base_revision": stale_revision,
                    "add": [{"text": "Уточнить дату решения.", "reason": "Новый факт."}],
                    "retire": [{"item_id": item_id, "reason": "Больше не нужно."}],
                    "reopen": [{"item_id": item_id, "reason": "Появилось новое обстоятельство."}],
                },
            )

            by_id = {item["id"]: item for item in updated["items"]}
            self.assertTrue(by_id[item_id]["completed"])
            self.assertIn("Уточнить дату решения.", [item["text"] for item in updated["items"]])

    def test_analysis_materialization_is_idempotent_and_context_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            update = {
                "business_date": "2026-08-11",
                "base_revision": 0,
                "add": [{"text": "Получить договор.", "reason": "Договора нет в истории."}],
                "retire": [],
                "reopen": [],
            }
            first = apply_deal_daily_checklist_update(
                db_path,
                deal_id="101",
                source_report_id=7,
                update=update,
            )
            second = apply_deal_daily_checklist_update(
                db_path,
                deal_id="101",
                source_report_id=7,
                update=update,
            )
            context = get_deal_daily_checklist_analysis_projection(
                db_path,
                "101",
                business_date="2026-08-11",
            )
            with connect(db_path) as conn:
                applied_events = conn.execute(
                    "SELECT COUNT(*) FROM deal_daily_checklist_events WHERE event_type = 'analysis_applied'"
                ).fetchone()[0]

            self.assertEqual(first["items"], second["items"])
            self.assertEqual(applied_events, 1)
            self.assertFalse(context["manager_marks_are_client_evidence"])
            self.assertEqual(set(context["items"][0]), {"id", "text", "completed", "change_kind"})

    def test_ai_delta_validation_requires_revision_date_and_reasons(self) -> None:
        errors: list[str] = []
        _validate_daily_checklist_update(
            {
                "business_date": "2026-08-11",
                "base_revision": 3,
                "add": [{"text": "Уточнить срок.", "reason": "Появилась новая дата."}],
                "retire": [],
                "reopen": [{"item_id": "12", "reason": "Клиент изменил решение."}],
            },
            errors,
        )
        self.assertEqual(errors, [])

        invalid_errors: list[str] = []
        _validate_daily_checklist_update(
            {
                "business_date": "11.08.2026",
                "base_revision": -1,
                "add": [{"text": "", "reason": ""}],
                "retire": [],
                "reopen": [],
            },
            invalid_errors,
        )
        self.assertTrue(any("business_date" in error for error in invalid_errors))
        self.assertTrue(any("base_revision" in error for error in invalid_errors))
        self.assertTrue(any("daily_checklist_update.add[0].text" in error for error in invalid_errors))


if __name__ == "__main__":
    unittest.main()
