"""Privacy-preserving JSONL trace for OpenAI usage and prompt caching."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from setup import LOGS_DIR, MSK_TZ


logger = logging.getLogger(__name__)
DEFAULT_USAGE_TRACE_PATH = LOGS_DIR / "openai_usage.jsonl"
DEFAULT_DAILY_USAGE_DIR = LOGS_DIR / "openai_usage_daily"


def usage_trace_path() -> Path:
    configured = os.getenv("OPENAI_USAGE_TRACE_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_USAGE_TRACE_PATH


def daily_usage_dir() -> Path:
    configured = os.getenv("OPENAI_USAGE_DAILY_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DAILY_USAGE_DIR


def _nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _requested_at_msk(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(MSK_TZ)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MSK_TZ)


def _one_line(value: Any, default: str = "-") -> str:
    text = str(value if value is not None else default)
    return " ".join(text.replace("|", "/").split()) or default


def _token_value(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "-"


def _cost_value(usd: Any, rub: Any) -> str:
    try:
        usd_text = f"${float(usd):.4f}"
    except (TypeError, ValueError):
        usd_text = "$-"
    try:
        rub_text = f"{float(rub):.2f} ₽"
    except (TypeError, ValueError):
        rub_text = "- ₽"
    return f"{usd_text}/{rub_text}"


def build_daily_usage_line(event: dict[str, Any]) -> tuple[str, str]:
    """Build one human-readable line and its Moscow-date filename."""
    requested_at = _requested_at_msk(event.get("requested_at"))
    entity_type = _one_line(event.get("entity_type"))
    entity_id = _one_line(event.get("entity_id"))
    entity = f"{entity_type}:{entity_id}" if entity_type != "-" or entity_id != "-" else "-"
    fields = [
        requested_at.strftime("%Y-%m-%d %H:%M:%S MSK"),
        f"status={_one_line(event.get('status'))}",
        f"model={_one_line(event.get('model'))}",
        f"call={_one_line(event.get('call_type'))}",
        f"entity={entity}",
        f"input={_token_value(event.get('input_tokens'))}",
        f"cached={_token_value(event.get('cached_input_tokens'))}",
        f"cache_write={_token_value(event.get('cache_write_tokens'))}",
        f"output={_token_value(event.get('output_tokens'))}",
        f"cost={_cost_value(event.get('estimated_cost_usd'), event.get('estimated_cost_rub'))}",
    ]
    return requested_at.strftime("%Y-%m-%d.log"), " | ".join(fields)


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
    cache_prefixes = request_fingerprint.get("cache_prefixes")
    cache_prefixes = cache_prefixes if isinstance(cache_prefixes, list) else []
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
        "cache_prefixes": [prefix for prefix in cache_prefixes if isinstance(prefix, dict)],
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


def _record_spend_diary(event: dict[str, Any]) -> None:
    """Human daily diary; a write failure must not fail the model call."""
    if event.get("estimated_cost_rub") is None and event.get("estimated_cost_usd") is None:
        return
    try:
        from openai_api.spend_diary import record_paid_call

        record_paid_call(
            kind=str(event.get("call_type") or "openai"),
            estimated_cost_rub=event.get("estimated_cost_rub"),
            estimated_cost_usd=event.get("estimated_cost_usd"),
            entity_type=event.get("entity_type"),
            entity_id=event.get("entity_id"),
            model=event.get("model"),
            now=_requested_at_msk(event.get("requested_at")),
        )
    except Exception as error:  # noqa: BLE001 - diary is best-effort
        logger.warning("Unable to append spend diary: %s", type(error).__name__)


def append_usage_trace(
    metadata: dict[str, Any],
    *,
    status: str = "success",
    entity_type: str | None = None,
    entity_id: str | None = None,
    error_type: str | None = None,
) -> None:
    """Append one sanitized event; trace failures never fail the model call."""
    event = build_usage_trace_event(
        metadata,
        status=status,
        entity_type=entity_type,
        entity_id=entity_id,
        error_type=error_type,
    )
    try:
        path = usage_trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        daily_filename, daily_line = build_daily_usage_line(event)
        daily_path = daily_usage_dir() / daily_filename
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        with daily_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(daily_line + "\n")
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Unable to append OpenAI usage trace: %s", type(error).__name__)
    _record_spend_diary(event)
