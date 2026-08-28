from __future__ import annotations

import unittest
import json
import os
import tempfile
import io
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIConnectionError

from openai_api.llm.llm_client import (
    ModelJsonParseError,
    ValidatedAnalysisFailure,
    call_validated_analysis_json,
    call_analysis_json,
)
from openai_api.llm.analyze_deal import build_prompt as deal_prompt
from openai_api.llm.analyze_lead import build_prompt as lead_prompt, validate_lead_analysis_for_crm_state
from openai_api.llm.full_analysis_repair import build_full_repair_builder, SectionRepairError
from openai_api.llm.deal_semantic_dependencies import DEPENDENCIES
from openai_api.llm.validation import (
    AnalysisValidationError, DEAL_REQUIRED_FIELDS, normalize_analysis_for_validation, validate_deal_analysis,
)
from openai_api.pricing import estimate_analysis_cost
from reliability.retry import RetryPolicy
from test_lead_qualification_assessment import lead_analysis
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


class FullSectionRepairTests(unittest.TestCase):
    def test_full_and_v2_share_contract_without_daily_checklist(self):
        from openai_api.llm.deal_incremental_v2 import run_incremental_v2
        from openai_api.llm.deal_semantic_state import bootstrap_semantic_state
        from openai_api.llm.deal_semantic_dependencies import resolve_affected_sections

        legacy = deepcopy(self.good)
        legacy["daily_checklist_update"] = {"add": [{"text": "legacy marker"}]}
        legacy["manager_action_block"]["manager_checklist"] = ["legacy marker"]
        normalized = deepcopy(legacy)
        normalize_analysis_for_validation(normalized)
        validate_deal_analysis(normalized)
        self.assertNotIn("daily_checklist_update", normalized)
        self.assertNotIn("manager_checklist", normalized["manager_action_block"])

        state = bootstrap_semantic_state(self.good, deal_id="42", source_analysis_run_id=1,
                                         source_fingerprint="old", evidence_coverage={})
        delta = [{"evidence_id": "call:2", "kind": "call_transcript", "delta_kind": "new_evidence", "text": "test"}]
        affected = resolve_affected_sections([], delta)
        self.assertNotIn("daily_checklist_update", affected)
        materialized = {key: deepcopy(legacy.get(key, {})) for key in affected}
        responses = iter([
            {"changed_domains": [], "change_reasons": {}, "semantic_state": state},
            {"sections": materialized},
        ])

        def caller(prompt, **kwargs):
            for retired in ("manager_checklist", "daily_checklist_update", "CURRENT_DAILY_MANAGER_CHECKLIST", "legacy marker"):
                self.assertNotIn(retired, prompt)
            response = next(responses)
            kwargs["normalizer"](response)
            kwargs["validator"](response)
            return response, {}

        with patch("openai_api.llm.deal_incremental_v2.call_validated_analysis_json", side_effect=caller):
            result = run_incremental_v2(
                deal_id="42", previous_analysis=legacy, previous_semantic_state=state,
                evidence_delta=delta, next_evidence_coverage={}, crm_delta={}, stage_policy={},
                prior_recommendation=None, source_fingerprint="new", model="test-model", compact_policy_text="test rules",
            )
        validate_deal_analysis(result.analysis)
        self.assertNotIn("daily_checklist_update", result.analysis)
        self.assertNotIn("manager_checklist", result.analysis["manager_action_block"])
        self.assertIn("manager_checklist", legacy["manager_action_block"])
        self.assertIn("competitor_defense_checklist", result.analysis)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"OPENAI_VALIDATION_DIAGNOSTICS_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []
        self.events = []
        self.good = {key: {} for key in DEAL_REQUIRED_FIELDS}
        self.good.update(deal_id="42", what_changed=[], communication_quality_audit={})
        lead = lead_analysis()
        for key in ("rop_manager_message_block", "manager_action_block", "qualification_assessment", "main_risk"):
            self.good[key] = lead[key]
        self.good["qualification_assessment"].pop("lead_category")
        self.good["qualification_assessment"].pop("lead_route")
        normalize_analysis_for_validation(self.good)
        validate_deal_analysis(self.good)
        self.bad = deepcopy(self.good)
        self.bad["rop_manager_message_block"]["deadline"] = "27/08/2026"
        self.prompt = deal_prompt("42", "FULL_HISTORY_SENTINEL" * 1000, "FULL_TRANSCRIPT_SENTINEL" * 1000,
                                  "DIAGNOSTICS_SENTINEL", [(Path("test.md"), "KNOWLEDGE_SENTINEL" * 1000)], {})

    def run_responses(self, responses, *, entity="deal", validator=None, normalizer=normalize_analysis_for_validation,
                      builder=None, prompt=None):
        def caller(text, **kwargs):
            self.calls.append((text, kwargs))
            response = responses[len(self.calls) - 1]
            if isinstance(response, BaseException):
                raise response
            model = kwargs["model"]
            usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                     "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 50},
                     "output_tokens_details": {"reasoning_tokens": 40}}
            cost = estimate_analysis_cost(model, usage, 75)
            return deepcopy(response), {
                "model": model, "reasoning_effort": kwargs.get("reasoning_effort"),
                "usage": usage, "estimated_cost": cost, "latency_seconds": 1.25,
                "raw_output_text": json.dumps(response, ensure_ascii=False),
            }
        effective_prompt = prompt or self.prompt
        with patch("openai_api.llm.llm_client.call_analysis_json", side_effect=caller) as mocked:
            with patch("openai_api.llm.llm_client.append_usage_trace") as trace:
                try:
                    return call_validated_analysis_json(
                        effective_prompt, model="gpt-5.6-terra", reasoning_effort="low",
                        repair_model="gpt-5.6-luna", repair_reasoning_effort="xhigh",
                        validator=validator or validate_deal_analysis, normalizer=normalizer,
                        validation_error_types=(AnalysisValidationError,), analysis_caller=mocked,
                        targeted_repair_builder=builder or build_full_repair_builder(entity, effective_prompt),
                        prompt_cache_marker="Нужная JSON-структура:",
                    )
                finally:
                    self.events = [build_usage_trace_event(call.args[0], status=call.kwargs.get("status", "success"))
                                   for call in trace.call_args_list]

    def test_primary_passes_without_repair(self):
        result, meta = self.run_responses([self.good])
        self.assertEqual(result, self.good)
        self.assertEqual(meta["semantic_attempt_count"], 1)
        self.assertFalse(meta["repair_invoked"])
        self.assertFalse(meta["primary_validation_failed"])

    def test_local_repair_excludes_full_context_and_preserves_primary(self):
        snapshot = deepcopy(self.bad)
        result, meta = self.run_responses([self.bad, {"sections": {"rop_manager_message_block": {"deadline": "2026-08-27"}}}])
        self.assertEqual(self.bad, snapshot)
        validate_deal_analysis(result)
        self.assertEqual(result["rop_manager_message_block"]["deadline"], "2026-08-27")
        for key in snapshot:
            if key != "rop_manager_message_block":
                self.assertEqual(result[key], snapshot[key])
        text, options = self.calls[1]
        for marker in ("FULL_HISTORY_SENTINEL", "FULL_TRANSCRIPT_SENTINEL", "KNOWLEDGE_SENTINEL", "DIAGNOSTICS_SENTINEL"):
            self.assertNotIn(marker, text)
        self.assertLess(len(text), len(self.prompt) // 5)
        self.assertIsNone(options["cache_prefixes"])
        self.assertIsNone(options["prompt_cache_key"])
        self.assertFalse(options["preview_prompt"])
        self.assertEqual(meta["model"], "gpt-5.6-terra")
        self.assertEqual(json.loads(meta["raw_output_text"]), snapshot)
        self.assertTrue(meta["repair_succeeded"])
        self.assertFalse(meta["fallback_invoked"])
        self.assertEqual(meta["semantic_attempt_count"], 2)

    def test_dependencies_use_v2_domain_set_and_full_validator(self):
        bad = deepcopy(self.good)
        bad["qualification_assessment"]["solution_fit"]["reason_code"] = "technical_mismatch"
        with self.assertRaises(AnalysisValidationError) as error:
            validate_deal_analysis(bad)
        plan = build_full_repair_builder("deal", self.prompt)(bad, error.exception)
        self.assertIsNotNone(plan)
        self.assertEqual(set(plan.sections), DEPENDENCIES["qualification"])
        repaired = {key: deepcopy(self.good[key]) for key in plan.sections}
        result, meta = self.run_responses([bad, {"sections": repaired}])
        validate_deal_analysis(result)
        self.assertEqual(result, self.good)
        self.assertTrue(meta["repair_succeeded"])

    def test_invalid_envelopes_and_merged_validation_fail_use_full_fallback(self):
        for repair in (
            {"sections": {}, "extra": True},
            {"sections": {"deal_id": "evil"}},
            {"sections": {"rop_manager_message_block": []}},
            {"sections": {"rop_manager_message_block": {"deadline": "still-invalid"}}},
            {"sections": {"rop_manager_message_block": {"deadline": "2026-08-27", "evidence": ["invented"]}}},
            {"cannot_repair": True},
            ModelJsonParseError("bad JSON", "{bad", metadata(0.1, "{bad")),
        ):
            with self.subTest(repair=type(repair).__name__):
                self.calls.clear()
                result, meta = self.run_responses([self.bad, repair, self.good])
                self.assertEqual(result, self.good)
                self.assertEqual([call[1]["model"] for call in self.calls],
                                 ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-terra"])
                self.assertEqual(self.calls[2][1]["reasoning_effort"], "low")
                self.assertIn("FULL_HISTORY_SENTINEL", self.calls[2][0])
                self.assertTrue(meta["fallback_invoked"])
                self.assertFalse(meta["repair_succeeded"])

    def test_invalid_primary_json_does_not_attempt_partial_merge(self):
        result, meta = self.run_responses([ModelJsonParseError("bad", "{bad", metadata(0.1)), self.good])
        self.assertEqual(result, self.good)
        self.assertFalse(meta["repair_invoked"])
        self.assertEqual([item["attempt_phase"] for item in meta["semantic_attempts"]], ["primary", "fallback"])

    def test_mixed_model_costs_and_trace_are_per_attempt(self):
        _, meta = self.run_responses([self.bad, {"sections": {"rop_manager_message_block": {"deadline": "2026-08-27"}}}])
        attempts = meta["semantic_attempts"]
        self.assertEqual([item["reasoning_effort"] for item in attempts], ["low", "xhigh"])
        self.assertAlmostEqual(meta["estimated_cost_usd"], sum(item["estimated_cost_usd"] for item in attempts))
        self.assertEqual(meta["usage"]["total_tokens"], 2200)
        self.assertEqual(meta["usage"]["output_tokens_details"]["reasoning_tokens"], 80)
        self.assertEqual(meta["estimated_cost"]["cached_input_tokens"], 200)
        self.assertEqual(meta["estimated_cost"]["cache_write_tokens"], 100)
        self.assertNotIn("input_usd_per_1m", meta["estimated_cost"])
        self.assertEqual(meta["latency_seconds"], 2.5)
        self.assertEqual(meta["cost_by_phase"]["repair"]["estimated_cost_usd"], attempts[1]["estimated_cost_usd"])
        self.assertEqual([event["validation_status"] for event in self.events], ["failed", "passed"])
        self.assertEqual(len({event["analysis_attempt_id"] for event in self.events}), 1)
        self.assertEqual([event["final_attempt"] for event in self.events], [False, True])
        self.assertNotIn("primary_sections", json.dumps(self.events))

    def test_all_three_fail_preserves_final_failure(self):
        with self.assertRaises(ValidatedAnalysisFailure) as raised:
            self.run_responses([self.bad, {"cannot_repair": True}, self.bad])
        meta = raised.exception.metadata
        self.assertEqual(meta["semantic_attempt_count"], 3)
        self.assertFalse(meta["final_validation_passed"])
        self.assertEqual(raised.exception.analysis, self.bad)
        self.assertTrue(self.events[-1]["final_attempt"])

    def test_deterministic_normalization_precedes_repair(self):
        def normalize(value):
            value["rop_manager_message_block"]["deadline"] = "2026-07-15"
            return [{"path": "rop_manager_message_block.deadline", "action": "test_normalization"}]
        result, meta = self.run_responses([self.bad], normalizer=normalize)
        self.assertEqual(result, self.good)
        self.assertFalse(meta["repair_invoked"])

    def test_lead_full_local_repair_and_category_fallback(self):
        good = lead_analysis()
        crm = {"status_id": "IN_PROCESS", "status_name": "В работе", "status_semantic_id": "P", "is_closed_lost": False}
        prompt = lead_prompt("42", "FULL_HISTORY_SENTINEL", "FULL_TRANSCRIPT_SENTINEL", "", [], crm)
        validator = lambda value: validate_lead_analysis_for_crm_state(value, crm)
        bad = deepcopy(good)
        bad["rop_manager_message_block"]["deadline"] = "bad-date"
        result, meta = self.run_responses([bad, {"sections": {"rop_manager_message_block": {"deadline": "2026-07-15"}}}],
                                         entity="lead", prompt=prompt, validator=validator)
        self.assertEqual(result, good)
        self.assertTrue(meta["repair_succeeded"])
        self.calls.clear()
        bad = deepcopy(good)
        bad["lead_state"]["qualification"] = "D"
        result, meta = self.run_responses([bad, good], entity="lead", prompt=prompt, validator=validator)
        self.assertFalse(meta["repair_invoked"])
        self.assertTrue(meta["fallback_invoked"])

    def test_unknown_error_or_missing_contract_declines_repair(self):
        for error in (AnalysisValidationError("legacy error"),
                      AnalysisValidationError("unmapped", errors=["unknown domain rule"]),
                      AnalysisValidationError("missing evidence", errors=["rop_manager_message_block.evidence must not be empty"])):
            self.assertIsNone(build_full_repair_builder("deal", self.prompt)(self.bad, error))
        error = AnalysisValidationError("deadline", errors=["rop_manager_message_block.deadline must use YYYY-MM-DD"])
        self.assertIsNone(build_full_repair_builder("deal", "no contract")(self.bad, error))

    def test_invalid_value_cannot_expand_scope_and_unknown_fields_are_rejected(self):
        error = AnalysisValidationError("invalid", errors=[
            "invalid enum at manager_action_block.recommended_channel: expected phone, got 'x; qualification_assessment.bant'"
        ])
        plan = build_full_repair_builder("deal", self.prompt)(self.good, error)
        self.assertEqual(plan.sections, ("manager_action_block",))
        with self.assertRaises(SectionRepairError):
            plan.merge({"sections": {"manager_action_block": {"unknown_new_field": "invented"}}})

    def test_repair_cannot_downgrade_confirmed_conclusion(self):
        error = AnalysisValidationError("invalid", errors=[
            "qualification_assessment.solution_fit.reason_code must be technical_mismatch when status=not_compatible"
        ])
        plan = build_full_repair_builder("deal", self.prompt)(self.good, error)
        sections = {key: deepcopy(self.good[key]) for key in plan.sections}
        sections["qualification_assessment"]["bant"]["budget"]["status"] = "unknown"
        with self.assertRaisesRegex(SectionRepairError, "downgraded"):
            plan.merge({"sections": sections})

    def test_packet_overflow_declines_instead_of_truncating(self):
        bad = deepcopy(self.bad)
        bad["rop_manager_message_block"]["evidence"] = ["EVIDENCE" * 10000]
        result, meta = self.run_responses([bad, self.good])
        self.assertFalse(meta["repair_invoked"])

    def test_normalizer_cannot_change_unrelated_section_after_merge(self):
        calls = 0
        def normalizer(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                value["main_risk"]["description"] = "changed outside repair scope"
            return []
        _, meta = self.run_responses([self.bad, {"sections": {"rop_manager_message_block": {"deadline": "2026-08-27"}}}, self.good],
                                     normalizer=normalizer)
        self.assertTrue(meta["fallback_invoked"])

    def test_primary_transport_failure_never_invokes_luna(self):
        failure = APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        with self.assertRaises(APIConnectionError):
            self.run_responses([failure])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.events[0]["validation_status"])
        self.assertTrue(self.events[0]["transport_error"])

    def test_repair_transport_failure_uses_full_fallback_without_semantic_error(self):
        failure = APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        result, meta = self.run_responses([self.bad, failure, self.good])
        self.assertEqual(result, self.good)
        self.assertTrue(meta["fallback_invoked"])
        self.assertIsNone(meta["semantic_attempts"][1]["validation_passed"])
        self.assertIsNone(meta["estimated_cost_usd"])  # Unavailable usage is not a zero-cost claim.

    def test_actual_transport_retry_is_one_semantic_attempt_and_reasoning_is_per_call(self):
        response = SimpleNamespace(output_text=json.dumps(self.good), usage={"input_tokens": 10, "output_tokens": 5}, id="test")
        failure = APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        with patch("openai_api.llm.llm_client.client.responses.create", side_effect=[failure, response]) as create, \
             patch("openai_api.llm.llm_client.DEFAULT_TRANSPORT_RETRY", RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0)), \
             patch("openai_api.llm.llm_client.log_model_text_payload"), \
             patch("openai_api.llm.llm_client.append_usage_trace"):
            _, meta = call_validated_analysis_json(
                "prompt", model="gpt-5.6-terra", reasoning_effort="low", analysis_caller=call_analysis_json,
                validator=validate_deal_analysis, normalizer=normalize_analysis_for_validation,
                validation_error_types=(AnalysisValidationError,), targeted_repair_builder=build_full_repair_builder("deal", self.prompt),
            )
        self.assertEqual(create.call_count, 2)
        self.assertEqual(meta["semantic_attempt_count"], 1)
        self.assertEqual(meta["semantic_attempts"][0]["transport_retry_count"], 1)
        self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "low"})

    def test_full_entrypoints_save_merged_metadata_and_never_render_failure(self):
        from openai_api.llm import analyze_deal, analyze_lead
        for entity, module in (("deal", analyze_deal), ("lead", analyze_lead)):
            for success in (True, False):
                with self.subTest(entity=entity, success=success), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    root = Path(directory)
                    history = root / f"{entity}_42" / "history"
                    history.mkdir(parents=True)
                    (history / f"{entity}_42_customer_path.md").write_text("Синтетическая история", encoding="utf-8")
                    knowledge = root / "knowledge"
                    knowledge.mkdir()
                    args = SimpleNamespace(allow_direct_llm=True, dry_run=False, model="gpt-5.6-terra",
                                           transcript="none", knowledge_dir=str(knowledge),
                                           **{f"{entity}_id": "42", f"{entity}_root": str(root)})
                    good = self.good if entity == "deal" else lead_analysis()
                    bad = deepcopy(good)
                    bad["rop_manager_message_block"]["deadline"] = "27/08/2026"
                    responses = [bad, {"sections": {"rop_manager_message_block": {"deadline": "2026-08-27"}}}] if success else [bad, {"cannot_repair": True}, bad]
                    calls = []
                    def caller(prompt, **options):
                        calls.append(options)
                        value = deepcopy(responses[len(calls) - 1])
                        meta = metadata(0.1, json.dumps(value, ensure_ascii=False))
                        meta["model"] = options["model"]
                        return value, meta
                    for name in ("log_model_file_payload", "log_model_text_payload", "emit_progress"):
                        stack.enter_context(patch.object(module, name))
                    stack.enter_context(patch.object(module, "parse_args", return_value=args))
                    stack.enter_context(patch.object(module, "call_analysis_json", side_effect=caller))
                    stack.enter_context(patch.object(module, "load_context_diagnostics_for_analysis", return_value=("", None, {})))
                    if entity == "deal":
                        for name in ("get_latest_neuro_rop_recommendation_projection",):
                            stack.enter_context(patch.object(module, name, return_value=None))
                        stack.enter_context(patch.object(module, "build_deal_stage_policy", return_value={}))
                    else:
                        stack.enter_context(patch.object(module, "load_lead_crm_state", return_value={
                            "status_id": "IN_PROCESS", "status_name": "В работе", "status_semantic_id": "P", "is_closed_lost": False,
                        }))
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    if success:
                        module.main()
                    else:
                        with self.assertRaises(ValidatedAnalysisFailure):
                            module.main()
                    output_dir = root / f"{entity}_42" / "analysis"
                    self.assertEqual((output_dir / f"{entity}_42_rop_report.md").exists(), success)
                    name = f"{entity}_42_analysis.json" if success else f"{entity}_42_analysis_error.json"
                    payload = json.loads((output_dir / name).read_text(encoding="utf-8"))
                    self.assertEqual(payload["model_metadata"]["semantic_attempt_count"], 2 if success else 3)
                    self.assertEqual(payload["model_metadata"]["repair_succeeded"], success)
                    if success:
                        self.assertEqual(payload["analysis"]["rop_manager_message_block"]["deadline"], "2026-08-27")


if __name__ == "__main__":
    unittest.main()
