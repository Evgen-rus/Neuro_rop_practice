import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from api import jobs
from api.jobs import JobState
from openai_api.llm.analyze_deal import build_prompt
from openai_api.llm.validation import (
    _validate_recommendation_feedback,
    validate_deal_recommendation_materialization,
)
from storage.rop_db import (
    apply_deal_recommendation_feedback,
    connect,
    get_latest_neuro_rop_recommendation_projection,
    list_deal_control_tasks,
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

    def test_prior_projection_does_not_create_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "missing" / "state.sqlite"
            self.assertIsNone(get_latest_neuro_rop_recommendation_projection(db_path, "101"))
            self.assertFalse(db_path.exists())

    def test_feedback_no_prior_and_wrong_source_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            new_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis())
            feedback = {
                "applicable": True, "source_report_id": 999, "status": "contacted",
                "what_manager_did": "Клиент ответил", "contact_confirmed": True,
                "target_result_achieved": False, "evidence": ["transcript"],
                "next_action_required": True, "next_action_text": "Уточнить срок",
                "next_action_at": "2026-08-11T12:00:00+03:00", "next_action_reason": "Нужен срок",
            }
            self.assertIsNone(apply_deal_recommendation_feedback(db_path, "101", feedback, new_report_id, self._analysis()))
            self.assertIsNone(apply_deal_recommendation_feedback(db_path, "999", feedback, new_report_id, self._analysis()))
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_outcomes").fetchone()[0], 0)

    def test_feedback_maps_attempt_contact_and_explicit_achieved_idempotently(self) -> None:
        for status, expected_state, contact_confirmed, target_achieved in (
            ("attempted", "attempted", False, False),
            ("contacted", "contacted", True, False),
            ("achieved", "achieved", True, True),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "state.sqlite"
                self._save_deal(db_path)
                old_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis())
                task = materialize_deal_recommendation_from_report(db_path, "101", old_report_id, self._analysis())
                new_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis("Новый шаг"))
                feedback = {
                    "applicable": True, "source_report_id": old_report_id, "status": status,
                    "what_manager_did": "Менеджер выполнил шаг", "contact_confirmed": contact_confirmed,
                    "target_result_achieved": target_achieved, "evidence": ["транскрипт клиента"],
                    "next_action_required": True, "next_action_text": "Зафиксировать следующий шаг",
                    "next_action_at": "2026-08-11T12:00:00+03:00", "next_action_reason": "Контроль",
                }
                first = apply_deal_recommendation_feedback(db_path, "101", feedback, new_report_id, self._analysis("Fallback"))
                second = apply_deal_recommendation_feedback(db_path, "101", feedback, new_report_id, self._analysis("Fallback"))
                self.assertEqual(first["id"], second["id"])
                self.assertEqual(list_deal_control_tasks(db_path)[0]["recommendation_state"], expected_state)
                with connect(db_path) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_outcomes WHERE source_role='system'").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_events WHERE event_type='system_outcome'").fetchone()[0], 1)

    def test_feedback_missing_evidence_or_next_step_is_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            old_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis())
            task = materialize_deal_recommendation_from_report(db_path, "101", old_report_id, self._analysis())
            new_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis("Новый шаг"))
            feedback = {
                "applicable": True, "source_report_id": old_report_id, "status": "achieved",
                "what_manager_did": "Неясно", "contact_confirmed": True,
                "target_result_achieved": True, "evidence": [],
                "next_action_required": False, "next_action_text": None,
                "next_action_at": None, "next_action_reason": None,
            }
            apply_deal_recommendation_feedback(db_path, "101", feedback, new_report_id)
            self.assertEqual(list_deal_control_tasks(db_path)[0]["recommendation_state"], "unconfirmed")

    def test_feedback_same_key_is_idempotent_under_concurrent_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            self._save_deal(db_path)
            old_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis())
            materialize_deal_recommendation_from_report(db_path, "101", old_report_id, self._analysis())
            new_report_id = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json=self._analysis("Новый шаг"))
            feedback = {
                "applicable": True, "source_report_id": old_report_id, "status": "contacted",
                "what_manager_did": "Менеджер получил ответ клиента", "contact_confirmed": True,
                "target_result_achieved": False, "evidence": ["transcript"],
                "next_action_required": True, "next_action_text": "Зафиксировать следующий шаг",
                "next_action_at": "2026-08-11T12:00:00+03:00", "next_action_reason": "Контроль",
            }

            def apply_once(_index: int) -> dict:
                result = apply_deal_recommendation_feedback(
                    db_path, "101", feedback, new_report_id, self._analysis("Fallback")
                )
                assert result is not None
                return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(apply_once, (1, 2)))
            self.assertEqual(results[0]["id"], results[1]["id"])
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_outcomes WHERE source_role='system'").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_control_task_events WHERE event_type='system_outcome'").fetchone()[0], 1)

    def test_deal_recommendation_materialization_contract_requires_canonical_fields(self) -> None:
        valid = self._analysis()
        validate_deal_recommendation_materialization(valid)
        for field, value in (("deadline", None), ("deadline", "завтра")):
            invalid = self._analysis()
            invalid["rop_manager_message_block"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_deal_recommendation_materialization(invalid)
        invalid_channel = self._analysis()
        invalid_channel["manager_action_block"]["recommended_channel"] = None
        with self.assertRaises(ValueError):
            validate_deal_recommendation_materialization(invalid_channel)

    def test_feedback_validator_rejects_inconsistent_combinations(self) -> None:
        neutral = {
            "applicable": False, "source_report_id": None, "status": "unconfirmed",
            "what_manager_did": None, "contact_confirmed": False, "target_result_achieved": False,
            "evidence": [], "next_action_required": False, "next_action_text": None,
            "next_action_at": None, "next_action_reason": None,
        }
        errors: list[str] = []
        _validate_recommendation_feedback(neutral, errors)
        self.assertEqual(errors, [])
        invalid = dict(neutral)
        invalid.update({"applicable": True, "source_report_id": 1, "status": "contacted", "contact_confirmed": True, "target_result_achieved": True})
        _validate_recommendation_feedback(invalid, errors)
        self.assertTrue(errors)

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
        self.assertIn('"recommendation_feedback"', prompt)
        self.assertIn("<recommendation_feedback_rules>", prompt)
        self.assertIn('"task_id": 7', prompt)
        self.assertNotIn("report_json", prompt)
        self.assertNotIn("quick_help", prompt)
        self.assertIn("## PRIOR_NEURO_ROP_RECOMMENDATION\n\nnull", build_prompt("101", "", "", "", [], {}))

    def test_collect_results_materializes_only_after_successful_deal_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            analysis_path.write_text(
                json.dumps({"analysis_run_id": 73, "analysis": self._analysis()}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths = {"analysis_json": analysis_path, "report_md": root / "deal_101.md", "error_json": root / "error.json"}
            job = JobState(job_id="job")
            calls: list[str] = []
            saved_kwargs: dict[str, object] = {}

            def save(*_args, **kwargs):
                calls.append("save")
                saved_kwargs.update(kwargs)
                return 42

            def materialize(*_args, **_kwargs):
                calls.append("materialize")
                return {"id": 9}

            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "get_ui_report_by_analysis_run_id", return_value=None), \
                 patch.object(jobs, "save_ui_report", side_effect=save), \
                 patch.object(jobs, "apply_deal_recommendation_feedback", side_effect=lambda *_args, **_kwargs: calls.append("apply")), \
                 patch.object(jobs, "materialize_deal_recommendation_from_report", side_effect=materialize):
                jobs._collect_results(job, "deal", ["101"])
            self.assertEqual(calls, ["save", "apply", "materialize"])
            self.assertEqual(saved_kwargs["analysis_run_id"], 73)

    def test_collect_results_reuses_existing_report_for_same_analysis_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            analysis_path.write_text(
                json.dumps({"analysis_run_id": 73, "analysis": self._analysis()}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths = {"analysis_json": analysis_path, "report_md": root / "deal_101.md", "error_json": root / "error.json"}
            job = JobState(job_id="job")
            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "get_ui_report_by_analysis_run_id", return_value={"id": 15}), \
                 patch.object(jobs, "save_ui_report") as save, \
                 patch.object(jobs, "materialize_deal_recommendation_from_report") as materialize:
                jobs._collect_results(job, "deal", ["101"])
            save.assert_not_called()
            materialize.assert_not_called()
            self.assertEqual(job.report_ids, [15])
            self.assertEqual(job.results[0]["report_id"], 15)

    def test_collect_results_rejects_invalid_deal_recommendation_before_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis_path = root / "deal_101_analysis.json"
            invalid = self._analysis()
            invalid["rop_manager_message_block"]["deadline"] = None
            analysis_path.write_text(json.dumps({"analysis": invalid}, ensure_ascii=False), encoding="utf-8")
            paths = {"analysis_json": analysis_path, "report_md": root / "deal_101.md", "error_json": root / "error.json"}
            job = JobState(job_id="job")
            with patch.object(jobs, "analysis_paths", return_value=paths), \
                 patch.object(jobs, "save_ui_report") as save, \
                 patch.object(jobs, "materialize_deal_recommendation_from_report") as materialize:
                with self.assertRaises(ValueError):
                    jobs._collect_results(job, "deal", ["101"])
            save.assert_not_called()
            materialize.assert_not_called()

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
