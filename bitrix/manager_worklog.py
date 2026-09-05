"""Deterministic parsing of dated manager worklog comments.

This module only classifies and parses already fetched Bitrix rows.  It does
not call Bitrix, storage, or an LLM.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any


MIN_WORKLOG_LENGTH = 60
MIN_DATED_BLOCKS = 3
MIN_ENTRY_TEXT_LENGTH = 8

# Public aliases keep the threshold names readable at call sites.
MIN_WORKLOG_CHARS = MIN_WORKLOG_LENGTH
MIN_WORKLOG_DATED_BLOCKS = MIN_DATED_BLOCKS

_DATE_RE = re.compile(
    r"(?m)^[ \t]*(?:[-*•][ \t]+)?"
    r"(?P<day>3[01]|[12]\d|0?[1-9])(?!\d)"
    r"(?P<separator>[./])"
    r"(?P<month>1[0-2]|0?[1-9])(?!\d)"
    r"(?:(?P=separator)(?P<year>\d{4}|\d{2}))?"
    r"[ \t]*(?:[-–—:][ \t]*)?(?P<inline>[^\r\n]*)$"
)
_TEXT_KEYS = ("COMMENT", "TEXT", "DESCRIPTION", "comment", "text", "description")
_ID_KEYS = ("ID", "id", "comment_id")
_CREATED_KEYS = ("CREATED", "created", "created_at")
_AUTHOR_KEYS = ("AUTHOR_ID", "author_id", "CREATED_BY", "created_by")


def normalize_worklog_text(value: Any) -> str:
    """Normalize Unicode and whitespace without losing Cyrillic text."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(" ".join(line.split()) for line in text.split("\n") if line.strip()).strip()


def normalized_text_sha256(value: Any) -> str:
    """Return a stable SHA-256 digest of :func:`normalize_worklog_text`."""
    return hashlib.sha256(normalize_worklog_text(value).encode("utf-8")).hexdigest()


def _first_value(row: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
    return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _comment_text(comment: Mapping[str, Any] | str) -> str:
    if isinstance(comment, str):
        return comment
    value = _first_value(comment, _TEXT_KEYS)
    return "" if value is None else str(value)


def _created_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _string(value)
    if not text:
        return None
    iso_text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _year_value(value: str | None) -> int | None:
    if not value:
        return None
    year = int(value)
    return 2000 + year if len(value) == 2 else year


def _entry_text(match: re.Match[str], end: int, source: str) -> str:
    lines = [match.group("inline")]
    lines.extend(source[match.end():end].splitlines())
    kept: list[str] = []
    for line in lines:
        cleaned = " ".join(line.split()).strip(" \t-–—:;")
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def _is_substantive(text: str) -> bool:
    normalized = normalize_worklog_text(text)
    if len(normalized) < MIN_ENTRY_TEXT_LENGTH:
        return False
    letters = sum(character.isalpha() for character in normalized)
    return letters >= 4 and any(character.isalnum() for character in normalized)


def _month_day_direction(parts: list[tuple[int, int]]) -> str | None:
    ascending = descending = 0
    for (previous_month, previous_day), (month, day) in zip(parts, parts[1:]):
        previous = previous_month * 100 + previous_day
        current = month * 100 + day
        if previous_month >= 10 and month <= 3 and current < previous:
            ascending += 1  # December -> January in chronological order.
        elif previous_month <= 3 and month >= 10 and current > previous:
            descending += 1  # January -> December in reverse chronological order.
        elif current > previous:
            ascending += 1
        elif current < previous:
            descending += 1
    if ascending > descending:
        return "ascending"
    if descending > ascending:
        return "descending"
    return None


def _next_year(
    previous: tuple[int, int, int],
    current: tuple[int, int],
    direction: str | None,
) -> int:
    previous_year, previous_month, previous_day = previous
    month, day = current
    year = previous_year
    if direction == "ascending" and month * 100 + day < previous_month * 100 + previous_day:
        if previous_month >= 10 and month <= 3:
            year += 1
    elif direction == "descending" and month * 100 + day > previous_month * 100 + previous_day:
        if previous_month <= 3 and month >= 10:
            year -= 1
    return year


def _previous_year(
    following: tuple[int, int, int],
    current: tuple[int, int],
    direction: str | None,
) -> int:
    following_year, following_month, following_day = following
    month, day = current
    year = following_year
    if direction == "ascending" and month * 100 + day > following_month * 100 + following_day:
        if month >= 10 and following_month <= 3:
            year -= 1
    elif direction == "descending" and month * 100 + day < following_month * 100 + following_day:
        if month <= 3 and following_month >= 10:
            year += 1
    return year


def _infer_years(
    parts: list[tuple[int, int, int | None]],
    created: date | None,
) -> list[int] | None:
    direction = _month_day_direction([(month, day) for day, month, _year in parts])
    years = [year for _day, _month, year in parts]
    known = [index for index, year in enumerate(years) if year is not None]

    if not known:
        if created is None:
            return None
        anchor = len(parts) - 1 if direction == "ascending" else 0
        years[anchor] = created.year
        for index in range(anchor + 1, len(parts)):
            previous = (years[index - 1], parts[index - 1][1], parts[index - 1][0])
            years[index] = _next_year(previous, (parts[index][1], parts[index][0]), direction)
        for index in range(anchor - 1, -1, -1):
            following = (years[index + 1], parts[index + 1][1], parts[index + 1][0])
            years[index] = _previous_year(following, (parts[index][1], parts[index][0]), direction)
        return [int(year) for year in years if year is not None]

    # Explicit years are safer anchors than CREATED.  Fill each missing run
    # from its nearest available neighbor and only cross a year at a clear
    # chronological December/January boundary.
    for index in range(1, len(parts)):
        if years[index] is None and years[index - 1] is not None:
            previous = (years[index - 1], parts[index - 1][1], parts[index - 1][0])
            years[index] = _next_year(previous, (parts[index][1], parts[index][0]), direction)
    for index in range(len(parts) - 2, -1, -1):
        if years[index] is None and years[index + 1] is not None:
            following = (years[index + 1], parts[index + 1][1], parts[index + 1][0])
            years[index] = _previous_year(following, (parts[index][1], parts[index][0]), direction)
    if any(year is None for year in years):
        fallback = created.year if created is not None else None
        if fallback is None:
            return None
        years = [fallback if year is None else year for year in years]
    return [int(year) for year in years]


def _parse_entries(text: str, created: date | None) -> list[dict[str, Any]] | None:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_DATE_RE.finditer(text))
    if len(matches) < MIN_DATED_BLOCKS:
        return None

    raw_entries: list[tuple[int, int, int | None, str, str]] = []
    for index, match in enumerate(matches):
        try:
            day = int(match.group("day"))
            month = int(match.group("month"))
            explicit_year = _year_value(match.group("year"))
            if explicit_year is not None:
                date(explicit_year, month, day)
        except ValueError:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = _entry_text(match, end, text)
        if _is_substantive(body):
            raw_date = f"{match.group('day')}{match.group('separator')}{match.group('month')}"
            if match.group("year"):
                raw_date += f"{match.group('separator')}{match.group('year')}"
            raw_entries.append((day, month, explicit_year, raw_date, body))

    if len(raw_entries) < MIN_DATED_BLOCKS:
        return None
    years = _infer_years([(day, month, year) for day, month, year, _raw, _body in raw_entries], created)
    if years is None:
        return None

    entries: list[dict[str, Any]] = []
    for (day, month, explicit_year, raw_date, body), year in zip(raw_entries, years):
        entry_date = date(year, month, day).isoformat()
        entries.append({
            "entry_date": entry_date,
            "date_raw": raw_date,
            "text": body,
            "year_inferred": explicit_year is None,
        })
    return entries


def parse_manager_worklog(
    comment: Mapping[str, Any] | str,
    *,
    created: Any = None,
    comment_id: Any = None,
    author_id: Any = None,
) -> dict[str, Any] | None:
    """Parse one Bitrix comment if it meets the deterministic worklog rules.

    Mapping input accepts normal Bitrix keys (``ID``, ``CREATED``,
    ``AUTHOR_ID``, and ``COMMENT``).  Explicit keyword metadata overrides a
    missing mapping value and is useful when the text is stored separately.
    """
    text = _comment_text(comment)
    normalized = normalize_worklog_text(text)
    if len(normalized) < MIN_WORKLOG_LENGTH:
        return None

    if isinstance(comment, Mapping):
        raw_created = _first_value(comment, _CREATED_KEYS)
        raw_id = _first_value(comment, _ID_KEYS)
        raw_author = _first_value(comment, _AUTHOR_KEYS)
    else:
        raw_created = raw_id = raw_author = None
    if created is not None:
        raw_created = created
    if comment_id is not None:
        raw_id = comment_id
    if author_id is not None:
        raw_author = author_id

    entries = _parse_entries(text, _created_date(raw_created))
    if entries is None:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "is_worklog": True,
        "comment_id": _string(raw_id),
        "bitrix_created_at": raw_created.isoformat() if isinstance(raw_created, (date, datetime)) else _string(raw_created),
        "author_id": _string(raw_author),
        "text": text,
        "content_hash": digest,
        "entries": entries,
        "entry_count": len(entries),
        "latest_entry_date": max(item["entry_date"] for item in entries),
    }


def classify_manager_worklog(comment: Mapping[str, Any] | str, **metadata: Any) -> bool:
    """Return whether a comment is a manager worklog candidate."""
    return parse_manager_worklog(comment, **metadata) is not None


def parse_manager_worklogs(comments: Iterable[Mapping[str, Any] | str]) -> list[dict[str, Any]]:
    """Return valid worklogs in input order; never picks one by length alone."""
    return [parsed for comment in comments if (parsed := parse_manager_worklog(comment)) is not None]


# Short aliases for callers that already use the generic ``worklog`` name.
parse_worklog = parse_manager_worklog
is_manager_worklog = classify_manager_worklog


__all__ = [
    "MIN_ENTRY_TEXT_LENGTH",
    "MIN_DATED_BLOCKS",
    "MIN_WORKLOG_CHARS",
    "MIN_WORKLOG_DATED_BLOCKS",
    "MIN_WORKLOG_LENGTH",
    "classify_manager_worklog",
    "is_manager_worklog",
    "normalize_worklog_text",
    "normalized_text_sha256",
    "parse_manager_worklog",
    "parse_manager_worklogs",
    "parse_worklog",
]
