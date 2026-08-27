"""Bounded, private evidence for failed validation attempts (never usage text)."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import LOGS_DIR, MSK_TZ


logger = logging.getLogger(__name__)
DEFAULT_DIAGNOSTICS_DIR = LOGS_DIR / "private_validation"
MAX_ISSUES = 12
MAX_VALUE_CHARS = 512
PATH_PATTERN = re.compile(r"\b[a-z][a-z0-9_]*(?:\[\d+\])?(?:\.[a-z][a-z0-9_]*(?:\[\d+\])?)+\b")


def _redact(text: str) -> str:
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[key]", text)
    return re.sub(r"(?i)\bBearer\s+\S+", "Bearer [token]", text)


def _lookup(analysis: Any, path: str) -> tuple[bool, Any]:
    value = analysis
    for key, index in re.findall(r"([a-z][a-z0-9_]*)|\[(\d+)\]", path):
        if key:
            if not isinstance(value, dict) or key not in value:
                return False, None
            value = value[key]
        else:
            position = int(index)
            if not isinstance(value, list) or position >= len(value):
                return False, None
            value = value[position]
    return True, value


def _fragment(analysis: Any, path: str) -> dict[str, Any]:
    present, value = _lookup(analysis, path)
    if not present:
        return {"present": False}
    result: dict[str, Any] = {"present": True, "type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        result["value"] = value
        return result
    text = _redact(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    result["value" if isinstance(value, str) else "json_excerpt"] = text[:MAX_VALUE_CHARS]
    result["truncated"] = len(text) > MAX_VALUE_CHARS
    return result


def _related_paths(path: str) -> list[str]:
    parent = path.rsplit(".", 1)[0]
    fields: tuple[str, ...] = ()
    if parent == "qualification_assessment.bant.timeframe":
        fields = ("status", "decision_timing_status", "decision_timing", "need_or_launch_timing_status", "need_or_launch_timing")
    elif parent == "qualification_assessment.commercial_fit":
        fields = ("new_equipment_budget_status", "confirmed_budget_rub", "new_equipment_minimum_rub", "reason_code")
    elif parent == "recommendation_feedback":
        fields = ("applicable", "next_action_required", "next_action_at")
    elif re.fullmatch(r"deal_context\.pain_points\[\d+\]", parent):
        fields = ("pain_id", "title", "status")
    elif re.fullmatch(r"deal_context\.journey\[\d+\]", parent):
        fields = ("entry_id", "title", "occurred_at", "status")
    return [f"{parent}.{field}" for field in fields if f"{parent}.{field}" != path]


def build_validation_diagnostic(
    *, error: str, analysis: dict[str, Any] | None,
    original_analysis: dict[str, Any] | None, raw_output_text: str,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    # A model-provided invalid value may itself look like another field path.
    field_errors = re.sub(r"(\bgot\s+)[^;]+", r"\1[invalid value]", error)
    paths = list(dict.fromkeys(PATH_PATTERN.findall(field_errors)))
    safe_error = _redact(error)
    for path in paths[:MAX_ISSUES]:
        issues.append({
            "path": path,
            "received": _fragment(original_analysis, path),
            "validated": _fragment(analysis, path),
            "related_received": {key: _fragment(original_analysis, key) for key in _related_paths(path)},
        })
    result: dict[str, Any] = {
        "error": safe_error[:2000],
        "error_truncated": len(safe_error) > 2000,
        "issues": issues,
        "issues_truncated": len(paths) > MAX_ISSUES,
    }
    if analysis is None and raw_output_text:
        # Invalid JSON has no resolvable field. Keep only a small syntax window.
        position = 0
        try:
            json.loads(raw_output_text)
        except json.JSONDecodeError as parse_error:
            position = parse_error.pos
        start = max(0, position - 128)
        result["syntax_excerpt"] = _redact(raw_output_text[start:start + MAX_VALUE_CHARS])
        result["syntax_excerpt_start"] = start
        result["syntax_excerpt_truncated"] = start > 0 or len(raw_output_text) > MAX_VALUE_CHARS
    return result


def save_validation_diagnostic(
    *, error: str, analysis: dict[str, Any] | None,
    original_analysis: dict[str, Any] | None, raw_output_text: str,
    metadata: dict[str, Any], attempt: int,
) -> str | None:
    """Save each failed attempt privately; return only an opaque relative reference."""
    try:
        payload = build_validation_diagnostic(
            error=error, analysis=analysis, original_analysis=original_analysis,
            raw_output_text=raw_output_text,
        )
        now = datetime.now(MSK_TZ)
        payload.update({
            "schema_version": 1, "recorded_at": now.isoformat(),
            "requested_at": metadata.get("requested_at"), "attempt": attempt,
            "model": metadata.get("model"), "call_type": metadata.get("call_type"),
        })
        root = Path(os.getenv("OPENAI_VALIDATION_DIAGNOSTICS_DIR", "").strip() or DEFAULT_DIAGNOSTICS_DIR)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        day = root / now.date().isoformat()
        day.mkdir(mode=0o700, exist_ok=True)
        path = day / f"{uuid.uuid4().hex}.json"
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
        return path.relative_to(root).as_posix()
    except (OSError, TypeError, ValueError, OverflowError, RecursionError) as error:
        # Do not log the exception message: it may contain a failed value or path.
        logger.warning("Unable to save private validation diagnostic: %s", type(error).__name__)
        return None
