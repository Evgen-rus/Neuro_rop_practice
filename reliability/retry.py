"""Explicit retry policy shared by OpenAI, Bitrix and audio downloads."""

from __future__ import annotations

import asyncio
import email.utils
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar


T = TypeVar("T")
RetryCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.jitter_ratio < 0:
            raise ValueError("jitter_ratio must be non-negative")


DEFAULT_TRANSPORT_RETRY = RetryPolicy()


@dataclass(frozen=True)
class RetryEvent:
    status: str
    attempt: int
    max_attempts: int
    operation: str
    error: str | None = None
    delay_seconds: float | None = None


def _emit(callback: RetryCallback | None, event: RetryEvent) -> None:
    if callback is not None:
        callback(asdict(event))


def safe_error_message(error: BaseException) -> str:
    text = re.sub(r"https?://\S+", "[url]", str(error))
    return f"{error.__class__.__name__}: {text}"[:400]


def status_code_from_error(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if isinstance(value, int):
        return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def headers_from_error(error: BaseException) -> Any:
    response = getattr(error, "response", None)
    return getattr(response, "headers", None)


def retry_after_seconds(error: BaseException) -> float | None:
    headers = headers_from_error(error)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        target = email.utils.parsedate_to_datetime(str(value))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def is_transient_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if error.__class__.__name__ in {
        "APITimeoutError",
        "APIConnectionError",
        "ConnectionError",
        "ChunkedEncodingError",
        "ContentDecodingError",
        "ProxyError",
        "SSLError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "Timeout",
    }:
        return True
    status_code = status_code_from_error(error)
    return status_code in {408, 409, 429} or bool(status_code and status_code >= 500)


def retry_delay(policy: RetryPolicy, attempt: int, error: BaseException) -> float:
    explicit = retry_after_seconds(error)
    if explicit is not None:
        return min(explicit, policy.max_delay_seconds)
    base = min(policy.base_delay_seconds * (2 ** max(0, attempt - 1)), policy.max_delay_seconds)
    if not base or not policy.jitter_ratio:
        return base
    spread = base * policy.jitter_ratio
    return max(0.0, min(policy.max_delay_seconds, base + random.uniform(-spread, spread)))


def run_with_retry(
    operation: Callable[[], T],
    *,
    operation_name: str,
    policy: RetryPolicy = DEFAULT_TRANSPORT_RETRY,
    is_retryable: Callable[[BaseException], bool] = is_transient_error,
    on_event: RetryCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        _emit(on_event, RetryEvent("attempt", attempt, policy.max_attempts, operation_name))
        try:
            result = operation()
        except BaseException as error:
            retryable = is_retryable(error)
            if not retryable or attempt >= policy.max_attempts:
                _emit(
                    on_event,
                    RetryEvent("failed", attempt, policy.max_attempts, operation_name, error=safe_error_message(error)),
                )
                raise
            delay = retry_delay(policy, attempt, error)
            _emit(
                on_event,
                RetryEvent(
                    "retry_wait",
                    attempt,
                    policy.max_attempts,
                    operation_name,
                    error=safe_error_message(error),
                    delay_seconds=round(delay, 3),
                ),
            )
            sleep(delay)
        else:
            _emit(on_event, RetryEvent("success", attempt, policy.max_attempts, operation_name))
            return result
    raise RuntimeError("retry loop exhausted unexpectedly")


async def run_with_retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    policy: RetryPolicy = DEFAULT_TRANSPORT_RETRY,
    is_retryable: Callable[[BaseException], bool] = is_transient_error,
    on_event: RetryCallback | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        _emit(on_event, RetryEvent("attempt", attempt, policy.max_attempts, operation_name))
        try:
            result = await operation()
        except BaseException as error:
            retryable = is_retryable(error)
            if not retryable or attempt >= policy.max_attempts:
                _emit(
                    on_event,
                    RetryEvent("failed", attempt, policy.max_attempts, operation_name, error=safe_error_message(error)),
                )
                raise
            delay = retry_delay(policy, attempt, error)
            _emit(
                on_event,
                RetryEvent(
                    "retry_wait",
                    attempt,
                    policy.max_attempts,
                    operation_name,
                    error=safe_error_message(error),
                    delay_seconds=round(delay, 3),
                ),
            )
            await sleep(delay)
        else:
            _emit(on_event, RetryEvent("success", attempt, policy.max_attempts, operation_name))
            return result
    raise RuntimeError("retry loop exhausted unexpectedly")
