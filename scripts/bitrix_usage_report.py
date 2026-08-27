"""Summarize privacy-safe Bitrix REST traces without reading CRM payload values."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.usage_trace import DEFAULT_DAILY_USAGE_DIR, normalize_component


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Privacy-safe Bitrix REST usage summary")
    parser.add_argument("--usage-dir", default=str(DEFAULT_DAILY_USAGE_DIR))
    parser.add_argument("--from-date", help="Первая дата YYYY-MM-DD, включительно")
    parser.add_argument("--to-date", help="Последняя дата YYYY-MM-DD, включительно")
    parser.add_argument("--run-id", help="Только события с этим run_id")
    parser.add_argument("--json", action="store_true", help="Вывести JSON вместо таблицы")
    return parser.parse_args()


def _parse_date(value: str | None, *, fallback: date) -> date:
    if not value:
        return fallback
    return date.fromisoformat(value)


def iter_events(directory: Path, start: date, end: date) -> Iterable[dict[str, Any]]:
    if start > end:
        raise ValueError("from-date должна быть не позже to-date")
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < start or file_date > end:
            continue
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _empty_bucket() -> dict[str, Any]:
    return {
        "physical_http": 0,
        "logical_commands": 0,
        "batch_http": 0,
        "batch_cmds": 0,
        "follow_pages": 0,
        "retries": 0,
        "errors": 0,
        "empty": 0,
        "item_count": 0,
        "duration_ms": 0.0,
    }


def _add_event(bucket: dict[str, Any], event: dict[str, Any]) -> None:
    method = str(event.get("method") or "unknown")
    is_batch = method == "batch"
    batch_cmds = _int(event.get("batch_cmd_count")) if is_batch else 0
    page_start = None
    shape = event.get("request_shape")
    if isinstance(shape, dict) and isinstance(shape.get("page_start"), int):
        page_start = shape["page_start"]
    bucket["physical_http"] += 1
    bucket["logical_commands"] += batch_cmds if is_batch else 1
    bucket["batch_http"] += int(is_batch)
    bucket["batch_cmds"] += batch_cmds
    bucket["follow_pages"] += int(bool(event.get("has_next_page")) or (page_start not in (None, 0)))
    bucket["retries"] += int(bool(event.get("is_retry")) or _int(event.get("attempt"), 1) > 1)
    bucket["errors"] += int(not bool(event.get("ok")))
    bucket["empty"] += int(bool(event.get("empty")))
    bucket["item_count"] += 0 if is_batch else max(0, _int(event.get("item_count")))
    bucket["duration_ms"] += max(0.0, _float(event.get("duration_ms")))


def _round_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    return {**bucket, "duration_ms": round(float(bucket["duration_ms"]), 3)}


def build_report(
    events: Iterable[dict[str, Any]],
    *,
    start: date,
    end: date,
    run_id: str | None = None,
) -> dict[str, Any]:
    wanted_run = str(run_id).strip() if run_id else ""
    method_rows: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    component_rows: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
    pair_rows: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty_bucket)
    totals = _empty_bucket()
    run_ids: set[str] = set()
    for event in events:
        event_run = str(event.get("run_id") or "").strip()
        if wanted_run and event_run != wanted_run:
            continue
        method = str(event.get("method") or "unknown")
        component = normalize_component(event.get("component"))
        _add_event(method_rows[method], event)
        _add_event(component_rows[component], event)
        _add_event(pair_rows[(component, method)], event)
        _add_event(totals, event)
        if event_run:
            run_ids.add(event_run)

    methods = [
        {"method": method, **_round_bucket(values)}
        for method, values in sorted(
            method_rows.items(),
            key=lambda item: (-int(item[1]["physical_http"]), item[0]),
        )
    ]
    components = [
        {"component": component, **_round_bucket(values)}
        for component, values in sorted(
            component_rows.items(),
            key=lambda item: (-int(item[1]["physical_http"]), item[0]),
        )
    ]
    by_component_method = [
        {"component": component, "method": method, **_round_bucket(values)}
        for (component, method), values in sorted(
            pair_rows.items(),
            key=lambda item: (-int(item[1]["physical_http"]), item[0][0], item[0][1]),
        )
    ]
    rounded = _round_bucket(totals)
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "run_id": wanted_run or None,
        "physical_attempts": rounded["physical_http"],
        "physical_http": rounded["physical_http"],
        "logical_commands": rounded["logical_commands"],
        "batch_http": rounded["batch_http"],
        "batch_cmds": rounded["batch_cmds"],
        "follow_pages": rounded["follow_pages"],
        "item_count": rounded["item_count"],
        "run_count": len(run_ids),
        "errors": rounded["errors"],
        "retries": rounded["retries"],
        "duration_ms": rounded["duration_ms"],
        "methods": methods,
        "components": components,
        "by_component_method": by_component_method,
    }


def render_table(report: dict[str, Any]) -> str:
    lines = [
        f"Bitrix REST: {report['period']['from']} .. {report['period']['to']}",
        (
            f"Физических попыток: {report['physical_attempts']}; logical: {report['logical_commands']}; "
            f"batch HTTP: {report['batch_http']}; batch cmds: {report['batch_cmds']}; "
            f"запусков: {report['run_count']}; ошибок: {report['errors']}; retries: {report['retries']}; "
            f"суммарно: {report['duration_ms'] / 1000:.1f} с"
        ),
        "",
        f"{'Метод':34} {'HTTP':>8} {'Logical':>8} {'Ошибки':>7} {'Retry':>6} {'Пусто':>6} {'Сек':>9}",
    ]
    for row in report["methods"]:
        lines.append(
            f"{str(row['method'])[:34]:34} {int(row['physical_http']):8d} {int(row['logical_commands']):8d} "
            f"{int(row['errors']):7d} {int(row['retries']):6d} {int(row['empty']):6d} "
            f"{float(row['duration_ms']) / 1000:9.1f}"
        )
    if report.get("components"):
        lines.extend(["", f"{'Компонент':34} {'HTTP':>8} {'Logical':>8} {'Batch':>7} {'Cmds':>6} {'Сек':>9}"])
        for row in report["components"]:
            lines.append(
                f"{str(row['component'])[:34]:34} {int(row['physical_http']):8d} {int(row['logical_commands']):8d} "
                f"{int(row['batch_http']):7d} {int(row['batch_cmds']):6d} {float(row['duration_ms']) / 1000:9.1f}"
            )
    return "\n".join(lines)


def main() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    args = parse_args()
    today = date.today()
    start = _parse_date(args.from_date, fallback=today)
    end = _parse_date(args.to_date, fallback=start)
    report = build_report(
        iter_events(Path(args.usage_dir), start, end),
        start=start,
        end=end,
        run_id=args.run_id,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_table(report))


if __name__ == "__main__":
    main()
