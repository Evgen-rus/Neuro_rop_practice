"""
Small OpenAI Responses API wrapper for JSON analysis calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from openai_api.config import (
    ANALYSIS_INPUT_USD_PER_1M,
    ANALYSIS_MAX_OUTPUT_TOKENS,
    ANALYSIS_MODEL,
    ANALYSIS_OUTPUT_USD_PER_1M,
    OPENAI_API_KEY,
    logger,
)
from openai_api.logging_utils import log_model_text_payload


client = OpenAI(api_key=OPENAI_API_KEY)


def response_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks).strip()


def usage_to_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return dict(getattr(usage, "__dict__", {}))


def estimate_usage_cost_usd(usage: dict[str, Any]) -> float | None:
    if not ANALYSIS_INPUT_USD_PER_1M and not ANALYSIS_OUTPUT_USD_PER_1M:
        return None

    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    try:
        return (float(input_tokens) * ANALYSIS_INPUT_USD_PER_1M / 1_000_000) + (
            float(output_tokens) * ANALYSIS_OUTPUT_USD_PER_1M / 1_000_000
        )
    except (TypeError, ValueError):
        return None


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])

    if not isinstance(value, dict):
        raise ValueError("Model returned JSON, but top-level value is not an object")
    return value


def call_analysis_json(prompt: str, *, model: str = ANALYSIS_MODEL) -> tuple[dict[str, Any], dict[str, Any]]:
    log_model_text_payload(
        logger,
        title="deal analysis prompt",
        model=model,
        text=prompt,
        metadata={"api": "responses.create", "response_format": "json_object"},
    )

    response = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_object"}},
        store=False,
    )

    text = response_output_text(response)
    usage = usage_to_dict(response)
    estimated_cost = estimate_usage_cost_usd(usage)
    logger.info(
        "OpenAI analysis response usage: model=%s input_tokens=%s output_tokens=%s total_tokens=%s estimated_cost_usd=%s",
        model,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        estimated_cost,
    )

    parsed = parse_json_object(text)
    metadata = {
        "model": model,
        "usage": usage,
        "estimated_cost_usd": estimated_cost,
        "response_id": getattr(response, "id", None),
        "raw_output_text": text,
    }
    return parsed, metadata

