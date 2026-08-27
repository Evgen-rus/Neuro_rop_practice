from __future__ import annotations

import unittest
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from openai_api.llm.llm_client import (
    ModelJsonParseError,
    ValidatedAnalysisFailure,
    call_validated_analysis_json,
)
from openai_api.llm.validation_diagnostics import build_validation_diagnostic
from openai_api.llm.usage_trace import build_usage_trace_event


class FakeValidationError(ValueError):
    pass


def metadata(cost: float, raw: str = "{}") -> dict:
    return {
        "model": "test-model",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 1, "cache_write_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "estimated_cost": {"estimated_cost_usd": cost, "estimated_cost_rub": cost * 75},
        "estimated_cost_usd": cost,
        "estimated_cost_rub": cost * 75,
        "raw_output_text": raw,
    }


class ValidatedAnalysisRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.diagnostic_dir = Path(directory.name) / "private"
        env = patch.dict(os.environ, {"OPENAI_VALIDATION_DIAGNOSTICS_DIR": str(self.diagnostic_dir)})
        env.start()
        self.addCleanup(env.stop)

    def test_custom_correction_prompt_builder_replaces_generic_full_prompt(self) -> None:
        calls: list[str] = []

        def caller(prompt: str, **_kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return {"ok": False}, metadata(0.1, '{"ok":false}')
            return {"ok": True}, metadata(0.1, '{"ok":true}')

        result, _metadata = call_validated_analysis_json(
            "LARGE_ORIGINAL_PROMPT",
            validator=lambda value: None if value.get("ok") else (_ for _ in ()).throw(
                FakeValidationError("repair this")
            ),
            normalizer=lambda _value: [],
            validation_error_types=(FakeValidationError,),
            analysis_caller=caller,
            correction_prompt_builder=lambda _original, error, raw: f"REPAIR|{error}|{raw}",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0], "LARGE_ORIGINAL_PROMPT")
        self.assertIn("REPAIR|repair this|", calls[1])
        self.assertNotIn("LARGE_ORIGINAL_PROMPT", calls[1])

    def test_validation_failure_gets_one_correction_attempt(self) -> None:
        calls: list[str] = []

        def caller(prompt: str, **_kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return {"ok": False}, metadata(0.1, '{"ok":false}')
            return {"ok": True}, metadata(0.2, '{"ok":true}')

        def validator(value: dict) -> None:
            if value.get("ok") is not True:
                raise FakeValidationError("ok must be true")

        result, result_metadata = call_validated_analysis_json(
            "ORIGINAL",
            validator=validator,
            normalizer=lambda _value: [],
            validation_error_types=(FakeValidationError,),
            analysis_caller=caller,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertIn("ok must be true", calls[1])
        self.assertEqual(result_metadata["semantic_attempt_count"], 2)
        self.assertEqual(result_metadata["usage"]["total_tokens"], 30)
        self.assertEqual(result_metadata["usage"]["input_tokens_details"]["cache_write_tokens"], 6)
        self.assertEqual(result_metadata["estimated_cost_rub"], 22.5)
        attempts = result_metadata["semantic_attempts"]
        self.assertEqual([item["attempt_number"] for item in attempts], [1, 2])
        self.assertEqual([item["validation_passed"] for item in attempts], [False, True])
        self.assertFalse(attempts[0]["semantic_correction_retry"])
        self.assertTrue(attempts[1]["semantic_correction_retry"])
        self.assertEqual(attempts[0]["input_tokens"], 10)
        self.assertEqual(attempts[0]["cached_tokens"], 1)
        self.assertEqual(attempts[0]["cache_write_tokens"], 3)
        self.assertEqual(attempts[0]["output_tokens"], 5)
        self.assertEqual(attempts[0]["reasoning_tokens"], 2)
        self.assertIn("ok must be true", attempts[0]["validation_error"])
        self.assertIsNone(attempts[1]["validation_error"])
        self.assertNotIn("raw_output_text", attempts[0])

    def test_invalid_json_gets_one_correction_attempt(self) -> None:
        calls = 0

        def caller(_prompt: str, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelJsonParseError("bad json", "{bad", metadata(0.1, "{bad"))
            return {"ok": True}, metadata(0.1, '{"ok":true}')

        result, result_metadata = call_validated_analysis_json(
            "ORIGINAL",
            validator=lambda _value: None,
            normalizer=lambda _value: [],
            validation_error_types=(FakeValidationError,),
            analysis_caller=caller,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result_metadata["semantic_attempt_count"], 2)
        self.assertEqual(
            result_metadata["semantic_attempts"][0]["validation_error"],
            "ModelJsonParseError: invalid JSON response",
        )
        self.assertNotIn("{bad", result_metadata["semantic_attempts"][0]["validation_error"])
        ref = result_metadata["semantic_attempts"][0]["validation_diagnostic_ref"]
        diagnostic = json.loads((self.diagnostic_dir / ref).read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["syntax_excerpt"], "{bad")
        self.assertNotIn("validation_diagnostic_ref", result_metadata["semantic_attempts"][1])

    def test_two_invalid_attempts_raise_final_failure(self) -> None:
        def caller(_prompt: str, **_kwargs):
            return {"ok": False}, metadata(0.1, '{"ok":false}')

        with self.assertRaises(ValidatedAnalysisFailure) as context:
            call_validated_analysis_json(
                "ORIGINAL",
                validator=lambda _value: (_ for _ in ()).throw(FakeValidationError("still invalid")),
                normalizer=lambda _value: [],
                validation_error_types=(FakeValidationError,),
                analysis_caller=caller,
            )
        self.assertEqual(context.exception.metadata["semantic_attempt_count"], 2)
        self.assertEqual(context.exception.analysis, {"ok": False})
        self.assertEqual(
            [item["validation_passed"] for item in context.exception.metadata["semantic_attempts"]],
            [False, False],
        )
        references = [item["validation_diagnostic_ref"] for item in context.exception.metadata["semantic_attempts"]]
        self.assertEqual(len(set(references)), 2)
        self.assertTrue(all((self.diagnostic_dir / ref).is_file() for ref in references))

    def test_private_field_evidence_survives_successful_retry_without_leaking(self) -> None:
        calls = 0
        def caller(_prompt, **_kwargs):
            nonlocal calls
            calls += 1
            return {"block": {"status": " секретный текст " if calls == 1 else "confirmed"}}, metadata(0.1)

        def normalizer(value):
            value["block"]["status"] = value["block"]["status"].strip()
            return []

        def validator(value):
            if value["block"]["status"] != "confirmed":
                raise FakeValidationError("invalid enum at block.status: expected confirmed, got 'секретный текст'")

        with patch("openai_api.llm.llm_client.call_analysis_json", side_effect=caller) as traced_caller:
            with patch("openai_api.llm.llm_client.append_usage_trace") as trace:
                _, result = call_validated_analysis_json(
                    "PROMPT_NOT_FOR_DIAGNOSTICS", validator=validator, normalizer=normalizer,
                    validation_error_types=(FakeValidationError,), analysis_caller=traced_caller,
                )
        self.assertEqual(trace.call_count, 2)
        self.assertTrue(traced_caller.call_args.kwargs["defer_usage_trace"])
        attempt = result["semantic_attempts"][0]
        path = self.diagnostic_dir / attempt["validation_diagnostic_ref"]
        diagnostic = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["issues"][0]["received"]["value"], " секретный текст ")
        self.assertEqual(diagnostic["issues"][0]["validated"]["value"], "секретный текст")
        self.assertEqual(diagnostic["attempt"], 1)
        self.assertNotIn("PROMPT_NOT_FOR_DIAGNOSTICS", path.read_text(encoding="utf-8"))
        self.assertNotIn("секретный текст", json.dumps(result, ensure_ascii=False))
        event = build_usage_trace_event(attempt, status="error")
        self.assertNotIn("секретный текст", json.dumps(event, ensure_ascii=False))
        self.assertEqual(event["validation_diagnostic_ref"], attempt["validation_diagnostic_ref"])
        self.assertEqual(trace.call_args_list[0].args[0]["validation_diagnostic_ref"], attempt["validation_diagnostic_ref"])
        self.assertNotIn("validation_diagnostic_ref", trace.call_args_list[1].args[0])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_diagnostic_extracts_all_observed_error_families_and_related_values(self) -> None:
        original = {
            "recommendation_feedback": {"next_action_at": "2026-08-27"},
            "qualification_assessment": {
                "bant": {"timeframe": {"status": "not_confirmed", "need_or_launch_timing": "В сентябре", "need_or_launch_timing_status": "unknown"}},
                "commercial_fit": {"new_equipment_budget_status": "below_minimum", "confirmed_budget_rub": None},
            },
            "deal_context": {"pain_points": [{"title": "Проблема"}], "journey": [{"title": "Событие"}]},
        }
        paths = [
            "recommendation_feedback.next_action_at", "qualification_assessment.bant.timeframe.status",
            "qualification_assessment.bant.timeframe.need_or_launch_timing", "qualification_assessment.commercial_fit.confirmed_budget_rub",
            "deal_context.pain_points[0].pain_id", "deal_context.journey[0].status",
        ]
        normalized = deepcopy(original)
        normalized["recommendation_feedback"]["next_action_at"] = "2026-08-27T18:00:00+03:00"
        result = build_validation_diagnostic(
            error="; ".join(f"invalid field at {path}" for path in paths),
            analysis=normalized, original_analysis=original, raw_output_text="PRIVATE_FULL_RESPONSE",
        )
        issues = result["issues"]
        self.assertEqual([issue["path"] for issue in issues], paths)
        self.assertEqual(issues[0]["received"]["value"], "2026-08-27")
        self.assertEqual(issues[0]["validated"]["value"], "2026-08-27T18:00:00+03:00")
        self.assertEqual(issues[2]["related_received"]["qualification_assessment.bant.timeframe.need_or_launch_timing_status"]["value"], "unknown")
        self.assertTrue(issues[3]["received"]["present"])
        self.assertIsNone(issues[3]["received"]["value"])
        self.assertFalse(issues[4]["received"]["present"])
        self.assertFalse(issues[5]["received"]["present"])
        self.assertNotIn("PRIVATE_FULL_RESPONSE", json.dumps(result, ensure_ascii=False))

    def test_diagnostic_is_bounded_and_redacts_urls_and_keys(self) -> None:
        value = "https://example.test/private sk-test123 Bearer token123 " + "Текст" * 1000
        analysis = {"block": {"text": value}}
        result = build_validation_diagnostic(error="expected string at block.text", analysis=analysis, original_analysis=analysis, raw_output_text="")
        fragment = result["issues"][0]["received"]
        self.assertTrue(fragment["truncated"])
        self.assertLessEqual(len(fragment["value"]), 512)
        self.assertNotIn("example.test", fragment["value"])
        self.assertNotIn("sk-test123", fragment["value"])
        self.assertNotIn("token123", fragment["value"])

    def test_invalid_value_cannot_select_unrelated_diagnostic_fields(self) -> None:
        analysis = {"block": {"status": "customer.secret"}, "customer": {"secret": "DO_NOT_CAPTURE"}}
        result = build_validation_diagnostic(
            error="invalid enum at block.status: expected confirmed, got 'customer.secret'",
            analysis=analysis, original_analysis=analysis, raw_output_text="",
        )
        self.assertEqual([issue["path"] for issue in result["issues"]], ["block.status"])
        self.assertNotIn("DO_NOT_CAPTURE", json.dumps(result))

    def test_diagnostic_write_failure_does_not_break_retry(self) -> None:
        calls = 0
        def caller(_prompt, **_kwargs):
            nonlocal calls
            calls += 1
            return {"ok": calls == 2}, metadata(0.1)

        with patch("openai_api.llm.validation_diagnostics.os.open", side_effect=OSError("PRIVATE_ERROR")):
            with self.assertLogs("openai_api.llm.validation_diagnostics", level="WARNING") as logs:
                result, meta = call_validated_analysis_json(
                    "ORIGINAL", validator=lambda value: None if value["ok"] else (_ for _ in ()).throw(FakeValidationError("bad block.status")),
                    normalizer=lambda _: [], validation_error_types=(FakeValidationError,), analysis_caller=caller,
                )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, 2)
        self.assertIsNone(meta["semantic_attempts"][0]["validation_diagnostic_ref"])
        self.assertEqual(meta["semantic_attempts"][0]["validation_diagnostic_status"], "unavailable")
        self.assertNotIn("PRIVATE_ERROR", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
