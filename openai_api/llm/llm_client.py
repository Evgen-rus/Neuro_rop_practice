"""
Small OpenAI Responses API wrapper for JSON analysis calls.
"""

from __future__ import annotations

import json
import re
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
from openai_api.pricing import estimate_analysis_cost
from reliability.retry import DEFAULT_TRANSPORT_RETRY, RetryCallback, run_with_retry


client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)


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


def call_analysis_json(
    prompt: str,
    *,
    model: str = ANALYSIS_MODEL,
    retry_callback: RetryCallback | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
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

    response = run_with_retry(
        lambda: client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=ANALYSIS_MAX_OUTPUT_TOKENS,
            reasoning={"effort": ANALYSIS_REASONING_EFFORT},
            text={"format": {"type": "json_object"}},
            store=False,
        ),
        operation_name="openai:responses.create",
        policy=DEFAULT_TRANSPORT_RETRY,
        on_event=retry_callback,
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
            },
            "output_tokens_details": {
                "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in output_details_rows),
            },
        }
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
        try:
            analysis, metadata = analysis_caller(
                current_prompt,
                model=model,
                retry_callback=retry_callback,
            )
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
        except validation_error_types as error:
            final_error = str(error)
        else:
            attempts.append(dict(metadata))
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

        attempts.append(dict(metadata))
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
    max_output_tokens: int = ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
    retry_callback: RetryCallback | None = None,
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
    response = run_with_retry(
        lambda: client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=max_output_tokens,
            reasoning={"effort": ANALYSIS_REASONING_EFFORT},
            text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
            store=False,
        ),
        operation_name=f"openai:responses.create:{schema_name}",
        policy=DEFAULT_TRANSPORT_RETRY,
        on_event=retry_callback,
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
