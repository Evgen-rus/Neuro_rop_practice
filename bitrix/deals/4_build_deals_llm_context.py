"""
Build compact LLM context for deal analysis from raw Bitrix JSON.

The full customer_path.md remains the audit/UI artifact. This script creates a
smaller deterministic context for LLM prompts and removes repeated email quotes,
signatures, long URLs, and low-signal activity dumps.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import load_json
from setup import BASE_DIR, get_logger


DEFAULT_INPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"

logger = get_logger(__file__)


def load_deal_report_module() -> Any:
    path = Path(__file__).with_name("2_build_deals_customer_path_report.py")
    spec = importlib.util.spec_from_file_location("deal_customer_path_report", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load deal report module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deal_report = load_deal_report_module()

COMMERCIAL_KEYWORDS = (
    "КП",
    "ТКП",
    "ПТКП",
    "КОММЕРЧ",
    "СЧЕТ",
    "СЧЁТ",
    "ДОГОВОР",
    "ПРЕДЛОЖЕНИ",
    "OFFER",
    "PROPOSAL",
    "INVOICE",
    "CONTRACT",
    "ОПЛАТ",
    "ЦЕН",
    "ПРИНТЕР",
    "ИНОКС",
)

RISK_KEYWORDS = (
    "ЦЕН",
    "ДОРОГО",
    "НЕ УСТРОИЛ",
    "МЕХАНИК",
    "ЛПР",
    "СРОК",
    "РЕШЕНИ",
    "СЧЕТ",
    "СЧЁТ",
    "ДОГОВОР",
    "ОПЛАТ",
    "КП",
    "КОНКУР",
    "ИНОКС",
)

QUOTE_MARKERS = (
    "От кого:",
    "Кому:",
    "Дата:",
    "Тема:",
    "-----Original Message-----",
    "---------- Forwarded message",
    "From:",
    "Sent:",
    "To:",
    "Subject:",
)

SIGNATURE_MARKERS = (
    "\n--\n",
    "\n-- \n",
    "\nС уважением",
    "\nАлександр,\n",
    "\nИнженер-консультант",
    "\nООО \"ПрактикМ\"",
    "\nРабочий телефон:",
    "\nБесплатный номер",
    "\nНаши сайты:",
    "\nНаш YouTube:",
    "\nНаш RuTube:",
    "\nНаш Вконтакте:",
    "\nНаш Телеграм:",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact LLM contexts from raw deal JSON")
    parser.add_argument("--deal-ids", nargs="*", help="Deal IDs. If omitted, all deal_*_context.json files are used.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Raw JSON dir")
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT), help="Deal workspace root")
    return parser.parse_args()


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2", text, flags=re.I)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</div\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
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
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()
    if type_id == "2" or "CALL" in provider or "TELPHIN" in provider:
        return "call"
    if type_id == "4" or "EMAIL" in provider:
        return "email"
    if any(token in provider for token in ("IM", "OPENLINE", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")):
        return "message"
    if type_id == "6" or "TASK" in provider or "TODO" in provider:
        return "task"
    return "activity"


def activity_source_label(source: str, source_id: str | None = None) -> str:
    return f"{source}:{source_id}" if source_id else source


def merge_activities(
    activities_response: dict[str, Any] | None,
    details: dict[str, Any] | None,
    *,
    source: str,
    source_id: str | None = None,
) -> list[dict[str, Any]]:
    activities = result_items(activities_response)
    details = details or {}
    merged = []
    for activity in activities:
        activity_id = str(activity.get("ID") or "")
        detail_container = details.get(activity_id)
        detail = result_item(detail_container) if isinstance(detail_container, dict) else {}
        item = {**activity, **detail} if detail else dict(activity)
        item["_source"] = activity_source_label(source, source_id)
        merged.append(item)
    return merged


def all_activities(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    merged = merge_activities(
        bundle.get("activities"),
        bundle.get("activity_details"),
        source="deal",
        source_id=str(bundle.get("deal_id") or ""),
    )

    source_lead = bundle.get("source_lead") or {}
    source_lead_id = str(source_lead.get("lead_id") or "")
    if source_lead:
        merged.extend(
            merge_activities(
                source_lead.get("activities"),
                source_lead.get("activity_details"),
                source="source_lead",
                source_id=source_lead_id,
            )
        )
    return sorted(
        merged,
        key=lambda item: (
            item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE") or "",
            int(item.get("ID") or 0),
        ),
    )


def timeline_comments(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for source, attempts in (
        ("deal", bundle.get("timeline_comments") or []),
        ("source_lead", (bundle.get("source_lead") or {}).get("timeline_comments") or []),
    ):
        for attempt in attempts:
            for item in result_items(attempt):
                identity = (
                    str(item.get("ID") or ""),
                    str(item.get("CREATED") or item.get("DATE_CREATE") or ""),
                    clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"), 500),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                row = dict(item)
                row["_source"] = source
                rows.append(row)
    return sorted(rows, key=lambda item: (item.get("CREATED") or "", int(item.get("ID") or 0)))


def strip_quoted_history(text: str) -> str:
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    positions = [text.find(marker) for marker in QUOTE_MARKERS if text.find(marker) > 0]
    lines = text.splitlines()
    offset = 0
    for index, line in enumerate(lines):
        cleaned = line.strip()
        if re.match(r"^(пн|вт|ср|чт|пт|сб|вс),?\s+\d{1,2}\s+.+?\s+\d{4}.*(<[^>]+>|@).*>?:?\s*$", cleaned, flags=re.I):
            positions.append(offset)
        if re.match(r"^.+?\d{4}.*(<[^>]+>|@).*>?:?\s*$", cleaned):
            positions.append(offset)
        offset += len(line) + 1
    if positions:
        return text[: min(positions)].rstrip()
    return text


def strip_email_signature(text: str) -> str:
    positions = []
    for marker in SIGNATURE_MARKERS:
        idx = text.lower().find(marker.lower())
        if idx > 0:
            positions.append(idx)
    if not positions:
        return text
    return text[: min(positions)].rstrip()


def compact_urls(text: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlparse(url)
        domain = parsed.netloc or "unknown"
        return f"[url_domain={domain}]"

    return re.sub(r"https?://[^\s\]\[<>)\"']+", replace_url, text)


def attachment_type(value: Any) -> str:
    text = str(value).lower()
    if any(ext in text for ext in (".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return "image"
    if any(ext in text for ext in (".mp4", ".mov", ".avi", ".webm")):
        return "video"
    if ".pdf" in text:
        return "pdf"
    if any(ext in text for ext in (".doc", ".docx")):
        return "doc"
    if any(ext in text for ext in (".xls", ".xlsx")):
        return "spreadsheet"
    return "file"


def compact_files(files: Any) -> str:
    if not isinstance(files, list) or not files:
        return "-"
    rows = []
    for item in files[:8]:
        if isinstance(item, dict):
            file_id = item.get("id") or item.get("ID") or item.get("fileId")
            rows.append(f"file_id={file_id or '-'} type={attachment_type(item)}")
        else:
            rows.append(f"type={attachment_type(item)}")
    if len(files) > 8:
        rows.append(f"+{len(files) - 8} files")
    return "; ".join(rows)


def compact_email_body(value: Any, limit: int = 500) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = strip_quoted_history(text)
    text = strip_email_signature(text)
    text = compact_urls(text)
    text = clean_text(text)
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def direction_label(value: Any) -> str:
    direction = str(value or "")
    return {"1": "incoming", "2": "outgoing", "0": "unknown"}.get(direction, direction or "unknown")


def contains_keywords(value: Any, keywords: tuple[str, ...]) -> bool:
    text = clean_text(value).upper()
    return any(keyword in text for keyword in keywords)


def contact_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for contact_id, response in bundle.get("contacts", {}).items():
        contact = result_item(response)
        name = " ".join(
            part for part in [contact.get("NAME"), contact.get("SECOND_NAME"), contact.get("LAST_NAME")] if part
        )
        rows.append(
            {
                "id": str(contact_id),
                "name": clean_text(name) or "-",
                "phone": ", ".join(item.get("VALUE", "") for item in contact.get("PHONE", []) if isinstance(item, dict)),
                "email": ", ".join(item.get("VALUE", "") for item in contact.get("EMAIL", []) if isinstance(item, dict)),
            }
        )
    return rows


def contact_role(contact: dict[str, Any]) -> str:
    text = f"{contact.get('name', '')} {contact.get('email', '')}".lower()
    if "механик" in text:
        return "механик"
    if "инженер" in text:
        return "инженер"
    if "конвейер" in text:
        return "технический контакт"
    return "контакт"


def comments_section(comments: list[dict[str, Any]], limit: int = 10) -> list[str]:
    if not comments:
        return ["- Комментарии таймлайна не найдены."]
    selected = comments[-limit:]
    rows = []
    for item in selected:
        text = clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"), 700)
        if not text:
            continue
        rows.append(
            f"- {item.get('CREATED') or '-'} source={item.get('_source') or 'deal'} "
            f"comment_id={item.get('ID') or '-'} author={item.get('AUTHOR_ID') or '-'}: {text}"
        )
    return rows or ["- Значимые комментарии не найдены."]


def need_summary(comments: list[dict[str, Any]], emails: list[dict[str, Any]]) -> list[str]:
    sources = []
    for item in comments[-5:]:
        text = clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"), 500)
        if text:
            sources.append(f"- Из комментария {item.get('CREATED') or '-'}: {text}")
    for item in emails[-5:]:
        body = compact_email_body(item.get("DESCRIPTION"), 300)
        if body:
            sources.append(f"- Из email {item.get('ID')}: {body}")
    return sources[:8] or ["- Потребность явно не извлечена автоматически; см. комментарии и письма выше."]


def commercial_section(bundle: dict[str, Any], activities: list[dict[str, Any]]) -> list[str]:
    rows = []
    product_rows = result_items(bundle.get("product_rows"))
    if product_rows:
        rows.append(f"- Товарные строки: {len(product_rows)}")
    for attempt in bundle.get("invoice_attempts") or []:
        items = result_items(attempt if isinstance(attempt, dict) else None)
        if items:
            rows.append(f"- Счета/смарт-процессы: найдено {len(items)} записей")
    for item in activities:
        if contains_keywords(item, COMMERCIAL_KEYWORDS):
            rows.append(
                f"- {item.get('START_TIME') or item.get('CREATED') or '-'} source={item.get('_source') or 'deal'} "
                f"{activity_type(item)} id={item.get('ID')}: "
                f"{clean_text(item.get('SUBJECT') or item.get('DESCRIPTION'), 260)}"
            )
    for ref in bundle.get("file_and_recording_refs") or []:
        if contains_keywords(ref, COMMERCIAL_KEYWORDS):
            rows.append(
                f"- file_ref path={clean_text(ref.get('path'), 120)} key={clean_text(ref.get('key'), 60)} "
                f"type={attachment_type(ref.get('value'))}"
            )
    return rows[:18] or ["- Коммерческие события явно не найдены."]


def grouped_email_rows(emails: list[dict[str, Any]]) -> list[str]:
    if not emails:
        return ["- Письма не найдены."]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in emails:
        day = str(item.get("START_TIME") or item.get("CREATED") or "")[:10] or "unknown"
        by_day[day].append(item)

    rows: list[str] = []
    for day in sorted(by_day):
        day_items = by_day[day]
        media_items = [
            item
            for item in day_items
            if contains_keywords(item.get("SUBJECT"), ("ФОТО", "ВИДЕО", "PHOTO", "VIDEO"))
            or attachment_type(item.get("FILES")) in {"image", "video"}
        ]
        if len(media_items) >= 2:
            ids = ", ".join(str(item.get("ID")) for item in media_items[:8])
            rows.append(f"- {day}: группа писем с фото/видео, count={len(media_items)}, email_ids={ids}")

        for item in day_items:
            if item in media_items and len(media_items) >= 2:
                continue
            body = compact_email_body(item.get("DESCRIPTION"), 500)
            files = compact_files(item.get("FILES"))
            if not body and files == "-":
                rows.append(
                    f"- {item.get('START_TIME') or item.get('CREATED') or '-'} email_id={item.get('ID')} "
                    f"direction={direction_label(item.get('DIRECTION'))} subject={clean_text(item.get('SUBJECT'), 120)}; body=empty"
                )
                continue
            rows.append(
                f"- {item.get('START_TIME') or item.get('CREATED') or '-'} email_id={item.get('ID')} "
                f"direction={direction_label(item.get('DIRECTION'))} subject={clean_text(item.get('SUBJECT'), 120)}; "
                f"files={files}; body={body or 'empty'}"
            )
    return rows[-35:]


def compact_call_rows(calls: list[dict[str, Any]], transcripts_dir: Path | None = None) -> list[str]:
    rows = [f"- Всего звонков: {len(calls)}"]
    auto = [
        item
        for item in calls
        if contains_keywords(item.get("SUBJECT") or item.get("DESCRIPTION"), ("АВТООТВЕТ", "НЕДОЗВ", "НЕ ОТВЕТ"))
    ]
    rows.append(f"- Автоответчики/недозвоны по теме: {len(auto)}")
    if transcripts_dir and transcripts_dir.exists():
        transcript_count = len([path for path in transcripts_dir.glob("*.md") if path.is_file()])
        rows.append(f"- Транскрипций в workspace: {transcript_count}")
    else:
        rows.append("- Транскрипций в workspace: 0")
    rows.append("- Последний meaningful contact: не определен автоматически; см. комментарии и транскрипт.")
    rows.append("")
    rows.append("| Дата | Источник | ID | Тема | Статус |")
    rows.append("|---|---|---:|---|---|")
    for item in calls[-8:]:
        status = "завершено" if str(item.get("COMPLETED", "")).upper() in ("Y", "1", "TRUE") else "открыто/неясно"
        rows.append(
            f"| {md_escape(item.get('START_TIME') or item.get('CREATED'))} | {md_escape(item.get('_source') or 'deal')} | {md_escape(item.get('ID'))} | "
            f"{md_escape(item.get('SUBJECT'))} | {status} |"
        )
    return rows


def significant_activities(activities: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    important = [item for item in activities if contains_keywords(item, COMMERCIAL_KEYWORDS)]
    latest = activities[-limit:]
    by_id = {str(item.get("ID")): item for item in important + latest}
    return sorted(
        by_id.values(),
        key=lambda item: (item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE") or "", int(item.get("ID") or 0)),
    )


def activity_rows(items: list[dict[str, Any]]) -> list[str]:
    rows = ["| Дата | Источник | Тип | ID | Тема | Статус |", "|---|---|---|---:|---|---|"]
    for item in items:
        status = "завершено" if str(item.get("COMPLETED", "")).upper() in ("Y", "1", "TRUE") else "открыто/неясно"
        rows.append(
            f"| {md_escape(item.get('START_TIME') or item.get('CREATED') or item.get('DEADLINE'))} | "
            f"{md_escape(item.get('_source') or 'deal')} | {activity_type(item)} | {md_escape(item.get('ID'))} | "
            f"{md_escape(clean_text(item.get('SUBJECT') or item.get('DESCRIPTION'), 180))} | {status} |"
        )
    return rows if len(rows) > 2 else ["- Значимые активности не найдены."]


def risks_section(comments: list[dict[str, Any]], emails: list[dict[str, Any]], activities: list[dict[str, Any]]) -> list[str]:
    rows = []
    sources = []
    for item in comments:
        sources.append(("comment", item.get("ID"), item.get("CREATED"), item.get("COMMENT") or item.get("TEXT")))
    for item in emails:
        sources.append(("email", item.get("ID"), item.get("START_TIME") or item.get("CREATED"), item.get("DESCRIPTION")))
    for item in activities:
        sources.append(("activity", item.get("ID"), item.get("START_TIME") or item.get("CREATED"), item.get("SUBJECT") or item.get("DESCRIPTION")))

    for source_type, source_id, when, text in sources:
        cleaned = compact_email_body(text, 260) if source_type == "email" else clean_text(text, 260)
        if cleaned and contains_keywords(cleaned, RISK_KEYWORDS):
            rows.append(f"- {source_type} {source_id} {when}: {cleaned}")
    rows = rows[-12:]
    rows.append("- Проверить срок решения, ЛПР и следующий шаг к договору/счету/оплате, если они явно не зафиксированы.")
    return rows


def build_llm_context(bundle: dict[str, Any], workspace_root: Path) -> str:
    deal = bundle.get("deal", {}).get("item", {}) or {}
    deal_id = str(deal.get("ID") or bundle.get("deal_id") or "")
    stage_info = bundle.get("stage_info") or {}
    stage = stage_info.get("stage") or {}
    pipeline = stage_info.get("pipeline") or {}
    company = result_item(bundle.get("company")) if bundle.get("company") else {}
    contacts = contact_rows(bundle)
    activities = all_activities(bundle)
    comments = timeline_comments(bundle)
    calls = [item for item in activities if activity_type(item) == "call"]
    emails = [item for item in activities if activity_type(item) == "email"]
    messages = [item for item in activities if activity_type(item) == "message"]
    next_step = deal_report.choose_next_step(bundle)
    transcripts_dir = workspace_root / f"deal_{deal_id}" / "transcripts"

    primary_contact = contacts[0] if contacts else None
    technical_contacts = [item for item in contacts[1:] if contact_role(item) != "контакт"]
    other_contacts = [item for item in contacts[1:] if contact_role(item) == "контакт"]

    lines = [
        f"# Compact LLM context по сделке {deal_id}",
        "",
        "## Что исключено из compact context",
        "",
        "- Полные email quotes.",
        "- Подписи менеджера и технические footer-блоки.",
        "- Длинные ссылки вложений.",
        "- Технические повторы полей От кого/Кому/Дата/Тема.",
        "- Полный список незначимых активностей.",
        "",
        "## 1. Карточка сделки",
        "",
        f"- Название: {clean_text(deal.get('TITLE')) or '-'}",
        f"- ID сделки: {deal_id}",
        f"- Исходный лид: {deal.get('LEAD_ID') or '-'}",
        f"- Воронка: {pipeline.get('name') or deal.get('CATEGORY_ID') or '-'}",
        f"- Этап: {stage.get('name') or deal.get('STAGE_ID') or '-'} (`{deal.get('STAGE_ID') or '-'}`)",
        f"- Сумма: {deal.get('OPPORTUNITY') or '-'} {deal.get('CURRENCY_ID') or ''}".rstrip(),
        f"- Ответственный: {deal.get('ASSIGNED_BY_ID') or '-'}",
        f"- Создана: {deal.get('DATE_CREATE') or '-'}",
        f"- Изменена: {deal.get('DATE_MODIFY') or '-'}",
        "",
        "## 2. Компания и контакты",
        "",
        f"- Компания: {clean_text(company.get('TITLE')) or '-'} (ID: {deal.get('COMPANY_ID') or '-'})",
    ]

    if primary_contact:
        lines.append(
            f"- Основной контакт: {primary_contact['name']} (ID: {primary_contact['id']}), "
            f"телефон: {primary_contact['phone'] or '-'}, email: {primary_contact['email'] or '-'}"
        )
    if technical_contacts:
        lines.append(
            "- Технические контакты: "
            + "; ".join(f"{item['name']} ({contact_role(item)}, ID: {item['id']})" for item in technical_contacts)
        )
    if other_contacts:
        lines.append("- Прочие контакты: " + ", ".join(f"{item['name']} (ID: {item['id']})" for item in other_contacts))

    lines.extend(
        [
            "",
            "## 3. Текущий следующий шаг",
            "",
            next_step,
            "",
            "## 4. Комментарии менеджера / ключевые факты",
            "",
            *comments_section(comments),
            "",
            "## 5. Потребность и текущая суть сделки",
            "",
            *need_summary(comments, emails),
            "",
            "## 6. Коммерческие события",
            "",
            *commercial_section(bundle, activities),
            "",
            "## 7. Email-лента compact",
            "",
            *grouped_email_rows(emails),
            "",
            "## 8. Звонки compact",
            "",
            *compact_call_rows(calls, transcripts_dir),
            "",
            "## 9. Сообщения / открытые линии compact",
            "",
            *activity_rows(messages[-10:]),
            "",
            "## 10. Последние / значимые активности",
            "",
            *activity_rows(significant_activities(activities)),
            "",
            "## 11. Открытые вопросы и риски",
            "",
            *risks_section(comments, emails, activities),
        ]
    )
    return "\n".join(lines) + "\n"


def input_files(input_dir: Path, deal_ids: list[str] | None) -> list[Path]:
    if deal_ids:
        return [input_dir / f"deal_{deal_id}_context.json" for deal_id in deal_ids]
    return sorted(input_dir.glob("deal_*_context.json"))


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir)
    workspace_root = Path(args.workspace_root)
    for raw_path in input_files(input_dir, args.deal_ids):
        if not raw_path.exists():
            logger.warning("Raw bundle not found: %s", raw_path)
            continue
        bundle = load_json(raw_path)
        deal_id = str(bundle.get("deal_id") or raw_path.stem.replace("deal_", "").replace("_context", ""))
        output_path = workspace_root / f"deal_{deal_id}" / "history" / f"deal_{deal_id}_llm_context.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(build_llm_context(bundle, workspace_root), encoding="utf-8")
        logger.info("Saved compact deal LLM context: %s", output_path)
        print(f"LLM context saved: {output_path}")


if __name__ == "__main__":
    main()
