"""Split a production prompt into a static template and dynamic context sections."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def assemble_prompt(static_template: str, context_sections: list[str]) -> str:
    """Join a static instruction block with already formatted CONTEXT sections."""
    parts = [str(static_template or "").strip()]
    parts.extend(str(section).strip() for section in context_sections if str(section or "").strip())
    return "\n\n".join(part for part in parts if part)


def static_prompt_from_full(full_prompt: str, first_context_marker: str) -> str:
    """Return the instruction prefix before the first dynamic CONTEXT marker."""
    marker = str(first_context_marker or "").strip()
    if marker and not marker.endswith(":"):
        marker = f"{marker}:"
    text = str(full_prompt or "")
    index = text.find(marker) if marker else -1
    if index < 0:
        return text.strip()
    return text[:index].rstrip()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))
