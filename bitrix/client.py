"""
Small read-only Bitrix24 REST helpers for local reporting scripts.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from reliability.retry import DEFAULT_TRANSPORT_RETRY, RetryCallback, RetryPolicy, run_with_retry
from bitrix.usage_trace import (
    append_trace_event,
    batch_command_methods,
    build_trace_event,
    env_flag,
    resolve_run_id,
)


PAGE_SIZE = 50
BATCH_REQUEST_LIMIT = 50
DEFAULT_BATCH_CHUNK_SIZE = 20


class BitrixTransientError(RuntimeError):
    status_code = 429


class BitrixWriteBlockedError(Exception):
    """Diagnostic guard: a mutation method was about to be sent. Not a RuntimeError so safe_call cannot swallow it."""


_WRITE_METHOD_PARTS = frozenset(
    {
        "add",
        "update",
        "delete",
        "move",
        "complete",
        "start",
        "pause",
        "renew",
        "share",
        "upload",
        "copy",
        "setstatus",
    }
)


def forbidden_write_methods(method: str, payload: dict[str, Any] | None = None) -> list[str]:
    """Return REST methods that look like Bitrix mutations, including batch subcommands."""
    candidates = [str(method or "").strip()]
    if candidates[0] == "batch":
        candidates.extend(batch_command_methods(payload))
    blocked: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts = [part.lower() for part in name.replace("-", ".").split(".") if part]
        if any(part in _WRITE_METHOD_PARTS for part in parts):
            blocked.append(name)
    return blocked


def assert_read_only_method(method: str, payload: dict[str, Any] | None = None) -> None:
    if not env_flag("BITRIX_DENY_WRITE_METHODS"):
        return
    blocked = forbidden_write_methods(method, payload)
    if blocked:
        raise BitrixWriteBlockedError(
            "Диагностика остановила Bitrix write до HTTP: " + ", ".join(blocked)
        )


class BitrixReadOnlyClient:
    def __init__(
        self,
        webhook_url: str,
        timeout: int = 30,
        retry_callback: RetryCallback | None = None,
        retry_policy: RetryPolicy = DEFAULT_TRANSPORT_RETRY,
        trace_run_id: str | None = None,
    ):
        self.webhook_url = webhook_url.rstrip("/")
        self.timeout = timeout
        self.retry_callback = retry_callback
        self.retry_policy = retry_policy
        self.trace_run_id = trace_run_id or uuid.uuid4().hex

    def method_url(self, method: str) -> str:
        return f"{self.webhook_url}/{method}"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert_read_only_method(method, payload)
        attempt = 0

        def request_once() -> tuple[requests.Response, dict[str, Any]]:
            nonlocal attempt
            attempt += 1
            started_at = time.perf_counter()
            response: requests.Response | None = None
            data: dict[str, Any] = {}
            error_type: str | None = None
            try:
                response = requests.post(
                    self.method_url(method),
                    json=payload or {},
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
                if response.status_code in {408, 409, 429} or response.status_code >= 500:
                    response.raise_for_status()
                try:
                    parsed = response.json()
                    data = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    data = {}
                error_code = str(data.get("error") or "").upper()
                if error_code in {"QUERY_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS", "OPERATION_TIME_LIMIT"}:
                    raise BitrixTransientError(f"{method}: {data.get('error_description') or error_code}")
                return response, data
            except BaseException as error:
                error_type = type(error).__name__
                raise
            finally:
                http_status = response.status_code if response is not None else None
                api_error = bool(data.get("error"))
                append_trace_event(
                    build_trace_event(
                        run_id=resolve_run_id(self.trace_run_id),
                        method=method,
                        payload=payload,
                        attempt=attempt,
                        duration_ms=(time.perf_counter() - started_at) * 1000,
                        ok=bool(response is not None and response.ok and not api_error and error_type is None),
                        http_status=http_status,
                        data=data,
                        error_type=error_type,
                    )
                )

        response, data = run_with_retry(
            request_once,
            operation_name=f"bitrix:{method}",
            policy=self.retry_policy,
            on_event=self.retry_callback,
        )

        if not response.ok:
            error_text = data.get("error_description") or data.get("error") or response.text
            raise RuntimeError(f"{method}: HTTP {response.status_code}: {error_text}")

        if data.get("error"):
            error_text = data.get("error_description") or data.get("error")
            raise RuntimeError(f"{method}: {error_text}")

        return data

    def safe_call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, "method": method, "payload": payload or {}, "response": self.call(method, payload)}
        except (requests.RequestException, RuntimeError) as error:
            return {"ok": False, "method": method, "payload": payload or {}, "error": str(error)}

    def list_all(self, method: str, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        base_payload = dict(payload or {})
        start: int | str = base_payload.pop("start", 0)

        while True:
            page_payload = dict(base_payload)
            page_payload["start"] = start
            data = self.call(method, page_payload)
            result = data.get("result", [])

            if isinstance(result, dict) and isinstance(result.get("items"), list):
                batch = result["items"]
            elif isinstance(result, dict):
                batch = list(result.values())
            elif isinstance(result, list):
                batch = result
            else:
                batch = []

            items.extend([item for item in batch if isinstance(item, dict)])

            next_start = data.get("next")
            if next_start is None or len(batch) < PAGE_SIZE:
                break
            start = next_start

        return items

    @staticmethod
    def _batch_command(method: str, payload: dict[str, Any] | None = None) -> str:
        """Encode one read-only REST command for Bitrix batch."""
        pairs: list[tuple[str, Any]] = []

        def append(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    append(f"{prefix}[{key}]" if prefix else str(key), child)
                return
            if isinstance(value, (list, tuple, set)):
                for child in value:
                    append(f"{prefix}[]", child)
                return
            if value is None:
                return
            if isinstance(value, bool):
                value = "1" if value else "0"
            pairs.append((prefix, value))

        for key, value in (payload or {}).items():
            append(str(key), value)
        query = urlencode(pairs)
        return f"{method}?{query}" if query else method

    @staticmethod
    def _batch_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            rows = value["items"]
        elif isinstance(value, dict):
            rows = list(value.values())
        elif isinstance(value, list):
            rows = value
        else:
            rows = []
        return [item for item in rows if isinstance(item, dict)]

    def safe_batch_call(
        self,
        requests_to_run: list[tuple[str, str, dict[str, Any]]],
        *,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
    ) -> dict[str, dict[str, Any]]:
        """Run independent read calls through batch and preserve ``safe_call`` shape."""
        size = max(1, min(int(chunk_size), BATCH_REQUEST_LIMIT))
        normalized: list[tuple[str, str, dict[str, Any]]] = []
        seen_keys: set[str] = set()
        for key, method, payload in requests_to_run:
            normalized_key = str(key).strip()
            if not normalized_key or normalized_key in seen_keys:
                raise ValueError("Batch request keys must be non-empty and unique")
            seen_keys.add(normalized_key)
            normalized.append((normalized_key, str(method).strip(), dict(payload or {})))

        results: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(normalized), size):
            chunk = normalized[offset:offset + size]
            batch_keys = {
                key: f"r{offset + index}"
                for index, (key, _method, _payload) in enumerate(chunk)
            }
            commands = {
                batch_keys[key]: self._batch_command(method, payload)
                for key, method, payload in chunk
            }
            outer = self.safe_call("batch", {"halt": 0, "cmd": commands})
            if not outer.get("ok"):
                error = str(outer.get("error") or "Bitrix batch unavailable")
                for key, method, payload in chunk:
                    results[key] = {
                        "ok": False,
                        "method": method,
                        "payload": payload,
                        "error": error,
                    }
                continue

            response = (outer.get("response") or {}).get("result")
            container = response if isinstance(response, dict) else {}
            raw_results = container.get("result") if isinstance(container.get("result"), dict) else {}
            raw_errors = container.get("result_error") if isinstance(container.get("result_error"), dict) else {}
            raw_next = container.get("result_next") if isinstance(container.get("result_next"), dict) else {}
            raw_total = container.get("result_total") if isinstance(container.get("result_total"), dict) else {}
            for key, method, payload in chunk:
                batch_key = batch_keys[key]
                if batch_key in raw_errors:
                    raw_error = raw_errors.get(batch_key)
                    if isinstance(raw_error, dict):
                        error = raw_error.get("error_description") or raw_error.get("error")
                    else:
                        error = raw_error
                    results[key] = {
                        "ok": False,
                        "method": method,
                        "payload": payload,
                        "error": str(error or "Bitrix batch item unavailable"),
                    }
                    continue

                if batch_key not in raw_results:
                    results[key] = {
                        "ok": False,
                        "method": method,
                        "payload": payload,
                        "error": "Bitrix batch item missing from response",
                    }
                    continue

                results[key] = {
                    "ok": True,
                    "method": method,
                    "payload": payload,
                    "response": {"result": raw_results.get(batch_key)},
                    "next": raw_next.get(batch_key),
                    "total": raw_total.get(batch_key),
                }
        return results

    def safe_batch_list(
        self,
        requests_to_run: list[tuple[str, str, dict[str, Any]]],
        *,
        chunk_size: int = DEFAULT_BATCH_CHUNK_SIZE,
    ) -> dict[str, dict[str, Any]]:
        """Run independent list reads through batch and finish paginated tails normally."""
        batched = self.safe_batch_call(requests_to_run, chunk_size=chunk_size)
        results: dict[str, dict[str, Any]] = {}
        for key, method, payload in requests_to_run:
            response = batched[key]
            if not response.get("ok"):
                results[key] = {**response, "items": []}
                continue
            raw_result = (response.get("response") or {}).get("result")
            items = self._batch_items(raw_result)
            next_start = response.get("next")
            if next_start is not None:
                tail_payload = dict(payload)
                tail_payload["start"] = next_start
                tail = self.safe_list_all(method, tail_payload)
                if not tail.get("ok"):
                    results[key] = tail
                    continue
                items.extend(tail.get("items") or [])
            results[key] = {
                "ok": True,
                "method": method,
                "payload": payload,
                "items": items,
            }
        return results

    def safe_list_all(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, "method": method, "payload": payload or {}, "items": self.list_all(method, payload)}
        except (requests.RequestException, RuntimeError) as error:
            return {"ok": False, "method": method, "payload": payload or {}, "error": str(error), "items": []}


def get_env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is empty or missing")
    return value


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]
