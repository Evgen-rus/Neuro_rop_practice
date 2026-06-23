"""
Step 2. Build Markdown customer-path reports from raw Bitrix24 JSON bundles.
"""

from __future__ import annotations

import argparse
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import load_json
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_INPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "markdown"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2: build Markdown reports from raw Bitrix24 deal context",
    )
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help=f"Raw JSON dir. Default: {DEFAULT_INPUT_DIR}")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help=f"Markdown dir. Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help=f"Call audio manifest dir. Default: {DEFAULT_AUDIO_DIR}")
    parser.add_argument("--deal-ids", nargs="*", help="Optional deal IDs. If omitted, all deal_*_context.json files are used.")
    return parser.parse_args()


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
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


def readable_value(value: Any, limit: int | None = None) -> str:
    return clean_text(unquote(str(value)) if value is not None else "", limit)


def md_escape(value: Any) -> str:
    text = clean_text(value)
    return text.replace("|", "\\|") if text else "-"


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def result_items(call_container: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_container:
        return []
    if isinstance(call_container.get("items"), list):
        return call_container["items"]
    if call_container.get("ok"):
        result = call_container.get("response", {}).get("result", [])
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
    return []


def get_contact_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for contact_id, response in bundle.get("contacts", {}).items():
        contact = result_item(response)
        rows.append(
            {
                "id": contact_id,
                "name": " ".join(
                    part for part in [contact.get("NAME"), contact.get("SECOND_NAME"), contact.get("LAST_NAME")] if part
                ),
                "phone": ", ".join(item.get("VALUE", "") for item in contact.get("PHONE", []) if isinstance(item, dict)),
                "email": ", ".join(item.get("VALUE", "") for item in contact.get("EMAIL", []) if isinstance(item, dict)),
            }
        )
    return rows


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
    if type_id == "6" or "TASK" in provider:
        return "task"
    return "activity"


def all_activities(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    activities = result_items(bundle.get("activities"))
    details = bundle.get("activity_details", {})
    merged = []
    for activity in activities:
        activity_id = str(activity.get("ID") or "")
        detail = result_item(details.get(activity_id, {}).get("response") if isinstance(details.get(activity_id), dict) else None)
        merged.append({**activity, **detail} if detail else activity)

    return sorted(
        merged,
        key=lambda item: (
            item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE") or "",
            int(item.get("ID") or 0),
        ),
    )


def timeline_comment_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    for attempt in bundle.get("timeline_comments", []):
        items = result_items(attempt)
        if items:
            return items
    return []


def extract_urls_from_text(value: Any) -> list[str]:
    text = str(value or "")
    urls = re.findall(r"https?://[^\s\]\[<>)\"']+", text)
    return [html.unescape(url).rstrip(".,;") for url in urls]


def walk_text_urls(value: Any, path: str = "") -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            refs.extend(walk_text_urls(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.extend(walk_text_urls(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        for url in extract_urls_from_text(value):
            refs.append({"path": path, "url": url, "context": clean_text(value, 260)})
    return refs


def compact_activity(item: dict[str, Any]) -> str:
    parts = [
        f"ID {item.get('ID')}" if item.get("ID") else "",
        item.get("START_TIME") or item.get("CREATED") or "",
        item.get("SUBJECT") or "",
        item.get("DESCRIPTION") or "",
    ]
    return " | ".join(clean_text(part, 180) for part in parts if clean_text(part))


def compact_file_ref(ref: dict[str, Any]) -> str:
    value = ref.get("value")
    if isinstance(value, dict):
        name = value.get("name") or value.get("fileName") or value.get("filename") or value.get("id")
        url = value.get("urlDownload") or value.get("urlShow") or value.get("url")
        return " | ".join(part for part in [readable_value(name), readable_value(url, 260)] if part)
    if isinstance(value, list):
        return "; ".join(compact_file_ref({"value": item}) for item in value if isinstance(item, dict))
    return readable_value(value, 300)


def file_refs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    seen = set()
    for ref in bundle.get("file_and_recording_refs", []):
        path = str(ref.get("path") or "")
        key = str(ref.get("key") or "").upper()
        value = ref.get("value")
        if "NOT_LOADED_ATTACHMENTS" in path:
            continue
        if not ("FILE" in key or "ATTACH" in key or "URL" in key or "DOWNLOAD" in key or str(value).startswith("http")):
            continue
        summary = compact_file_ref(ref)
        identity = summary
        if summary and identity not in seen:
            seen.add(identity)
            refs.append({"path": path, "summary": summary, "raw": ref})
    return refs


def find_kp_related(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    keywords = ("КП", "ТКП", "ПТКП", "КОММЕРЧ", "PROPOSAL", "OFFER", "СЧЕТ", "СЧЁТ")
    matches: list[dict[str, Any]] = []
    for item in all_activities(bundle):
        text = clean_text(item)
        if any(keyword in text.upper() for keyword in keywords):
            matches.append({"source": "activity", "summary": compact_activity(item)})

    for ref in file_refs(bundle):
        text = readable_value(ref.get("summary"))
        if any(keyword in text.upper() for keyword in keywords) or "timeline_comments" in ref.get("path", ""):
            matches.append({"source": "file", "summary": text})

    product_rows = result_items(bundle.get("product_rows"))
    if product_rows:
        matches.append({"source": "product_rows", "summary": f"Товарных строк: {len(product_rows)}"})
    return matches


def find_audio_refs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in file_refs(bundle):
        text = clean_text(ref).upper()
        if any(token in text for token in ("RECORD", "AUDIO", ".MP3", ".WAV", ".M4A", "CALL")):
            refs.append(ref)
    for ref in walk_text_urls(bundle.get("deal", {}).get("item", {}), "deal.item"):
        context = clean_text(ref.get("context")).upper()
        url = str(ref.get("url") or "")
        path = str(ref.get("path") or "")
        if "ССЫЛКА НА ЗАПИСЬ" in context or "ЗАПИС" in context or ("COMMENTS" in path and "disk.yandex.ru" in url):
            refs.append({"path": ref.get("path"), "summary": readable_value(ref.get("url"), 300), "raw": ref})
    return refs


def choose_next_step(bundle: dict[str, Any]) -> str:
    open_items = []
    for activity in all_activities(bundle):
        if str(activity.get("COMPLETED", "")).upper() in ("N", "0", "FALSE", ""):
            open_items.append(activity)

    if open_items:
        first = sorted(open_items, key=lambda item: item.get("DEADLINE") or item.get("START_TIME") or "")[0]
        subject = first.get("SUBJECT") or first.get("DESCRIPTION") or "активность без названия"
        deadline = first.get("DEADLINE") or first.get("START_TIME") or "срок не указан"
        return f"В Bitrix есть открытая активность: {clean_text(subject, 220)}. Срок/дата: {deadline}."

    comments = timeline_comment_items(bundle)
    if comments:
        last = comments[-1]
        text = clean_text(last.get("COMMENT") or last.get("TEXT") or last.get("DESCRIPTION"), 260)
        if text:
            return f"Явной открытой задачи не найдено. Аналитически следующий шаг нужно определить по последнему комментарию: {text}"

    activities = all_activities(bundle)
    if activities:
        last = activities[-1]
        text = clean_text(last.get("SUBJECT") or last.get("DESCRIPTION"), 260)
        return f"Явной открытой задачи не найдено. Аналитически ориентируюсь на последнюю активность: {text or 'описания нет'}."

    return "Явного следующего шага в выгрузке не найдено. Нужно проверить карточку сделки вручную."


def activity_table(items: list[dict[str, Any]], max_description: int = 180) -> list[str]:
    lines = ["| Дата | Тип | ID | Тема | Статус |", "|---|---:|---:|---|---|"]
    for item in items:
        when = item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE") or "-"
        status = "завершено" if str(item.get("COMPLETED", "")).upper() in ("Y", "1", "TRUE") else "открыто/неясно"
        subject = item.get("SUBJECT") or item.get("DESCRIPTION") or ""
        lines.append(
            f"| {md_escape(when)} | {activity_type(item)} | {md_escape(item.get('ID'))} | {md_escape(clean_text(subject, max_description))} | {status} |"
        )
    if len(lines) == 2:
        lines.append("| - | - | - | Не найдено | - |")
    return lines


def load_audio_manifest(audio_dir: Path, deal_id: str) -> dict[str, Any]:
    path = audio_dir / f"deal_{deal_id}_call_audio_manifest.json"
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def audio_status_by_activity(manifest: dict[str, Any]) -> dict[str, str]:
    rows = {}
    for call in manifest.get("calls", []):
        activity_id = str(call.get("activity_id") or "")
        downloads = call.get("downloads") or []
        local_paths = [item.get("local_path") for item in downloads if item.get("ok") and item.get("local_path")]
        if local_paths:
            rows[activity_id] = ", ".join(local_paths)
        elif call.get("status") == "no_files_in_crm_activity":
            rows[activity_id] = "нет файла в CRM-активности"
        elif downloads:
            statuses = []
            for item in downloads:
                status = audio_status_label(item.get("status") or "не скачано")
                error = item.get("disk_file_get_error") or item.get("error")
                detail = f"{status}: {error}" if error else status
                manual_url = item.get("url")
                if manual_url:
                    detail = f"{detail}; вручную: [скачать]({manual_url})"
                statuses.append(detail)
            rows[activity_id] = "; ".join(statuses)
        else:
            rows[activity_id] = audio_status_label(call.get("status") or "не проверено")
    return rows


def audio_status_label(status: str) -> str:
    labels = {
        "downloaded": "скачано",
        "not_downloaded": "не скачано",
        "no_files_in_crm_activity": "нет файла в CRM-активности",
        "download_returned_html_auth_required": "ссылка требует авторизации",
        "download_http_error": "ошибка HTTP при скачивании",
        "download_request_error": "ошибка сетевого запроса",
        "no_download_url": "нет ссылки скачивания",
    }
    return labels.get(status, status)


def call_table(items: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    audio_by_id = audio_status_by_activity(manifest)
    lines = ["| Дата | Тип | ID | Тема | Статус | Аудио |", "|---|---:|---:|---|---|---|"]
    for item in items:
        when = item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE") or "-"
        status = "завершено" if str(item.get("COMPLETED", "")).upper() in ("Y", "1", "TRUE") else "открыто/неясно"
        subject = item.get("SUBJECT") or item.get("DESCRIPTION") or ""
        activity_id = str(item.get("ID") or "")
        audio_status = audio_by_id.get(activity_id, "не проверено")
        lines.append(
            f"| {md_escape(when)} | {activity_type(item)} | {md_escape(activity_id)} | {md_escape(clean_text(subject, 180))} | {status} | {md_escape(audio_status)} |"
        )
    if len(lines) == 2:
        lines.append("| - | - | - | Не найдено | - | - |")
    return lines


def audio_section(manifest: dict[str, Any]) -> list[str]:
    lines = ["| Звонок | Статус | Файл / причина |", "|---:|---|---|"]
    for call in manifest.get("calls", []):
        activity_id = call.get("activity_id") or "-"
        downloads = call.get("downloads") or []
        if not downloads:
            lines.append(f"| {activity_id} | {md_escape(audio_status_label(call.get('status') or ''))} | - |")
            continue
        for item in downloads:
            detail = item.get("local_path") or item.get("disk_file_get_error") or item.get("error") or "-"
            if item.get("url"):
                detail = f"{detail}; вручную: [скачать]({item.get('url')})"
            lines.append(f"| {activity_id} | {md_escape(audio_status_label(item.get('status') or ''))} | {md_escape(detail)} |")
    if len(lines) == 2:
        lines.append("| - | не проверено | Манифест скачивания аудио не найден |")
    return lines


def email_text_section(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    if not items:
        lines.append("Текст писем не найден.")
        return lines

    for item in items:
        body = clean_text(item.get("DESCRIPTION"), 2200)
        lines.extend(
            [
                f"#### Email {item.get('ID') or '-'} — {clean_text(item.get('SUBJECT')) or '-'}",
                "",
                f"- Дата: {item.get('START_TIME') or item.get('CREATED') or '-'}",
                f"- Направление Bitrix: {item.get('DIRECTION') or '-'}",
                "",
            ]
        )
        if body:
            lines.extend(["```text", body, "```", ""])
        else:
            lines.extend(["Тело письма пустое или недоступно в `DESCRIPTION`.", ""])

        files = item.get("FILES")
        if files:
            lines.append(f"Вложения: {readable_value(files, 500)}")
            lines.append("")

    lines.append("Примечание: текст очищен от HTML. Если письмо содержит цепочку ответов, Bitrix часто возвращает ее целиком вместе с цитатами и подписями.")
    return lines


def comments_section(comments: list[dict[str, Any]]) -> list[str]:
    lines = ["| Дата | Автор | Комментарий |", "|---|---:|---|"]
    for item in comments:
        text = item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION") or item.get("FILES")
        lines.append(
            f"| {md_escape(item.get('CREATED') or item.get('DATE_CREATE'))} | {md_escape(item.get('AUTHOR_ID') or item.get('CREATED_BY'))} | {md_escape(clean_text(text, 300))} |"
        )
    if len(lines) == 2:
        lines.append("| - | - | Комментарии таймлайна не найдены или метод недоступен текущему вебхуку |")
    return lines


def refs_table(refs: list[dict[str, Any]]) -> list[str]:
    lines = ["| Источник | Значение |", "|---|---|"]
    for ref in refs:
        value = ref.get("summary") if isinstance(ref, dict) else ref
        if not value and isinstance(ref, dict):
            value = ref.get("value")
        lines.append(f"| {md_escape(ref.get('path'))} | {md_escape(readable_value(value, 320))} |")
    if len(lines) == 2:
        lines.append("| - | Не найдено |")
    return lines


def build_report(bundle: dict[str, Any], audio_manifest: dict[str, Any] | None = None) -> str:
    audio_manifest = audio_manifest or {}
    deal = bundle.get("deal", {}).get("item", {})
    stage_info = bundle.get("stage_info") or {}
    stage = stage_info.get("stage") or {}
    pipeline = stage_info.get("pipeline") or {}
    company = result_item(bundle.get("company")) if bundle.get("company") else {}
    contacts = get_contact_rows(bundle)
    activities = all_activities(bundle)
    comments = timeline_comment_items(bundle)
    calls = [item for item in activities if activity_type(item) == "call"]
    emails = [item for item in activities if activity_type(item) == "email"]
    messages = [item for item in activities if activity_type(item) == "message"]
    kp_items = find_kp_related(bundle)
    audio_refs = find_audio_refs(bundle)

    lines = [
        f"# Путь клиента по сделке #{deal.get('ID') or bundle.get('deal_id')}",
        "",
        f"Отчет собран: {datetime.now(MSK_TZ).isoformat(timespec='seconds')}",
        "",
        "## 1. Карточка сделки",
        "",
        f"- Название: {clean_text(deal.get('TITLE')) or '-'}",
        f"- ID сделки: {deal.get('ID') or bundle.get('deal_id')}",
        f"- Воронка: {pipeline.get('name') or deal.get('CATEGORY_ID') or '-'}",
        f"- Этап: {stage.get('name') or deal.get('STAGE_ID') or '-'} (`{deal.get('STAGE_ID') or '-'}`)",
        f"- Сумма: {deal.get('OPPORTUNITY') or '-'} {deal.get('CURRENCY_ID') or ''}".rstrip(),
        f"- Источник: {deal.get('SOURCE_ID') or '-'}",
        f"- Ответственный: {deal.get('ASSIGNED_BY_ID') or '-'}",
        f"- Создана: {deal.get('DATE_CREATE') or '-'}",
        f"- Изменена: {deal.get('DATE_MODIFY') or '-'}",
        "",
        "## 2. Клиент и контакт",
        "",
        f"- Компания: {clean_text(company.get('TITLE')) or '-'} (ID: {deal.get('COMPANY_ID') or '-'})",
    ]

    if contacts:
        for contact in contacts:
            lines.extend(
                [
                    f"- Контакт: {contact['name'] or '-'} (ID: {contact['id']})",
                    f"  Телефон: {contact['phone'] or '-'}",
                    f"  Email: {contact['email'] or '-'}",
                ]
            )
    else:
        lines.append("- Контакт: не найден")

    lines.extend(
        [
            "",
            "## 3. Последний следующий шаг",
            "",
            choose_next_step(bundle),
            "",
            "## 4. Комментарии менеджера / таймлайн",
            "",
            *comments_section(comments),
            "",
            "## 5. Звонки",
            "",
            *call_table(calls, audio_manifest),
            "",
            "## 6. Письма и сообщения",
            "",
            "### Письма",
            "",
            *activity_table(emails),
            "",
            "### Текст писем",
            "",
            *email_text_section(emails),
            "",
            "### Сообщения / открытые линии",
            "",
            *activity_table(messages),
            "",
            "## 7. КП / ТКП / счета / товары",
            "",
        ]
    )

    if kp_items:
        lines.extend(["| Источник | Найденный фрагмент |", "|---|---|"])
        for item in kp_items:
            lines.append(f"| {md_escape(item.get('source'))} | {md_escape(readable_value(item.get('summary'), 360))} |")
    else:
        lines.append("КП/ТКП в выгрузке явно не найдено. Возможные места: файлы активности, письма, комментарии или пользовательские поля.")

    lines.extend(
        [
            "",
            "## 8. Аудио звонков / записи",
            "",
            *audio_section(audio_manifest),
            "",
            "### Прочие ссылки на записи",
            "",
            *refs_table(audio_refs),
            "",
            "## 9. Полная история активностей",
            "",
            *activity_table(activities, max_description=240),
            "",
            "## 10. Технические замечания",
            "",
            "- Скрипт ничего не менял в Bitrix, только читал данные через REST API.",
            "- Если в разделе комментариев, файлов или записей пусто, это может означать отсутствие данных или недостаточные права вебхука на конкретный метод.",
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
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for path in input_files(input_dir, args.deal_ids):
        if not path.exists():
            logger.warning("Raw bundle not found: %s", path)
            continue
        bundle = load_json(path)
        deal_id = bundle.get("deal_id") or path.stem.replace("deal_", "").replace("_context", "")
        audio_manifest = load_audio_manifest(Path(args.audio_dir), str(deal_id))
        output_path = output_dir / f"deal_{deal_id}_customer_path.md"
        output_path.write_text(build_report(bundle, audio_manifest), encoding="utf-8")
        logger.info("Saved Markdown report: %s", output_path)


if __name__ == "__main__":
    main()
