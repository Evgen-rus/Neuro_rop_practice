from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APITimeoutError, InternalServerError, RateLimitError

from openai_api.config import OPENAI_ANALYSIS_MODEL
from openai_api.config import OPENAI_LEARNING_SHADOW_MODEL, OPENAI_MANAGER_MODEL
from openai_api.llm.full_analysis_repair import SectionRepairPlan
from openai_api.llm.llm_client import (
    ValidatedAnalysisFailure,
    call_analysis_json,
    call_structured_output_json,
    call_validated_analysis_json,
)


class InvalidAnswer(ValueError):
    pass


def chat_response(payload: dict, *, model: str = "provider/model", cost: float = 0.01) -> SimpleNamespace:
    return SimpleNamespace(
        id="or-test",
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost": cost,
            "prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    )


def openai_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="openai-test",
        output_text=json.dumps(payload),
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def validator(value: dict) -> None:
    if value.get("x") != 1:
        raise InvalidAnswer("x must be 1")


def repair_builder(primary: dict, _error: BaseException) -> SectionRepairPlan:
    return SectionRepairPlan(
        prompt="repair",
        sections=("x",),
        primary=primary,
        contract={"x": 0},
    )


class ProviderFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        trace = patch("openai_api.llm.llm_client.append_usage_trace")
        self.usage_trace = trace.start()
        self.addCleanup(trace.stop)
        diagnostics = patch("openai_api.llm.llm_client.save_validation_diagnostic", return_value=None)
        diagnostics.start()
        self.addCleanup(diagnostics.stop)

    def flow(self, **kwargs):
        return call_validated_analysis_json(
            "primary",
            validator=validator,
            normalizer=lambda _value: [],
            validation_error_types=(InvalidAnswer,),
            model="provider/analysis",
            reasoning_effort="high",
            repair_model="provider/repair",
            repair_reasoning_effort="low",
            preview_prompt=False,
            preview_response_errors=False,
            **kwargs,
        )

    def test_successful_openrouter_analysis_normalizes_usage(self) -> None:
        with patch(
            "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
            return_value=chat_response({"x": 1}),
        ) as create:
            result, metadata = call_analysis_json(
                "prompt", provider="openrouter", model="provider/analysis", prompt_cache_key="cache-key",
                preview_prompt=False,
            )
        self.assertEqual(result, {"x": 1})
        self.assertEqual(metadata["provider"], "openrouter")
        self.assertEqual(metadata["model"], "provider/model")
        self.assertEqual(metadata["usage"]["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(metadata["usage"]["output_tokens_details"]["reasoning_tokens"], 3)
        self.assertEqual(metadata["estimated_cost_usd"], 0.01)
        self.assertEqual(create.call_args.kwargs["extra_body"]["prompt_cache_key"], "cache-key")

    def test_openrouter_semantic_repair_uses_repair_role(self) -> None:
        with (
            patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", False),
            patch(
                "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
                side_effect=[chat_response({"x": 0}), chat_response({"sections": {"x": 1}})],
            ) as create,
        ):
            result, metadata = self.flow(targeted_repair_builder=repair_builder)
        self.assertEqual(result, {"x": 1})
        self.assertEqual([row["attempt_phase"] for row in metadata["semantic_attempts"]], ["primary", "repair"])
        self.assertEqual(create.call_args_list[1].kwargs["model"], "provider/repair")

    def test_openrouter_repair_failure_runs_full_correction(self) -> None:
        with (
            patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", False),
            patch(
                "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
                side_effect=[
                    chat_response({"x": 0}),
                    chat_response({"sections": {"x": 0}}),
                    chat_response({"x": 1}),
                ],
            ) as create,
        ):
            _result, metadata = self.flow(targeted_repair_builder=repair_builder)
        self.assertEqual(
            [row["attempt_phase"] for row in metadata["semantic_attempts"]],
            ["primary", "repair", "fallback"],
        )
        self.assertEqual(create.call_args_list[2].kwargs["model"], "provider/analysis")

    def test_final_semantic_failure_falls_back_to_complete_openai_flow(self) -> None:
        with (
            patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", True),
            patch(
                "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
                side_effect=[chat_response({"x": 0}), chat_response({"x": 0})],
            ),
            patch("openai_api.llm.llm_client.client.responses.create", return_value=openai_response({"x": 1})) as openai,
        ):
            result, metadata = self.flow()
        self.assertEqual(result, {"x": 1})
        self.assertTrue(metadata["provider_fallback"])
        self.assertEqual(metadata["provider"], "openai")
        self.assertIn("ValidatedAnalysisFailure", metadata["provider_fallback_reason"])
        self.assertEqual(openai.call_args.kwargs["model"], OPENAI_ANALYSIS_MODEL)
        self.assertFalse(metadata["fallback_invoked"])
        self.assertTrue(any(call.args[0].get("provider_fallback") for call in self.usage_trace.call_args_list))

    def test_transport_failure_falls_back_without_semantic_repair(self) -> None:
        request = httpx.Request("POST", "https://example.invalid")
        errors = (
            TimeoutError("timeout"),
            ConnectionError("network"),
            APITimeoutError(request),
            RateLimitError("rate", response=httpx.Response(429, request=request), body=None),
            InternalServerError("server", response=httpx.Response(503, request=request), body=None),
        )
        for transport_error in errors:
            with self.subTest(error=type(transport_error).__name__):
                operations: list[str] = []

                def runner(operation, *, operation_name, **_kwargs):
                    operations.append(operation_name)
                    if operation_name.startswith("openrouter:"):
                        raise transport_error
                    return operation()

                with (
                    patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
                    patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", True),
                    patch("openai_api.llm.llm_client.run_with_retry", side_effect=runner),
                    patch("openai_api.llm.llm_client.client.responses.create", return_value=openai_response({"x": 1})),
                ):
                    _result, metadata = self.flow(targeted_repair_builder=repair_builder)
                self.assertEqual(operations, ["openrouter:json.create", "openai:json.create"])
                self.assertTrue(metadata["provider_fallback"])
                self.assertIn(type(transport_error).__name__, metadata["provider_fallback_reason"])

    def test_repair_transport_failure_starts_complete_openai_flow(self) -> None:
        operations: list[str] = []

        def runner(operation, *, operation_name, **_kwargs):
            operations.append(operation_name)
            if operation_name == "openrouter:json.create" and operations.count(operation_name) == 2:
                raise ConnectionError("repair network failure")
            return operation()

        with (
            patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", True),
            patch("openai_api.llm.llm_client.run_with_retry", side_effect=runner),
            patch(
                "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
                return_value=chat_response({"x": 0}),
            ) as openrouter,
            patch(
                "openai_api.llm.llm_client.client.responses.create",
                return_value=openai_response({"x": 1}),
            ),
        ):
            result, metadata = self.flow(targeted_repair_builder=repair_builder)
        self.assertEqual(result, {"x": 1})
        self.assertEqual(operations, ["openrouter:json.create", "openrouter:json.create", "openai:json.create"])
        self.assertEqual(openrouter.call_count, 1)
        self.assertTrue(metadata["provider_fallback"])

    def test_disabled_fallback_surfaces_openrouter_error(self) -> None:
        with (
            patch("openai_api.llm.llm_client.LLM_PROVIDER", "openrouter"),
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", False),
            patch("openai_api.llm.llm_client.run_with_retry", side_effect=ConnectionError("network")),
        ):
            with self.assertRaises(ConnectionError):
                self.flow()

    def test_prompt_lab_never_falls_back(self) -> None:
        with (
            patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", True),
            patch(
                "openai_api.llm.llm_client.openrouter_client.chat.completions.create",
                side_effect=ConnectionError("network"),
            ),
            patch("openai_api.llm.llm_client.run_with_retry", side_effect=lambda operation, **_kwargs: operation()),
            patch("openai_api.llm.llm_client.client.responses.create") as openai,
        ):
            with self.assertRaises(ConnectionError):
                call_structured_output_json(
                    "prompt",
                    provider="openrouter",
                    schema={"type": "object", "properties": {}, "additionalProperties": False},
                    schema_name="prompt_lab_test",
                    call_type="prompt_lab_test",
                    model="provider/model",
                    reasoning_effort="low",
                )
        openai.assert_not_called()

    def test_structured_production_roles_fall_back_to_corresponding_openai_profile(self) -> None:
        cases = (
            ("deal_manager_quick_help", OPENAI_MANAGER_MODEL),
            ("deal_task_guidance", OPENAI_ANALYSIS_MODEL),
            ("learning_shadow_case", OPENAI_LEARNING_SHADOW_MODEL),
        )
        for call_type, expected_model in cases:
            with self.subTest(call_type=call_type):
                def runner(operation, *, operation_name, **_kwargs):
                    if operation_name.startswith("openrouter:"):
                        raise ConnectionError("network")
                    return operation()

                with (
                    patch("openai_api.llm.llm_client.LLM_FALLBACK_TO_OPENAI", True),
                    patch("openai_api.llm.llm_client.run_with_retry", side_effect=runner),
                    patch(
                        "openai_api.llm.llm_client.client.responses.create",
                        return_value=openai_response({"ok": True}),
                    ) as openai,
                ):
                    result, metadata = call_structured_output_json(
                        "prompt",
                        provider="openrouter",
                        schema={"type": "object", "properties": {}, "additionalProperties": False},
                        schema_name="answer",
                        call_type=call_type,
                        model="provider/model",
                        reasoning_effort="low",
                    )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(openai.call_args.kwargs["model"], expected_model)
                self.assertTrue(metadata["provider_fallback"])
                self.assertEqual(metadata["provider"], "openai")
                self.assertTrue(any(call.args[0].get("provider_fallback") for call in self.usage_trace.call_args_list))


if __name__ == "__main__":
    unittest.main()
