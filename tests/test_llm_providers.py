from __future__ import annotations

from types import SimpleNamespace
import unittest

from openai_api.llm.providers import (
    chat_output_text,
    normalize_openrouter_usage,
    openrouter_cost,
    openrouter_request_payload,
)


class OpenRouterProviderTests(unittest.TestCase):
    def test_request_uses_chat_schema_reasoning_and_cache_key(self) -> None:
        schema = {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": {}}}
        payload = openrouter_request_payload(
            "Привет",
            model="anthropic/claude-sonnet-4",
            reasoning_effort="high",
            max_output_tokens=5000,
            response_format=schema,
            prompt_cache_key="neuro-rop:test:v1",
        )
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Привет"}])
        self.assertEqual(payload["response_format"], schema)
        self.assertEqual(payload["extra_body"]["reasoning"], {"effort": "high"})
        self.assertEqual(payload["extra_body"]["provider"], {"require_parameters": True})
        self.assertEqual(payload["extra_body"]["prompt_cache_key"], "neuro-rop:test:v1")

    def test_response_usage_and_cost_are_normalized(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "cost": 0.012,
                "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 20},
            },
        )
        usage = normalize_openrouter_usage(response)
        cost = openrouter_cost("anthropic/claude-sonnet-4", usage, 90)
        self.assertEqual(chat_output_text(response), '{"ok":true}')
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 40)
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 20)
        self.assertEqual(cost["estimated_cost_usd"], 0.012)
        self.assertEqual(cost["estimated_cost_rub"], 1.08)

    def test_missing_openrouter_cost_stays_unknown(self) -> None:
        cost = openrouter_cost("unknown/model", {"input_tokens": 1, "output_tokens": 1}, 90)
        self.assertIsNone(cost["estimated_cost_usd"])
        self.assertIsNone(cost["estimated_cost_rub"])


if __name__ == "__main__":
    unittest.main()
