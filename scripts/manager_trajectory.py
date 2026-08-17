"""Agent-first CLI for manual manager telemetry collection and retrospectives."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.candidates import custom_period_bounds, make_client, profile_candidates_preview
from api.manager_trajectory import build_manager_trajectory_report, collect_manager_trajectory
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import DEFAULT_DB_PATH, get_analysis_profile, get_last_analysis_profile


def _datetime_arg(value: str, *, is_end: bool = False) -> datetime:
    normalized = str(value).strip()
    if len(normalized) == 10:
        parsed_date = date.fromisoformat(normalized)
        return datetime.combine(parsed_date + (timedelta(days=1) if is_end else timedelta()), time.min, MSK_TZ)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed.astimezone(MSK_TZ)


def _period(args: argparse.Namespace, *, default_yesterday: bool) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(MSK_TZ)
    if args.date_from:
        start = _datetime_arg(args.date_from)
    elif default_yesterday:
        start = datetime.combine(now.date() - timedelta(days=1), time.min, MSK_TZ)
    else:
        start = None
    if args.date_to:
        end = _datetime_arg(args.date_to, is_end=len(args.date_to.strip()) == 10)
    elif default_yesterday:
        end = datetime.combine(now.date(), time.min, MSK_TZ)
    else:
        end = None
    return start, end


def _manager_ids(args: argparse.Namespace) -> list[str] | None:
    return list(dict.fromkeys(str(item).strip() for item in (args.manager_id or []) if str(item).strip())) or None


def _print_text(payload: dict[str, Any], command: str) -> None:
    if command == "collect":
        print(f"Статус: {payload['status']}")
        print(f"Период: {payload['period']['from']} — {payload['period']['to']}")
        print(f"Менеджеры: {', '.join(payload['manager_ids'])}")
        print(f"Версии CRM-активностей, полученные по LAST_UPDATED: {payload['counts']['activities']}")
        print(f"Изменения стадий между наблюдаемыми срезами: {payload['counts']['stage_changes']}")
        for source, error in payload.get("errors", {}).items():
            print(f"Недоступно {source}: {error}")
        return
    if command == "candidates":
        print(f"Профиль: {payload['profile'].get('name') or payload['profile'].get('id')}")
        print(f"Кандидатов: {len(payload['candidates'])}")
        for item in payload["candidates"]:
            reasons = ", ".join(item.get("reason_codes") or []) or "без reason code"
            print(f"- {item['entity_type']} #{item['entity_id']}: {reasons}")
        print(payload["cost_preview"].get("message") or "")
        return
    print(f"Период: {payload['period']['from']} — {payload['period']['to']}")
    for manager in payload["managers"]:
        print(f"Менеджер {manager['manager_id']}")
        counts = manager.get("counts") or {}
        print(
            "  generated/shown/viewed: "
            f"{counts.get('recommendation_generated', 0)}/"
            f"{counts.get('recommendation_shown', 0)}/"
            f"{counts.get('recommendation_viewed', 0)}"
        )
        print(f"  CRM-события: {counts.get('crm_activity_observed', 0)}; сущностей: {manager['entities']}")
        excluded = manager.get("excluded_unverified_lifecycle_events", 0)
        if excluded:
            print(f"  Исключено неподтверждённых shown/viewed: {excluded}")
        for window in manager.get("viewed_windows_60m") or []:
            print(
                f"  После {window['recommendation_kind']} #{window['recommendation_id']}: "
                f"{window['observation']} (целевая сущность: {window['target_entity_events']}, "
                f"другие: {window['other_entity_events']})"
            )
    for warning in payload.get("warnings") or []:
        print(f"Важно: {warning}")


def _candidate_payload(args: argparse.Namespace) -> dict[str, Any]:
    profile = (
        get_analysis_profile(args.db_path, args.profile_id)
        if args.profile_id is not None
        else get_last_analysis_profile(args.db_path)
    )
    if not profile:
        raise ValueError("Профиль анализа не найден")
    period_override = None
    if args.date_from or args.date_to:
        if not args.date_from or not args.date_to:
            raise ValueError("Для candidates укажите одновременно --from и --to")
        period_override = custom_period_bounds(
            _datetime_arg(args.date_from).date(),
            (_datetime_arg(args.date_to).date()),
        )
    preview = profile_candidates_preview(
        profile,
        client=make_client(),
        db_path=args.db_path,
        period_override=period_override,
    )
    candidates = [
        {
            "entity_type": item.get("entity_type"),
            "entity_id": item.get("entity_id"),
            "reason_codes": item.get("reason_codes") or [],
            "analysis_freshness": item.get("analysis_freshness"),
            "workset_selected": bool(item.get("workset_selected")),
        }
        for item in preview.get("candidates") or []
    ]
    return {
        "period": preview.get("period"),
        "collection_status": None,
        "managers": [],
        "warnings": ["Candidate preview не запускает transcription или LLM."],
        "profile": preview.get("profile") or {},
        "summary": preview.get("summary") or {},
        "candidates": candidates,
        "cost_preview": preview.get("cost_preview") or {},
        "llm_called": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ручная telemetry НейроРОПа для developer/admin")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "report", "candidates"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--from", dest="date_from")
        subparser.add_argument("--to", dest="date_to")
        subparser.add_argument("--manager-id", action="append")
        subparser.add_argument("--format", choices=("text", "json"), default="text")
        if name == "candidates":
            subparser.add_argument("--profile-id", type=int)
    return parser


def _configure_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = build_parser().parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    try:
        if args.command == "collect":
            start, end = _period(args, default_yesterday=False)
            payload = collect_manager_trajectory(
                make_client(),
                db_path=args.db_path,
                manager_ids=_manager_ids(args),
                from_at=start,
                to_at=end,
            )
            exit_code = 0 if payload["status"] == "success" else 2
        elif args.command == "report":
            start, end = _period(args, default_yesterday=True)
            assert start is not None and end is not None
            payload = build_manager_trajectory_report(
                db_path=args.db_path,
                from_at=start,
                to_at=end,
                manager_ids=_manager_ids(args),
            )
            exit_code = 0
        else:
            payload = _candidate_payload(args)
            exit_code = 0
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_text(payload, args.command)
        return exit_code
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
