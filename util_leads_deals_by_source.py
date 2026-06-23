"""
Показывает активные лиды и сделки Bitrix24 по источнику за период.

Методы Bitrix24 REST API:
- crm.lead.list: получает список активных лидов по SOURCE_ID и периоду создания.
- crm.deal.list: получает список активных сделок по SOURCE_ID и периоду создания.

По умолчанию ищет источник ЛидгенБюро:
    SOURCE_ID = 10
    период = последние 7 дней
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_SOURCE_ID = "10"
DEFAULT_DAYS = 23
DEFAULT_SUMMARY_ROOT = BASE_DIR / "reports" / "source_scan"
PAGE_SIZE = 50

logger = get_logger(__file__)


def get_env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Переменная окружения {name} не задана или пуста")
    return value


def build_api_method_url(webhook_url: str, method: str) -> str:
    return f"{webhook_url.rstrip('/')}/{method}"


def call_bitrix_api(
    webhook_url: str,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    method_url = build_api_method_url(webhook_url, method)
    response = requests.post(
        method_url,
        json=payload or {},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok:
        error_text = result.get("error_description") or result.get("error") or response.text
        raise RuntimeError(f"HTTP {response.status_code}: {error_text}")

    if result.get("error"):
        error_text = result.get("error_description") or result.get("error")
        raise RuntimeError(str(error_text))

    return result


def fetch_crm_list(
    webhook_url: str,
    method: str,
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start: int | str = 0

    while True:
        payload = {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": filters,
            "select": ["*", "UF_*"],
            "start": start,
        }
        response = call_bitrix_api(webhook_url, method, payload)
        batch = response.get("result", [])

        if not isinstance(batch, list):
            raise RuntimeError(f"Неожиданный формат result для {method}: {type(batch)}")

        items.extend(batch)

        next_start = response.get("next")
        if next_start is None or len(batch) < PAGE_SIZE:
            break

        start = next_start

    return items


def build_date_from(days: int) -> str:
    date_from = datetime.now(MSK_TZ) - timedelta(days=days)
    return date_from.isoformat(timespec="seconds")


def log_json_section(title: str, items: List[Dict[str, Any]]) -> None:
    logger.info("")
    logger.info("%s", title)
    logger.info("Найдено: %s", len(items))
    logger.info(json.dumps(items, ensure_ascii=False, indent=2))


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2: \1", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def md_cell(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return "-"
    return text.replace("|", "\\|").replace("\n", "<br>")


def first_multifield(item: Dict[str, Any], field_name: str) -> str:
    values = item.get(field_name)
    if not isinstance(values, list):
        return ""
    for row in values:
        if isinstance(row, dict) and row.get("VALUE"):
            return clean_text(row.get("VALUE"))
    return ""


def non_empty_uf_count(item: Dict[str, Any]) -> int:
    count = 0
    for key, value in item.items():
        if not str(key).startswith("UF_"):
            continue
        if value not in (None, "", [], {}, False):
            count += 1
    return count


def has_value(value: Any) -> bool:
    return value not in (None, "", [], {}, False)


def present_parts(parts: list[tuple[str, bool]]) -> str:
    found = [label for label, exists in parts if exists]
    return ", ".join(found) if found else "только базовые поля"


def extract_comment_details(text: str) -> Dict[str, str]:
    source = clean_text(text)
    details: Dict[str, str] = {
        "phone": "",
        "request": "",
        "recording_url": "",
    }

    phone_match = re.search(r"Телефон:\s*([+\d][\d\s().-]{8,})", source, flags=re.I)
    if not phone_match:
        phone_match = re.search(r"\b(?:7|8)\d{10}\b", source)
    if phone_match:
        details["phone"] = re.sub(r"\D", "", phone_match.group(1) if phone_match.groups() else phone_match.group(0))

    request_match = re.search(
        r"Комментарий:\s*(.*?)(?:\nДоп\.\s*комментарий:|\nСсылка на запись:|$)",
        source,
        flags=re.I | re.S,
    )
    if request_match:
        details["request"] = clean_text(request_match.group(1))

    url_match = re.search(r"https?://\S+", source)
    if url_match:
        details["recording_url"] = url_match.group(0).rstrip(".,;):")

    return details


def compact_request(text: str, limit: int = 140) -> str:
    details = extract_comment_details(text)
    request = details.get("request") or clean_text(text)
    return clean_text(request, limit)


def lead_summary(lead: Dict[str, Any]) -> Dict[str, Any]:
    lead_id = str(lead.get("ID") or "")
    comment_full = clean_text(lead.get("COMMENTS"))
    comment_details = extract_comment_details(comment_full)
    phone = first_multifield(lead, "PHONE") or comment_details.get("phone", "")
    email = first_multifield(lead, "EMAIL")
    comments = clean_text(comment_full, 300)
    uf_count = non_empty_uf_count(lead)
    return {
        "entity_type": "lead",
        "id": lead_id,
        "title": clean_text(lead.get("TITLE")),
        "client": clean_text(" ".join(str(lead.get(key) or "") for key in ("NAME", "LAST_NAME"))),
        "phone": phone,
        "email": email,
        "status_id": clean_text(lead.get("STATUS_ID")),
        "status_semantic_id": clean_text(lead.get("STATUS_SEMANTIC_ID")),
        "source_id": clean_text(lead.get("SOURCE_ID")),
        "assigned_by_id": clean_text(lead.get("ASSIGNED_BY_ID")),
        "date_create": clean_text(lead.get("DATE_CREATE")),
        "last_activity_time": clean_text(lead.get("LAST_ACTIVITY_TIME") or lead.get("DATE_MODIFY")),
        "comment_preview": comments,
        "request_preview": compact_request(comment_full),
        "comment_details": comment_details,
        "uf_non_empty_count": uf_count,
        "present": present_parts(
            [
                ("телефон", bool(phone)),
                ("email", bool(email)),
                ("комментарий", bool(comments)),
                ("пользовательские поля", uf_count > 0),
            ]
        ),
        "pipeline_command": f".\\venv\\Scripts\\python.exe .\\bitrix\\leads\\run_leads_customer_path_pipeline.py --lead-ids {lead_id}",
    }


def deal_summary(deal: Dict[str, Any]) -> Dict[str, Any]:
    deal_id = str(deal.get("ID") or "")
    comment_full = clean_text(deal.get("COMMENTS"))
    comment_details = extract_comment_details(comment_full)
    comments = clean_text(comment_full, 300)
    amount = clean_text(deal.get("OPPORTUNITY"))
    currency = clean_text(deal.get("CURRENCY_ID"))
    uf_count = non_empty_uf_count(deal)
    return {
        "entity_type": "deal",
        "id": deal_id,
        "title": clean_text(deal.get("TITLE")),
        "company_id": clean_text(deal.get("COMPANY_ID")),
        "contact_id": clean_text(deal.get("CONTACT_ID")),
        "amount": f"{amount} {currency}".strip(),
        "stage_id": clean_text(deal.get("STAGE_ID")),
        "category_id": clean_text(deal.get("CATEGORY_ID")),
        "source_id": clean_text(deal.get("SOURCE_ID")),
        "assigned_by_id": clean_text(deal.get("ASSIGNED_BY_ID")),
        "date_create": clean_text(deal.get("DATE_CREATE")),
        "last_activity_time": clean_text(deal.get("LAST_ACTIVITY_TIME") or deal.get("DATE_MODIFY")),
        "comment_preview": comments,
        "request_preview": compact_request(comment_full),
        "comment_details": comment_details,
        "uf_non_empty_count": uf_count,
        "present": present_parts(
            [
                ("компания", has_value(deal.get("COMPANY_ID"))),
                ("контакт", has_value(deal.get("CONTACT_ID"))),
                ("сумма", bool(amount) and amount not in {"0", "0.00"}),
                ("комментарий", bool(comments)),
                ("пользовательские поля", uf_count > 0),
            ]
        ),
        "pipeline_command": f".\\venv\\Scripts\\python.exe .\\bitrix\\deals\\run_deals_customer_path_pipeline.py --deal-ids {deal_id}",
    }


def command_for_many(entity_type: str, ids: list[str]) -> str:
    ids_text = " ".join(ids)
    if not ids_text:
        return ""
    if entity_type == "lead":
        return f".\\venv\\Scripts\\python.exe .\\bitrix\\leads\\run_leads_customer_path_pipeline.py --lead-ids {ids_text}"
    return f".\\venv\\Scripts\\python.exe .\\bitrix\\deals\\run_deals_customer_path_pipeline.py --deal-ids {ids_text}"


def markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    if not rows:
        lines.append("| " + " | ".join("-" for _ in headers) + " |")
        return lines
    for row in rows:
        lines.append("| " + " | ".join(md_cell(value) for value in row) + " |")
    return lines


def item_phone(item: Dict[str, Any]) -> str:
    return clean_text(item.get("phone")) or extract_comment_details(item.get("comment_preview", "")).get("phone", "")


def item_request(item: Dict[str, Any], limit: int = 160) -> str:
    return clean_text(item.get("request_preview"), limit) or compact_request(item.get("comment_preview", ""), limit)


def detail_card(entity_label: str, item: Dict[str, Any]) -> list[str]:
    details = item.get("comment_details") if isinstance(item.get("comment_details"), dict) else {}
    if not details:
        details = extract_comment_details(item.get("comment_preview", ""))

    lines = [
        f"### {entity_label} {item.get('id') or '-'} — {clean_text(item.get('client') or item.get('title')) or 'без названия'}",
        "",
        f"- Название: {clean_text(item.get('title')) or '-'}",
        f"- Телефон: {item_phone(item) or '-'}",
    ]
    if item.get("entity_type") == "deal":
        lines.extend(
            [
                f"- Компания ID: {clean_text(item.get('company_id')) or '-'}",
                f"- Контакт ID: {clean_text(item.get('contact_id')) or '-'}",
                f"- Сумма: {clean_text(item.get('amount')) or '-'}",
                f"- Этап: {clean_text(item.get('stage_id')) or '-'}",
            ]
        )
    else:
        lines.append(f"- Статус: {clean_text(item.get('status_id')) or '-'}")

    lines.extend(
        [
            f"- Создан: {clean_text(item.get('date_create')) or '-'}",
            f"- Последняя активность: {clean_text(item.get('last_activity_time')) or '-'}",
            f"- Что есть: {clean_text(item.get('present')) or '-'}",
            "",
            "Заявка:",
            "",
            "```text",
            clean_text(details.get("request") or item.get("comment_preview")) or "-",
            "```",
            "",
        ]
    )
    if details.get("recording_url"):
        lines.extend([f"- Запись: {details['recording_url']}", ""])

    lines.extend(["Команда pipeline:", "", "```powershell", clean_text(item.get("pipeline_command")), "```", ""])
    return lines


def build_summary_markdown(summary: Dict[str, Any]) -> str:
    lead_ids = [item["id"] for item in summary["leads"] if item.get("id")]
    deal_ids = [item["id"] for item in summary["deals"] if item.get("id")]
    lines = [
        f"# Активные лиды и сделки по источнику {summary['source_id']}",
        "",
        f"- Период: последние {summary['days']} дней",
        f"- Дата с: {summary['date_from']}",
        f"- Сформировано: {summary['generated_at']}",
        f"- Лидов: {len(summary['leads'])}",
        f"- Сделок: {len(summary['deals'])}",
        "",
        "## Готовые команды",
        "",
    ]
    if lead_ids:
        lines.extend(["Лиды:", "", "```powershell", command_for_many("lead", lead_ids), "```", ""])
    else:
        lines.extend(["Лиды не найдены.", ""])
    if deal_ids:
        lines.extend(["Сделки:", "", "```powershell", command_for_many("deal", deal_ids), "```", ""])
    else:
        lines.extend(["Сделки не найдены.", ""])

    lines.extend(
        [
            "## Лиды",
            "",
            *markdown_table(
                ["ID", "Клиент", "Телефон", "Статус", "Создан", "Активность", "Заявка кратко"],
                [
                    [
                        item.get("id"),
                        item.get("client"),
                        item_phone(item),
                        item.get("status_id"),
                        item.get("date_create"),
                        item.get("last_activity_time"),
                        item_request(item),
                    ]
                    for item in summary["leads"]
                ],
            ),
            "",
            "## Сделки",
            "",
            *markdown_table(
                ["ID", "Название", "Телефон", "Сумма", "Этап", "Создана", "Активность", "Заявка кратко"],
                [
                    [
                        item.get("id"),
                        item.get("title"),
                        item_phone(item),
                        item.get("amount"),
                        item.get("stage_id"),
                        item.get("date_create"),
                        item.get("last_activity_time"),
                        item_request(item),
                    ]
                    for item in summary["deals"]
                ],
            ),
            "",
            "## Детали лидов",
            "",
        ]
    )
    for item in summary["leads"]:
        lines.extend(detail_card("Лид", item))
    if not summary["leads"]:
        lines.append("Активные лиды не найдены.")

    lines.extend(
        [
            "",
            "## Детали сделок",
            "",
        ]
    )
    for item in summary["deals"]:
        lines.extend(detail_card("Сделка", item))
    if not summary["deals"]:
        lines.append("Активные сделки не найдены.")

    lines.extend(
        [
            "",
            "## Команды по одному элементу",
            "",
            "### Лиды",
            "",
        ]
    )
    for item in summary["leads"]:
        lines.extend([f"- `{item['id']}`: `{item['pipeline_command']}`"])
    if not summary["leads"]:
        lines.append("- Нет активных лидов")

    lines.extend(["", "### Сделки", ""])
    for item in summary["deals"]:
        lines.extend([f"- `{item['id']}`: `{item['pipeline_command']}`"])
    if not summary["deals"]:
        lines.append("- Нет активных сделок")

    return "\n".join(lines) + "\n"


def save_source_summary(
    *,
    source_id: str,
    days: int,
    date_from: str,
    leads: List[Dict[str, Any]],
    deals: List[Dict[str, Any]],
    summary_root: Path,
) -> tuple[Path, Path]:
    summary_root.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    stamp = datetime.now(MSK_TZ).strftime("%Y-%m-%d_%H-%M-%S")
    summary = {
        "source_id": source_id,
        "days": days,
        "date_from": date_from,
        "generated_at": generated_at,
        "leads": [lead_summary(item) for item in leads],
        "deals": [deal_summary(item) for item in deals],
    }

    json_path = summary_root / f"source_{source_id}_{stamp}_summary.json"
    md_path = summary_root / f"source_{source_id}_{stamp}_summary.md"
    latest_json_path = summary_root / f"source_{source_id}_latest_summary.json"
    latest_md_path = summary_root / f"source_{source_id}_latest_summary.md"

    json_text = json.dumps(summary, ensure_ascii=False, indent=2)
    md_text = build_summary_markdown(summary)
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    latest_json_path.write_text(json_text, encoding="utf-8")
    latest_md_path.write_text(md_text, encoding="utf-8")
    return md_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Показать активные лиды и сделки Bitrix24 по SOURCE_ID за период",
    )
    parser.add_argument(
        "--source-id",
        default=DEFAULT_SOURCE_ID,
        help=f"SOURCE_ID источника. По умолчанию: {DEFAULT_SOURCE_ID}",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Период поиска в днях. По умолчанию: {DEFAULT_DAYS}",
    )
    parser.add_argument(
        "--summary-root",
        default=str(DEFAULT_SUMMARY_ROOT),
        help=f"Папка для summary.md/summary.json. По умолчанию: {DEFAULT_SUMMARY_ROOT}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    try:
        webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
    except ValueError as error:
        logger.error("Ошибка: %s", error)
        sys.exit(1)

    date_from = build_date_from(args.days)
    logger.info(
        "Поиск активных лидов и сделок: SOURCE_ID=%s, DATE_CREATE >= %s",
        args.source_id,
        date_from,
    )

    lead_filter = {
        "SOURCE_ID": args.source_id,
        "STATUS_SEMANTIC_ID": "P",
        ">=DATE_CREATE": date_from,
    }
    deal_filter = {
        "SOURCE_ID": args.source_id,
        "CLOSED": "N",
        ">=DATE_CREATE": date_from,
    }

    try:
        leads = fetch_crm_list(webhook_url, "crm.lead.list", lead_filter)
        deals = fetch_crm_list(webhook_url, "crm.deal.list", deal_filter)
    except (requests.RequestException, RuntimeError) as error:
        logger.error("Ошибка при запросе Bitrix24: %s", error)
        sys.exit(1)

    log_json_section("Активные лиды", leads)
    log_json_section("Активные сделки", deals)
    summary_md_path, summary_json_path = save_source_summary(
        source_id=args.source_id,
        days=args.days,
        date_from=date_from,
        leads=leads,
        deals=deals,
        summary_root=Path(args.summary_root),
    )
    logger.info("Краткий Markdown-отчет сохранен: %s", summary_md_path)
    logger.info("Краткий JSON-отчет сохранен: %s", summary_json_path)
    print(f"Краткий Markdown-отчет: {summary_md_path}")
    print(f"Краткий JSON-отчет: {summary_json_path}")


if __name__ == "__main__":
    main()
