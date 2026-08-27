"""Privacy-preserving daily JSONL trace for physical Bitrix REST attempts."""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from setup import LOGS_DIR, MSK_TZ


logger = logging.getLogger(__name__)
DEFAULT_DAILY_USAGE_DIR = LOGS_DIR / "bitrix_usage_daily"

KNOWN_COMPONENTS = frozenset(
    {
        "deal_control",
        "manager_trajectory_core",
        "manager_trajectory_timeline",
        "manager_trajectory_stage_history",
        "manager_trajectory_task_history",
        "manager_presence",
        "per_deal_context",
        "cheap_change_detection",
        "timeline_delta",
        "stage_delta",
        "task_delta",
        "chat_discovery",
        "chat_update",
        "entity_cache",
        "reconciliation",
        "timeline",
        "stage_history",
        "task_history",
        "invoice",
        "entity_get",
        "product_rows",
        "audio_discovery",
        "audio_readiness",
        "disk_file_get",
        "other",
    }
)

_COMPONENT: ContextVar[str | None] = ContextVar("bitrix_trace_component", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("bitrix_trace_run_id", default=None)
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})


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


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY_ENV


def normalize_component(value: Any) -> str:
    name = _safe_name(value, default="other")
    return name if name in KNOWN_COMPONENTS else "other"


def resolve_component(explicit: str | None = None) -> str:
    if explicit is not None:
        return normalize_component(explicit)
    context_value = _COMPONENT.get()
    if context_value:
        return normalize_component(context_value)
    return normalize_component(os.getenv("BITRIX_TRACE_COMPONENT"))


def resolve_run_id(fallback: str) -> str:
    override = _RUN_ID.get()
    if override:
        return _safe_name(override)
    env_id = os.getenv("BITRIX_TRACE_RUN_ID", "").strip()
    if env_id:
        return _safe_name(env_id)
    return _safe_name(fallback)


def resolve_entity_id() -> str | None:
    """Deal/lead id only for local diagnostic traces, never in default production JSONL."""
    if not env_flag("BITRIX_TRACE_ALLOW_ENTITY_ID"):
        return None
    value = os.getenv("BITRIX_TRACE_ENTITY_ID", "").strip()
    return _safe_name(value) if value else None


@contextmanager
def bitrix_trace_context(*, component: str | None = None, run_id: str | None = None) -> Iterator[None]:
    """Attach privacy-safe trace metadata without changing REST payloads."""
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    if component is not None:
        tokens.append((_COMPONENT, _COMPONENT.set(normalize_component(component))))
    if run_id is not None:
        tokens.append((_RUN_ID, _RUN_ID.set(_safe_name(run_id))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


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


def batch_command_methods(payload: dict[str, Any] | None) -> list[str]:
    """Return sanitized batch method names; never keep query values."""
    cmd = (payload or {}).get("cmd") if isinstance(payload, dict) else None
    if not isinstance(cmd, dict):
        return []
    methods: list[str] = []
    for raw in cmd.values():
        text = str(raw or "").strip()
        method = text.split("?", 1)[0].strip()
        if method:
            methods.append(_safe_name(method))
    return methods


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
    component: str | None = None,
) -> dict[str, Any]:
    requested_at = datetime.now(MSK_TZ)
    safe_method = _safe_name(method)
    is_batch = safe_method == "batch"
    item_count = 0 if is_batch else response_item_count(data)
    api_error = (data or {}).get("error") if isinstance(data, dict) else None
    cmd_methods = batch_command_methods(payload) if is_batch else []
    event = {
        "requested_at": requested_at.isoformat(timespec="milliseconds"),
        "run_id": resolve_run_id(run_id),
        "pid": os.getpid(),
        "component": resolve_component(component),
        "method": safe_method,
        "request_shape": build_request_shape(payload),
        "attempt": int(attempt),
        "is_retry": int(attempt) > 1,
        "duration_ms": round(max(0.0, float(duration_ms)), 3),
        "ok": bool(ok),
        "http_status": int(http_status) if isinstance(http_status, int) else None,
        "item_count": item_count,
        "empty": item_count == 0 and not is_batch,
        "has_next_page": bool((data or {}).get("next")) if isinstance(data, dict) else False,
        "api_error_code": _safe_name(api_error, default="") or None,
        "error_type": _safe_name(error_type, default="") or None,
    }
    entity_id = resolve_entity_id()
    if entity_id:
        event["entity_id"] = entity_id
    if is_batch:
        event["batch_cmd_count"] = len(cmd_methods)
        event["batch_cmd_methods"] = sorted(set(cmd_methods))
        event["empty"] = len(cmd_methods) == 0
    return event


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
