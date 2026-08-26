"""Shared selection rules for compact customer-history sections."""

from __future__ import annotations

from typing import Any, Callable

from bitrix.customer_history import clean_text


TextCleaner = Callable[[Any, int | None], str]

COMPACT_HISTORY_POLICIES: dict[str, dict[str, int]] = {
    "client_touchpoints": {"limit": 30, "text_limit": 500},
    "tasks_and_control": {"limit": 20, "text_limit": 500},
    "internal_context": {"limit": 20, "text_limit": 1200},
    "system_events": {"limit": 20, "text_limit": 500},
}


def compact_history_value(item: dict[str, Any]) -> tuple[str, Any]:
    """Return the same field that the compact Markdown sends to the model."""
    if item.get("category") == "internal_im_chat":
        return "text", item.get("text")
    if item.get("subject"):
        return "subject", item.get("subject")
    return "text", item.get("text")


def compact_history_coverage(
    items: list[dict[str, Any]],
    *,
    limit: int,
    text_limit: int,
    cleaner: TextCleaner = clean_text,
) -> list[dict[str, Any]]:
    """Describe inclusion and truncation without parsing rendered Markdown."""
    first_included = max(0, len(items) - limit)
    coverage: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        field, value = compact_history_value(item)
        full_text = cleaner(value, None)
        prompt_text = cleaner(value, text_limit)
        coverage.append(
            {
                "item": item,
                "included": index >= first_included,
                "selected_field": field,
                "full_text": full_text,
                "prompt_text": prompt_text,
                "full_chars": len(full_text),
                "prompt_chars": len(prompt_text),
                "omitted_chars": max(0, len(full_text) - len(prompt_text)),
                "truncated": len(full_text) > len(prompt_text),
            }
        )
    return coverage


def compact_history_section_coverage(
    section: str,
    items: list[dict[str, Any]],
    *,
    cleaner: TextCleaner = clean_text,
) -> list[dict[str, Any]]:
    policy = COMPACT_HISTORY_POLICIES[section]
    return compact_history_coverage(items, cleaner=cleaner, **policy)
