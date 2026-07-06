"""
Markdown renderer for customer_history_bundle JSON files.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import load_json, save_json
from bitrix.customer_history import clean_text, result_item
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_DEAL_INPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_DEAL_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "markdown"
DEFAULT_LEAD_INPUT_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"
DEFAULT_LEAD_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "markdown"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Markdown reports from customer history bundles")
    parser.add_argument("--entity-type", choices=["lead", "deal"], required=True)
    parser.add_argument("--entity-ids", nargs="*", help="Entity IDs. If omitted, all matching bundles are used.")
    parser.add_argument("--input-dir", help="Raw JSON dir with *_customer_history_bundle.json")
    parser.add_argument("--output-dir", help="Markdown output dir")
    return parser.parse_args()


def md_escape(value: Any) -> str:
    text = clean_text(value)
    return text.replace("|", "\\|").replace("\n", "<br>") if text else "-"


def contact_name(contact: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (contact.get("NAME"), contact.get("SECOND_NAME"), contact.get("LAST_NAME"))
        if part
    )


def contact_values(contact: dict[str, Any], field: str) -> str:
    values = contact.get(field) or []
    if isinstance(values, list):
        return ", ".join(str(item.get("VALUE")) for item in values if isinstance(item, dict) and item.get("VALUE"))
    return str(values or "")


def contact_section(bundle: dict[str, Any]) -> list[str]:
    contacts = bundle.get("contacts") or {}
    if not contacts:
        return ["- Контакт: не найден"]

    lines: list[str] = []
    primary_id = (bundle.get("contact_resolution") or {}).get("primary_contact_id")
    for contact_id, response in contacts.items():
        contact = result_item(response)
        primary = "основной" if str(contact_id) == str(primary_id) else "связанный"
        lines.extend(
            [
                f"- Контакт {primary}: {contact_name(contact) or '-'} (ID: {contact_id})",
                f"  Телефон: {contact_values(contact, 'PHONE') or '-'}",
                f"  Email: {contact_values(contact, 'EMAIL') or '-'}",
            ]
        )
    return lines


def related_deals_table(bundle: dict[str, Any]) -> list[str]:
    lines = [
        "| ID | Название | Воронка | Стадия | Сумма | Создана | Изменена | Закрыта | Ответственный | Статус |",
        "|---:|---|---|---|---:|---|---|---|---:|---|",
    ]
    for deal in bundle.get("related_deals") or []:
        pipeline = (deal.get("pipeline") or {}).get("name") or deal.get("category_id") or "-"
        amount = f"{deal.get('opportunity') or '-'} {deal.get('currency_id') or ''}".strip()
        status = "закрыта" if deal.get("is_closed") else "открыта/неясно"
        lines.append(
            f"| {md_escape(deal.get('id'))} | {md_escape(deal.get('title'))} | {md_escape(pipeline)} | "
            f"{md_escape(deal.get('stage_name') or deal.get('stage_id'))} | {md_escape(amount)} | "
            f"{md_escape(deal.get('date_create'))} | {md_escape(deal.get('date_modify'))} | "
            f"{md_escape(deal.get('closedate'))} | {md_escape(deal.get('assigned_by_id'))} | {status} |"
        )
    if len(lines) == 2:
        lines.append("| - | Связанные сделки не найдены | - | - | - | - | - | - | - | - |")
    return lines


def related_leads_table(bundle: dict[str, Any]) -> list[str]:
    lines = [
        "| ID | Название | Статус | Сумма | Создан | Изменен | Закрыт | Ответственный |",
        "|---:|---|---|---:|---|---|---|---:|",
    ]
    for lead in bundle.get("related_leads") or []:
        amount = f"{lead.get('opportunity') or '-'} {lead.get('currency_id') or ''}".strip()
        lines.append(
            f"| {md_escape(lead.get('id'))} | {md_escape(lead.get('title'))} | "
            f"{md_escape(lead.get('status_id'))} | {md_escape(amount)} | "
            f"{md_escape(lead.get('date_create'))} | {md_escape(lead.get('date_modify'))} | "
            f"{md_escape(lead.get('date_closed'))} | {md_escape(lead.get('assigned_by_id'))} |"
        )
    if len(lines) == 2:
        lines.append("| - | Связанные дубль-лиды не найдены | - | - | - | - | - | - |")
    return lines


def event_text(item: dict[str, Any], limit: int = 260) -> str:
    value = item.get("subject") or item.get("text") or ""
    return clean_text(value, limit)


def timeline_table(items: list[dict[str, Any]], empty: str) -> list[str]:
    lines = ["| Дата | Сущность | Тип | ID | Содержание |", "|---|---|---|---:|---|"]
    for item in items:
        entity = f"{item.get('entity_type')}:{item.get('entity_id')}"
        lines.append(
            f"| {md_escape(item.get('when'))} | {md_escape(entity)} | {md_escape(item.get('event_type'))} | "
            f"{md_escape(item.get('id'))} | {md_escape(event_text(item))} |"
        )
    if len(lines) == 2:
        lines.append(f"| - | - | - | - | {md_escape(empty)} |")
    return lines


def diagnostics_section(bundle: dict[str, Any]) -> list[str]:
    diagnostics = bundle.get("diagnostics") or {}
    lines = [
        f"- Контакт не найден: {'да' if diagnostics.get('missing_contact') else 'нет'}",
        f"- CONTACT_ID отсутствовал: {'да' if diagnostics.get('contact_id_missing') else 'нет'}",
        f"- Fallback-связка использована: {'да' if diagnostics.get('fallback_match_used') else 'нет'}",
        f"- Дубль-лиды по fallback использованы: {'да' if diagnostics.get('fallback_related_leads_used') else 'нет'}",
    ]
    fallback_matches = diagnostics.get("fallback_matches") or []
    if fallback_matches:
        lines.append("- Подтвержденные fallback-совпадения:")
        for item in fallback_matches:
            if isinstance(item, dict):
                lines.append(
                    f"  - contact_id={item.get('contact_id')}, по {item.get('matched_by')} из {item.get('source')}"
                )
            else:
                lines.append(f"  - {clean_text(item)}")
    fallback_lead_matches = diagnostics.get("fallback_lead_matches") or []
    if fallback_lead_matches:
        lines.append("- Подтвержденные дубль-лиды по fallback:")
        for item in fallback_lead_matches:
            if isinstance(item, dict):
                lines.append(f"  - lead_id={item.get('lead_id')}, по {item.get('matched_by')} из {item.get('source')}")
            else:
                lines.append(f"  - {clean_text(item)}")
    fallback_candidates = diagnostics.get("fallback_candidates") or []
    if fallback_candidates:
        lines.append("- Кандидаты для ручной fallback-проверки:")
        for item in fallback_candidates:
            if isinstance(item, dict):
                lines.append(f"  - {item.get('type')}: {item.get('value')} ({item.get('source')})")
            else:
                lines.append(f"  - {clean_text(item)}")
    warnings = diagnostics.get("warnings") or []
    if warnings:
        lines.append("- Предупреждения:")
        lines.extend(f"  - {clean_text(item)}" for item in warnings)
    unavailable = diagnostics.get("unavailable_sources") or []
    if unavailable:
        lines.append("- Недоступные / неиспользованные источники:")
        for item in unavailable:
            if isinstance(item, dict):
                lines.append(
                    f"  - {item.get('source')}: {item.get('reason') or item.get('note') or 'без деталей'}"
                )
            else:
                lines.append(f"  - {clean_text(item)}")
    return lines


def render_customer_history_markdown(bundle: dict[str, Any]) -> str:
    root = bundle.get("root_entity") or {}
    period = bundle.get("history_period") or {}
    root_type = root.get("type") or "unknown"
    root_id = str(root.get("id") or "")
    timeline = bundle.get("unified_timeline") or []
    client_touchpoints = bundle.get("client_touchpoints") or []
    internal_context = bundle.get("internal_context") or []
    tasks_and_control = bundle.get("tasks_and_control") or []
    system_events = bundle.get("system_events") or []

    lines = [
        "# Полная история клиента",
        "",
        f"Отчет собран: {datetime.now(MSK_TZ).isoformat(timespec='seconds')}",
        "",
        "## Корневая сущность",
        "",
        f"- Тип: {root_type}",
        f"- ID: {root_id}",
        f"- Название: {clean_text(root.get('title')) or '-'}",
        f"- Период истории: {period.get('days')} дней, с {period.get('date_from')} по {period.get('date_to')}",
        "",
        "## Контакт",
        "",
        *contact_section(bundle),
        "",
        "## Связанные сделки контакта",
        "",
        *related_deals_table(bundle),
        "",
        "## Связанные дубль-лиды",
        "",
        *related_leads_table(bundle),
        "",
        "## Хронология",
        "",
        "Единая временная линия по лиду, контакту, связанным сделкам, задачам, звонкам и комментариям.",
        "",
        *timeline_table(timeline, "События за период не найдены"),
        "",
        "## Клиентские касания",
        "",
        *timeline_table(client_touchpoints, "Клиентские касания за период не найдены"),
        "",
        "## Задачи и контроль",
        "",
        *timeline_table(tasks_and_control, "Задачи и контрольные активности за период не найдены"),
        "",
        "## Внутренний контекст",
        "",
        "Этот блок отделен от клиентских касаний. Не считать его словами клиента.",
        "",
        *timeline_table(internal_context, "Внутренние комментарии/заметки не найдены или исключены флагом"),
        "",
        "## Системные события и состояния",
        "",
        *timeline_table(system_events, "Системные события не найдены"),
        "",
        "## Диагностика выгрузки",
        "",
        *diagnostics_section(bundle),
        "",
        "## Технические замечания",
        "",
        "- Скрипт ничего не менял в Bitrix, только читал данные через REST API.",
        "- Открытые линии не используются как обязательный источник для ПрактикМ.",
        "- Если CONTACT_ID пустой, fallback по телефону/email применяется только после подтверждения найденного контакта через crm.contact.get.",
    ]
    return "\n".join(lines) + "\n"


def input_paths(entity_type: str, input_dir: Path, entity_ids: list[str] | None) -> list[Path]:
    if entity_ids:
        return [input_dir / f"{entity_type}_{entity_id}_customer_history_bundle.json" for entity_id in entity_ids]
    return sorted(input_dir.glob(f"{entity_type}_*_customer_history_bundle.json"))


def default_dirs(entity_type: str) -> tuple[Path, Path]:
    if entity_type == "deal":
        return DEFAULT_DEAL_INPUT_DIR, DEFAULT_DEAL_OUTPUT_DIR
    return DEFAULT_LEAD_INPUT_DIR, DEFAULT_LEAD_OUTPUT_DIR


def main() -> None:
    args = parse_args()
    default_input, default_output = default_dirs(args.entity_type)
    input_dir = Path(args.input_dir) if args.input_dir else default_input
    output_dir = Path(args.output_dir) if args.output_dir else default_output
    output_dir.mkdir(parents=True, exist_ok=True)

    index_items = []
    for raw_path in input_paths(args.entity_type, input_dir, args.entity_ids):
        if not raw_path.exists():
            logger.warning("Customer history bundle not found: %s", raw_path)
            continue
        bundle = load_json(raw_path)
        root = bundle.get("root_entity") or {}
        entity_id = str(root.get("id") or raw_path.stem.split("_")[1])
        output_path = output_dir / f"{args.entity_type}_{entity_id}_customer_path.md"
        output_path.write_text(render_customer_history_markdown(bundle), encoding="utf-8")
        index_items.append({f"{args.entity_type}_id": entity_id, "output_path": str(output_path)})
        logger.info("Saved customer history Markdown: %s", output_path)

    save_json(output_dir / "index.json", {"generated_at": datetime.now(MSK_TZ).isoformat(), "items": index_items})


if __name__ == "__main__":
    main()
