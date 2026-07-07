"""
Export a short Markdown summary of Bitrix24 leads created and modified recently.

Read-only Bitrix methods:
- crm.lead.list
- crm.status.list
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required
from openai_api.bitrix_links import bitrix_entity_url
from setup import MSK_TZ, get_logger


logger = get_logger(__file__)
DEFAULT_OUTPUT = PROJECT_ROOT / "leads_last_30_days_summary.md"
PAGE_SIZE = 50


def configure_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Выгрузить краткий Markdown-список лидов Bitrix24 за период."
    )
    parser.add_argument("--days", type=int, default=30, help="Период в днях. Default: 30")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Куда сохранить Markdown. Default: leads_last_30_days_summary.md в корне проекта",
    )
    parser.add_argument(
        "--include-source-description",
        action="store_true",
        help="Добавить SOURCE_DESCRIPTION. Обычно там длинные UTM-ссылки, поэтому по умолчанию выключено.",
    )
    parser.add_argument("--request-timeout", type=int, default=60, help="HTTP timeout Bitrix request, seconds")
    return parser.parse_args()


def date_for_bitrix(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_status_items(client: BitrixReadOnlyClient, entity_id: str) -> dict[str, dict[str, Any]]:
    rows = client.list_all("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        status_id = str(row.get("STATUS_ID") or "")
        name = str(row.get("NAME") or "").strip()
        if status_id:
            result[status_id] = {
                "name": name or status_id,
                "sort": parse_int(row.get("SORT"), default=999999),
            }
    return result


def load_status_map(client: BitrixReadOnlyClient, entity_id: str) -> dict[str, str]:
    return {key: str(value.get("name") or key) for key, value in load_status_items(client, entity_id).items()}


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def list_all_with_progress(client: BitrixReadOnlyClient, method: str, payload: dict[str, Any], title: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start: int | str = 0
    page = 1

    while True:
        page_payload = dict(payload)
        page_payload["start"] = start
        data = client.call(method, page_payload)
        result = data.get("result", [])

        if isinstance(result, dict) and isinstance(result.get("items"), list):
            batch = result["items"]
        elif isinstance(result, dict):
            batch = list(result.values())
        elif isinstance(result, list):
            batch = result
        else:
            batch = []

        valid_batch = [item for item in batch if isinstance(item, dict)]
        items.extend(valid_batch)
        print(f"{title}: page={page}, batch={len(valid_batch)}, total={len(items)}")

        next_start = data.get("next")
        if next_start is None or len(batch) < PAGE_SIZE:
            break
        start = next_start
        page += 1

    return items


def fetch_leads(client: BitrixReadOnlyClient, date_field: str, start_date: datetime) -> list[dict[str, Any]]:
    return list_all_with_progress(
        client,
        "crm.lead.list",
        {
            "order": {date_field: "DESC", "ID": "DESC"},
            "filter": {f">={date_field}": date_for_bitrix(start_date)},
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "SOURCE_ID",
                "SOURCE_DESCRIPTION",
                "DATE_CREATE",
                "DATE_MODIFY",
            ],
        },
        title=f"crm.lead.list by {date_field}",
    )


def clean_cell(value: Any) -> str:
    text = str(value or "").replace("\r\n", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|") or "-"


def status_label(lead: dict[str, Any], status_names: dict[str, str]) -> str:
    code = str(lead.get("STATUS_ID") or "")
    name = status_names.get(code, "")
    return f"{name} ({code})" if name and name != code else code or "-"


def status_sort_key(status_id: str, status_items: dict[str, dict[str, Any]]) -> tuple[int, str, str]:
    item = status_items.get(status_id) or {}
    sort = parse_int(item.get("sort"), default=999999)
    name = str(item.get("name") or status_id)
    return sort, name.lower(), status_id


def source_label(lead: dict[str, Any], source_names: dict[str, str], *, include_description: bool = False) -> str:
    code = str(lead.get("SOURCE_ID") or "")
    name = source_names.get(code, "")
    description = str(lead.get("SOURCE_DESCRIPTION") or "").strip()
    main = f"{name} ({code})" if name and name != code else code
    if include_description and description:
        return f"{main or '-'}; {description}"
    return main or "-"


def lead_link(lead_id: str) -> str:
    url = bitrix_entity_url("lead", lead_id)
    return url or "-"


def unique_by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for row in rows:
        lead_id = str(row.get("ID") or "")
        if not lead_id or lead_id in seen:
            continue
        seen.add(lead_id)
        result.append(row)
    return result


def sort_by_date_create(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("DATE_CREATE") or ""),
            parse_int(item.get("ID"), default=0),
        ),
        reverse=True,
    )


def render_table(
    rows: list[dict[str, Any]],
    status_names: dict[str, str],
    source_names: dict[str, str],
    *,
    include_source_description: bool,
) -> list[str]:
    lines = [
        "| ID | Название | Этап/статус | Источник | Создан | Изменен | Ссылка |",
        "|---:|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | Нет лидов за период | - | - | - | - | - |")
        return lines

    for lead in rows:
        lead_id = str(lead.get("ID") or "")
        lines.append(
            "| "
            + " | ".join(
                [
                    clean_cell(lead_id),
                    clean_cell(lead.get("TITLE")),
                    clean_cell(status_label(lead, status_names)),
                    clean_cell(source_label(lead, source_names, include_description=include_source_description)),
                    clean_cell(lead.get("DATE_CREATE")),
                    clean_cell(lead.get("DATE_MODIFY")),
                    clean_cell(lead_link(lead_id)),
                ]
            )
            + " |"
        )
    return lines


def render_grouped_by_status(
    rows: list[dict[str, Any]],
    status_items: dict[str, dict[str, Any]],
    status_names: dict[str, str],
    source_names: dict[str, str],
    *,
    include_source_description: bool,
) -> list[str]:
    if not rows:
        return ["Нет лидов за период."]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        status_id = str(row.get("STATUS_ID") or "")
        grouped.setdefault(status_id, []).append(row)

    lines: list[str] = []
    for status_id in sorted(grouped, key=lambda value: status_sort_key(value, status_items)):
        group_rows = sort_by_date_create(grouped[status_id])
        lines.extend(
            [
                f"### Этап: {status_label({'STATUS_ID': status_id}, status_names)} - {len(group_rows)}",
                "",
                *render_table(
                    group_rows,
                    status_names,
                    source_names,
                    include_source_description=include_source_description,
                ),
                "",
            ]
        )
    return lines


def render_report(
    *,
    days: int,
    start_date: datetime,
    created: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    status_items: dict[str, dict[str, Any]],
    status_names: dict[str, str],
    source_names: dict[str, str],
    include_source_description: bool,
) -> str:
    generated_at = datetime.now(MSK_TZ)
    created_ids = {str(row.get("ID") or "") for row in created}
    modified_ids = {str(row.get("ID") or "") for row in modified}
    modified_only = [row for row in modified if str(row.get("ID") or "") not in created_ids]
    all_unique = unique_by_id(created + modified)

    lines = [
        f"# Лиды Bitrix24 за последние {days} дней",
        "",
        f"- Сформировано: {generated_at.isoformat(timespec='seconds')}",
        f"- Период: последние {days} дней, с {start_date.isoformat(timespec='seconds')}",
        f"- Созданы за период: {len(created)}",
        f"- Изменены за период: {len(modified)}",
        f"- Изменены, но созданы раньше периода: {len(modified_only)}",
        f"- Уникальных лидов в выгрузке: {len(all_unique)}",
        "- Группировка: по этапам лида в порядке `SORT` из Bitrix24; внутри этапа по дате создания от новых к старым.",
        "",
        "## Лиды, созданные за период",
        "",
        *render_grouped_by_status(
            created,
            status_items,
            status_names,
            source_names,
            include_source_description=include_source_description,
        ),
        "",
        "## Лиды, измененные за период",
        "",
        *render_grouped_by_status(
            modified,
            status_items,
            status_names,
            source_names,
            include_source_description=include_source_description,
        ),
        "",
        "## Потенциально потерянные старые лиды",
        "",
        "Лиды ниже были созданы раньше периода, но изменялись за последние дни. Их удобно проверить отдельно.",
        "",
        *render_grouped_by_status(
            modified_only,
            status_items,
            status_names,
            source_names,
            include_source_description=include_source_description,
        ),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    configure_console()
    args = parse_args()
    if args.days <= 0:
        raise SystemExit("--days должен быть больше 0")

    load_dotenv(PROJECT_ROOT / ".env")
    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"), timeout=args.request_timeout)
    start_date = datetime.now(MSK_TZ) - timedelta(days=args.days)
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    logger.info("Fetching lead dictionaries")
    status_items = load_status_items(client, "STATUS")
    status_names = {key: str(value.get("name") or key) for key, value in status_items.items()}
    source_names = load_status_map(client, "SOURCE")

    logger.info("Fetching leads by DATE_CREATE since %s", start_date.isoformat(timespec="seconds"))
    created = fetch_leads(client, "DATE_CREATE", start_date)
    logger.info("Fetching leads by DATE_MODIFY since %s", start_date.isoformat(timespec="seconds"))
    modified = fetch_leads(client, "DATE_MODIFY", start_date)

    content = render_report(
        days=args.days,
        start_date=start_date,
        created=created,
        modified=modified,
        status_items=status_items,
        status_names=status_names,
        source_names=source_names,
        include_source_description=args.include_source_description,
    )
    output_path.write_text(content, encoding="utf-8")

    print(f"Markdown saved: {output_path}")
    print(json.dumps({"created": len(created), "modified": len(modified)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
