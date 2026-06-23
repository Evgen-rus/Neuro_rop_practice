"""
Build Markdown customer-path reports from raw Bitrix24 lead JSON bundles.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import load_json, save_json
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_INPUT_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"
DEFAULT_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "markdown"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Markdown reports from raw Bitrix24 lead context")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Raw lead JSON dir")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Markdown output dir")
    parser.add_argument("--lead-ids", nargs="*", help="Lead IDs. If omitted, all lead_*_context.json files are used.")
    return parser.parse_args()


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def md_escape(value: Any) -> str:
    text = clean_text(value)
    return text.replace("|", "\\|").replace("\n", "<br>") if text else "-"


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def result_items(call_container: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_container:
        return []
    if isinstance(call_container.get("items"), list):
        return [item for item in call_container["items"] if isinstance(item, dict)]
    if call_container.get("ok"):
        result = call_container.get("response", {}).get("result", [])
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
    return []


def activity_type(activity: dict[str, Any]) -> str:
    type_id = str(activity.get("TYPE_ID") or "")
    provider = " ".join(str(activity.get(key) or "") for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT")).upper()
    if type_id == "2" or "CALL" in provider or "ИСХОДЯЩ" in provider:
        return "call"
    if type_id == "4" or "EMAIL" in provider:
        return "email"
    if any(token in provider for token in ("IM", "OPENLINE", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")):
        return "message"
    if type_id == "6" or "TODO" in provider or "TASK" in provider:
        return "task"
    return "activity"


def lead_phone(lead: dict[str, Any]) -> str:
    phones = lead.get("PHONE") or []
    if isinstance(phones, list):
        return ", ".join(str(item.get("VALUE", "")) for item in phones if isinstance(item, dict) and item.get("VALUE"))
    return str(phones or "")


def lead_email(lead: dict[str, Any]) -> str:
    emails = lead.get("EMAIL") or []
    if isinstance(emails, list):
        return ", ".join(str(item.get("VALUE", "")) for item in emails if isinstance(item, dict) and item.get("VALUE"))
    return str(emails or "")


def timeline_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for attempt in bundle.get("timeline_comments", []):
        rows.extend(result_items(attempt))
    return rows


def activity_details(bundle: dict[str, Any], activity: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or "")
    detail = bundle.get("activity_details", {}).get(activity_id)
    return result_item(detail) if isinstance(detail, dict) else {}


def file_refs(bundle: dict[str, Any]) -> list[dict[str, str]]:
    refs = []
    seen = set()
    for ref in bundle.get("file_and_recording_refs", []):
        path = str(ref.get("path") or "")
        key = str(ref.get("key") or "")
        summary = format_ref_value(ref.get("value"), 500)
        if summary and summary not in seen:
            refs.append({"path": path, "key": key, "summary": summary})
            seen.add(summary)
    return refs


def format_ref_value(value: Any, limit: int | None = None) -> str:
    if isinstance(value, list):
        parts = [format_ref_value(item) for item in value]
        text = "; ".join(part for part in parts if part)
    elif isinstance(value, dict):
        preferred_keys = ("id", "ID", "name", "NAME", "url", "URL", "DOWNLOAD_URL")
        parts = [f"{key}={value[key]}" for key in preferred_keys if value.get(key)]
        text = "; ".join(parts) if parts else json.dumps(value, ensure_ascii=False)
    else:
        text = clean_text(value)
    text = clean_text(text)
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def render_lead_markdown(bundle: dict[str, Any]) -> str:
    lead = result_item(bundle.get("lead"))
    lead_id = str(bundle.get("lead_id") or lead.get("ID") or "")
    activities = result_items(bundle.get("activities"))
    calls = [item for item in activities if activity_type(item) == "call"]
    tasks = [item for item in activities if activity_type(item) == "task"]
    emails = [item for item in activities if activity_type(item) == "email"]
    messages = [item for item in activities if activity_type(item) == "message"]
    timeline = timeline_items(bundle)
    refs = file_refs(bundle)

    lines = [
        f"# Путь клиента по лиду #{lead_id}",
        "",
        f"Отчет собран: {datetime.now(MSK_TZ).isoformat()}",
        "",
        "## 1. Карточка лида",
        "",
        f"- Название: {clean_text(lead.get('TITLE')) or '-'}",
        f"- ID лида: {lead_id}",
        f"- Имя: {clean_text(lead.get('NAME')) or '-'}",
        f"- Компания: {clean_text(lead.get('COMPANY_TITLE')) or '-'}",
        f"- Статус: {clean_text(lead.get('STATUS_ID')) or '-'}",
        f"- Семантика статуса: {clean_text(lead.get('STATUS_SEMANTIC_ID')) or '-'}",
        f"- Источник: {clean_text(lead.get('SOURCE_ID')) or '-'}",
        f"- Ответственный: {clean_text(lead.get('ASSIGNED_BY_ID')) or '-'}",
        f"- Сумма: {clean_text(lead.get('OPPORTUNITY')) or '-'} {clean_text(lead.get('CURRENCY_ID')) or ''}".rstrip(),
        f"- Создан: {clean_text(lead.get('DATE_CREATE')) or '-'}",
        f"- Изменен: {clean_text(lead.get('DATE_MODIFY')) or '-'}",
        f"- Закрыт: {clean_text(lead.get('DATE_CLOSED')) or '-'}",
        "",
        "## 2. Контакты",
        "",
        f"- Телефон: {lead_phone(lead) or '-'}",
        f"- Email: {lead_email(lead) or '-'}",
        "",
        "## 3. Комментарий / заявка",
        "",
        "```text",
        clean_text(lead.get("COMMENTS")) or "Комментарий не найден",
        "```",
        "",
        "## 4. Звонки",
        "",
    ]

    if calls:
        lines.extend(["| Дата | ID | Тема | Статус | Файлы |", "|---|---:|---|---|---|"])
        for call in calls:
            detail = activity_details(bundle, call)
            files = detail.get("FILES") or call.get("FILES") or []
            file_text = "; ".join(str(item.get("id") or item.get("ID") or item) for item in files if isinstance(item, dict)) or "-"
            lines.append(
                f"| {md_escape(call.get('START_TIME') or call.get('CREATED'))} | {md_escape(call.get('ID'))} | "
                f"{md_escape(call.get('SUBJECT'))} | {md_escape(call.get('STATUS'))} | {md_escape(file_text)} |"
            )
    else:
        lines.append("Звонки не найдены.")

    lines.extend(["", "## 5. Задачи", ""])
    if tasks:
        lines.extend(["| Дата | ID | Тема | Статус |", "|---|---:|---|---|"])
        for task in tasks:
            lines.append(
                f"| {md_escape(task.get('START_TIME') or task.get('DEADLINE') or task.get('CREATED'))} | "
                f"{md_escape(task.get('ID'))} | {md_escape(task.get('SUBJECT'))} | {md_escape(task.get('STATUS'))} |"
            )
    else:
        lines.append("Задачи не найдены.")

    lines.extend(["", "## 6. Письма и сообщения", ""])
    if emails or messages:
        lines.extend(["| Дата | Тип | ID | Тема |", "|---|---|---:|---|"])
        for item in emails + messages:
            lines.append(
                f"| {md_escape(item.get('START_TIME') or item.get('CREATED'))} | {activity_type(item)} | "
                f"{md_escape(item.get('ID'))} | {md_escape(item.get('SUBJECT'))} |"
            )
    else:
        lines.append("Письма и сообщения не найдены.")

    lines.extend(["", "## 7. Комментарии таймлайна", ""])
    if timeline:
        lines.extend(["| Дата | Автор | Комментарий |", "|---|---:|---|"])
        for item in timeline:
            lines.append(
                f"| {md_escape(item.get('CREATED'))} | {md_escape(item.get('AUTHOR_ID'))} | {md_escape(item.get('COMMENT'))} |"
            )
    else:
        lines.append("Комментарии таймлайна не найдены.")

    lines.extend(["", "## 8. Файлы / записи / ссылки", ""])
    if refs:
        lines.extend(["| Источник | Ключ | Значение |", "|---|---|---|"])
        for ref in refs:
            lines.append(f"| {md_escape(ref['path'])} | {md_escape(ref['key'])} | {md_escape(ref['summary'])} |")
    else:
        lines.append("Файлы и ссылки не найдены.")

    lines.extend(
        [
            "",
            "## 9. Технические замечания",
            "",
            "- Скрипт ничего не менял в Bitrix, только читал данные через REST API.",
            "- Markdown по лидам является первым тестовым форматом и может уточняться после проверки разных лидов.",
        ]
    )
    return "\n".join(lines) + "\n"


def input_paths(input_dir: Path, lead_ids: list[str] | None) -> list[Path]:
    if lead_ids:
        return [input_dir / f"lead_{lead_id}_context.json" for lead_id in lead_ids]
    return sorted(input_dir.glob("lead_*_context.json"))


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    index_items = []
    for raw_path in input_paths(input_dir, args.lead_ids):
        if not raw_path.exists():
            logger.warning("Raw lead bundle not found: %s", raw_path)
            continue
        bundle = load_json(raw_path)
        lead_id = str(bundle.get("lead_id") or raw_path.stem.removeprefix("lead_").removesuffix("_context"))
        output_path = output_dir / f"lead_{lead_id}_customer_path.md"
        output_path.write_text(render_lead_markdown(bundle), encoding="utf-8")
        index_items.append({"lead_id": lead_id, "output_path": str(output_path)})
        logger.info("Saved lead markdown report: %s", output_path)

    save_json(output_dir / "index.json", {"generated_at": datetime.now(MSK_TZ).isoformat(), "items": index_items})


if __name__ == "__main__":
    main()
