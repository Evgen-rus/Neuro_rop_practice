"""
Small read-only Bitrix24 REST helpers for local reporting scripts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

from reliability.retry import DEFAULT_TRANSPORT_RETRY, RetryCallback, RetryPolicy, run_with_retry


PAGE_SIZE = 50


class BitrixTransientError(RuntimeError):
    status_code = 429


class BitrixReadOnlyClient:
    def __init__(
        self,
        webhook_url: str,
        timeout: int = 30,
        retry_callback: RetryCallback | None = None,
        retry_policy: RetryPolicy = DEFAULT_TRANSPORT_RETRY,
    ):
        self.webhook_url = webhook_url.rstrip("/")
        self.timeout = timeout
        self.retry_callback = retry_callback
        self.retry_policy = retry_policy

    def method_url(self, method: str) -> str:
        return f"{self.webhook_url}/{method}"

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        def request_once() -> tuple[requests.Response, dict[str, Any]]:
            response = requests.post(
                self.method_url(method),
                json=payload or {},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            if response.status_code in {408, 409, 429} or response.status_code >= 500:
                response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = {}
            error_code = str(data.get("error") or "").upper()
            if error_code in {"QUERY_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS", "OPERATION_TIME_LIMIT"}:
                raise BitrixTransientError(f"{method}: {data.get('error_description') or error_code}")
            return response, data

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
        start: int | str = 0
        base_payload = dict(payload or {})

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
