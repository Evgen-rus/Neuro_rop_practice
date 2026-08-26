"""Shared helpers for separating the current email from quoted history."""

from __future__ import annotations

import re


QUOTE_MARKERS = (
    "От кого:",
    "Кому:",
    "Дата:",
    "Тема:",
    "-----Original Message-----",
    "---------- Forwarded message",
    "From:",
    "Sent:",
    "To:",
    "Subject:",
)


def strip_quoted_history(text: str) -> str:
    """Keep the current message and remove a following quoted email chain."""
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    positions = [text.find(marker) for marker in QUOTE_MARKERS if text.find(marker) > 0]
    lines = text.splitlines()
    offset = 0
    for line in lines:
        cleaned = line.strip()
        if re.match(
            r"^(пн|вт|ср|чт|пт|сб|вс),?\s+\d{1,2}\s+.+?\s+\d{4}.*(<[^>]+>|@).*>?:?\s*$",
            cleaned,
            flags=re.I,
        ):
            positions.append(offset)
        if re.match(r"^.+?\d{4}.*(<[^>]+>|@).*>?:?\s*$", cleaned):
            positions.append(offset)
        offset += len(line) + 1
    if positions:
        return text[: min(positions)].rstrip()
    return text
