"""Privacy-preserving daily JSONL trace for physical Bitrix REST attempts."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import LOGS_DIR, MSK_TZ


logger = logging.getLogger(__name__)
DEFAULT_DAILY_USAGE_DIR = LOGS_DIR / "bitrix_usage_daily"


def daily_usage_dir() -> Path:
    configured = os.getenv("BITRIX_USAGE_DAILY_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DAILY_USAGE_DIR


def _safe_name(value: Any, *, default: str = "unknown") -> str:
    text = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120]
    return cleaned or default


def _safe_filter_key(value: Any) -> str:
    text = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.><=!@%]+", "_", text)[:120]
    return cleaned or "unknown"


def build_request_shape(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Describe request structure without retaining any CRM values."""
    value = payload if isinstance(payload, dict) else {}
    raw_filter = value.get("filter") if isinstance(value.get("filter"), dict) else value.get("FILTER")
    filter_value = raw_filter if isinstance(raw_filter, dict) else {}
    select = value.get("select") if isinstance(value.get("select"), list) else value.get("SELECT")
    return {
        "payload_keys": sorted(_safe_name(key) for key in value if str(key).lower() not in {"auth"}),
        "filter_keys": sorted(_safe_filter_key(key) for key in filter_value),
        "select_count": len(select) if isinstance(select, list) else None,
        "has_order": isinstance(value.get("order") or value.get("ORDER"), dict),
        "page_start": value.get("start") if isinstance(value.get("start"), int) else None,
    }


def response_item_count(data: dict[str, Any] | None) -> int:
    value = data if isinstance(data, dict) else {}
    result = value.get("result")
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return len(result["items"])
    if isinstance(result, dict):
        return 1 if result else 0
    return 0


def build_trace_event(
    *,
    run_id: str,
    method: str,
    payload: dict[str, Any] | None,
    attempt: int,
    duration_ms: float,
    ok: bool,
    http_status: int | None,
    data: dict[str, Any] | None,
    error_type: str | None = None,
) -> dict[str, Any]:
    requested_at = datetime.now(MSK_TZ)
    item_count = response_item_count(data)
    api_error = (data or {}).get("error") if isinstance(data, dict) else None
    return {
        "requested_at": requested_at.isoformat(timespec="milliseconds"),
        "run_id": _safe_name(run_id),
        "pid": os.getpid(),
        "method": _safe_name(method),
        "request_shape": build_request_shape(payload),
        "attempt": int(attempt),
        "duration_ms": round(max(0.0, float(duration_ms)), 3),
        "ok": bool(ok),
        "http_status": int(http_status) if isinstance(http_status, int) else None,
        "item_count": item_count,
        "empty": item_count == 0,
        "has_next_page": bool((data or {}).get("next")) if isinstance(data, dict) else False,
        "api_error_code": _safe_name(api_error, default="") or None,
        "error_type": _safe_name(error_type, default="") or None,
    }


def append_trace_event(event: dict[str, Any]) -> None:
    """Append one sanitized event; trace failures never fail the Bitrix call."""
    try:
        requested_at = datetime.fromisoformat(str(event.get("requested_at") or ""))
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=MSK_TZ)
        filename = requested_at.astimezone(MSK_TZ).strftime("%Y-%m-%d.jsonl")
        path = daily_usage_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Unable to append Bitrix usage trace: %s", type(error).__name__)
