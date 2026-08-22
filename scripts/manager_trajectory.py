"""Agent-first CLI for manager telemetry collection and retrospectives.

`collect` uses the same `api.manager_trajectory.collect_manager_trajectory`
helper as the server daytime cycle; it does not start LLM analysis.
"""

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
from setup import BASE_DIR, MSK_TZ, configure_console
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
    selected_date = str(getattr(args, "date", "") or "").strip()
    if selected_date:
        if args.date_from or args.date_to:
            raise ValueError("Используйте либо --date, либо --from/--to")
        day = date.fromisoformat(selected_date)
        return (
            datetime.combine(day, time.min, MSK_TZ),
            datetime.combine(day + timedelta(days=1), time.min, MSK_TZ),
        )
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


def _clock(value: Any) -> str:
    if not value:
        return "время не указано"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MSK_TZ)
        return parsed.astimezone(MSK_TZ).strftime("%H:%M:%S МСК")
    except ValueError:
        return str(value)


def _activity_label(action: dict[str, Any]) -> str:
    if action.get("action_type") == "stage_change":
        before = action.get("from_stage_name") or action.get("from_stage_id") or "не указана"
        after = action.get("to_stage_name") or action.get("to_stage_id") or "не указана"
        return f"смена стадии {before} → {after}"
    labels = {
        "call": "звонок",
        "email": "письмо",
        "message": "сообщение",
        "task": "задача",
        "other": "другая CRM-активность",
        "task_history": "изменение задачи",
        "timeline_comment": "комментарий таймлайна",
        "business_field_change": "изменение бизнес-поля",
        "stage_history": "вход в стадию (история Bitrix)",
    }
    label = labels.get(str(action.get("activity_kind") or "other"), str(action.get("activity_kind") or "активность"))
    direction = {"1": "входящий", "2": "исходящий"}.get(str(action.get("direction") or ""))
    completed = action.get("completed")
    details = [value for value in (direction, "завершена" if completed is True else None) if value]
    activity_id = action.get("activity_id")
    suffix = f"activity #{activity_id}" if activity_id else None
    annotations = ", ".join([*details, *([suffix] if suffix else [])])
    content = str(action.get("subject") or action.get("description") or "").strip()
    content = " ".join(content.split())
    if len(content) > 180:
        content = content[:177].rstrip() + "..."
    return f"{label}{f' ({annotations})' if annotations else ''}{f' — {content}' if content else ''}"


def _print_actions(actions: list[dict[str, Any]], *, indent: str) -> None:
    if not actions:
        print(f"{indent}действий не обнаружено")
        return
    for action in actions:
        print(f"{indent}{_clock(action.get('occurred_at'))} — {_activity_label(action)}")


def _recommendation_label(kind: Any) -> str:
    return "Quick Help" if str(kind or "") == "quick_help" else "задача НейроРОПа"


def _print_text(payload: dict[str, Any], command: str) -> None:
    if command == "collect":
        print(f"Статус: {payload['status']}")
        print(f"Период: {payload['period']['from']} — {payload['period']['to']}")
        print(f"Менеджеры: {', '.join(payload['manager_ids'])}")
        print(f"Версии CRM-активностей, полученные по LAST_UPDATED: {payload['counts']['activities']}")
        print(f"Изменения стадий между наблюдаемыми срезами: {payload['counts']['stage_changes']}")
        print(
            "Дополнительно: "
            f"история стадий {payload['counts'].get('stage_history', 0)}, "
            f"история задач {payload['counts'].get('task_history', 0)}, "
            f"комментарии {payload['counts'].get('timeline_comments', 0)}, "
            f"изменения бизнес-полей {payload['counts'].get('business_field_changes', 0)}, "
            f"presence-снимки {payload['counts'].get('presence_snapshots', 0)}"
        )
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
    if command == "snapshot":
        collection = payload.get("collection_run") or {}
        print(f"Сбор Bitrix: {collection.get('status') or 'статус неизвестен'}")
    print(f"Период: {payload['period']['from']} — {payload['period']['to']}")
    summary = payload.get("summary") or {}
    print(
        "Итого: "
        f"менеджеров {summary.get('managers', 0)}, "
        f"уникальных CRM-действий {summary.get('unique_crm_actions', 0)}, "
        f"рекомендаций создано/показано/просмотрено "
        f"{summary.get('recommendations_generated', 0)}/"
        f"{summary.get('recommendations_shown', 0)}/"
        f"{summary.get('recommendations_viewed', 0)}; "
        f"входов в Quick Help {summary.get('quick_help_opened', 0)}"
    )
    for manager in payload["managers"]:
        manager_label = manager.get("manager_name") or "Имя не найдено"
        print(f"\nМенеджер {manager_label} (ID {manager['manager_id']})")
        counts = manager.get("counts") or {}
        workday = manager.get("workday") or {}
        print("  1. Чем занимался в течение дня")
        activity_counts = workday.get("activity_counts") or {}
        print(
            "    Уникальных CRM-действий: "
            f"{workday.get('unique_crm_actions', 0)}; "
            f"звонков {activity_counts.get('call', 0)}, "
            f"сообщений {activity_counts.get('message', 0)}, "
            f"писем {activity_counts.get('email', 0)}, "
            f"задач {activity_counts.get('task', 0)}, "
            f"смен стадий {workday.get('stage_changes', 0)}"
        )
        print(
            f"    Сделок затронуто: {workday.get('deals_touched', 0)}; "
            f"лидов: {workday.get('leads_touched', 0)}"
        )
        for entity in workday.get("entities") or []:
            entity_label = "Сделка" if entity.get("entity_type") == "deal" else "Лид"
            stage = entity.get("stage_name") or entity.get("stage_id")
            print(f"    {entity_label} #{entity.get('entity_id')}{f' · этап {stage}' if stage else ''}")
            timeline = [
                *(entity.get("crm_actions") or []), *(entity.get("stage_changes") or []),
                *(entity.get("task_history") or []), *(entity.get("timeline_comments") or []),
                *(entity.get("business_field_changes") or []), *(entity.get("stage_history") or []),
            ]
            timeline.sort(key=lambda item: str(item.get("occurred_at") or ""))
            _print_actions(timeline, indent="      ")

        print("  2. Как использовал НейроРОП")
        print(
            "    Создано/показано/просмотрено: "
            f"{counts.get('recommendation_generated', 0)}/"
            f"{counts.get('recommendation_shown', 0)}/"
            f"{counts.get('recommendation_viewed', 0)}"
        )
        recommendations = (manager.get("product_usage") or {}).get("recommendations") or []
        if not recommendations:
            print("    Событий использования рекомендаций не обнаружено")
        for recommendation in recommendations:
            print(
                f"    Сделка #{recommendation.get('deal_id')} · "
                f"{_recommendation_label(recommendation.get('recommendation_kind'))} "
                f"#{recommendation.get('recommendation_id')}"
            )
            print(
                f"      создана: {', '.join(_clock(item) for item in recommendation.get('generated_at') or []) or 'вне периода или нет данных'}"
            )
            print(
                f"      показана: {', '.join(_clock(item) for item in recommendation.get('shown_at') or []) or 'нет данных'}"
            )
            print(
                f"      просмотры: {', '.join(_clock(item) for item in recommendation.get('viewed_at') or []) or 'нет'}; "
                f"точность: {recommendation.get('view_tracking_precision')}"
            )
        openings = (manager.get("product_usage") or {}).get("quick_help_openings") or []
        print(f"    Входов в раздел Quick Help: {len(openings)}")
        for opening in openings:
            mode = opening.get("assistant_mode") or "режим не указан"
            print(
                f"      {_clock(opening.get('opened_at'))} — сделка #{opening.get('deal_id')} · {mode}; "
                f"точность: {opening.get('tracking_precision')}"
            )

        print("  3. Что происходило до и после просмотров")
        correlations = manager.get("correlations") or []
        if not correlations:
            print("    Подтверждённых просмотров за период не обнаружено")
        for deal in correlations:
            print(f"    Сделка #{deal.get('deal_id')}")
            for view in deal.get("views") or []:
                print(
                    f"      Просмотр {view.get('view_index_for_deal')} · {_clock(view.get('occurred_at'))} · "
                    f"{_recommendation_label(view.get('recommendation_kind'))} #{view.get('recommendation_id')}"
                )
                print("        До просмотра (после предыдущего просмотра этой сделки):")
                _print_actions(view.get("actions_before_since_previous_view") or [], indent="          ")
                print("        После просмотра до следующего просмотра этой сделки или конца периода:")
                _print_actions(view.get("actions_after_until_next_view") or [], indent="          ")
                delay = view.get("minutes_to_first_same_deal_action")
                print(
                    "        В первые 60 минут по этой сделке: "
                    f"{len(view.get('same_deal_actions_within_60m') or [])}; "
                    f"по другим сделкам: {view.get('other_deal_actions_within_60m', 0)}; "
                    f"до первого действия по этой сделке: {f'{delay} мин.' if delay is not None else 'действий не обнаружено'}"
                )
            for opening in deal.get("quick_help_openings") or []:
                print(
                    f"      Вход в Quick Help {opening.get('opening_index_for_deal')} · "
                    f"{_clock(opening.get('opened_at'))} · {opening.get('assistant_mode') or 'режим не указан'}"
                )
                print("        До входа (после предыдущего входа в Quick Help по этой сделке):")
                _print_actions(opening.get("actions_before_since_previous_open") or [], indent="          ")
                print("        После входа до следующего входа или конца периода:")
                _print_actions(opening.get("actions_after_until_next_open") or [], indent="          ")
                delay = opening.get("minutes_to_first_same_deal_action")
                print(
                    "        В первые 60 минут по этой сделке: "
                    f"{len(opening.get('same_deal_actions_within_60m') or [])}; "
                    f"по другим сделкам: {opening.get('other_deal_actions_within_60m', 0)}; "
                    f"до первого действия по этой сделке: {f'{delay} мин.' if delay is not None else 'действий не обнаружено'}"
                )
        excluded = manager.get("excluded_unverified_lifecycle_events", 0)
        if excluded:
            print(f"  Исключено неподтверждённых shown/viewed событий: {excluded}")
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
    for name in ("snapshot", "collect", "report", "candidates"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--from", dest="date_from")
        subparser.add_argument("--to", dest="date_to")
        subparser.add_argument("--manager-id", action="append")
        subparser.add_argument("--format", choices=("text", "json"), default="text")
        if name == "snapshot":
            subparser.add_argument("--date")
        if name == "candidates":
            subparser.add_argument("--profile-id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
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
        elif args.command in {"report", "snapshot"}:
            start, end = _period(args, default_yesterday=True)
            assert start is not None and end is not None
            collection = None
            if args.command == "snapshot":
                collection = collect_manager_trajectory(
                    make_client(),
                    db_path=args.db_path,
                    manager_ids=_manager_ids(args),
                    from_at=start,
                    to_at=end,
                )
            payload = build_manager_trajectory_report(
                db_path=args.db_path,
                from_at=start,
                to_at=end,
                manager_ids=_manager_ids(args),
            )
            if collection is not None:
                payload["collection_run"] = collection
            exit_code = 0 if collection is None or collection["status"] == "success" else 2
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
