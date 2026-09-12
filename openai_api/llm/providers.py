"""Provider-specific text LLM transport helpers."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from openai_api.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENAI_REQUEST_TIMEOUT_SECONDS
from openai_api.pricing import rub_from_usd


openrouter_client = OpenAI(
    api_key=OPENROUTER_API_KEY or "missing-openrouter-key",
    base_url=OPENROUTER_BASE_URL,
    max_retries=0,
    timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
)


def openrouter_request_payload(
    prompt: str,
    *,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    response_format: dict[str, Any],
    prompt_cache_key: str | None,
) -> dict[str, Any]:
    extra_body: dict[str, Any] = {
        "reasoning": {"effort": reasoning_effort},
        "provider": {"require_parameters": True},
    }
    if prompt_cache_key:
        extra_body["prompt_cache_key"] = prompt_cache_key
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
        "response_format": response_format,
        "extra_body": extra_body,
    }


def chat_output_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
    return "" if content is None else str(content)


def normalize_openrouter_usage(response: Any) -> dict[str, Any]:
    raw = getattr(response, "usage", None)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    elif raw is not None and not isinstance(raw, dict):
        raw = dict(getattr(raw, "__dict__", {}))
    raw = dict(raw or {})
    prompt_details = dict(raw.get("prompt_tokens_details") or {})
    completion_details = dict(raw.get("completion_tokens_details") or {})
    return {
        **raw,
        "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens")),
        "output_tokens": raw.get("completion_tokens", raw.get("output_tokens")),
        "input_tokens_details": {
            "cached_tokens": prompt_details.get("cached_tokens", 0),
            "cache_write_tokens": prompt_details.get("cache_write_tokens", 0),
        },
        "output_tokens_details": {
            "reasoning_tokens": completion_details.get("reasoning_tokens", 0),
        },
    }


def openrouter_cost(model: str, usage: dict[str, Any], usd_rub_rate: float) -> dict[str, Any]:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    input_details = usage.get("input_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    cache_write_tokens = int(input_details.get("cache_write_tokens") or 0)
    try:
        cost_usd = float(usage["cost"]) if usage.get("cost") is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    return {
        "model": model,
        "usd_rub_rate": usd_rub_rate,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "billable_input_tokens": max(input_tokens - cached_tokens, 0),
        "cache_hit_ratio": round(cached_tokens / input_tokens, 4) if input_tokens else 0.0,
        "cache_write_ratio": round(cache_write_tokens / input_tokens, 4) if input_tokens else 0.0,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost_usd,
        "estimated_cost_rub": rub_from_usd(cost_usd, usd_rub_rate),
        "pricing_source": "openrouter_usage" if cost_usd is not None else "openrouter_cost_unavailable",
    }


def chat_response_status(response: Any) -> tuple[str, str | None]:
    choices = getattr(response, "choices", None) or []
    finish_reason = str(getattr(choices[0], "finish_reason", "") or "") if choices else ""
    return ("incomplete", finish_reason) if finish_reason == "length" else ("completed", finish_reason or None)
