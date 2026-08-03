from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage.rop_db import (
    connect,
    list_deal_manager_quick_help,
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


if __name__ == "__main__":
    unittest.main()
