"""Shared Moscow business-time rules for recommendation deadlines."""

from datetime import date, datetime, time
from typing import Any

from setup import MSK_TZ


def recommendation_due_at(deadline: Any) -> str | None:
    """Interpret a date as 18:00 MSK and a naive datetime as Moscow time."""
    value = str(deadline or "").strip()
    if not value:
        return None
    if len(value) == 10:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time(18, 0), tzinfo=MSK_TZ).isoformat(timespec="seconds")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time(18, 0), tzinfo=MSK_TZ).isoformat(timespec="seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed.astimezone(MSK_TZ).isoformat(timespec="seconds")
