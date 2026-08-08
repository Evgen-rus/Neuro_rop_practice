"""Privacy-preserving JSONL trace for OpenAI usage and prompt caching."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from setup import LOGS_DIR


logger = logging.getLogger(__name__)
DEFAULT_USAGE_TRACE_PATH = LOGS_DIR / "openai_usage.jsonl"


def usage_trace_path() -> Path:
    configured = os.getenv("OPENAI_USAGE_TRACE_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_USAGE_TRACE_PATH


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_usage_trace_event(
    metadata: dict[str, Any],
    *,
    status: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    usage = _nested_dict(metadata.get("usage"))
    input_details = _nested_dict(usage.get("input_tokens_details"))
    output_details = _nested_dict(usage.get("output_tokens_details"))
    cost = _nested_dict(metadata.get("estimated_cost"))
    cache = _nested_dict(metadata.get("prompt_cache"))
    request_fingerprint = _nested_dict(metadata.get("request_fingerprint"))
    prompt_fingerprint = _nested_dict(request_fingerprint.get("prompt"))
    prefix_fingerprint = _nested_dict(request_fingerprint.get("stable_prefix"))
    return {
        "requested_at": metadata.get("requested_at"),
        "call_type": metadata.get("call_type"),
        "model": metadata.get("model"),
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "status": status,
        "error_type": error_type,
        "reasoning_effort": metadata.get("reasoning_effort"),
        "cache_mode": cache.get("mode"),
        "prompt_cache_key": cache.get("prompt_cache_key"),
        "cache_breakpoint_count": cache.get("breakpoint_count"),
        "cache_ttl": cache.get("ttl"),
        "prompt_chars": prompt_fingerprint.get("chars"),
        "prompt_bytes_utf8": prompt_fingerprint.get("bytes_utf8"),
        "prompt_sha256_16": prompt_fingerprint.get("sha256_16"),
        "stable_prefix_chars": prefix_fingerprint.get("chars"),
        "stable_prefix_bytes_utf8": prefix_fingerprint.get("bytes_utf8"),
        "stable_prefix_sha256_16": prefix_fingerprint.get("sha256_16"),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": input_details.get("cached_tokens", usage.get("cached_input_tokens")),
        "cache_write_tokens": input_details.get("cache_write_tokens", usage.get("cache_write_tokens")),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens", usage.get("reasoning_tokens")),
        "total_tokens": usage.get("total_tokens"),
        "latency_seconds": metadata.get("latency_seconds"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd", cost.get("estimated_cost_usd")),
        "estimated_cost_rub": metadata.get("estimated_cost_rub", cost.get("estimated_cost_rub")),
        "response_id": metadata.get("response_id"),
    }


def append_usage_trace(
    metadata: dict[str, Any],
    *,
    status: str = "success",
    entity_type: str | None = None,
    entity_id: str | None = None,
    error_type: str | None = None,
) -> None:
    """Append one sanitized event; trace failures never fail the model call."""
    try:
        path = usage_trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        event = build_usage_trace_event(
            metadata,
            status=status,
            entity_type=entity_type,
            entity_id=entity_id,
            error_type=error_type,
        )
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Unable to append OpenAI usage trace: %s", type(error).__name__)
