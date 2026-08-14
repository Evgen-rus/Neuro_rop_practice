from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage.rop_db import (
    connect,
    list_deal_context_lever_priorities,
    list_deal_manager_assistant_events,
    list_deal_manager_quick_help,
    record_deal_manager_assistant_event,
    save_deal_context_lever_priority,
    save_deal_manager_quick_help,
    save_deal_manager_situation_confirmation,
    save_ui_report,
    upsert_deal_control_deal,
)


def _seed_deal(db_path: Path, *, manager_id: str = "42") -> tuple[int, int]:
    upsert_deal_control_deal(
        db_path,
        deal_id="101",
        source="initial",
        title="Тестовая сделка",
        manager_id=manager_id,
        manager_name="Менеджер",
        stage_id="C15:NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="100000",
        currency_id="RUB",
        created_at_crm="2026-08-01T09:00:00+03:00",
        modified_at_crm="2026-08-01T09:00:00+03:00",
        is_active=True,
    )
    report_id = save_ui_report(
        db_path,
        entity_type="deal",
        entity_id="101",
        report_json={"deal_control_brief": {"current_situation": "КП отправлено"}},
    )
    review = save_deal_manager_situation_confirmation(
        db_path,
        deal_id="101",
        source_report_id=report_id,
    )
    return report_id, int(review["id"])


class DealManagerQuickHelpStorageTests(unittest.TestCase):
    def test_context_lever_priorities_are_versioned_and_unique_per_rank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, _review_id = _seed_deal(db_path)
            save_deal_context_lever_priority(
                db_path, deal_id="101", source_report_id=report_id,
                lever_id="deadline", priority=1, actor_role="rop",
            )
            save_deal_context_lever_priority(
                db_path, deal_id="101", source_report_id=report_id,
                lever_id="budget", priority=1, actor_role="manager",
            )
            current = {item["lever_id"]: item["priority"] for item in list_deal_context_lever_priorities(
                db_path, deal_id="101", source_report_id=report_id,
            )}
            self.assertIsNone(current["deadline"])
            self.assertEqual(current["budget"], 1)
            with connect(db_path) as conn:
                events = conn.execute(
                    "SELECT lever_id, priority FROM deal_context_lever_priority_events ORDER BY id"
                ).fetchall()
            self.assertEqual(len(events), 3)

    def test_history_is_append_only_utf8_and_filtered_by_current_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            first = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Клиент третий раз не отвечает. Что изменить?",
                answer_json={"problem_summary": "Один канал связи не даёт ответа"},
                model_meta={"model": "test-model"},
            )
            second = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Как написать ему сегодня?",
                answer_json={"client_message": "Добрый день! Уточню удобное время."},
            )

            history = list_deal_manager_quick_help(db_path, deal_id="101")
            self.assertEqual([item["id"] for item in history], [second["id"], first["id"]])
            self.assertEqual(history[0]["content"]["client_message"], "Добрый день! Уточню удобное время.")
            self.assertEqual(
                [item["id"] for item in list_deal_manager_quick_help(
                    db_path,
                    deal_id="101",
                    before_id=int(second["id"]),
                )],
                [first["id"]],
            )
            with connect(db_path) as conn:
                raw = conn.execute(
                    "SELECT question, answer_json FROM deal_manager_quick_help WHERE id = ?",
                    (int(first["id"]),),
                ).fetchone()
                conn.execute("UPDATE deal_control_deals SET manager_id = '77' WHERE deal_id = '101'")
            self.assertIn("Клиент", raw["question"])
            self.assertNotIn("\\u041a", raw["question"])
            self.assertNotIn("\\u041e", raw["answer_json"])
            self.assertEqual(list_deal_manager_quick_help(db_path, deal_id="101"), [])

    def test_mode_and_origin_are_stored_and_legacy_rows_read_as_reanimator(self) -> None:
        from storage.rop_db import get_current_deal_manager_quick_help

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            legacy = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="подскажи что делать",
                answer_json={"answer_contract": "strategy_v2", "next_action": "Написать"},
            )
            push = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Сформируй текущий дожим сделки",
                answer_json={"answer_contract": "strategy_v3", "mode": "push", "pressure_lever": {"title": "Сроки", "rationale": "Есть дата"}},
                mode="push",
                origin="auto",
            )
            self.assertEqual(legacy["mode"], "reanimator")
            self.assertEqual(legacy["origin"], "manager")
            self.assertEqual(push["mode"], "push")
            self.assertEqual(push["origin"], "auto")
            self.assertEqual(legacy.get("turn_id"), None)
            paired_push = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Давай другой рычаг",
                answer_json={"answer_contract": "strategy_v3", "mode": "push"},
                mode="push",
                origin="manager",
                turn_id="turn-shared",
            )
            paired_reanimator = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Давай другой рычаг",
                answer_json={"answer_contract": "strategy_v3", "mode": "reanimator"},
                mode="reanimator",
                origin="manager",
                turn_id="turn-shared",
            )
            self.assertEqual(paired_push["turn_id"], "turn-shared")
            self.assertEqual(paired_reanimator["turn_id"], "turn-shared")
            self.assertEqual(
                get_current_deal_manager_quick_help(
                    db_path, deal_id="101", source_report_id=report_id,
                    situation_review_id=review_id, mode="push",
                )["id"],
                paired_push["id"],
            )
            self.assertEqual(
                get_current_deal_manager_quick_help(
                    db_path, deal_id="101", source_report_id=report_id,
                    situation_review_id=review_id, mode="reanimator",
                )["id"],
                paired_reanimator["id"],
            )

    def test_rejects_review_from_another_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            next_report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={"deal_control_brief": {"current_situation": "Новый анализ"}},
            )
            with self.assertRaisesRegex(ValueError, "подтверждённая ситуация"):
                save_deal_manager_quick_help(
                    db_path,
                    deal_id="101",
                    source_report_id=next_report_id,
                    situation_review_id=review_id,
                    question="Что делать?",
                    answer_json={"recommended_action": "Проверить контекст"},
                )
            self.assertNotEqual(report_id, next_report_id)

    def test_assistant_communication_event_is_local_idempotent_and_manager_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            report_id, review_id = _seed_deal(db_path)
            answer = save_deal_manager_quick_help(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                situation_review_id=review_id,
                question="Что делать после звонка?",
                answer_json={"recommended_action": "Зафиксировать результат"},
            )
            first = record_deal_manager_assistant_event(
                db_path,
                deal_id="101",
                event_type="communication_completed",
                quick_help_id=int(answer["id"]),
            )
            repeated = record_deal_manager_assistant_event(
                db_path,
                deal_id="101",
                event_type="communication_completed",
                quick_help_id=int(answer["id"]),
            )

            self.assertEqual(first["id"], repeated["id"])
            self.assertEqual(len(list_deal_manager_assistant_events(db_path, deal_id="101")), 1)
            with connect(db_path) as conn:
                conn.execute("UPDATE deal_control_deals SET manager_id = '77' WHERE deal_id = '101'")
            self.assertEqual(list_deal_manager_assistant_events(db_path, deal_id="101"), [])


if __name__ == "__main__":
    unittest.main()
