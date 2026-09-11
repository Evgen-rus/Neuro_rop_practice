"""Admin-only projections of the local OpenAI spend diary. Estimate, not an invoice."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from statistics import median

from openai_api.bitrix_links import bitrix_entity_url
from openai_api.spend_diary import (
    _as_float,
    _as_int,
    _now_msk,
    calls_count_label,
    day_label,
    display_kind_label,
    format_rub_ui,
    format_tokens_ui,
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
MAX_RANGE_DAYS = 90
REPEAT_WINDOW_MINUTES = 15
CACHE_HIT_LOW_PERCENT = 10.0
EXPENSIVE_MULTIPLIER = 3
EXPENSIVE_MIN_SAMPLES = 3
TOP_ENTITIES_LIMIT = 8
DEFAULT_EVENTS_PAGE_SIZE = 20
KIND_GROUPS = (
    ("full_analysis", "Полный анализ"),
    ("quick_help", "Quick Help"),
    ("scripts", "Скрипты"),
    ("transcription", "Транскрибация"),
    ("other", "Другое"),
)
KIND_GROUP_LABELS = dict(KIND_GROUPS)
ATTENTION_META = {
    "repeated_attempts": {
        "title": "Повторные платные попытки",
        "severity": "warning",
        "explanation": "Одна и та же операция по сущности оплачивалась повторно за короткое время.",
    },
    "errors": {
        "title": "Ошибки платных вызовов",
        "severity": "error",
        "explanation": "Вызов завершился ошибкой, но мог создать стоимость.",
    },
    "low_cache_hit": {
        "title": "Низкий cache hit",
        "severity": "warning",
        "explanation": "Кэш промпта почти не сработал на LLM-вызове, где он применим.",
    },
    "expensive_calls": {
        "title": "Дорогие вызовы",
        "severity": "info",
        "explanation": "Стоимость заметно выше медианы той же операции за выбранный период.",
    },
}


def kind_group_id(kind: str | None) -> str:
    raw = str(kind or "").strip()
    if raw in {
        "full_deal_analysis",
        "full_lead_analysis",
        "full_analysis",
        "incremental_deal_analysis",
    }:
        return "full_analysis"
    if raw.startswith("deal_manager_quick_help_"):
        return "quick_help"
    if raw.startswith("deal_manager_full_script_"):
        return "scripts"
    if raw.startswith("transcription"):
        return "transcription"
    return "other"


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
    group_id = kind_group_id(kind)
    token_total = (input_tokens or 0) + (output_tokens or 0)
    return {
        "at": moment.isoformat(timespec="seconds") if moment else None,
        "time": moment.strftime("%H:%M") if moment else "—",
        "datetime_label": moment.strftime("%d.%m %H:%M") if moment else "—",
        "kind": kind,
        "kind_label": display_kind_label(kind),
        "kind_group": group_id,
        "kind_group_label": KIND_GROUP_LABELS[group_id],
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_label": _entity_label(entity_type, entity_id),
        "bitrix_url": bitrix_entity_url(entity_type or "", entity_id) or None,
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
        "token_total": token_total or None,
        "cache_hit_percent": _cache_hit_percent(input_tokens, cached_input_tokens),
        "duration_seconds": duration_seconds,
        "day": day.isoformat(),
        "attention": [],
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


def resolve_ai_spend_period(
    *,
    preset: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    now: datetime | None = None,
) -> tuple[date, date, str, date]:
    today = _now_msk(now).date()
    key = str(preset or "30").strip() or "30"
    if key in {"7", "30"}:
        days = int(key)
        end = today
        start = today - timedelta(days=days - 1)
        return start, end, key, today
    if key != "custom":
        raise ValueError("Период может быть 7, 30 или custom.")
    if from_date is None or to_date is None:
        raise ValueError("Для своего периода укажите даты from и to.")
    start = from_date
    end = to_date
    if end < start:
        raise ValueError("Дата окончания не может быть раньше даты начала.")
    if end > today:
        end = today
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValueError(f"Период не может быть длиннее {MAX_RANGE_DAYS} дней.")
    if span < 1:
        raise ValueError("Период должен содержать хотя бы один день.")
    return start, end, "custom", today


def _previous_period(start: date, end: date) -> tuple[date, date]:
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return prev_start, prev_end


def _period_label(start: date, end: date) -> str:
    return f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"


def _delta_percent(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _is_paid(event: dict[str, Any]) -> bool:
    return event.get("estimated_cost_rub") is not None or event.get("estimated_cost_usd") is not None


def _is_transcription_kind(kind: str | None) -> bool:
    return str(kind or "").startswith("transcription")


def _cache_applicable(event: dict[str, Any]) -> bool:
    if _is_transcription_kind(event.get("kind")):
        return False
    return event.get("input_tokens") and event.get("cached_input_tokens") is not None


def _project_range(from_date: date, to_date: date) -> tuple[list[dict[str, Any]], int]:
    by_day, skipped = _load_range(from_date, to_date)
    projected: list[dict[str, Any]] = []
    current = from_date
    while current <= to_date:
        for index, event in enumerate(by_day.get(current.isoformat()) or []):
            item = _project_event(event, current)
            item["event_key"] = "|".join(
                [
                    item.get("at") or "",
                    item.get("kind") or "",
                    item.get("entity_id") or "",
                    str(item.get("attempt") or ""),
                    item.get("run_id") or "",
                    current.isoformat(),
                    str(index),
                ]
            )
            projected.append(item)
        current += timedelta(days=1)
    return projected, skipped


def _aggregate_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    rub_total = 0.0
    usd_total = 0.0
    has_rub = False
    has_usd = False
    paid = 0
    unknown = 0
    tokens = 0
    for item in events:
        tokens += int(item.get("token_total") or 0)
        rub = _as_float(item.get("estimated_cost_rub"))
        usd = _as_float(item.get("estimated_cost_usd"))
        if rub is None and usd is None:
            unknown += 1
            continue
        paid += 1
        if rub is not None:
            rub_total += rub
            has_rub = True
        if usd is not None:
            usd_total += usd
            has_usd = True
    cost_rub = round(rub_total, 2) if has_rub else None
    if paid == 0 and unknown > 0:
        cost_label = format_rub_ui(None)
    else:
        cost_label = format_rub_ui(cost_rub if paid else 0.0)
    average = round(rub_total / paid, 2) if has_rub and paid else None
    return {
        "estimated_cost_rub": cost_rub if paid else (None if unknown else 0.0),
        "estimated_cost_usd": round(usd_total, 6) if has_usd else None,
        "estimated_cost_rub_label": cost_label,
        "paid_calls": paid,
        "paid_calls_label": paid_calls_label(paid),
        "unknown_cost_calls": unknown,
        "total_tokens": tokens,
        "total_tokens_label": format_tokens_ui(tokens),
        "average_cost_rub": average,
        "average_cost_rub_label": format_rub_ui(average) if average is not None else "—",
        "event_count": len(events),
    }


def _daily_series(
    events: list[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for item in events:
        by_day.setdefault(str(item.get("day") or ""), []).append(item)
    series = []
    current = start
    while current <= end:
        day_events = by_day.get(current.isoformat()) or []
        totals = _aggregate_events(day_events)
        series.append({
            "date": current.isoformat(),
            "label": day_label(current),
            "short_label": current.strftime("%d.%m"),
            "estimated_cost_rub": totals["estimated_cost_rub"],
            "estimated_cost_rub_label": totals["estimated_cost_rub_label"],
            "paid_calls": totals["paid_calls"],
            "paid_calls_label": totals["paid_calls_label"],
            "total_tokens": totals["total_tokens"],
            "total_tokens_label": totals["total_tokens_label"],
            "unknown_cost_calls": totals["unknown_cost_calls"],
        })
        current += timedelta(days=1)
    return series


def _share(part: float | None, whole: float | None) -> float | None:
    if part is None or whole is None or whole <= 0:
        return None
    return round(part / whole * 100, 1)


def _breakdown_rows(
    events: list[dict[str, Any]],
    *,
    key_fn,
    label_fn,
    total_cost: float | None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in events:
        grouped.setdefault(key_fn(item), []).append(item)
    rows = []
    for key, group in grouped.items():
        totals = _aggregate_events(group)
        rows.append({
            "id": key or "unknown",
            "label": label_fn(key, group),
            "estimated_cost_rub": totals["estimated_cost_rub"],
            "estimated_cost_rub_label": totals["estimated_cost_rub_label"],
            "paid_calls": totals["paid_calls"],
            "paid_calls_label": totals["paid_calls_label"],
            "calls_label": calls_count_label(totals["paid_calls"]),
            "total_tokens": totals["total_tokens"],
            "share": _share(totals["estimated_cost_rub"] or 0.0 if totals["paid_calls"] else 0.0, total_cost),
        })
    rows.sort(key=lambda item: (item["estimated_cost_rub"] is None, -(item["estimated_cost_rub"] or 0), item["label"]))
    return rows


def _assign_attention(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    flags: dict[str, set[str]] = {str(item.get("event_key") or ""): set() for item in events}
    by_entity_kind: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    costs_by_kind: dict[str, list[float]] = {}

    for item in events:
        key = str(item.get("event_key") or "")
        if item.get("status") == "error":
            flags[key].add("errors")
        attempt = item.get("attempt")
        if isinstance(attempt, int) and attempt >= 2 and _is_paid(item):
            flags[key].add("repeated_attempts")
        if _cache_applicable(item):
            hit = item.get("cache_hit_percent")
            if hit is not None and hit < CACHE_HIT_LOW_PERCENT:
                flags[key].add("low_cache_hit")
        kind = str(item.get("kind") or "")
        cost = _as_float(item.get("estimated_cost_rub"))
        if kind and cost is not None:
            costs_by_kind.setdefault(kind, []).append(cost)
        entity_id = str(item.get("entity_id") or "").strip()
        if entity_id and kind:
            group_key = (str(item.get("entity_type") or ""), entity_id, kind)
            by_entity_kind.setdefault(group_key, []).append(item)

    for group in by_entity_kind.values():
        ordered = sorted(group, key=lambda item: item.get("at") or "")
        cluster: list[dict[str, Any]] = []
        for item in ordered:
            moment = _parse_at(item.get("at"))
            if not cluster:
                cluster = [item]
                continue
            previous = _parse_at(cluster[-1].get("at"))
            if moment and previous and (moment - previous).total_seconds() <= REPEAT_WINDOW_MINUTES * 60:
                cluster.append(item)
                continue
            if len(cluster) >= 2:
                for member in cluster:
                    if _is_paid(member):
                        flags[str(member.get("event_key") or "")].add("repeated_attempts")
            cluster = [item]
        if len(cluster) >= 2:
            for member in cluster:
                if _is_paid(member):
                    flags[str(member.get("event_key") or "")].add("repeated_attempts")

    medians = {
        kind: median(values)
        for kind, values in costs_by_kind.items()
        if len(values) >= EXPENSIVE_MIN_SAMPLES
    }
    for item in events:
        kind = str(item.get("kind") or "")
        cost = _as_float(item.get("estimated_cost_rub"))
        kind_median = medians.get(kind)
        if cost is None or kind_median is None or kind_median <= 0:
            continue
        if cost >= kind_median * EXPENSIVE_MULTIPLIER:
            flags[str(item.get("event_key") or "")].add("expensive_calls")

    attached: dict[str, list[str]] = {}
    for key, values in flags.items():
        ordered = [name for name in ATTENTION_META if name in values]
        attached[key] = ordered
    return attached


def _attention_items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {name: 0 for name in ATTENTION_META}
    for item in events:
        for name in item.get("attention") or []:
            counts[str(name)] = counts.get(str(name), 0) + 1
    items = []
    for name, meta in ATTENTION_META.items():
        count = counts.get(name) or 0
        if count <= 0:
            continue
        items.append({
            "id": name,
            "title": meta["title"],
            "severity": meta["severity"],
            "count": count,
            "count_label": f"{count} {_cases_word(count)}",
            "explanation": meta["explanation"],
        })
    return items


def _cases_word(count: int) -> str:
    n = abs(count) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        return "случаев"
    if n1 == 1:
        return "случай"
    if 2 <= n1 <= 4:
        return "случая"
    return "случаев"


def _top_entities(events: list[dict[str, Any]], total_cost: float | None) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in events:
        entity_id = str(item.get("entity_id") or "").strip()
        entity_type = str(item.get("entity_type") or "").strip()
        grouped.setdefault((entity_type, entity_id), []).append(item)
    rows = []
    for (entity_type, entity_id), group in grouped.items():
        totals = _aggregate_events(group)
        kind_costs: dict[str, float] = {}
        for item in group:
            kind = str(item.get("kind") or "")
            if not kind:
                continue
            cost = _as_float(item.get("estimated_cost_rub")) or 0.0
            kind_costs[kind] = kind_costs.get(kind, 0.0) + cost
        primary_kind = ""
        if kind_costs:
            primary_kind = max(kind_costs.items(), key=lambda pair: pair[1])[0]
        label = _entity_label(entity_type or None, entity_id or None) or "Без сущности"
        rows.append({
            "entity_type": entity_type or None,
            "entity_id": entity_id or None,
            "label": label,
            "bitrix_url": bitrix_entity_url(entity_type, entity_id) or None,
            "estimated_cost_rub": totals["estimated_cost_rub"],
            "estimated_cost_rub_label": totals["estimated_cost_rub_label"],
            "paid_calls": totals["paid_calls"],
            "calls_label": calls_count_label(totals["paid_calls"]),
            "primary_kind": primary_kind or None,
            "primary_kind_label": display_kind_label(primary_kind) if primary_kind else "—",
            "share": _share(totals["estimated_cost_rub"] or 0.0 if totals["paid_calls"] else 0.0, total_cost),
        })
    rows.sort(key=lambda item: (item["estimated_cost_rub"] is None, -(item["estimated_cost_rub"] or 0), item["label"]))
    return rows[:TOP_ENTITIES_LIMIT]


def _matches_event_query(
    event: dict[str, Any],
    *,
    q: str,
    kind_group: str | None,
    status: str | None,
    attention: str | None,
) -> bool:
    if kind_group and kind_group != "all" and event.get("kind_group") != kind_group:
        return False
    if status and event.get("status") != status:
        return False
    if attention and attention not in (event.get("attention") or []):
        return False
    needle = q.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(event.get(name) or "")
        for name in (
            "entity_id",
            "entity_label",
            "entity_type",
            "model",
            "model_label",
            "kind",
            "kind_label",
            "run_id",
            "job_id",
            "status",
            "status_label",
        )
    ).lower()
    return needle in haystack


def _decorate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags = _assign_attention(events)
    decorated = []
    for item in events:
        copy = dict(item)
        copy["attention"] = flags.get(str(item.get("event_key") or ""), [])
        decorated.append(copy)
    return decorated


def _day_snapshot(value: date) -> dict[str, Any]:
    events, _skipped = _project_range(value, value)
    totals = _aggregate_events(events)
    totals["date"] = value.isoformat()
    totals["label"] = day_label(value)
    return totals


def build_ai_spend_analytics(
    *,
    preset: str | None = "30",
    from_date: date | None = None,
    to_date: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    start, end, resolved_preset, today = resolve_ai_spend_period(
        preset=preset, from_date=from_date, to_date=to_date, now=now,
    )
    prev_start, prev_end = _previous_period(start, end)
    events, skipped = _project_range(start, end)
    previous_events, _prev_skipped = _project_range(prev_start, prev_end)
    events = _decorate_events(events)
    totals = _aggregate_events(events)
    previous_totals = _aggregate_events(previous_events)
    total_cost = totals["estimated_cost_rub"]
    daily = _daily_series(events, start, end)
    kind_group_rows = []
    for group_id, label in KIND_GROUPS:
        group_events = [item for item in events if item.get("kind_group") == group_id]
        group_totals = _aggregate_events(group_events)
        kind_group_rows.append({
            "id": group_id,
            "label": label,
            "estimated_cost_rub": group_totals["estimated_cost_rub"],
            "estimated_cost_rub_label": group_totals["estimated_cost_rub_label"],
            "paid_calls": group_totals["paid_calls"],
            "calls_label": calls_count_label(group_totals["paid_calls"]),
            "total_tokens": group_totals["total_tokens"],
            "share": _share(group_totals["estimated_cost_rub"] or 0.0 if group_totals["paid_calls"] else 0.0, total_cost),
            "daily": _daily_series(group_events, start, end),
        })
    by_kind = _breakdown_rows(
        events,
        key_fn=lambda item: str(item.get("kind") or "unknown"),
        label_fn=lambda key, group: group[0].get("kind_label") or display_kind_label(key),
        total_cost=total_cost,
    )
    by_model = _breakdown_rows(
        events,
        key_fn=lambda item: str(item.get("model") or "unknown"),
        label_fn=lambda key, group: group[0].get("model_label") or (key if key != "unknown" else "Без модели"),
        total_cost=total_cost,
    )
    comparison = {
        "cost_percent": _delta_percent(totals["estimated_cost_rub"], previous_totals["estimated_cost_rub"]),
        "calls_percent": _delta_percent(float(totals["paid_calls"]), float(previous_totals["paid_calls"])),
        "tokens_percent": _delta_percent(float(totals["total_tokens"]), float(previous_totals["total_tokens"])),
        "average_cost_percent": _delta_percent(totals["average_cost_rub"], previous_totals["average_cost_rub"]),
    }
    yesterday = today - timedelta(days=1)
    return {
        "disclaimer": DISCLAIMER,
        "title": "Расходы AI — оценка",
        "period": {
            "preset": resolved_preset,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "label": _period_label(start, end),
            "days": (end - start).days + 1,
            "today": today.isoformat(),
        },
        "today": _day_snapshot(today),
        "yesterday": _day_snapshot(yesterday),
        "previous_period": {
            "from": prev_start.isoformat(),
            "to": prev_end.isoformat(),
            "label": _period_label(prev_start, prev_end),
        },
        "totals": totals,
        "comparison": comparison,
        "daily_series": daily,
        "kind_groups": kind_group_rows,
        "by_kind": by_kind,
        "by_model": by_model,
        "top_entities": _top_entities(events, total_cost),
        "attention": _attention_items(events),
        "skipped_lines": skipped,
        "has_events": bool(events),
        "has_unknown_cost": totals["unknown_cost_calls"] > 0,
    }


def build_ai_spend_events(
    *,
    preset: str | None = "30",
    from_date: date | None = None,
    to_date: date | None = None,
    q: str = "",
    kind_group: str | None = None,
    status: str | None = None,
    attention: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_EVENTS_PAGE_SIZE,
    now: datetime | None = None,
) -> dict[str, Any]:
    start, end, resolved_preset, _today = resolve_ai_spend_period(
        preset=preset, from_date=from_date, to_date=to_date, now=now,
    )
    if attention and attention not in ATTENTION_META:
        raise ValueError("Неизвестный тип внимания.")
    if kind_group and kind_group not in {"all", *KIND_GROUP_LABELS}:
        raise ValueError("Неизвестная группа операций.")
    if status and status not in STATUS_LABELS:
        raise ValueError("Неизвестный статус.")
    safe_page = max(int(page or 1), 1)
    safe_size = min(max(int(page_size or DEFAULT_EVENTS_PAGE_SIZE), 1), 100)
    events, skipped = _project_range(start, end)
    events = _decorate_events(events)
    events.sort(key=lambda item: (item.get("at") or "", item.get("event_key") or ""), reverse=True)
    filtered = [
        item for item in events
        if _matches_event_query(
            item,
            q=q or "",
            kind_group=kind_group,
            status=status,
            attention=attention,
        )
    ]
    total = len(filtered)
    pages = max((total + safe_size - 1) // safe_size, 1)
    if safe_page > pages:
        safe_page = pages
    offset = (safe_page - 1) * safe_size
    page_events = filtered[offset:offset + safe_size]
    return {
        "disclaimer": DISCLAIMER,
        "period": {
            "preset": resolved_preset,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "label": _period_label(start, end),
        },
        "q": (q or "").strip(),
        "kind_group": kind_group or None,
        "status": status or None,
        "attention": attention or None,
        "page": safe_page,
        "page_size": safe_size,
        "total": total,
        "pages": pages,
        "skipped_lines": skipped,
        "events": page_events,
        "empty_reason": None if total else ("search" if (q or "").strip() or kind_group or status or attention else "period"),
    }
