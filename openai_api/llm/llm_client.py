"""
Small OpenAI Responses API wrapper for JSON analysis calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from openai_api.config import (
    ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
    ANALYSIS_MAX_OUTPUT_TOKENS,
    ANALYSIS_MODEL,
    ANALYSIS_REASONING_EFFORT,
    OPENAI_API_KEY,
    USD_RUB_RATE,
    logger,
)
from openai_api.logging_utils import log_model_text_payload
from openai_api.pricing import estimate_analysis_cost


client = OpenAI(api_key=OPENAI_API_KEY)


class ModelJsonParseError(ValueError):
    """Raised when the model response cannot be parsed as a JSON object."""

    def __init__(self, message: str, raw_output_text: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.raw_output_text = raw_output_text
        self.metadata = metadata


class ModelResponseIncompleteError(ValueError):
    """Raised before parsing when a Responses output was truncated."""

    def __init__(self, message: str, raw_output_text: str, metadata: dict[str, Any]):
        super().__init__(message)
        self.raw_output_text = raw_output_text
        self.metadata = metadata


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


def response_status(response: Any) -> str | None:
    value = getattr(response, "status", None)
    return str(value) if value is not None else None


def response_incomplete_reason(response: Any) -> str | None:
    details = getattr(response, "incomplete_details", None)
    if isinstance(details, dict):
        value = details.get("reason")
    else:
        value = getattr(details, "reason", None)
    return str(value) if value is not None else None


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
        metadata={
            "api": "responses.create",
            "response_format": "json_object",
            "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        },
    )

    response = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
        reasoning={"effort": ANALYSIS_REASONING_EFFORT},
        text={"format": {"type": "json_object"}},
        store=False,
    )

    text = response_output_text(response)
    usage = usage_to_dict(response)
    estimated_cost = estimate_analysis_cost(model, usage, USD_RUB_RATE)
    logger.info(
        "OpenAI analysis response usage: model=%s input_tokens=%s cached_input_tokens=%s output_tokens=%s total_tokens=%s estimated_cost_usd=%s estimated_cost_rub=%s",
        model,
        usage.get("input_tokens"),
        estimated_cost.get("cached_input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        estimated_cost.get("estimated_cost_usd"),
        estimated_cost.get("estimated_cost_rub"),
    )

    metadata = {
        "model": model,
        "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        "usage": usage,
        "estimated_cost": estimated_cost,
        "estimated_cost_usd": estimated_cost.get("estimated_cost_usd"),
        "estimated_cost_rub": estimated_cost.get("estimated_cost_rub"),
        "response_id": getattr(response, "id", None),
        "raw_output_text": text,
    }

    try:
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError) as error:
        preview = text[:500].replace("\n", "\\n")
        raise ModelJsonParseError(
            f"Model returned invalid JSON: {error}. Raw output preview: {preview}",
            raw_output_text=text,
            metadata=metadata,
        ) from error

    return parsed, metadata


def call_structured_output_json(
    prompt: str,
    *,
    schema: dict[str, Any],
    schema_name: str,
    model: str = ANALYSIS_MODEL,
    max_output_tokens: int = ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call Responses structured outputs without changing the legacy JSON client."""
    log_model_text_payload(
        logger,
        title="attention delta shadow prompt",
        model=model,
        text=prompt,
        metadata={
            "api": "responses.create",
            "response_format": "json_schema",
            "schema_name": schema_name,
            "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        },
    )
    response = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=max_output_tokens,
        reasoning={"effort": ANALYSIS_REASONING_EFFORT},
        text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
        store=False,
    )
    text = response_output_text(response)
    usage = usage_to_dict(response)
    estimated_cost = estimate_analysis_cost(model, usage, USD_RUB_RATE)
    metadata = {
        "model": model,
        "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        "usage": usage,
        "estimated_cost": estimated_cost,
        "estimated_cost_usd": estimated_cost.get("estimated_cost_usd"),
        "estimated_cost_rub": estimated_cost.get("estimated_cost_rub"),
        "response_id": getattr(response, "id", None),
        "raw_output_text": text,
        "schema_name": schema_name,
        "response_status": response_status(response),
        "incomplete_reason": response_incomplete_reason(response),
        "max_output_tokens": max_output_tokens,
    }
    if metadata["response_status"] == "incomplete":
        reason = metadata["incomplete_reason"] or "unknown"
        raise ModelResponseIncompleteError(
            f"Structured output is incomplete: {reason}",
            raw_output_text=text,
            metadata=metadata,
        )
    try:
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError) as error:
        preview = text[:500].replace("\n", "\\n")
        raise ModelJsonParseError(
            f"Structured output returned invalid JSON: {error}. Raw output preview: {preview}",
            raw_output_text=text,
            metadata=metadata,
        ) from error
    return parsed, metadata
