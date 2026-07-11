from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.compare_attention_delta import compare_case
from benchmarks.run_attention_delta_shadow import build_shadow_request, load_shadow_inputs, run_shadow_case, verify_api_limits
from openai_api.config import ATTENTION_DELTA_MAX_OUTPUT_TOKENS
from openai_api.llm.attention_delta import (
    build_deal_attention_delta_prompt,
    build_lead_attention_delta_prompt,
    deal_attention_delta_schema,
    lead_attention_delta_schema,
    materialize_deal_attention_delta,
    materialize_lead_attention_delta,
    validate_deal_attention_delta,
    validate_lead_attention_delta,
)
from openai_api.llm.attention_delta_report import render_attention_delta_preview
from openai_api.llm.llm_client import ModelResponseIncompleteError, call_structured_output_json
from openai_api.llm.prompt_budget import build_prompt_budget


def deal_delta(*, attention_required: bool = True) -> dict:
    return {
        "entity_type": "deal",
        "entity_id": "42",
        "attention_required": attention_required,
        "severity": "high",
        "reason": "Нет подтвержденного следующего шага.",
        "rop_action": None
        if not attention_required
        else {
            "check": "Проверить срок решения.",
            "message_to_manager": "Свяжитесь с клиентом и внесите срок решения в CRM.",
            "expected_crm_fact": "Дата решения клиента.",
            "deadline": "2026-07-11",
            "success_condition": "В CRM есть дата и результат контакта.",
            "owner": None,
            "evidence_ids": ["activity:42:1"],
        },
        "memory_patch": {
            "confirmed_facts_add": [],
            "open_questions_add": ["Срок решения"],
            "open_questions_resolve": [],
            "risks_add": ["Нет следующего шага"],
            "risks_resolve": [],
            "next_step": "Получить срок решения",
        },
        "deal_review": {
            "type": "other",
            "decision": "manager_action_required",
            "action_playbook": "manual_context_audit" if attention_required else "none",
            "closure_status": "not_applicable",
            "technical_input_status": "not_applicable",
            "required_technical_inputs": [],
            "invoice_status": "not_applicable",
            "invoice_agreed": False,
            "payment_intent_confirmed": False,
            "advance_agreed": False,
            "contract_signed": False,
            "payment_date_confirmed": False,
            "customer_compares_options": False,
            "comparison_subject_known": False,
            "price_or_terms_gap_known": False,
            "budget_not_disclosed_confirmed": False,
            "competitor_confirmed": False,
            "confirmed_refusal": False,
            "budget_known": False,
            "decision_maker_known": False,
            "decision_date_known": False,
            "clarifying_contact_completed": False,
            "next_step_confirmed": False,
            "price_competitor_risk": "none",
        },
    }


def lead_delta() -> dict:
    value = deal_delta()
    value["entity_type"] = "lead"
    value["entity_id"] = "99"
    value.pop("deal_review")
    value["lead_review"] = {
        "qualification": "B",
        "lead_quality": "good",
        "processing_quality": "good",
        "final_verdict": "ready_for_deal",
        "meaningful_contact": True,
        "action_playbook": "qualification_followup",
    }
    return value


def no_contact_bad_processing_delta() -> dict:
    value = lead_delta()
    value["lead_review"] = {
        "qualification": "unknown",
        "lead_quality": "unknown",
        "processing_quality": "bad",
        "final_verdict": "bad_processing",
        "meaningful_contact": False,
        "action_playbook": "restore_no_contact_processing",
    }
    value["reason"] = "Подтверждённого содержательного контакта и CRM-следа обработки нет; связка контакта неполна."
    value["rop_action"]["check"] = "Проверить отсутствие подтверждённой обработки."
    value["rop_action"]["deadline"] = None
    value["rop_action"]["evidence_ids"] = ["lead:99", "diag:contact_resolution", "diag:task_comments"]
    return value


def assert_strict_object_nodes(test: unittest.TestCase, node: object, path: str = "$") -> None:
    """Recursively enforce the OpenAI strict-schema object contract."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            test.assertIn("required", node, path)
            test.assertEqual(set(node["required"]), set(properties), path)
            test.assertIs(node.get("additionalProperties"), False, path)
        for key, value in node.items():
            assert_strict_object_nodes(test, value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            assert_strict_object_nodes(test, value, f"{path}[{index}]")


class AttentionDeltaSchemaTests(unittest.TestCase):
    def test_valid_deal_delta_passes_schema(self) -> None:
        validate_deal_attention_delta(deal_delta())
        self.assertFalse(deal_attention_delta_schema()["additionalProperties"])
        self.assertIsNone(deal_delta()["rop_action"]["owner"])

    def test_all_structured_output_object_nodes_are_strict(self) -> None:
        assert_strict_object_nodes(self, deal_attention_delta_schema())
        assert_strict_object_nodes(self, lead_attention_delta_schema())

    def test_valid_lead_delta_passes_schema(self) -> None:
        validate_lead_attention_delta(lead_delta())
        self.assertFalse(lead_attention_delta_schema()["additionalProperties"])

    def test_extra_field_and_unknown_enum_are_rejected(self) -> None:
        extra = deal_delta()
        extra["legacy_payload"] = {}
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            validate_deal_attention_delta(extra)
        unknown = lead_delta()
        unknown["lead_review"]["qualification"] = "Z"
        with self.assertRaisesRegex(ValueError, "invalid enum"):
            validate_lead_attention_delta(unknown)

    def test_no_attention_requires_null_action_and_evidence_is_bounded(self) -> None:
        no_attention = deal_delta(attention_required=False)
        no_attention["rop_action"] = deal_delta()["rop_action"]
        with self.assertRaisesRegex(ValueError, "must be null"):
            validate_deal_attention_delta(no_attention)
        too_many_evidence = deal_delta()
        too_many_evidence["rop_action"]["evidence_ids"] = [f"activity:42:{index}" for index in range(8)]
        with self.assertRaisesRegex(ValueError, "too many items"):
            validate_deal_attention_delta(too_many_evidence)

    def test_no_contact_bad_processing_requires_restore_playbook(self) -> None:
        delta = materialize_lead_attention_delta(no_contact_bad_processing_delta(), today=date(2031, 2, 3))
        validate_lead_attention_delta(delta)
        self.assertEqual(delta["lead_review"]["action_playbook"], "restore_no_contact_processing")
        self.assertIn("3 попытки звонка", delta["rop_action"]["message_to_manager"])

    def test_bad_lead_without_meaningful_contact_is_rejected(self) -> None:
        delta = no_contact_bad_processing_delta()
        delta["lead_review"]["final_verdict"] = "bad_lead"
        with self.assertRaisesRegex(ValueError, "bad_lead requires"):
            validate_lead_attention_delta(materialize_lead_attention_delta(delta, today=date(2031, 2, 3)))

    def test_invalid_number_and_meaningful_contact_paths_do_not_force_three_calls(self) -> None:
        invalid_number = lead_delta()
        invalid_number["lead_review"] = {
            "qualification": "unknown",
            "lead_quality": "unknown",
            "processing_quality": "unknown",
            "final_verdict": "data_gap",
            "meaningful_contact": False,
            "action_playbook": "verify_invalid_number",
        }
        validate_lead_attention_delta(invalid_number)
        confirmed_contact = lead_delta()
        self.assertNotEqual(confirmed_contact["lead_review"]["action_playbook"], "restore_no_contact_processing")
        validate_lead_attention_delta(confirmed_contact)

    def test_null_lead_review_is_rejected(self) -> None:
        delta = lead_delta()
        delta["lead_review"] = None
        with self.assertRaisesRegex(ValueError, "lead_review must be an object"):
            validate_lead_attention_delta(delta)

    def test_playbook_uses_provided_date_and_preserves_case_specific_fields(self) -> None:
        delta = no_contact_bad_processing_delta()
        delta["reason"] = "Case-specific reason must remain intact."
        delta["severity"] = "high"
        delta["rop_action"]["check"] = "Проверить доступную историю и карточку."
        delta["rop_action"]["deadline"] = "2032-04-05"
        delta["rop_action"]["evidence_ids"] = ["lead:99", "diag:custom"]
        review_before = dict(delta["lead_review"])
        result = materialize_lead_attention_delta(delta, today=date(2031, 2, 3))
        self.assertEqual(result["reason"], "Case-specific reason must remain intact.")
        self.assertEqual(result["severity"], "high")
        self.assertIn("Проверить доступную историю и карточку.", result["rop_action"]["check"])
        self.assertEqual(result["rop_action"]["deadline"], "2032-04-05")
        self.assertEqual(result["rop_action"]["evidence_ids"], ["lead:99", "diag:custom"])
        self.assertEqual(result["lead_review"], review_before)

    def test_playbook_calculates_deadline_from_supplied_run_date(self) -> None:
        delta = no_contact_bad_processing_delta()
        result = materialize_lead_attention_delta(delta, today=date(2033, 6, 7))
        self.assertEqual(result["rop_action"]["deadline"], "2033-06-07")

    def test_invoice_price_competitor_playbook_requires_clarification_not_payment_control(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "customer_compares_options": True,
                "price_competitor_risk": "suspected",
            }
        )
        result = materialize_deal_attention_delta(delta, today=date(2031, 2, 3))
        validate_deal_attention_delta(result)
        action = result["rop_action"]
        self.assertEqual(action["owner"], "Ответственный менеджер сделки")
        self.assertEqual(action["deadline"], "2026-07-11")
        for required_question in ("что именно сравнивает", "реальный бюджет", "кто принимает", "когда оно будет принято", "счёт согласованным"):
            self.assertIn(required_question, action["message_to_manager"])
        for required_fact in ("предмет сравнения", "бюджет", "ЛПР", "дата решения", "статус согласования счёта"):
            self.assertIn(required_fact, action["expected_crm_fact"])
        self.assertIn("Не закрывайте", action["message_to_manager"])
        self.assertNotIn("контролировать оплату", action["message_to_manager"].lower())

    def test_invoice_playbook_allows_documented_closure_only_after_confirmed_refusal(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "confirmed_refusal": True,
                "clarifying_contact_completed": True,
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertIn("подтверждённом явном отказе", result["rop_action"]["message_to_manager"])

    def test_invoice_raw_completed_flag_is_normalized_false_when_required_facts_are_missing(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "customer_compares_options": True,
                "clarifying_contact_completed": True,
                "price_competitor_risk": "confirmed",
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertFalse(result["deal_review"]["clarifying_contact_completed"])
        self.assertEqual(result["deal_review"]["action_playbook"], "invoice_price_competitor_risk")
        self.assertEqual(result["deal_review"]["decision"], "manager_action_required")
        self.assertIn("\u041d\u0435 \u0437\u0430\u043a\u0440\u044b\u0432\u0430\u0439\u0442\u0435", result["rop_action"]["message_to_manager"])
        self.assertIn("\u0443\u0442\u043e\u0447\u043d\u044f\u044e\u0449", result["rop_action"]["message_to_manager"])

    def test_invoice_completed_flag_becomes_true_only_with_all_required_facts_and_next_step(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "customer_compares_options": True,
                "comparison_subject_known": True,
                "price_or_terms_gap_known": True,
                "budget_known": True,
                "decision_maker_known": True,
                "decision_date_known": True,
                "next_step_confirmed": True,
                "clarifying_contact_completed": False,
                "price_competitor_risk": "confirmed",
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertTrue(result["deal_review"]["clarifying_contact_completed"])
        self.assertIn("\u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u044b\u0439 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0448\u0430\u0433", result["rop_action"]["expected_crm_fact"])

    def test_invoice_explicit_refusal_with_evidence_normalizes_completed_true(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "confirmed_refusal": True,
                "clarifying_contact_completed": False,
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertTrue(result["deal_review"]["clarifying_contact_completed"])
        self.assertIn("\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u043e\u043c \u044f\u0432\u043d\u043e\u043c \u043e\u0442\u043a\u0430\u0437\u0435", result["rop_action"]["message_to_manager"])

    def test_invoice_likely_refusal_without_confirmation_stays_in_clarification_path(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "invoice_price_competitor_risk",
                "invoice_status": "sent_unconfirmed",
                "clarifying_contact_completed": True,
                "price_competitor_risk": "confirmed",
                "confirmed_refusal": False,
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertFalse(result["deal_review"]["clarifying_contact_completed"])
        self.assertIn("\u041d\u0435 \u0437\u0430\u043a\u0440\u044b\u0432\u0430\u0439\u0442\u0435", result["rop_action"]["message_to_manager"])

    def test_dated_technical_input_playbook_keeps_sale_active_and_separates_dates(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "action_playbook": "dated_technical_input_control",
                "technical_input_status": "internal_control_only",
                "required_technical_inputs": ["фото тары", "размеры", "видео продукта"],
            }
        )
        result = materialize_deal_attention_delta(delta, today=date(2031, 2, 3))
        validate_deal_attention_delta(result)
        action = result["rop_action"]
        self.assertEqual(action["owner"], "Ответственный менеджер сделки")
        self.assertIn("фото тары; размеры; видео продукта", action["message_to_manager"])
        self.assertIn("Внутренний срок контроля не является обещанием клиента", action["message_to_manager"])
        self.assertIn("Не запускайте глубокую инженерную проработку", action["message_to_manager"])
        self.assertIn("дата ещё не согласована", action["success_condition"])

    def test_disputed_closed_playbook_does_not_reopen_confirmed_closure(self) -> None:
        delta = deal_delta()
        review = delta["deal_review"]
        review.update(
            {
                "type": "closed_wrong_qualification",
                "decision": "keep_current_state",
                "action_playbook": "disputed_closed_deal_review",
                "closure_status": "confirmed",
            }
        )
        result = materialize_deal_attention_delta(delta)
        validate_deal_attention_delta(result)
        self.assertIn("Не возвращайте сделку", result["rop_action"]["message_to_manager"])


class AttentionDeltaPromptTests(unittest.TestCase):
    def test_compact_prompt_keeps_grounding_without_legacy_contract(self) -> None:
        prompt = build_deal_attention_delta_prompt(
            "42", "history activity:42:1", "transcript", "diagnostics", [(Path("index.md"), "OKF rules")], {"is_closed_lost": False}
        )
        self.assertIn("<grounding_rules>", prompt)
        self.assertIn("Не выдумывай факты", prompt)
        self.assertNotIn("Нужная JSON-структура", prompt)
        self.assertNotIn("<structured_output_contract>", prompt)
        budget = build_prompt_budget(
            prompt=prompt,
            model="test-model",
            history_text="history activity:42:1",
            transcript_text="transcript",
            diagnostics_text="diagnostics",
            okf_sections=[(Path("index.md"), "OKF rules")],
            stage_policy={"is_closed_lost": False},
        )
        self.assertEqual(budget["total"]["unaccounted_chars"], 0)

    def test_preview_is_deterministic(self) -> None:
        first = render_attention_delta_preview(deal_delta())
        self.assertEqual(first, render_attention_delta_preview(deal_delta()))
        self.assertIn("## Что требует внимания", first)
        self.assertIn("activity:42:1", first)

    def test_lead_preview_contains_restore_playbook_rules(self) -> None:
        preview = render_attention_delta_preview(
            materialize_lead_attention_delta(no_contact_bad_processing_delta(), today=date(2031, 2, 3))
        )
        for fragment in ("Три попытки", "2 часов", "мессенджер", "результат каждой попытки", "следующим шагом", "2031-02-03"):
            self.assertIn(fragment, preview)

    def test_lead_prompt_has_clean_section_boundaries_and_spacing(self) -> None:
        prompt = build_lead_attention_delta_prompt(
            "99",
            "CRM facts.",
            "Transcript facts.",
            "## CONTEXT_COMPLETENESS\nDiagnostics facts.",
            [(Path("index.md"), "OKF facts.")],
        )
        self.assertNotIn("анализ" + ":" + "верни", prompt)
        self.assertNotIn("фактов" + "о клиенте", prompt)
        self.assertIn("shadow-анализ: верни", prompt)
        self.assertIn("фактов о клиенте", prompt)
        self.assertIn("## CRM HISTORY\nCRM facts.\n\n## TRANSCRIPT OR NEW EVENT\nTranscript facts.", prompt)
        self.assertIn("## CONTEXT_COMPLETENESS\nDiagnostics facts.\n\n## OKF RULES", prompt)

    def test_deal_contract_and_prompt_use_only_deal_playbooks(self) -> None:
        prompt = build_deal_attention_delta_prompt(
            "42", "history", "transcript", "diagnostics", [(Path("index.md"), "OKF")], {"is_closed_lost": False}
        )
        self.assertNotIn("restore_no_contact_processing", prompt)
        self.assertIn("action_playbook", deal_attention_delta_schema()["properties"]["deal_review"]["anyOf"][0]["properties"])
        self.assertIn("invoice_price_competitor_risk", prompt)


class AttentionDeltaShadowRunnerTests(unittest.TestCase):
    @staticmethod
    def _write_attention_packs(root: Path) -> None:
        (root / "attention_delta_core.md").write_text("Core pack", encoding="utf-8")
        (root / "attention_delta_lead.md").write_text("Lead pack", encoding="utf-8")
        (root / "attention_delta_deal.md").write_text("Deal pack", encoding="utf-8")

    def _case(self, root: Path) -> tuple[dict, Path, Path]:
        history = root / "history.md"
        transcript = root / "transcript.md"
        diagnostics = root / "diagnostics.md"
        knowledge = root / "index.md"
        history.write_text("history activity:42:1", encoding="utf-8")
        transcript.write_text("transcript", encoding="utf-8")
        diagnostics.write_text("diagnostics", encoding="utf-8")
        knowledge.write_text("OKF", encoding="utf-8")
        self._write_attention_packs(root)
        analysis = root / "deal_42_analysis.json"
        analysis.write_text(
            json.dumps(
                {
                    "deal_id": "42",
                    "crm_stage_policy": {"is_closed_lost": False},
                    "input_files": {
                        "history": str(history),
                        "transcript": str(transcript),
                        "context_diagnostics": [str(diagnostics)],
                        "knowledge": [str(knowledge)],
                    },
                }
            ),
            encoding="utf-8",
        )
        return {"case_id": "deal-01", "entity_type": "deal", "baseline": {"analysis_json": str(analysis)}}, analysis, root / "shadow"

    def _lead_case(self, root: Path) -> tuple[dict, Path]:
        history = root / "lead_history.md"
        transcript = root / "lead_transcript.md"
        diagnostics = root / "diagnostics.md"
        knowledge = root / "index.md"
        history.write_text("history call-busy", encoding="utf-8")
        transcript.write_text(
            "### Звонок: activity_id=call-busy\n\n- Дата звонка: 2031-02-03T10:00:00+03:00\n\n```text\n"
            "Линия занята, абоненту неудобно сейчас говорить. Я голосовой ассистент.\n```",
            encoding="utf-8",
        )
        diagnostics.write_text("diagnostics", encoding="utf-8")
        knowledge.write_text("OKF", encoding="utf-8")
        self._write_attention_packs(root)
        analysis = root / "lead_99_analysis.json"
        analysis.write_text(
            json.dumps(
                {
                    "lead_id": "99",
                    "input_files": {
                        "history": str(history),
                        "transcript": str(transcript),
                        "context_diagnostics": [str(diagnostics)],
                        "knowledge": [str(knowledge)],
                    },
                }
            ),
            encoding="utf-8",
        )
        return {"case_id": "lead-busy", "entity_type": "lead", "baseline": {"analysis_json": str(analysis)}}, root / "shadow"

    def test_without_allow_api_does_not_call_openai_or_overwrite_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, analysis, output = self._case(Path(directory))
            before = analysis.read_bytes()
            with patch("benchmarks.run_attention_delta_shadow.call_structured_output_json") as call:
                result = run_shadow_case(case, output_root=output, allow_api=False, model="test-model")
            self.assertEqual(result["status"], "inputs_ready_no_api_call")
            call.assert_not_called()
            self.assertEqual(before, analysis.read_bytes())
            self.assertTrue((output / "deal-01" / "attention_delta_prompt_budget.json").exists())
            self.assertFalse((output / "deal-01" / "attention_delta.json").exists())
            self.assertTrue(result["prompt_metrics"]["uses_real_transcript"])
            self.assertEqual(result["prompt_metrics"]["knowledge_packs"], ["core", "deal"])
            metadata = json.loads((output / "deal-01" / "attention_delta_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["knowledge_selection"]["selected_pack_ids"], ["core", "deal"])

    def test_shadow_prompt_uses_one_completeness_block_and_excludes_raw_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, _analysis, _output = self._case(Path(directory))
            raw_path = Path(case["baseline"]["analysis_json"]).parent / "diagnostics.md"
            raw_path.write_text("RAW_DIAGNOSTIC_PATH=C:/private/recovery.ps1", encoding="utf-8")
            inputs = load_shadow_inputs(case)
            prompt, schema, schema_name, _validator = build_shadow_request(inputs)
        self.assertIn("## CONTEXT_COMPLETENESS", prompt)
        self.assertEqual(prompt.count("## CONTEXT_COMPLETENESS"), 1)
        self.assertNotIn("RAW_DIAGNOSTIC_PATH", prompt)
        self.assertIn("history activity:42:1", prompt)
        self.assertIn("transcript", prompt)
        self.assertEqual(schema_name, "deal_attention_delta")
        self.assertEqual(schema, deal_attention_delta_schema())

    def test_shadow_prompt_omits_diagnostics_section_when_legacy_saved_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, analysis, _output = self._case(Path(directory))
            payload = json.loads(analysis.read_text(encoding="utf-8"))
            payload["input_files"]["context_diagnostics"] = []
            analysis.write_text(json.dumps(payload), encoding="utf-8")
            inputs = load_shadow_inputs(case)
            prompt, _schema, _schema_name, _validator = build_shadow_request(inputs)
        self.assertEqual(inputs["diagnostics_text"], "")
        self.assertEqual(inputs["diagnostics_raw_text"], "")
        self.assertNotIn("## CONTEXT_COMPLETENESS", prompt)

    def test_lead_busy_normalization_is_saved_in_metadata_before_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, output = self._lead_case(Path(directory))
            raw = no_contact_bad_processing_delta()
            metadata = {
                "model": "test-model",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "estimated_cost": {"estimated_cost_rub": 1},
                "raw_output_text": json.dumps(raw, ensure_ascii=False),
                "response_status": "completed",
                "incomplete_reason": None,
            }
            with patch("benchmarks.run_attention_delta_shadow.call_structured_output_json", return_value=(raw, metadata)):
                result = run_shadow_case(case, output_root=output, allow_api=True, model="test-model")
            saved = json.loads((output / "lead-busy" / "attention_delta.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        audit = saved["model_metadata"]["lead_playbook_normalization"]
        self.assertEqual(audit["raw_action_playbook"], "restore_no_contact_processing")
        self.assertEqual(audit["normalized_action_playbook"], "retry_busy_number")
        self.assertEqual(audit["normalization_evidence_ids"], ["call-busy"])
        self.assertEqual(saved["attention_delta"]["lead_review"]["action_playbook"], "retry_busy_number")

    def test_usage_is_saved_when_response_fails_local_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, _analysis, output = self._case(Path(directory))
            invalid = deal_delta()
            invalid["severity"] = "urgent"
            metadata = {"model": "test-model", "usage": {"input_tokens": 10, "output_tokens": 2}, "estimated_cost": {"estimated_cost_rub": 1}, "raw_output_text": "{}"}
            with patch("benchmarks.run_attention_delta_shadow.call_structured_output_json", return_value=(invalid, metadata)):
                with self.assertRaisesRegex(ValueError, "invalid severity"):
                    run_shadow_case(case, output_root=output, allow_api=True, model="test-model")
            budget = json.loads((output / "deal-01" / "attention_delta_prompt_budget.json").read_text(encoding="utf-8"))
            self.assertEqual(budget["actual_usage"]["output_tokens"], 2)

    def test_incomplete_output_limit_saves_usage_without_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            case, _analysis, output = self._case(Path(directory))
            response_metadata = {
                "model": "test-model",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
                    "output_tokens_details": {"reasoning_tokens": 1200},
                },
                "estimated_cost": {"estimated_cost_rub": 1},
                "raw_output_text": "{\"partial\": true",
                "response_status": "incomplete",
                "incomplete_reason": "max_output_tokens",
            }
            error = ModelResponseIncompleteError("Structured output is incomplete", response_metadata["raw_output_text"], response_metadata)
            with patch("benchmarks.run_attention_delta_shadow.call_structured_output_json", side_effect=error):
                result = run_shadow_case(case, output_root=output, allow_api=True, model="test-model")
            self.assertEqual(result["status"], "output_limit_exceeded")
            self.assertFalse((output / "deal-01" / "attention_delta.json").exists())
            self.assertFalse((output / "deal-01" / "attention_delta_preview.md").exists())
            metrics = result["response_metrics"]
            self.assertEqual(metrics["output_tokens"], ATTENTION_DELTA_MAX_OUTPUT_TOKENS)
            self.assertEqual(metrics["reasoning_tokens"], 1200)
            self.assertEqual(metrics["incomplete_reason"], "max_output_tokens")
            budget = json.loads((output / "deal-01" / "attention_delta_prompt_budget.json").read_text(encoding="utf-8"))
            self.assertEqual(budget["actual_usage"]["output_tokens"], ATTENTION_DELTA_MAX_OUTPUT_TOKENS)


class StructuredOutputClientTests(unittest.TestCase):
    def test_uses_responses_strict_json_schema(self) -> None:
        response = SimpleNamespace(
            id="resp_test",
            output_text=json.dumps(deal_delta(), ensure_ascii=False),
            usage={"input_tokens": 10, "output_tokens": 2},
        )
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response) as create:
            parsed, metadata = call_structured_output_json(
                "compact prompt", schema=deal_attention_delta_schema(), schema_name="deal_attention_delta", model="test-model"
            )
        self.assertEqual(parsed["entity_id"], "42")
        self.assertEqual(metadata["usage"]["output_tokens"], 2)
        self.assertTrue(create.call_args.kwargs["text"]["format"]["strict"])
        self.assertEqual(create.call_args.kwargs["text"]["format"]["type"], "json_schema")
        self.assertEqual(create.call_args.kwargs["max_output_tokens"], ATTENTION_DELTA_MAX_OUTPUT_TOKENS)

    def test_incomplete_response_is_not_parsed_as_attention_delta(self) -> None:
        response = SimpleNamespace(
            id="resp_incomplete",
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text="{\"entity_type\": \"deal\"",
            usage={"input_tokens": 10, "output_tokens": ATTENTION_DELTA_MAX_OUTPUT_TOKENS},
        )
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response):
            with self.assertRaises(ModelResponseIncompleteError) as captured:
                call_structured_output_json(
                    "compact prompt", schema=deal_attention_delta_schema(), schema_name="deal_attention_delta", model="test-model"
                )
        self.assertEqual(captured.exception.metadata["incomplete_reason"], "max_output_tokens")
        self.assertEqual(captured.exception.metadata["usage"]["output_tokens"], ATTENTION_DELTA_MAX_OUTPUT_TOKENS)


class AttentionDeltaSafetyLimitTests(unittest.TestCase):
    def test_api_limits_require_explicit_caps_and_reject_excess(self) -> None:
        prepared = [
            {
                "case": {"case_id": "deal-01"},
                "ready_for_api": True,
                "not_ready_reasons": [],
                "upper_estimated_cost": {"estimated_cost_rub": 2.5},
            },
            {
                "case": {"case_id": "lead-01"},
                "ready_for_api": True,
                "not_ready_reasons": [],
                "upper_estimated_cost": {"estimated_cost_rub": 2.5},
            },
        ]
        with self.assertRaisesRegex(ValueError, "requires a positive --max-cases"):
            verify_api_limits(prepared, max_cases=None, max_estimated_cost_rub=10)
        with self.assertRaisesRegex(ValueError, "exceeding --max-cases"):
            verify_api_limits(prepared, max_cases=1, max_estimated_cost_rub=10)
        with self.assertRaisesRegex(ValueError, "Expected upper cost"):
            verify_api_limits(prepared, max_cases=2, max_estimated_cost_rub=4)
        self.assertEqual(verify_api_limits(prepared, max_cases=2, max_estimated_cost_rub=5), 5.0)


class AttentionDeltaComparatorTests(unittest.TestCase):
    def test_comparison_contains_manual_review_and_token_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "legacy.json"
            shadow_root = root / "shadow"
            legacy_path.write_text(
                json.dumps(
                    {
                        "analysis": {
                            "main_risk": {"summary": "Legacy risk"},
                            "rop_manager_message_block": {"message_to_manager": "Legacy action", "evidence": ["legacy:1"]},
                        },
                        "model_metadata": {"usage": {"input_tokens": 100, "output_tokens": 20}},
                    }
                ),
                encoding="utf-8",
            )
            compact_dir = shadow_root / "deal-01"
            compact_dir.mkdir(parents=True)
            compact_dir.joinpath("attention_delta.json").write_text(
                json.dumps({"attention_delta": deal_delta(), "model_metadata": {"usage": {"input_tokens": 100, "output_tokens": 10}}}),
                encoding="utf-8",
            )
            result = compare_case({"case_id": "deal-01", "entity_type": "deal", "baseline": {"analysis_json": str(legacy_path)}}, shadow_root)
            self.assertEqual(result["output_token_reduction_percent"], 50.0)
            self.assertIn("same_management_decision_possible", result["manual_review"]["scores"])


if __name__ == "__main__":
    unittest.main()
