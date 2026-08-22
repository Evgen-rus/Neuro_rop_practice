"""
Small OpenAI Responses API wrapper for JSON analysis calls.
"""

from __future__ import annotations

import json
import re
import hashlib
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

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
from openai_api.llm.usage_trace import append_usage_trace
from openai_api.pricing import estimate_analysis_cost
from reliability.retry import DEFAULT_TRANSPORT_RETRY, RetryCallback, run_with_retry


client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
PROMPT_CACHE_TTL = "30m"


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


class ValidatedAnalysisFailure(ValueError):
    """Raised after both semantic attempts fail parsing or validation."""

    def __init__(
        self,
        message: str,
        *,
        raw_output_text: str,
        metadata: dict[str, Any],
        analysis: dict[str, Any] | None,
    ) -> None:
        super().__init__(message)
        self.raw_output_text = raw_output_text
        self.metadata = metadata
        self.analysis = analysis


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


def prompt_prefix_before(prompt: str, marker: str) -> str:
    """Return the unchanged prompt prefix before a required dynamic marker."""
    index = prompt.find(marker)
    if index <= 0:
        raise ValueError(f"Prompt cache marker not found: {marker}")
    return prompt[:index]


def deal_trace_id(deal: Any) -> str | None:
    """Return the CRM deal id for usage/spend traces. Never returns CRM text."""
    if not isinstance(deal, dict):
        return None
    value = str(deal.get("deal_id") or "").strip()
    return value or None


def _request_fingerprint(
    prompt: str,
    stable_prefix: str | None,
    cache_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    def fingerprint(text: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        return {
            "chars": len(text),
            "bytes_utf8": len(encoded),
            "sha256_16": hashlib.sha256(encoded).hexdigest()[:16],
        }

    effective_prefixes = cache_prefixes or ([stable_prefix] if stable_prefix is not None else [])
    return {
        "prompt": fingerprint(prompt),
        "stable_prefix": fingerprint(effective_prefixes[-1]) if effective_prefixes else None,
        "cache_prefixes": [fingerprint(prefix) for prefix in effective_prefixes],
    }


def _cache_request(
    prompt: str,
    *,
    model: str,
    prompt_cache_key: str | None,
    stable_prefix: str | None,
    disable_implicit_cache: bool,
    cache_prefixes: list[str] | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    if stable_prefix is not None and cache_prefixes is not None:
        raise ValueError("stable_prefix and cache_prefixes are mutually exclusive")
    effective_prefixes = cache_prefixes or ([stable_prefix] if stable_prefix is not None else [])
    if effective_prefixes and disable_implicit_cache:
        raise ValueError("cache prefixes and disable_implicit_cache are mutually exclusive")
    if len(effective_prefixes) > 4:
        raise ValueError("OpenAI explicit prompt caching supports at most 4 breakpoints per request")
    previous_length = 0
    for prefix in effective_prefixes:
        if not prefix or not prompt.startswith(prefix):
            raise ValueError("Cache prefixes must be non-empty exact prompt prefixes")
        if len(prefix) <= previous_length:
            raise ValueError("Cache prefixes must be unique and ordered from shortest to longest")
        previous_length = len(prefix)
    supports_explicit_cache = model.lower().startswith("gpt-5.6")
    if (effective_prefixes or disable_implicit_cache) and not supports_explicit_cache:
        request_options = {"prompt_cache_key": prompt_cache_key} if prompt_cache_key else {}
        return prompt, request_options, {
            "mode": "implicit_legacy",
            "prompt_cache_key": prompt_cache_key,
            "breakpoint_count": 0,
            "ttl": None,
        }
    if effective_prefixes:
        content: list[dict[str, Any]] = []
        start = 0
        for prefix in effective_prefixes:
            end = len(prefix)
            content.append(
                {
                    "type": "input_text",
                    "text": prompt[start:end],
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            )
            start = end
        if start < len(prompt):
            content.append({"type": "input_text", "text": prompt[start:]})
        request_input: Any = [{"type": "message", "role": "user", "content": content}]
        request_options: dict[str, Any] = {
            "extra_body": {"prompt_cache_options": {"mode": "explicit", "ttl": PROMPT_CACHE_TTL}}
        }
        if prompt_cache_key:
            request_options["prompt_cache_key"] = prompt_cache_key
        return request_input, request_options, {
            "mode": "explicit",
            "prompt_cache_key": prompt_cache_key,
            "breakpoint_count": len(effective_prefixes),
            "ttl": PROMPT_CACHE_TTL,
        }
    if disable_implicit_cache:
        return (
            prompt,
            {"extra_body": {"prompt_cache_options": {"mode": "explicit", "ttl": PROMPT_CACHE_TTL}}},
            {"mode": "explicit", "prompt_cache_key": None, "breakpoint_count": 0, "ttl": PROMPT_CACHE_TTL},
        )
    request_options = {"prompt_cache_key": prompt_cache_key} if prompt_cache_key else {}
    return prompt, request_options, {
        "mode": "implicit",
        "prompt_cache_key": prompt_cache_key,
        "breakpoint_count": 0,
        "ttl": None,
    }


def call_analysis_json(
    prompt: str,
    *,
    model: str = ANALYSIS_MODEL,
    retry_callback: RetryCallback | None = None,
    call_type: str = "full_analysis",
    prompt_cache_key: str | None = None,
    stable_prefix: str | None = None,
    cache_prefixes: list[str] | None = None,
    trace_entity_type: str | None = None,
    trace_entity_id: str | None = None,
    defer_usage_trace: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_fingerprint = _request_fingerprint(prompt, stable_prefix, cache_prefixes)
    request_input, cache_options, cache_metadata = _cache_request(
        prompt,
        model=model,
        prompt_cache_key=prompt_cache_key,
        stable_prefix=stable_prefix,
        cache_prefixes=cache_prefixes,
        disable_implicit_cache=False,
    )
    log_model_text_payload(
        logger,
        title="deal analysis prompt",
        model=model,
        text=prompt,
        metadata={
            "api": "responses.create",
            "response_format": "json_object",
            "reasoning_effort": ANALYSIS_REASONING_EFFORT,
            "call_type": call_type,
            "prompt_cache": cache_metadata,
        },
    )
    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started_at = perf_counter()
    transport_events: list[dict[str, Any]] = []

    def transport_callback(event: dict[str, Any]) -> None:
        transport_events.append(dict(event))
        if retry_callback is not None:
            retry_callback(event)

    try:
        response = run_with_retry(
            lambda: client.responses.create(
                model=model,
                input=request_input,
                max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
                reasoning={"effort": ANALYSIS_REASONING_EFFORT},
                text={"format": {"type": "json_object"}},
                store=False,
                **cache_options,
            ),
            operation_name="openai:responses.create",
            policy=DEFAULT_TRANSPORT_RETRY,
            on_event=transport_callback,
        )
    except Exception as error:
        append_usage_trace(
            {
                "model": model,
                "call_type": call_type,
                "requested_at": requested_at,
                "latency_seconds": round(perf_counter() - started_at, 4),
                "prompt_cache": cache_metadata,
                "request_fingerprint": request_fingerprint,
                "reasoning_effort": ANALYSIS_REASONING_EFFORT,
                "transport_attempt_count": sum(1 for event in transport_events if event.get("status") == "attempt"),
                "transport_retry_count": sum(1 for event in transport_events if event.get("status") == "retry_wait"),
                "transport_retry": any(event.get("status") == "retry_wait" for event in transport_events),
            },
            status="error",
            entity_type=trace_entity_type,
            entity_id=trace_entity_id,
            error_type=type(error).__name__,
        )
        raise
    latency_seconds = round(perf_counter() - started_at, 4)

    text = response_output_text(response)
    usage = usage_to_dict(response)
    estimated_cost = estimate_analysis_cost(model, usage, USD_RUB_RATE)
    logger.info(
        "OpenAI analysis response usage: call_type=%s model=%s input_tokens=%s cached_input_tokens=%s cache_write_tokens=%s output_tokens=%s total_tokens=%s latency_seconds=%s estimated_cost_usd=%s estimated_cost_rub=%s",
        call_type,
        model,
        usage.get("input_tokens"),
        estimated_cost.get("cached_input_tokens"),
        estimated_cost.get("cache_write_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        latency_seconds,
        estimated_cost.get("estimated_cost_usd"),
        estimated_cost.get("estimated_cost_rub"),
    )

    metadata = {
        "model": model,
        "call_type": call_type,
        "requested_at": requested_at,
        "latency_seconds": latency_seconds,
        "prompt_cache": cache_metadata,
        "request_fingerprint": request_fingerprint,
        "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        "usage": usage,
        "estimated_cost": estimated_cost,
        "estimated_cost_usd": estimated_cost.get("estimated_cost_usd"),
        "estimated_cost_rub": estimated_cost.get("estimated_cost_rub"),
        "response_id": getattr(response, "id", None),
        "transport_attempt_count": sum(1 for event in transport_events if event.get("status") == "attempt"),
        "transport_retry_count": sum(1 for event in transport_events if event.get("status") == "retry_wait"),
        "transport_retry": any(event.get("status") == "retry_wait" for event in transport_events),
        "raw_output_text": text,
    }

    try:
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError) as error:
        if not defer_usage_trace:
            append_usage_trace(
                metadata,
                status="error",
                entity_type=trace_entity_type,
                entity_id=trace_entity_id,
                error_type="ModelJsonParseError",
            )
        preview = text[:500].replace("\n", "\\n")
        raise ModelJsonParseError(
            f"Model returned invalid JSON: {error}. Raw output preview: {preview}",
            raw_output_text=text,
            metadata=metadata,
        ) from error

    if not defer_usage_trace:
        append_usage_trace(metadata, entity_type=trace_entity_type, entity_id=trace_entity_id)
    return parsed, metadata


def _correction_prompt(original_prompt: str, error: str, raw_output_text: str) -> str:
    previous = raw_output_text[-30_000:]
    return (
        original_prompt
        + "\n\n<correction_attempt>\n"
        + "Предыдущий ответ не прошёл машинную проверку. Верни заново полный JSON-объект, а не патч. "
        + "Исправь только указанные нарушения и сохрани опору на исходные факты.\n"
        + f"Ошибки проверки: {error}\n"
        + "Предыдущий ответ:\n"
        + previous
        + "\n</correction_attempt>"
    )


def _aggregate_attempt_metadata(attempts: list[dict[str, Any]], final_metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(final_metadata)
    def attempt_cost(item: dict[str, Any], key: str) -> float:
        if item.get(key) is not None:
            return float(item.get(key) or 0)
        nested = item.get("estimated_cost") if isinstance(item.get("estimated_cost"), dict) else {}
        return float(nested.get(key) or 0)

    total_usd = sum(attempt_cost(item, "estimated_cost_usd") for item in attempts)
    total_rub = sum(attempt_cost(item, "estimated_cost_rub") for item in attempts)
    result["semantic_attempt_count"] = len(attempts)
    result["semantic_attempts"] = [
        {key: value for key, value in item.items() if key != "raw_output_text"}
        for item in attempts
    ]
    result["estimated_cost_usd"] = round(total_usd, 6)
    result["estimated_cost_rub"] = round(total_rub, 2)
    estimated_cost = dict(result.get("estimated_cost") or {})
    estimated_cost["estimated_cost_usd"] = result["estimated_cost_usd"]
    estimated_cost["estimated_cost_rub"] = result["estimated_cost_rub"]
    result["estimated_cost"] = estimated_cost
    result["latency_seconds"] = round(
        sum(float(item.get("latency_seconds") or 0) for item in attempts),
        4,
    )
    if attempts:
        result["requested_at"] = attempts[0].get("requested_at", result.get("requested_at"))
    usage_rows = [item.get("usage") for item in attempts if isinstance(item.get("usage"), dict)]
    if usage_rows:
        input_details_rows = [
            row.get("input_tokens_details") for row in usage_rows if isinstance(row.get("input_tokens_details"), dict)
        ]
        output_details_rows = [
            row.get("output_tokens_details") for row in usage_rows if isinstance(row.get("output_tokens_details"), dict)
        ]
        result["usage"] = {
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in usage_rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in usage_rows),
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usage_rows),
            "input_tokens_details": {
                "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in input_details_rows),
                "cache_write_tokens": sum(
                    int(row.get("cache_write_tokens") or 0) for row in input_details_rows
                ),
            },
            "output_tokens_details": {
                "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in output_details_rows),
            },
        }
        aggregate_cost = estimate_analysis_cost(str(result.get("model") or ""), result["usage"], USD_RUB_RATE)
        for key, value in aggregate_cost.items():
            if key not in {"estimated_cost_usd", "estimated_cost_rub"}:
                estimated_cost[key] = value
        result["estimated_cost"] = estimated_cost
    return result


def _safe_validation_error(error: BaseException) -> str:
    if isinstance(error, ModelJsonParseError):
        return "ModelJsonParseError: invalid JSON response"
    text = re.sub(r"https?://\S+", "[url]", str(error or ""))
    text = re.sub(r"(\bgot\s+)[^;]+", r"\1[invalid value]", text)
    return f"{error.__class__.__name__}: {text}"[:2000]


def _semantic_attempt_metadata(
    metadata: dict[str, Any],
    *,
    attempt_number: int,
    validation_passed: bool,
    validation_error: str | None,
) -> dict[str, Any]:
    result = dict(metadata)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    input_details = input_details if isinstance(input_details, dict) else {}
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    output_details = output_details if isinstance(output_details, dict) else {}
    cost = result.get("estimated_cost") if isinstance(result.get("estimated_cost"), dict) else {}
    result.update(
        {
            "attempt_number": attempt_number,
            "semantic_correction_retry": attempt_number > 1,
            "input_tokens": usage.get("input_tokens", cost.get("input_tokens")),
            "cached_tokens": input_details.get(
                "cached_tokens",
                usage.get("cached_input_tokens", cost.get("cached_input_tokens")),
            ),
            "cache_write_tokens": input_details.get(
                "cache_write_tokens",
                usage.get("cache_write_tokens", cost.get("cache_write_tokens")),
            ),
            "output_tokens": usage.get("output_tokens", cost.get("output_tokens")),
            "reasoning_tokens": output_details.get("reasoning_tokens", usage.get("reasoning_tokens")),
            "estimated_cost_usd": result.get("estimated_cost_usd", cost.get("estimated_cost_usd")),
            "estimated_cost_rub": result.get("estimated_cost_rub", cost.get("estimated_cost_rub")),
            "validation_passed": validation_passed,
            "validation_error": validation_error,
            "transport_retry": bool(result.get("transport_retry")),
        }
    )
    return result


def call_validated_analysis_json(
    prompt: str,
    *,
    validator: Callable[[dict[str, Any]], None],
    normalizer: Callable[[dict[str, Any]], list[str]],
    validation_error_types: tuple[type[BaseException], ...],
    model: str = ANALYSIS_MODEL,
    retry_callback: RetryCallback | None = None,
    semantic_callback: RetryCallback | None = None,
    analysis_caller: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = call_analysis_json,
    call_type: str = "full_analysis",
    prompt_cache_key: str | None = None,
    prompt_cache_marker: str | None = None,
    prompt_cache_markers: list[str] | None = None,
    trace_entity_type: str | None = None,
    trace_entity_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    current_prompt = prompt
    final_raw = ""
    final_analysis: dict[str, Any] | None = None
    final_error = ""

    for semantic_attempt in (1, 2):
        if semantic_callback is not None:
            semantic_callback(
                {
                    "status": "attempt",
                    "attempt": semantic_attempt,
                    "max_attempts": 2,
                    "operation": "openai:validated_analysis",
                }
            )
        deferred_trace = analysis_caller is call_analysis_json
        attempt_error: BaseException | None = None
        try:
            if prompt_cache_marker is not None and prompt_cache_markers is not None:
                raise ValueError("prompt_cache_marker and prompt_cache_markers are mutually exclusive")
            markers = prompt_cache_markers or ([prompt_cache_marker] if prompt_cache_marker else [])
            cache_prefixes = [prompt_prefix_before(current_prompt, marker) for marker in markers]
            cache_prefixes = sorted(set(cache_prefixes), key=len)
            caller_options = {
                "model": model,
                "retry_callback": retry_callback,
                "call_type": call_type,
                "prompt_cache_key": prompt_cache_key,
                "cache_prefixes": cache_prefixes or None,
                "trace_entity_type": trace_entity_type,
                "trace_entity_id": trace_entity_id,
            }
            if deferred_trace:
                caller_options["defer_usage_trace"] = True
            analysis, metadata = analysis_caller(current_prompt, **caller_options)
            final_raw = str(metadata.get("raw_output_text") or "")
            final_analysis = analysis
            normalization_changes = normalizer(analysis)
            if normalization_changes:
                metadata["normalization_changes"] = normalization_changes
                logger.warning("Normalized analysis before validation: %s", normalization_changes)
            validator(analysis)
        except ModelJsonParseError as error:
            metadata = dict(error.metadata)
            final_raw = error.raw_output_text
            final_analysis = None
            final_error = str(error)
            attempt_error = error
        except validation_error_types as error:
            final_error = str(error)
            attempt_error = error
        else:
            attempt_metadata = _semantic_attempt_metadata(
                metadata,
                attempt_number=semantic_attempt,
                validation_passed=True,
                validation_error=None,
            )
            attempts.append(attempt_metadata)
            if deferred_trace:
                append_usage_trace(
                    attempt_metadata,
                    entity_type=trace_entity_type,
                    entity_id=trace_entity_id,
                )
            if semantic_callback is not None:
                semantic_callback(
                    {
                        "status": "success",
                        "attempt": semantic_attempt,
                        "max_attempts": 2,
                        "operation": "openai:validated_analysis",
                    }
                )
            return analysis, _aggregate_attempt_metadata(attempts, metadata)

        safe_attempt_error = _safe_validation_error(attempt_error or ValueError(final_error))
        attempt_metadata = _semantic_attempt_metadata(
            metadata,
            attempt_number=semantic_attempt,
            validation_passed=False,
            validation_error=safe_attempt_error,
        )
        attempts.append(attempt_metadata)
        if deferred_trace:
            append_usage_trace(
                attempt_metadata,
                status="error",
                entity_type=trace_entity_type,
                entity_id=trace_entity_id,
                error_type=type(attempt_error).__name__ if attempt_error is not None else "ValidationError",
            )
        if semantic_attempt == 1:
            if semantic_callback is not None:
                semantic_callback(
                    {
                        "status": "retry_wait",
                        "attempt": semantic_attempt,
                        "max_attempts": 2,
                        "operation": "openai:validated_analysis",
                        "error": final_error,
                        "delay_seconds": 0,
                    }
                )
            current_prompt = _correction_prompt(prompt, final_error, final_raw)
            continue

        failed_metadata = _aggregate_attempt_metadata(attempts, metadata)
        failed_metadata["raw_output_text"] = final_raw
        if semantic_callback is not None:
            semantic_callback(
                {
                    "status": "failed",
                    "attempt": semantic_attempt,
                    "max_attempts": 2,
                    "operation": "openai:validated_analysis",
                    "error": final_error,
                }
            )
        raise ValidatedAnalysisFailure(
            final_error,
            raw_output_text=final_raw,
            metadata=failed_metadata,
            analysis=final_analysis,
        )

    raise RuntimeError("semantic retry loop exhausted unexpectedly")


def call_structured_output_json(
    prompt: str,
    *,
    schema: dict[str, Any],
    schema_name: str,
    model: str = ANALYSIS_MODEL,
    reasoning_effort: str | None = None,
    max_output_tokens: int = ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
    retry_callback: RetryCallback | None = None,
    log_title: str = "structured output prompt",
    call_type: str | None = None,
    prompt_cache_key: str | None = None,
    stable_prefix: str | None = None,
    cache_prefixes: list[str] | None = None,
    disable_implicit_cache: bool = False,
    trace_entity_type: str | None = None,
    trace_entity_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call Responses structured outputs without changing the legacy JSON client."""
    effective_reasoning_effort = reasoning_effort or ANALYSIS_REASONING_EFFORT
    effective_call_type = call_type or schema_name
    request_fingerprint = _request_fingerprint(prompt, stable_prefix, cache_prefixes)
    request_input, cache_options, cache_metadata = _cache_request(
        prompt,
        model=model,
        prompt_cache_key=prompt_cache_key,
        stable_prefix=stable_prefix,
        cache_prefixes=cache_prefixes,
        disable_implicit_cache=disable_implicit_cache,
    )
    log_model_text_payload(
        logger,
        title=log_title,
        model=model,
        text=prompt,
        metadata={
            "api": "responses.create",
            "response_format": "json_schema",
            "schema_name": schema_name,
            "reasoning_effort": effective_reasoning_effort,
            "call_type": effective_call_type,
            "prompt_cache": cache_metadata,
        },
    )
    requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started_at = perf_counter()
    try:
        response = run_with_retry(
            lambda: client.responses.create(
                model=model,
                input=request_input,
                max_output_tokens=max_output_tokens,
                reasoning={"effort": effective_reasoning_effort},
                text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
                store=False,
                **cache_options,
            ),
            operation_name=f"openai:responses.create:{schema_name}",
            policy=DEFAULT_TRANSPORT_RETRY,
            on_event=retry_callback,
        )
    except Exception as error:
        append_usage_trace(
            {
                "model": model,
                "call_type": effective_call_type,
                "requested_at": requested_at,
                "latency_seconds": round(perf_counter() - started_at, 4),
                "prompt_cache": cache_metadata,
                "request_fingerprint": request_fingerprint,
                "reasoning_effort": effective_reasoning_effort,
            },
            status="error",
            entity_type=trace_entity_type,
            entity_id=trace_entity_id,
            error_type=type(error).__name__,
        )
        raise
    latency_seconds = round(perf_counter() - started_at, 4)
    text = response_output_text(response)
    usage = usage_to_dict(response)
    estimated_cost = estimate_analysis_cost(model, usage, USD_RUB_RATE)
    logger.info(
        "OpenAI structured response usage: call_type=%s model=%s input_tokens=%s cached_input_tokens=%s cache_write_tokens=%s output_tokens=%s total_tokens=%s latency_seconds=%s estimated_cost_usd=%s estimated_cost_rub=%s",
        effective_call_type,
        model,
        usage.get("input_tokens"),
        estimated_cost.get("cached_input_tokens"),
        estimated_cost.get("cache_write_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
        latency_seconds,
        estimated_cost.get("estimated_cost_usd"),
        estimated_cost.get("estimated_cost_rub"),
    )
    metadata = {
        "model": model,
        "call_type": effective_call_type,
        "requested_at": requested_at,
        "latency_seconds": latency_seconds,
        "prompt_cache": cache_metadata,
        "request_fingerprint": request_fingerprint,
        "reasoning_effort": effective_reasoning_effort,
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
        append_usage_trace(
            metadata,
            status="incomplete",
            entity_type=trace_entity_type,
            entity_id=trace_entity_id,
            error_type="ModelResponseIncompleteError",
        )
        reason = metadata["incomplete_reason"] or "unknown"
        raise ModelResponseIncompleteError(
            f"Structured output is incomplete: {reason}",
            raw_output_text=text,
            metadata=metadata,
        )
    try:
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError) as error:
        append_usage_trace(
            metadata,
            status="error",
            entity_type=trace_entity_type,
            entity_id=trace_entity_id,
            error_type="ModelJsonParseError",
        )
        preview = text[:500].replace("\n", "\\n")
        raise ModelJsonParseError(
            f"Structured output returned invalid JSON: {error}. Raw output preview: {preview}",
            raw_output_text=text,
            metadata=metadata,
        ) from error
    append_usage_trace(metadata, entity_type=trace_entity_type, entity_id=trace_entity_id)
    return parsed, metadata
