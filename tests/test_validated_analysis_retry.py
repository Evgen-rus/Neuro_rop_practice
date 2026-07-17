from __future__ import annotations

import unittest

from openai_api.llm.llm_client import (
    ModelJsonParseError,
    ValidatedAnalysisFailure,
    call_validated_analysis_json,
)


class FakeValidationError(ValueError):
    pass


def metadata(cost: float, raw: str = "{}") -> dict:
    return {
        "model": "test-model",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 1},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "estimated_cost": {"estimated_cost_usd": cost, "estimated_cost_rub": cost * 75},
        "estimated_cost_usd": cost,
        "estimated_cost_rub": cost * 75,
        "raw_output_text": raw,
    }


class ValidatedAnalysisRetryTests(unittest.TestCase):
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
        self.assertEqual(result_metadata["estimated_cost_rub"], 22.5)

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


if __name__ == "__main__":
    unittest.main()
