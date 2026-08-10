import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import jobs
from api.jobs import JobState
from openai_api.llm.analyze_deal import build_prompt
from storage.rop_db import (
    connect,
    get_latest_neuro_rop_recommendation_projection,
    materialize_deal_recommendation_from_report,
    save_deal_control_scope,
    save_deal_control_task_crm_fact,
    save_deal_control_task_outcome,
    save_ui_report,
    upsert_deal_control_deal,
)


class PriorNeuroRopRecommendationTests(unittest.TestCase):
    @staticmethod
    def _analysis(text: str = "Позвонить клиенту") -> dict:
        return {
            "deal_state": {"summary": "В работе"},
            "rop_manager_message_block": {
                "message_to_manager": text,
                "success_condition": "Получен подтверждённый следующий шаг",
                "deadline": "2026-08-10",
            },
            "manager_action_block": {"recommended_channel": "phone"},
        }

    @staticmethod
    def _save_deal(db_path: Path, deal_id: str = "101") -> None:
        upsert_deal_control_deal(
            db_path,
            deal_id=deal_id,
            source="initial",
            title="Сделка",
            manager_id="10",
            manager_name="Менеджер",
            stage_id="C15:NEW",
            stage_name="Новая",
            pipeline_id="15",
            amount="120000",
            currency_id="RUB",
            created_at_crm="2026-08-01T09:00:00+03:00",
            modified_at_crm="2026-08-02T09:00:00+03:00",
            is_active=True,
        )
        save_deal_control_scope(db_path, initial_deal_ids=[deal_id], manager_ids=[], pipeline_id="15")

    def test_prior_projection_is_compact_and_requires_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            report_id = save_ui_report(
                db_path, entity_type="deal", entity_id="101", report_json=self._analysis()
            )
            task = materialize_deal_recommendation_from_report(
                db_path, "101", report_id, self._analysis()
            )
            self.assertIsNotNone(task)
            save_deal_control_task_outcome(
                db_path,
                task_id=int(task["id"]),
                contact_status="confirmed_contact",
                result_status="pending",
                result_note="Клиент ответил",
                next_step_text="Ждать решение",
                next_step_at="2026-08-11T12:00:00+03:00",
                evidence_kind="manager_confirmation",
                evidence_id=None,
                source_role="manager",
            )
            save_deal_control_task_crm_fact(
                db_path,
                task_id=int(task["id"]),
                fact_key="private",
                activity_id="1",
                fact_kind="confirmed_contact",
                summary="Клиент ответил",
                occurred_at="2026-08-10T10:00:00+03:00",
                contact_class="confirmed_contact",
                payload={"private_extra": "не передавать"},
            )

            projection = get_latest_neuro_rop_recommendation_projection(db_path, "101")
            self.assertIsNotNone(projection)
            assert projection is not None
            self.assertEqual(projection["task_id"], int(task["id"]))
            self.assertEqual(projection["source_report_id"], report_id)
            self.assertEqual(projection["recommendation_state"], "contacted")
            self.assertEqual(projection["latest_outcome"]["result_note"], "Клиент ответил")
            self.assertEqual(projection["crm_facts"][0]["fact_key"], "private")
            encoded = json.dumps(projection, ensure_ascii=False)
            self.assertNotIn("payload_json", encoded)
            self.assertNotIn("private_extra", encoded)
            self.assertNotIn("report_json", encoded)
            self.assertNotIn("quick_help", encoded)

            with connect(db_path) as conn:
                conn.execute(
                    "DELETE FROM deal_control_task_baselines WHERE task_id = ?",
                    (int(task["id"]),),
                )
            self.assertIsNone(get_latest_neuro_rop_recommendation_projection(db_path, "101"))

    def test_prompt_has_structured_prior_section_without_private_fields(self) -> None:
        prior = {
            "task_id": 7,
            "source_report_id": 9,
            "task_text": "Позвонить клиенту",
            "expected_result": "Подтверждённый шаг",
            "due_at": "2026-08-10T18:00:00+03:00",
            "recommendation_state": "not_done",
            "latest_outcome": None,
            "crm_facts": [],
        }
        prompt = build_prompt("101", "История", "Новое событие", "Диагностика", [], {}, prior)
        self.assertIn("## PRIOR_NEURO_ROP_RECOMMENDATION", prompt)
        self.assertIn('"task_id": 7', prompt)
        self.assertNotIn("report_json", prompt)
        self.assertNotIn("quick_help", prompt)
        self.assertIn("## PRIOR_NEURO_ROP_RECOMMENDATION\n\nnull", build_prompt("101", "", "", "", [], {}))

    def test_collect_results_materializes_only_after_successful_deal_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            analysis_path.write_text(json.dumps({"analysis": self._analysis()}, ensure_ascii=False), encoding="utf-8")
            paths = {"analysis_json": analysis_path, "report_md": root / "deal_101.md", "error_json": root / "error.json"}
            job = JobState(job_id="job")
            calls: list[str] = []

            def save(*_args, **_kwargs):
                calls.append("save")
                return 42

            def materialize(*_args, **_kwargs):
                calls.append("materialize")
                return None

            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "save_ui_report", side_effect=save), \
                 patch.object(jobs, "materialize_deal_recommendation_from_report", side_effect=materialize):
                jobs._collect_results(job, "deal", ["101"])
            self.assertEqual(calls, ["save", "materialize"])

    def test_collect_results_does_not_materialize_lead_failed_analysis_or_failed_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {"analysis_json": root / "analysis.json", "report_md": root / "report.md", "error_json": root / "error.json"}
            job = JobState(job_id="job")
            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "save_ui_report") as save, \
                 patch.object(jobs, "materialize_deal_recommendation_from_report") as materialize:
                jobs._collect_results(job, "deal", ["101"])
            save.assert_not_called()
            materialize.assert_not_called()

            paths["analysis_json"].write_text(json.dumps({"analysis": {"lead_state": {}}}), encoding="utf-8")
            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "save_ui_report", return_value=42) as save, \
                 patch.object(jobs, "materialize_deal_recommendation_from_report") as materialize:
                jobs._collect_results(job, "lead", ["101"])
            save.assert_called_once()
            materialize.assert_not_called()

            paths["analysis_json"].write_text(json.dumps({"analysis": self._analysis()}), encoding="utf-8")
            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "save_ui_report", side_effect=RuntimeError("save failed")), \
                 patch.object(jobs, "materialize_deal_recommendation_from_report") as materialize:
                with self.assertRaises(RuntimeError):
                    jobs._collect_results(job, "deal", ["101"])
            materialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
