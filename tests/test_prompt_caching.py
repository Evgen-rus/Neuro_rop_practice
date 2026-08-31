from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI

from openai_api.llm.llm_client import _cache_request, call_analysis_json, call_structured_output_json


def response(payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_cache_test",
        output_text=json.dumps(payload or {}, ensure_ascii=False),
        usage={
            "input_tokens": 1_500,
            "output_tokens": 10,
            "total_tokens": 1_510,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1_200},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    )


class PromptCachingRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        trace_patch = patch("openai_api.llm.llm_client.append_usage_trace")
        trace_patch.start()
        self.addCleanup(trace_patch.stop)

    def test_analysis_reasoning_override_and_default_are_independent(self) -> None:
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response()) as create, \
             patch("openai_api.llm.llm_client.ANALYSIS_REASONING_EFFORT", "low"):
            _, repair = call_analysis_json("repair", model="gpt-5.6-luna", reasoning_effort="xhigh",
                                           max_output_tokens=8000, preview_prompt=False)
            self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "xhigh"})
            self.assertEqual(create.call_args.kwargs["max_output_tokens"], 8000)
            self.assertEqual(repair["reasoning_effort"], "xhigh")
            _, primary = call_analysis_json("primary", model="gpt-5.6-terra", preview_prompt=False)
            self.assertEqual(create.call_args.kwargs["reasoning"], {"effort": "low"})
            self.assertEqual(primary["reasoning_effort"], "low")

    def test_multiple_explicit_breakpoints_preserve_exact_prompt_text(self) -> None:
        prompt = "GLOBAL\nDEAL\nOLD CALLS\nLATEST\nDYNAMIC"
        prefixes = ["GLOBAL\n", "GLOBAL\nDEAL\nOLD CALLS\n", "GLOBAL\nDEAL\nOLD CALLS\nLATEST\n"]
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response()) as create:
            _, metadata = call_analysis_json(
                prompt,
                model="gpt-5.6-terra",
                prompt_cache_key="neuro-rop:full-deal:v1",
                cache_prefixes=prefixes,
            )

        content = create.call_args.kwargs["input"][0]["content"]
        self.assertEqual("".join(block["text"] for block in content), prompt)
        self.assertEqual(len([block for block in content if "prompt_cache_breakpoint" in block]), 3)
        self.assertEqual(metadata["prompt_cache"]["breakpoint_count"], 3)
        self.assertEqual(metadata["request_fingerprint"]["stable_prefix"]["chars"], len(prefixes[-1]))

    def test_full_analysis_explicit_breakpoint_preserves_exact_prompt_text(self) -> None:
        prompt = "STATIC CONTRACT\n\n## HISTORY\nDynamic facts"
        stable_prefix = "STATIC CONTRACT\n\n"
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response()) as create:
            _, metadata = call_analysis_json(
                prompt,
                model="gpt-5.6-terra",
                call_type="full_deal_analysis",
                prompt_cache_key="neuro-rop:full-deal:v1",
                stable_prefix=stable_prefix,
            )

        kwargs = create.call_args.kwargs
        content = kwargs["input"][0]["content"]
        self.assertEqual("".join(block["text"] for block in content), prompt)
        self.assertEqual(content[0]["text"], stable_prefix)
        self.assertEqual(content[0]["prompt_cache_breakpoint"], {"mode": "explicit"})
        self.assertNotIn("prompt_cache_breakpoint", content[1])
        self.assertEqual(kwargs["prompt_cache_key"], "neuro-rop:full-deal:v1")
        self.assertEqual(
            kwargs["extra_body"],
            {"prompt_cache_options": {"mode": "explicit", "ttl": "30m"}},
        )
        self.assertEqual(metadata["call_type"], "full_deal_analysis")
        self.assertEqual(metadata["prompt_cache"]["breakpoint_count"], 1)
        self.assertEqual(metadata["estimated_cost"]["cache_write_tokens"], 1_200)
        self.assertGreaterEqual(metadata["latency_seconds"], 0)
        self.assertIn("+00:00", metadata["requested_at"])

    def test_explicit_mode_without_breakpoint_keeps_string_input_and_disables_writes(self) -> None:
        prompt = "One-off task guidance prompt"
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response()) as create:
            call_structured_output_json(
                prompt,
                schema={"type": "object", "properties": {}, "additionalProperties": False},
                schema_name="deal_task_guidance",
                model="gpt-5.6-terra",
                disable_implicit_cache=True,
            )

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["input"], prompt)
        self.assertNotIn("prompt_cache_key", kwargs)
        self.assertEqual(kwargs["extra_body"]["prompt_cache_options"]["mode"], "explicit")

    def test_structured_output_raw_callback_captures_every_transport_attempt(self) -> None:
        attempts: list[dict] = []
        create_results = [TimeoutError("first failed"), response({"answer": "сырой ответ"})]

        def create(**_kwargs):
            value = create_results.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        def retry_runner(operation, **_kwargs):
            try:
                operation()
            except TimeoutError:
                pass
            return operation()

        with (
            patch("openai_api.llm.llm_client.run_with_retry", side_effect=retry_runner),
            patch("openai_api.llm.llm_client.client.responses.create", side_effect=create),
        ):
            parsed, _ = call_structured_output_json(
                "полный prompt",
                schema={"type": "object", "properties": {}, "additionalProperties": False},
                schema_name="prompt_lab_test",
                model="gpt-5.6-terra",
                reasoning_effort="high",
                max_output_tokens=321,
                raw_exchange_callback=attempts.append,
            )

        self.assertEqual(parsed, {"answer": "сырой ответ"})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["attempt"], 1)
        self.assertEqual(attempts[0]["request"]["input"], "полный prompt")
        self.assertEqual(attempts[0]["request"]["reasoning"], {"effort": "high"})
        self.assertEqual(attempts[0]["request"]["max_output_tokens"], 321)
        self.assertEqual(attempts[0]["error"]["type"], "TimeoutError")
        self.assertIsNone(attempts[0]["response"])
        self.assertEqual(attempts[1]["attempt"], 2)
        self.assertIn("сырой ответ", attempts[1]["raw_output_text"])
        self.assertIsNone(attempts[1]["error"])

    def test_pre_56_model_keeps_legacy_request_shape(self) -> None:
        prompt = "STATIC\nDYNAMIC"
        with patch("openai_api.llm.llm_client.client.responses.create", return_value=response()) as create:
            _, metadata = call_analysis_json(
                prompt,
                model="gpt-5.4-mini",
                prompt_cache_key="neuro-rop:legacy:v1",
                stable_prefix="STATIC\n",
            )

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["input"], prompt)
        self.assertEqual(kwargs["prompt_cache_key"], "neuro-rop:legacy:v1")
        self.assertNotIn("extra_body", kwargs)
        self.assertEqual(metadata["prompt_cache"]["mode"], "implicit_legacy")

    def test_installed_sdk_serializes_explicit_cache_fields(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "resp_sdk_cache_test",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "error": None,
                    "incomplete_details": None,
                    "instructions": None,
                    "max_output_tokens": None,
                    "model": "gpt-5.6-terra",
                    "output": [],
                    "parallel_tool_calls": True,
                    "previous_response_id": None,
                    "reasoning": {"effort": None, "summary": None},
                    "store": False,
                    "temperature": None,
                    "text": {"format": {"type": "text"}},
                    "tool_choice": "auto",
                    "tools": [],
                    "top_p": None,
                    "truncation": "disabled",
                    "usage": None,
                    "user": None,
                    "metadata": {},
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        sdk_client = OpenAI(api_key="test", base_url="https://local.test/v1", http_client=http_client)
        request_input, options, _ = _cache_request(
            "STATIC\nDYNAMIC",
            model="gpt-5.6-terra",
            prompt_cache_key="neuro-rop:sdk-test:v1",
            stable_prefix="STATIC\n",
            disable_implicit_cache=False,
        )
        try:
            sdk_client.responses.create(model="gpt-5.6-terra", input=request_input, **options)
        finally:
            sdk_client.close()

        self.assertEqual(captured["prompt_cache_key"], "neuro-rop:sdk-test:v1")
        self.assertEqual(captured["prompt_cache_options"], {"mode": "explicit", "ttl": "30m"})
        breakpoint = captured["input"][0]["content"][0]["prompt_cache_breakpoint"]
        self.assertEqual(breakpoint, {"mode": "explicit"})

    def test_invalid_stable_prefix_is_rejected_before_request(self) -> None:
        with patch("openai_api.llm.llm_client.client.responses.create") as create:
            with self.assertRaisesRegex(ValueError, "exact prompt prefix"):
                call_analysis_json("prompt", stable_prefix="not-prefix")
        create.assert_not_called()

    def test_transport_retry_is_recorded_without_exposing_error_text(self) -> None:
        def retry_runner(operation, *, on_event, **_kwargs):
            on_event({"status": "attempt", "attempt": 1, "max_attempts": 3, "operation": "test"})
            on_event({
                "status": "retry_wait",
                "attempt": 1,
                "max_attempts": 3,
                "operation": "test",
                "error": "sensitive transport detail",
                "delay_seconds": 0,
            })
            on_event({"status": "attempt", "attempt": 2, "max_attempts": 3, "operation": "test"})
            result = operation()
            on_event({"status": "success", "attempt": 2, "max_attempts": 3, "operation": "test"})
            return result

        with (
            patch("openai_api.llm.llm_client.run_with_retry", side_effect=retry_runner),
            patch("openai_api.llm.llm_client.client.responses.create", return_value=response()),
        ):
            _, metadata = call_analysis_json("prompt")

        self.assertTrue(metadata["transport_retry"])
        self.assertEqual(metadata["transport_attempt_count"], 2)
        self.assertEqual(metadata["transport_retry_count"], 1)
        self.assertNotIn("transport_events", metadata)

    def test_more_than_four_breakpoints_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 4"):
            _cache_request(
                "abcdef",
                model="gpt-5.6-terra",
                prompt_cache_key=None,
                stable_prefix=None,
                disable_implicit_cache=False,
                cache_prefixes=["a", "ab", "abc", "abcd", "abcde"],
            )


if __name__ == "__main__":
    unittest.main()
