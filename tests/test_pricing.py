from __future__ import annotations

import unittest

from openai_api.pricing import estimate_analysis_cost


class PricingTests(unittest.TestCase):
    def test_gpt_54_mini_standard_short_context_price(self) -> None:
        result = estimate_analysis_cost(
            "gpt-5.4-mini",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "input_tokens_details": {"cached_tokens": 200_000}},
            1,
        )
        self.assertEqual(result["input_usd_per_1m"], 0.75)
        self.assertEqual(result["cached_input_usd_per_1m"], 0.075)
        self.assertEqual(result["output_usd_per_1m"], 4.50)
        self.assertEqual(result["estimated_cost_usd"], 5.115)

    def test_gpt_54_and_gpt_55_prices_remain_explicit(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertEqual(estimate_analysis_cost("gpt-5.4", usage, 1)["estimated_cost_usd"], 17.5)
        self.assertEqual(estimate_analysis_cost("gpt-5.5", usage, 1)["estimated_cost_usd"], 35.0)

    def test_unknown_model_is_not_assigned_a_price(self) -> None:
        result = estimate_analysis_cost("unknown", {"input_tokens": 10, "output_tokens": 10}, 75)
        self.assertIsNone(result["estimated_cost_rub"])
        self.assertEqual(result["pricing_source"], "unknown_model")


if __name__ == "__main__":
    unittest.main()
