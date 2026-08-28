"""Deterministic section helpers shared by FULL repair and V2 materialization."""
from __future__ import annotations

import copy
from typing import Any


def _preserve_missing_object_keys(
    current: Any,
    previous: Any,
    *,
    path: str,
    changes: list[dict[str, Any]],
) -> None:
    """Backfill omitted object keys without replacing model-provided values or list items."""
    if not isinstance(current, dict) or not isinstance(previous, dict):
        return
    for key, previous_value in previous.items():
        child_path = f"{path}.{key}" if path else key
        if key not in current:
            current[key] = copy.deepcopy(previous_value)
            changes.append({"path": child_path, "action": "preserved_missing_object_key"})
            continue
        _preserve_missing_object_keys(
            current[key],
            previous_value,
            path=child_path,
            changes=changes,
        )


def merge_sections(previous: dict[str, Any], sections: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(previous)
    candidate.update(sections)
    return candidate
