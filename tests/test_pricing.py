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

    def test_gpt_6_sol_and_luna_short_context_prices_are_explicit(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        sol = estimate_analysis_cost("gpt-6-sol", usage, 1)
        luna = estimate_analysis_cost("gpt-6-luna", usage, 1)
        self.assertEqual(sol["pricing_source"], "built_in_model_table")
        self.assertEqual(luna["pricing_source"], "built_in_model_table")
        self.assertEqual(sol["input_usd_per_1m"], 2.00)
        self.assertEqual(sol["cached_input_usd_per_1m"], 0.20)
        self.assertEqual(sol["cache_write_usd_per_1m"], 2.50)
        self.assertEqual(sol["output_usd_per_1m"], 10.00)
        self.assertEqual(sol["estimated_cost_usd"], 12.0)
        self.assertEqual(luna["input_usd_per_1m"], 0.10)
        self.assertEqual(luna["cached_input_usd_per_1m"], 0.01)
        self.assertEqual(luna["cache_write_usd_per_1m"], 0.125)
        self.assertEqual(luna["output_usd_per_1m"], 0.50)
        self.assertEqual(luna["estimated_cost_usd"], 0.6)

    def test_gpt_6_cache_reads_and_writes_use_separate_rates(self) -> None:
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_tokens_details": {
                "cached_tokens": 200_000,
                "cache_write_tokens": 300_000,
            },
        }
        sol = estimate_analysis_cost("gpt-6-sol", usage, 1)
        luna = estimate_analysis_cost("gpt-6-luna", usage, 1)
        self.assertEqual(sol["billable_input_tokens"], 500_000)
        self.assertEqual(sol["estimated_cost_usd"], 1.79)
        self.assertEqual(luna["billable_input_tokens"], 500_000)
        self.assertEqual(luna["estimated_cost_usd"], 0.0895)

    def test_gpt_56_terra_and_luna_prices_are_explicit(self) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        terra = estimate_analysis_cost("gpt-5.6-terra", usage, 1)
        luna = estimate_analysis_cost("gpt-5.6-luna", usage, 1)
        self.assertEqual(terra["estimated_cost_usd"], 14.0)
        self.assertEqual(luna["estimated_cost_usd"], 1.4)

    def test_gpt_56_cache_reads_and_writes_use_separate_rates(self) -> None:
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "input_tokens_details": {
                "cached_tokens": 200_000,
                "cache_write_tokens": 300_000,
            },
        }
        result = estimate_analysis_cost("gpt-5.6-terra", usage, 1)
        self.assertEqual(result["billable_input_tokens"], 500_000)
        self.assertEqual(result["cached_input_tokens"], 200_000)
        self.assertEqual(result["cache_write_tokens"], 300_000)
        self.assertEqual(result["cache_write_usd_per_1m"], 2.50)
        self.assertEqual(result["estimated_cost_usd"], 1.79)

    def test_unknown_model_is_not_assigned_a_price(self) -> None:
        result = estimate_analysis_cost("unknown", {"input_tokens": 10, "output_tokens": 10}, 75)
        self.assertIsNone(result["estimated_cost_rub"])
        self.assertEqual(result["pricing_source"], "unknown_model")


if __name__ == "__main__":
    unittest.main()
