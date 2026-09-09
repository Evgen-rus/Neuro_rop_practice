"""Admin-only projections of the local OpenAI spend diary. Estimate, not an invoice."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from openai_api.spend_diary import (
    _as_float,
    _as_int,
    _now_msk,
    day_label,
    display_kind_label,
    format_rub_ui,
    load_day_events,
    model_label,
    paid_calls_label,
)
from setup import MSK_TZ


DISCLAIMER = (
    "Стоимость рассчитана по тарифам и курсу, настроенным в проекте. Это не счёт OpenAI."
)
STATUS_LABELS = {
    "success": "успешно",
    "error": "ошибка",
}
LOOKBACK_DAYS = 30


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed.astimezone(MSK_TZ)


def _entity_label(entity_type: str | None, entity_id: str | None) -> str | None:
    token = str(entity_id or "").strip()
    if not token:
        return None
    if entity_type == "lead":
        return f"Лид {token}"
    return f"Сделка {token}"


def _cache_hit_percent(input_tokens: int | None, cached_input_tokens: int | None) -> float | None:
    if not input_tokens or cached_input_tokens is None:
        return None
    return round(cached_input_tokens / input_tokens * 100, 1)


def _cost_label(paid_calls: int, rub: float | None) -> str:
    if paid_calls == 0:
        return format_rub_ui(0.0)
    return format_rub_ui(rub)


def _sum_costs(events: list[dict[str, Any]]) -> tuple[float | None, float | None, int]:
    rub_total = 0.0
    usd_total = 0.0
    has_rub = False
    has_usd = False
    paid = 0
    for item in events:
        rub = _as_float(item.get("estimated_cost_rub"))
        usd = _as_float(item.get("estimated_cost_usd"))
        if rub is None and usd is None:
            continue
        paid += 1
        if rub is not None:
            rub_total += rub
            has_rub = True
        if usd is not None:
            usd_total += usd
            has_usd = True
    return (
        round(rub_total, 2) if has_rub else None,
        round(usd_total, 6) if has_usd else None,
        paid,
    )


def _project_event(event: dict[str, Any], day: date) -> dict[str, Any]:
    moment = _parse_at(event.get("at"))
    input_tokens = _as_int(event.get("input_tokens"))
    cached_input_tokens = _as_int(event.get("cached_input_tokens"))
    cache_write_tokens = _as_int(event.get("cache_write_tokens"))
    output_tokens = _as_int(event.get("output_tokens"))
    reasoning_tokens = _as_int(event.get("reasoning_tokens"))
    duration_seconds = _as_float(event.get("duration_seconds"))
    rub = _as_float(event.get("estimated_cost_rub"))
    usd = _as_float(event.get("estimated_cost_usd"))
    status = str(event.get("status") or "").strip() or None
    kind = str(event.get("kind") or "").strip() or None
    entity_type = str(event.get("entity_type") or "").strip() or None
    entity_id = str(event.get("entity_id") or "").strip() or None
    model = str(event.get("model") or "").strip() or None
    return {
        "at": moment.isoformat(timespec="seconds") if moment else None,
        "time": moment.strftime("%H:%M") if moment else "—",
        "kind": kind,
        "kind_label": display_kind_label(kind),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_label": _entity_label(entity_type, entity_id),
        "model": model,
        "model_label": model_label(model),
        "estimated_cost_rub": rub,
        "estimated_cost_usd": usd,
        "estimated_cost_rub_label": format_rub_ui(rub),
        "status": status,
        "status_label": STATUS_LABELS.get(status or "", status),
        "attempt": _as_int(event.get("attempt")),
        "run_id": str(event.get("run_id") or "").strip() or None,
        "job_id": str(event.get("job_id") or "").strip() or None,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_hit_percent": _cache_hit_percent(input_tokens, cached_input_tokens),
        "duration_seconds": duration_seconds,
        "day": day.isoformat(),
    }


def _load_range(from_date: date, to_date: date) -> tuple[dict[str, list[dict[str, Any]]], int]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    skipped = 0
    current = from_date
    while current <= to_date:
        events, day_skipped = load_day_events(current)
        skipped += day_skipped
        if events:
            by_day[current.isoformat()] = events
        current += timedelta(days=1)
    return by_day, skipped


def build_ai_spend_summary(*, now: datetime | None = None) -> dict[str, Any]:
    moment = _now_msk(now)
    today = moment.date()
    from_date = today - timedelta(days=LOOKBACK_DAYS - 1)
    last_7_from = today - timedelta(days=6)
    by_day, skipped = _load_range(from_date, today)

    def _totals(start: date, end: date) -> tuple[float | None, float | None, int]:
        events: list[dict[str, Any]] = []
        current = start
        while current <= end:
            events.extend(by_day.get(current.isoformat()) or [])
            current += timedelta(days=1)
        return _sum_costs(events)

    today_rub, today_usd, today_paid = _totals(today, today)
    week_rub, week_usd, week_paid = _totals(last_7_from, today)
    month_rub, month_usd, month_paid = _totals(from_date, today)

    days = []
    current = today
    while current >= from_date:
        events = by_day.get(current.isoformat()) or []
        if events:
            rub, usd, paid = _sum_costs(events)
            days.append({
                "date": current.isoformat(),
                "label": day_label(current),
                "estimated_cost_rub": rub if paid else 0.0,
                "estimated_cost_usd": usd,
                "estimated_cost_rub_label": _cost_label(paid, rub),
                "paid_calls": paid,
                "paid_calls_label": paid_calls_label(paid),
            })
        current -= timedelta(days=1)

    return {
        "disclaimer": DISCLAIMER,
        "title": "Расходы AI — оценка",
        "today": {
            "date": today.isoformat(),
            "label": day_label(today),
            "estimated_cost_rub": today_rub if today_paid else 0.0,
            "estimated_cost_usd": today_usd,
            "estimated_cost_rub_label": format_rub_ui(today_rub if today_paid else 0.0),
            "paid_calls": today_paid,
            "paid_calls_label": paid_calls_label(today_paid),
        },
        "last_7_days": {
            "from": last_7_from.isoformat(),
            "to": today.isoformat(),
            "estimated_cost_rub": week_rub if week_paid else 0.0,
            "estimated_cost_usd": week_usd,
            "estimated_cost_rub_label": format_rub_ui(week_rub if week_paid else 0.0),
            "paid_calls": week_paid,
            "paid_calls_label": paid_calls_label(week_paid),
        },
        "last_30_days": {
            "from": from_date.isoformat(),
            "to": today.isoformat(),
            "estimated_cost_rub": month_rub if month_paid else 0.0,
            "estimated_cost_usd": month_usd,
            "estimated_cost_rub_label": format_rub_ui(month_rub if month_paid else 0.0),
            "paid_calls": month_paid,
            "paid_calls_label": paid_calls_label(month_paid),
        },
        "days": days,
        "skipped_lines": skipped,
    }


def build_ai_spend_day(value: date) -> dict[str, Any]:
    events, skipped = load_day_events(value)
    projected = [_project_event(event, value) for event in events]
    projected.sort(key=lambda item: (item.get("at") or "", item.get("time") or ""), reverse=True)
    rub, usd, paid = _sum_costs(events)
    return {
        "disclaimer": DISCLAIMER,
        "date": value.isoformat(),
        "label": day_label(value),
        "estimated_cost_rub": rub if paid else 0.0,
        "estimated_cost_usd": usd,
        "estimated_cost_rub_label": format_rub_ui(rub if paid else 0.0),
        "paid_calls": paid,
        "paid_calls_label": paid_calls_label(paid),
        "skipped_lines": skipped,
        "events": projected,
    }
