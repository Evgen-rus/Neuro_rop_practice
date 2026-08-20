from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from api.deal_control import _analysis_coaching, build_deal_control_dashboard
from storage.rop_db import (
    connect,
    get_deal_manager_situation_state,
    get_latest_deal_manager_situation_review,
    list_deal_manager_situation_review_history,
    next_deal_manager_situation_revision,
    save_deal_manager_situation_confirmation,
    save_deal_manager_situation_refined_projection,
    save_ui_report,
    upsert_deal_control_deal,
)


def _base_analysis() -> dict:
    return {
        "deal_control_brief": {
            "current_situation": "Базовая текущая ситуация",
            "what_to_check_now": "Базово проверить срок решения",
            "rop_focus": "Базовый фокус РОПа",
            "manager_coaching": "Базовая подсказка менеджеру",
            "known_facts": ["Базовый подтверждённый факт"],
            "missing_facts": ["Базовый пробел"],
            "contact_goal": "Базовая цель контакта",
            "contact_questions": ["Базовый вопрос"],
            "call_script": "Базовый сценарий",
            "call_opening_variants": ["Базовый вариант"],
        },
        "manager_action_block": {
            "primary_text": {"text": "Основной базовый текст"},
            "manager_checklist": ["Базовый пункт CRM"],
            "recommended_channel": "phone",
            "goal": "Цель из manager action",
        },
        "deal_state": {"summary": "Сводка сделки"},
    }


def _seed_deal(db_path: Path, *, deal_id: str = "101", manager_id: str = "42") -> None:
    upsert_deal_control_deal(
        db_path,
        deal_id=deal_id,
        source="initial",
        title=f"Сделка {deal_id}",
        manager_id=manager_id,
        manager_name="Менеджер Тестовый",
        stage_id="C15:NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="120000",
        currency_id="RUB",
        created_at_crm="2026-08-01T09:00:00+03:00",
        modified_at_crm="2026-08-01T09:00:00+03:00",
        is_active=True,
    )


class DealManagerSituationTests(unittest.TestCase):
    def test_reviews_are_append_only_utf8_and_use_local_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )

            confirmed = save_deal_manager_situation_confirmation(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                manager_context="Контекст менеджера: клиент просил вернуться в пятницу",
                model_meta={"модель": "локальная проверка"},
            )
            refined = save_deal_manager_situation_refined_projection(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                manager_context="Уточнённый контекст менеджера",
                refined_coaching={"current_situation": "Уточнённая ситуация"},
            )

            self.assertEqual(confirmed["manager_id"], "42")
            self.assertEqual(confirmed["action"], "confirmed")
            self.assertEqual(confirmed["revision"], 1)
            self.assertEqual(refined["action"], "context_added")
            self.assertEqual(refined["revision"], 2)
            self.assertEqual(next_deal_manager_situation_revision(
                db_path,
                deal_id="101",
                source_report_id=report_id,
            ), 3)
            self.assertEqual(
                [item["revision"] for item in list_deal_manager_situation_review_history(
                    db_path,
                    deal_id="101",
                    source_report_id=report_id,
                )],
                [2, 1],
            )

            with connect(db_path) as conn:
                raw = conn.execute(
                    "SELECT refined_coaching_json, model_meta_json FROM deal_manager_situation_reviews WHERE id = ?",
                    (confirmed["id"],),
                ).fetchone()
            self.assertIn("модель", raw["model_meta_json"])
            self.assertNotIn("\\u043c", raw["model_meta_json"])
            self.assertIsNone(raw["refined_coaching_json"])

            with connect(db_path) as conn:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(deal_manager_situation_reviews)")
                }
                count = conn.execute(
                    "SELECT COUNT(*) FROM deal_manager_situation_reviews"
                ).fetchone()[0]
            self.assertEqual(
                columns,
                {
                    "id", "deal_id", "manager_id", "source_report_id", "revision", "action",
                    "manager_context", "refined_coaching_json", "model_meta_json", "business_date",
                    "created_at",
                },
            )
            self.assertEqual(count, 2)

    def test_pre_feature_reviews_are_immediately_pending_after_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )
            review = save_deal_manager_situation_confirmation(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                business_date="2026-08-20",
            )
            with connect(db_path) as conn:
                conn.execute(
                    "UPDATE deal_manager_situation_reviews SET business_date = NULL WHERE id = ?",
                    (review["id"],),
                )

            state = get_deal_manager_situation_state(
                db_path,
                deal_id="101",
                business_date="2026-08-20",
            )

            self.assertEqual(state["status"], "pending")
            self.assertFalse(state["is_current"])
            self.assertIsNone(state["review_id"])
            self.assertIsNone(state["last_confirmation_business_date"])
            self.assertEqual(len(list_deal_manager_situation_review_history(db_path, deal_id="101")), 1)

    def test_new_moscow_day_requires_confirmation_and_preserves_refined_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )
            refined = save_deal_manager_situation_refined_projection(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                manager_context="Клиент попросил вернуться после совещания",
                refined_coaching={"current_situation": "Клиент ждёт внутреннее совещание"},
                business_date="2026-08-19",
            )

            stale = get_deal_manager_situation_state(
                db_path,
                deal_id="101",
                business_date="2026-08-20",
            )
            self.assertEqual(stale["status"], "pending")
            self.assertFalse(stale["is_current"])
            self.assertIsNone(stale["review_id"])
            self.assertEqual(stale["last_confirmation_business_date"], "2026-08-19")

            confirmed = save_deal_manager_situation_confirmation(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                business_date="2026-08-20",
            )
            current = get_deal_manager_situation_state(
                db_path,
                deal_id="101",
                business_date="2026-08-20",
            )

            self.assertEqual(confirmed["revision"], refined["revision"] + 1)
            self.assertEqual(confirmed["business_date"], "2026-08-20")
            self.assertEqual(confirmed["manager_context"], "Клиент попросил вернуться после совещания")
            self.assertEqual(
                confirmed["refined_coaching"],
                {"current_situation": "Клиент ждёт внутреннее совещание"},
            )
            self.assertEqual(current["status"], "confirmed")
            self.assertTrue(current["is_current"])
            self.assertEqual(current["review_id"], confirmed["id"])
            self.assertEqual(current["business_date"], "2026-08-20")
            self.assertEqual(_analysis_coaching(db_path, "101")["current_situation"], "Клиент ждёт внутреннее совещание")

    def test_confirmation_business_date_uses_moscow_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )
            utc_evening = datetime.fromisoformat("2026-08-19T21:30:00+00:00")
            review = save_deal_manager_situation_confirmation(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                business_date=utc_evening,
            )

            state = get_deal_manager_situation_state(
                db_path,
                deal_id="101",
                business_date=utc_evening,
            )

            self.assertEqual(review["business_date"], "2026-08-20")
            self.assertEqual(state["business_date"], "2026-08-20")
            self.assertTrue(state["is_current"])

    def test_latest_report_invalidates_old_review_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            first_report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )
            first_review = save_deal_manager_situation_confirmation(
                db_path,
                deal_id="101",
                source_report_id=first_report_id,
                manager_context="Старый контекст менеджера",
            )
            current = get_deal_manager_situation_state(db_path, deal_id="101")
            self.assertEqual(current["status"], "confirmed")
            self.assertEqual(current["state"], "confirmed")
            self.assertEqual(current["review_id"], first_review["id"])
            self.assertTrue(current["is_current"])

            second_report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={**_base_analysis(), "deal_control_brief": {
                    **_base_analysis()["deal_control_brief"],
                    "current_situation": "Новая ситуация из нового анализа",
                }},
            )
            stale = get_deal_manager_situation_state(db_path, deal_id="101")
            self.assertEqual(stale["status"], "pending")
            self.assertEqual(stale["source_report_id"], second_report_id)
            self.assertIsNone(stale["review_id"])
            self.assertIsNone(stale["revision"])
            self.assertIsNone(stale["manager_context"])
            self.assertIsNone(stale["confirmed_at"])
            self.assertFalse(stale["is_current"])
            self.assertEqual(
                get_latest_deal_manager_situation_review(
                    db_path,
                    deal_id="101",
                    source_report_id=first_report_id,
                )["id"],
                first_review["id"],
            )
            self.assertEqual(len(list_deal_manager_situation_review_history(db_path, deal_id="101")), 1)

    def test_refined_projection_overlays_only_nonempty_whitelist_values(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _seed_deal(db_path)
            report_id = save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json=_base_analysis(),
            )
            before = _base_analysis()
            save_deal_manager_situation_refined_projection(
                db_path,
                deal_id="101",
                source_report_id=report_id,
                manager_context="Это не клиентский evidence",
                refined_coaching={
                    "current_situation": "Ситуация после уточнения",
                    "what_to_check_now": "Проверить новую дату",
                    "manager_coaching": "Уточнённая подсказка",
                    "known": ["Известно менеджеру после проверки"],
                    "questions": [],
                    "script": "",
                    "unknowns": ["Уточнить бюджет"],
                    "ignored_field": "Не должен попасть в coaching",
                },
            )
            coaching = _analysis_coaching(db_path, "101")

            self.assertEqual(coaching["current_situation"], "Ситуация после уточнения")
            self.assertEqual(coaching["what_to_check_now"], "Проверить новую дату")
            self.assertEqual(coaching["manager_coaching"], "Уточнённая подсказка")
            self.assertEqual(coaching["known"], ["Известно менеджеру после проверки"])
            self.assertEqual(coaching["unknowns"], ["Уточнить бюджет"])
            self.assertEqual(coaching["questions"], before["deal_control_brief"]["contact_questions"])
            self.assertEqual(coaching["script"], before["deal_control_brief"]["call_script"])
            self.assertNotIn("ignored_field", coaching)
            self.assertEqual(coaching["report_id"], report_id)
            self.assertTrue(coaching["analysis_created_at"])
            self.assertNotIn("Это не клиентский evidence", coaching.get("known", []))

            state = build_deal_control_dashboard(db_path=db_path)["deals"][0]["manager_situation"]
            self.assertEqual(state["status"], "refined")
            self.assertEqual(state["source_report_id"], report_id)
            self.assertTrue(state["is_current"])
            self.assertEqual(state["manager_context"], "Это не клиентский evidence")

            with connect(db_path) as conn:
                saved_report_json = conn.execute(
                    "SELECT report_json FROM ui_reports WHERE id = ?",
                    (report_id,),
                ).fetchone()[0]
            self.assertEqual(saved_report_json, json.dumps(before, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
