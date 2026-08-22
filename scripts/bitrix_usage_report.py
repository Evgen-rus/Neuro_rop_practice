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

from bitrix.usage_trace import DEFAULT_DAILY_USAGE_DIR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Privacy-safe Bitrix REST usage summary")
    parser.add_argument("--usage-dir", default=str(DEFAULT_DAILY_USAGE_DIR))
    parser.add_argument("--from-date", help="Первая дата YYYY-MM-DD, включительно")
    parser.add_argument("--to-date", help="Последняя дата YYYY-MM-DD, включительно")
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


def build_report(events: Iterable[dict[str, Any]], *, start: date, end: date) -> dict[str, Any]:
    method_rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"attempts": 0, "errors": 0, "retries": 0, "empty": 0, "duration_ms": 0.0}
    )
    run_ids: set[str] = set()
    total = 0
    for event in events:
        method = str(event.get("method") or "unknown")
        row = method_rows[method]
        row["attempts"] += 1
        row["errors"] += int(not bool(event.get("ok")))
        row["retries"] += int(int(event.get("attempt") or 1) > 1)
        row["empty"] += int(bool(event.get("empty")))
        try:
            row["duration_ms"] += max(0.0, float(event.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            pass
        run_id = str(event.get("run_id") or "").strip()
        if run_id:
            run_ids.add(run_id)
        total += 1

    methods = [
        {
            "method": method,
            **values,
            "duration_ms": round(float(values["duration_ms"]), 3),
        }
        for method, values in sorted(
            method_rows.items(),
            key=lambda item: (-int(item[1]["attempts"]), item[0]),
        )
    ]
    return {
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "physical_attempts": total,
        "run_count": len(run_ids),
        "errors": sum(int(row["errors"]) for row in methods),
        "retries": sum(int(row["retries"]) for row in methods),
        "duration_ms": round(sum(float(row["duration_ms"]) for row in methods), 3),
        "methods": methods,
    }


def render_table(report: dict[str, Any]) -> str:
    lines = [
        f"Bitrix REST: {report['period']['from']} .. {report['period']['to']}",
        (
            f"Физических попыток: {report['physical_attempts']}; запусков: {report['run_count']}; "
            f"ошибок: {report['errors']}; retries: {report['retries']}; "
            f"суммарно: {report['duration_ms'] / 1000:.1f} с"
        ),
        "",
        f"{'Метод':34} {'Запросы':>8} {'Ошибки':>7} {'Retry':>6} {'Пусто':>6} {'Сек':>9}",
    ]
    for row in report["methods"]:
        lines.append(
            f"{str(row['method'])[:34]:34} {int(row['attempts']):8d} {int(row['errors']):7d} "
            f"{int(row['retries']):6d} {int(row['empty']):6d} {float(row['duration_ms']) / 1000:9.1f}"
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
    report = build_report(iter_events(Path(args.usage_dir), start, end), start=start, end=end)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_table(report))


if __name__ == "__main__":
    main()
