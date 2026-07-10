from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.compare_attention_delta import compare_case
from benchmarks.run_attention_delta_shadow import run_shadow_case, verify_api_limits
from openai_api.config import ATTENTION_DELTA_MAX_OUTPUT_TOKENS
from openai_api.llm.attention_delta import (
    build_deal_attention_delta_prompt,
    build_lead_attention_delta_prompt,
    deal_attention_delta_schema,
    lead_attention_delta_schema,
    validate_deal_attention_delta,
    validate_lead_attention_delta,
)
from openai_api.llm.attention_delta_report import render_attention_delta_preview
from openai_api.llm.llm_client import call_structured_output_json
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
        "deal_review": None,
    }


def lead_delta() -> dict:
    value = deal_delta()
    value["entity_type"] = "lead"
    value["entity_id"] = "99"
    value.pop("deal_review")
    value["lead_review"] = {"qualification": "B", "final_verdict": "bad_processing"}
    return value


class AttentionDeltaSchemaTests(unittest.TestCase):
    def test_valid_deal_delta_passes_schema(self) -> None:
        validate_deal_attention_delta(deal_delta())
        self.assertFalse(deal_attention_delta_schema()["additionalProperties"])

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


class AttentionDeltaShadowRunnerTests(unittest.TestCase):
    def _case(self, root: Path) -> tuple[dict, Path, Path]:
        history = root / "history.md"
        transcript = root / "transcript.md"
        diagnostics = root / "diagnostics.md"
        knowledge = root / "index.md"
        history.write_text("history activity:42:1", encoding="utf-8")
        transcript.write_text("transcript", encoding="utf-8")
        diagnostics.write_text("diagnostics", encoding="utf-8")
        knowledge.write_text("OKF", encoding="utf-8")
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
