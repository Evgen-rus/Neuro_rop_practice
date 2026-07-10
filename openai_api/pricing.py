"""
Built-in OpenAI pricing helpers for local cost estimates.
"""

from __future__ import annotations

from typing import Any


ANALYSIS_MODEL_PRICES_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        # OpenAI standard short-context pricing, verified 2026-07-10:
        # https://developers.openai.com/api/docs/pricing
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
}

TRANSCRIPTION_ESTIMATED_USD_PER_MINUTE: dict[str, float] = {
    "gpt-4o-mini-transcribe": 0.003,
}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cached_input_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        return int(_number(details.get("cached_tokens")))
    return int(_number(usage.get("cached_input_tokens")))


def rub_from_usd(cost_usd: float | None, usd_rub_rate: float) -> float | None:
    if cost_usd is None:
        return None
    return round(cost_usd * usd_rub_rate, 2)


def estimate_analysis_cost(
    model: str,
    usage: dict[str, Any],
    usd_rub_rate: float,
) -> dict[str, Any]:
    input_tokens = int(_number(usage.get("input_tokens")))
    output_tokens = int(_number(usage.get("output_tokens")))
    cached_input_tokens = min(_cached_input_tokens(usage), input_tokens)
    billable_input_tokens = max(input_tokens - cached_input_tokens, 0)
    prices = ANALYSIS_MODEL_PRICES_USD_PER_1M.get(model)

    result: dict[str, Any] = {
        "model": model,
        "usd_rub_rate": usd_rub_rate,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "billable_input_tokens": billable_input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": None,
        "estimated_cost_rub": None,
        "pricing_source": "built_in_model_table" if prices else "unknown_model",
    }

    if not prices:
        return result

    input_cost = billable_input_tokens * prices["input"] / 1_000_000
    cached_input_cost = cached_input_tokens * prices["cached_input"] / 1_000_000
    output_cost = output_tokens * prices["output"] / 1_000_000
    cost_usd = input_cost + cached_input_cost + output_cost

    result.update(
        {
            "input_usd_per_1m": prices["input"],
            "cached_input_usd_per_1m": prices["cached_input"],
            "output_usd_per_1m": prices["output"],
            "estimated_cost_usd": round(cost_usd, 4),
            "estimated_cost_rub": rub_from_usd(cost_usd, usd_rub_rate),
        }
    )
    return result


def estimate_transcription_cost(
    model: str,
    duration_seconds: float | None,
    usd_rub_rate: float,
) -> dict[str, Any]:
    per_minute = TRANSCRIPTION_ESTIMATED_USD_PER_MINUTE.get(model)
    duration_minutes = None if duration_seconds is None else duration_seconds / 60.0
    cost_usd = None
    if duration_minutes is not None and per_minute is not None:
        cost_usd = duration_minutes * per_minute

    return {
        "model": model,
        "duration_seconds": duration_seconds,
        "duration_minutes": duration_minutes,
        "estimated_usd_per_minute": per_minute,
        "usd_rub_rate": usd_rub_rate,
        "estimated_cost_usd": None if cost_usd is None else round(cost_usd, 4),
        "estimated_cost_rub": rub_from_usd(cost_usd, usd_rub_rate),
        "pricing_source": "built_in_model_table" if per_minute is not None else "unknown_model",
    }


def format_usd_rub(cost_usd: Any, cost_rub: Any) -> str:
    if cost_usd is None or cost_rub is None:
        return "не рассчитана"
    return f"${float(cost_usd):.4f} / {float(cost_rub):.2f} руб."
