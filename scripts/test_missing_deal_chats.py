"""
Read-only diagnostic for missing deal chat sources.

Checks whether Wazzup-like client messages and internal manager/ROP chats are
visible through the current Bitrix24 webhook for one deal. The script does not
change the main pipeline, database, or Bitrix data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, as_list, get_env_required, save_json
from setup import MSK_TZ


DEAL_OWNER_TYPE_ID = 2
CONTACT_OWNER_TYPE_ID = 3

WAZZUP_TOKENS = (
    "WAZZUP",
    "WAZZUP24",
    "WHATSAPP",
    "WHATS APP",
    "WA:",
    "WA_",
    "OPENLINE",
    "OPEN_LINE",
    "IMOPENLINES",
    "6882-4828",
)

INTERNAL_CHAT_TOKENS = (
    "ЧАТ",
    "CHAT",
    "ОБСУЖД",
    "DISCUSS",
    "IM:",
    "DIALOG",
    "MESSENGER",
    "РОП",
    "ROP",
)

TEXT_KEYS = (
    "ID",
    "TYPE_ID",
    "PROVIDER_ID",
    "PROVIDER_TYPE_ID",
    "PROVIDER_GROUP_ID",
    "SUBJECT",
    "DESCRIPTION",
    "COMMENT",
    "TEXT",
    "MESSAGE",
    "TITLE",
    "ENTITY_TYPE",
    "ENTITY_TYPE_ID",
    "OWNER_TYPE_ID",
    "OWNER_ID",
    "AUTHOR_ID",
    "RESPONSIBLE_ID",
    "CREATED",
    "DATE_CREATE",
    "START_TIME",
    "CHAT_ID",
    "DIALOG_ID",
    "ENTITY_ID",
    "ENTITY_TYPE",
)


def is_real_id(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text != "0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Wazzup and internal chat visibility for one Bitrix deal.",
    )
    parser.add_argument("--deal-id", required=True, help="Bitrix deal ID to check.")
    parser.add_argument(
        "--preview-limit",
        type=int,
        default=8,
        help="Max suspicious records to show in the Markdown report. Default: 8.",
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Optional output prefix in project root. Default: missing_deal_chats_<deal_id>.",
    )
    parser.add_argument(
        "--bitrix-timeout",
        type=int,
        default=12,
        help="HTTP timeout per Bitrix request in seconds. Default: 12.",
    )
    return parser.parse_args()


def get_result(call_result: dict[str, Any] | None) -> Any:
    if not call_result or not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def result_items(call_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_result:
        return []
    if isinstance(call_result.get("items"), list):
        return [item for item in call_result["items"] if isinstance(item, dict)]
    result = get_result(call_result)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("items", "messages", "chats", "users", "files"):
            if isinstance(result.get(key), list):
                return [item for item in result[key] if isinstance(item, dict)]
        return [item for item in result.values() if isinstance(item, dict)]
    return []


def result_value(call_result: dict[str, Any] | None) -> Any:
    if not call_result or not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def safe_text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def clean_message_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[USER=\d+(?:\s+[^\]]+)?\](.*?)\[/USER\]", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"\[(?:/)?(?:b|i|u|s)\]", "", text, flags=re.I)
    text = re.sub(r"\[br\s*/?\]", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def compact_json(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def searchable_text(value: Any) -> str:
    if isinstance(value, dict):
        parts = []
        for key, child in value.items():
            if str(key).upper() in TEXT_KEYS or isinstance(child, (dict, list)):
                parts.append(str(key))
                parts.append(searchable_text(child))
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(searchable_text(item) for item in value)
    return str(value or "")


def contains_any(value: Any, tokens: tuple[str, ...]) -> bool:
    text = searchable_text(value).upper()
    return any(token in text for token in tokens)


def classify_error(error: str | None) -> str:
    text = str(error or "").lower()
    if not text:
        return "нет ошибки"
    if (
        "access denied" in text
        or "higher privileges" in text
        or "insufficient" in text
        or "permission" in text
        or "доступ" in text
    ):
        return "нет прав"
    if "method" in text and ("not found" in text or "unknown" in text or "does not exist" in text):
        return "метод недоступен"
    if "required" in text or "invalid" in text or "incorrect" in text:
        return "метод доступен, но параметры не подошли"
    return "ошибка метода"


def call_status(response: dict[str, Any]) -> dict[str, Any]:
    items = result_items(response)
    return {
        "method": response.get("method"),
        "ok": bool(response.get("ok")),
        "items": len(items),
        "error": response.get("error"),
        "reason": None if response.get("ok") else classify_error(response.get("error")),
        "payload": response.get("payload"),
    }


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for activity in activities:
        activity_id = activity.get("ID") or activity.get("id")
        if activity_id:
            details[str(activity_id)] = client.safe_call("crm.activity.get", {"id": activity_id})
    return details


def merge_detail(activity: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or activity.get("id") or "")
    detail_response = details.get(activity_id)
    detail = get_result(detail_response)
    if isinstance(detail, dict):
        return {**activity, **detail}
    return dict(activity)


def fetch_timeline_comments(client: BitrixReadOnlyClient, entity_type: str, owner_type_id: int, entity_id: str) -> list[dict[str, Any]]:
    payload = {
        "order": {"CREATED": "ASC", "ID": "ASC"},
        "filter": {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id},
    }
    return [client.safe_list_all("crm.timeline.comment.list", payload)]


def fetch_entity_history(client: BitrixReadOnlyClient, entity_type: str, owner_type_id: int, entity_id: str) -> dict[str, Any]:
    activities = client.safe_list_all(
        "crm.activity.list",
        {
            "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
            "filter": {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": entity_id},
            "select": ["*", "UF_*"],
        },
    )
    activity_items = result_items(activities)
    details = fetch_activity_details(client, activity_items)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "activities": activities,
        "activity_details": details,
        "timeline_comments": fetch_timeline_comments(client, entity_type, owner_type_id, entity_id),
    }


def extract_contact_ids(client: BitrixReadOnlyClient, deal_id: str, deal: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    contact_ids = {str(item).strip() for item in as_list(deal.get("CONTACT_ID")) if is_real_id(item)}
    contact_items = client.safe_call("crm.deal.contact.items.get", {"id": deal_id})
    result = get_result(contact_items)
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and is_real_id(item.get("CONTACT_ID")):
                contact_ids.add(str(item["CONTACT_ID"]).strip())
    return sorted(contact_ids), contact_items


def extract_phone_candidates(*values: Any) -> list[str]:
    phones: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).upper() == "PHONE":
                    walk(child)
                elif key in {"TITLE", "COMMENTS", "SOURCE_DESCRIPTION"}:
                    walk(child)
                elif isinstance(child, (dict, list)):
                    walk(child)
            return
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        text = str(value or "")
        for match in re.findall(r"(?:\+?\d[\d\s().-]{8,}\d)", text):
            digits = re.sub(r"\D+", "", match)
            if len(digits) == 11 and digits.startswith("8"):
                digits = "7" + digits[1:]
            if len(digits) >= 10:
                phones.add(digits)

    for value in values:
        walk(value)
    return sorted(phones)


def unique_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(payload)
    return unique


def extract_chat_candidates_from_search(responses: list[dict[str, Any]], relevant_tokens: tuple[str, ...]) -> list[dict[str, str]]:
    candidates: dict[str, dict[str, str]] = {}
    for response in responses:
        if response.get("method") != "im.search.chat.list":
            continue
        for item in result_items(response):
            chat_id = str(item.get("id") or item.get("ID") or "").strip()
            if not chat_id:
                continue
            title = safe_text(item.get("name") or item.get("NAME") or item.get("title") or item.get("TITLE"), 200)
            entity_id = str(item.get("entity_id") or item.get("ENTITY_ID") or "")
            haystack = " ".join([chat_id, title, entity_id])
            if relevant_tokens and not any(token and token in haystack for token in relevant_tokens):
                continue
            candidates[f"chat{chat_id}"] = {
                "chat_id": chat_id,
                "dialog_id": f"chat{chat_id}",
                "title": title,
                "entity_id": entity_id,
            }
    return list(candidates.values())


def read_im_dialogs(
    client: BitrixReadOnlyClient,
    responses: list[dict[str, Any]],
    relevant_tokens: tuple[str, ...],
) -> list[dict[str, Any]]:
    dialog_responses: list[dict[str, Any]] = []
    for candidate in extract_chat_candidates_from_search(responses, relevant_tokens):
        dialog_id = candidate.get("dialog_id")
        chat_id = candidate.get("chat_id")
        if not chat_id or not dialog_id:
            continue
        dialog_responses.append(client.safe_call("im.chat.get", {"CHAT_ID": chat_id}))
        dialog_responses.append(
            client.safe_call(
                "im.dialog.messages.get",
                {
                    "DIALOG_ID": dialog_id,
                    "LIMIT": 50,
                },
            )
        )
        dialog_responses.append(client.safe_call("im.dialog.users.list", {"DIALOG_ID": dialog_id}))
    return dialog_responses


def bitrix_im_probe(client: BitrixReadOnlyClient, deal_id: str, deal: dict[str, Any], phone_candidates: list[str]) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    title = safe_text(deal.get("TITLE"), 200)
    relevant_tokens = tuple(token for token in (deal_id, title, *phone_candidates) if token)
    search_terms = [
        title,
        f"Сделка: {title}" if title else "",
        deal_id,
        *phone_candidates,
    ]
    for term in [item for item in search_terms if item]:
        for payload in unique_payloads(
            [
                {"FIND": term},
            ]
        ):
            responses.append(client.safe_call("im.search.chat.list", payload))

    responses.extend(read_im_dialogs(client, responses, relevant_tokens))
    return responses


def add_suspicious(
    rows: list[dict[str, Any]],
    *,
    source: str,
    item: dict[str, Any],
    reason: str,
) -> None:
    rows.append(
        {
            "source": source,
            "reason": reason,
            "id": item.get("ID") or item.get("id"),
            "when": (
                item.get("CREATED")
                or item.get("DATE_CREATE")
                or item.get("START_TIME")
                or item.get("DEADLINE")
                or item.get("date")
            ),
            "subject": safe_text(
                item.get("SUBJECT")
                or item.get("TITLE")
                or item.get("COMMENT")
                or item.get("TEXT")
                or item.get("text")
                or item.get("message"),
                220,
            ),
            "provider": {
                "PROVIDER_ID": item.get("PROVIDER_ID"),
                "PROVIDER_TYPE_ID": item.get("PROVIDER_TYPE_ID"),
                "PROVIDER_GROUP_ID": item.get("PROVIDER_GROUP_ID"),
                "TYPE_ID": item.get("TYPE_ID"),
            },
            "preview": compact_json(item, 900),
        }
    )


def analyze_sources(raw: dict[str, Any]) -> dict[str, Any]:
    wazzup_hits: list[dict[str, Any]] = []
    internal_hits: list[dict[str, Any]] = []
    method_statuses: list[dict[str, Any]] = []

    for entity_key, history in raw.get("entity_histories", {}).items():
        activities = result_items(history.get("activities"))
        details = history.get("activity_details") or {}
        method_statuses.append({**call_status(history.get("activities") or {}), "source": f"{entity_key}:activities"})
        for activity in activities:
            item = merge_detail(activity, details)
            if contains_any(item, WAZZUP_TOKENS):
                add_suspicious(wazzup_hits, source=f"{entity_key}:crm.activity", item=item, reason="activity содержит Wazzup/OpenLines/WhatsApp признаки")
            if contains_any(item, INTERNAL_CHAT_TOKENS):
                add_suspicious(internal_hits, source=f"{entity_key}:crm.activity", item=item, reason="activity содержит признаки внутреннего чата/обсуждения")

        for activity_id, detail_response in details.items():
            method_statuses.append({**call_status(detail_response), "source": f"{entity_key}:crm.activity.get:{activity_id}"})

        for idx, response in enumerate(history.get("timeline_comments") or [], start=1):
            method_statuses.append({**call_status(response), "source": f"{entity_key}:timeline_comments_attempt_{idx}"})
            for item in result_items(response):
                if contains_any(item, WAZZUP_TOKENS):
                    add_suspicious(wazzup_hits, source=f"{entity_key}:crm.timeline.comment", item=item, reason="timeline comment содержит Wazzup/OpenLines/WhatsApp признаки")
                if contains_any(item, INTERNAL_CHAT_TOKENS):
                    add_suspicious(internal_hits, source=f"{entity_key}:crm.timeline.comment", item=item, reason="timeline comment содержит признаки внутреннего чата/обсуждения")

    for response in raw.get("timeline_probe") or []:
        method_statuses.append({**call_status(response), "source": "timeline_probe"})
        for item in result_items(response):
            if contains_any(item, WAZZUP_TOKENS):
                add_suspicious(wazzup_hits, source=str(response.get("method")), item=item, reason="timeline probe содержит Wazzup/OpenLines/WhatsApp признаки")
            if contains_any(item, INTERNAL_CHAT_TOKENS):
                add_suspicious(internal_hits, source=str(response.get("method")), item=item, reason="timeline probe содержит признаки внутреннего чата/обсуждения")

    for response in raw.get("openlines_im_probe") or []:
        method_statuses.append({**call_status(response), "source": "openlines_im_probe"})
        for item in result_items(response):
            if contains_any(item, WAZZUP_TOKENS):
                add_suspicious(wazzup_hits, source=str(response.get("method")), item=item, reason="OpenLines/IM probe содержит Wazzup/OpenLines/WhatsApp признаки")
            if contains_any(item, INTERNAL_CHAT_TOKENS):
                add_suspicious(internal_hits, source=str(response.get("method")), item=item, reason="OpenLines/IM probe содержит признаки внутреннего чата/обсуждения")

    for response in raw.get("bitrix_im_probe") or []:
        method_statuses.append({**call_status(response), "source": "bitrix_im_probe"})
        result = result_value(response)
        deal = result_value(raw.get("deal")) if isinstance(raw.get("deal"), dict) else {}
        deal_tokens = tuple(
            token
            for token in (
                str(raw.get("deal_id") or ""),
                safe_text(deal.get("TITLE") if isinstance(deal, dict) else ""),
            )
            if token
        )
        if contains_any(result, WAZZUP_TOKENS):
            for item in result_items(response) or ([result] if isinstance(result, dict) else []):
                add_suspicious(wazzup_hits, source=str(response.get("method")), item=item, reason="Bitrix IM содержит Wazzup/OpenLines/WhatsApp признаки")
        if contains_any(result, INTERNAL_CHAT_TOKENS) or (deal_tokens and contains_any(result, deal_tokens)):
            for item in result_items(response) or ([result] if isinstance(result, dict) else []):
                add_suspicious(internal_hits, source=str(response.get("method")), item=item, reason="Bitrix IM содержит признаки чата сделки или сообщения")

    return {
        "wazzup_visible": bool(wazzup_hits),
        "internal_chat_visible": bool(internal_hits),
        "wazzup_hits": wazzup_hits,
        "internal_chat_hits": internal_hits,
        "method_statuses": method_statuses,
    }


def summarize_checked_places(analysis: dict[str, Any]) -> list[str]:
    grouped: dict[str, dict[str, Any]] = {}
    for status in analysis.get("method_statuses") or []:
        key = str(status.get("method") or "unknown")
        row = grouped.setdefault(key, {"ok_attempts": 0, "items": 0, "errors": {}})
        if status.get("ok"):
            row["ok_attempts"] += 1
            row["items"] += int(status.get("items") or 0)
        else:
            reason = str(status.get("reason") or "ошибка")
            row["errors"][reason] = row["errors"].get(reason, 0) + 1

    places = []
    for method, row in sorted(grouped.items()):
        parts = []
        if row["ok_attempts"]:
            parts.append(f"ok attempts={row['ok_attempts']}, items={row['items']}")
        for reason, count in sorted(row["errors"].items()):
            parts.append(f"{reason}={count}")
        places.append(f"{method}: {', '.join(parts) if parts else 'нет результата'}")
    return places


def method_reason_summary(analysis: dict[str, Any]) -> list[str]:
    errors: dict[str, int] = {}
    for status in analysis.get("method_statuses") or []:
        if status.get("ok"):
            continue
        reason = str(status.get("reason") or "ошибка")
        errors[reason] = errors.get(reason, 0) + 1
    return [f"{reason}: {count}" for reason, count in sorted(errors.items())]


def render_hits(hits: list[dict[str, Any]], limit: int) -> list[str]:
    if not hits:
        return ["не найдено записей с явными признаками источника"]
    lines = []
    def priority(hit: dict[str, Any]) -> tuple[int, str]:
        source = str(hit.get("source") or "")
        subject = str(hit.get("subject") or "")
        if source == "im.dialog.messages.get" and subject:
            return (0, str(hit.get("when") or ""))
        if source == "im.search.chat.list":
            return (1, str(hit.get("when") or ""))
        return (2, str(hit.get("when") or ""))

    sorted_hits = sorted(hits, key=priority)
    for hit in sorted_hits[:limit]:
        lines.append(
            f"{hit.get('source')} id={hit.get('id') or '-'} date={hit.get('when') or '-'}; "
            f"{hit.get('reason')}; preview={hit.get('subject') or hit.get('preview')}"
        )
    if len(sorted_hits) > limit:
        lines.append(f"... еще {len(sorted_hits) - limit} записей в JSON")
    return lines


def render_report(deal_id: str, raw: dict[str, Any], analysis: dict[str, Any], preview_limit: int) -> str:
    deal = get_result(raw.get("deal")) if isinstance(raw.get("deal"), dict) else {}
    contact_ids = raw.get("contact_ids") or []
    phone_candidates = raw.get("phone_candidates") or []
    checked_places = summarize_checked_places(analysis)
    reasons = method_reason_summary(analysis)

    if analysis["wazzup_visible"]:
        wazzup_conclusion = "Wazzup/WhatsApp/OpenLines-похожие записи видны через Bitrix. Нужно вручную подтвердить, что это именно Wazzup-переписка, по preview/JSON."
        wazzup_next = "после подтверждения добавить отдельный источник в основной pipeline отдельной задачей"
    else:
        wazzup_conclusion = "Через текущий Bitrix-доступ явная Wazzup-переписка не найдена."
        wazzup_next = "запросить у клиента Wazzup API token и проверить историческую выгрузку сообщений; связывать со сделкой по телефону клиента из Bitrix"

    if analysis["internal_chat_visible"]:
        chat_conclusion = "Найдены записи Bitrix IM с признаками чата сделки или сообщения. Нужно вручную проверить JSON и подтвердить полноту истории."
        chat_next = "если сообщения прочитались через im.dialog.messages.get, можно отдельно проектировать включение этого источника в customer history"
    else:
        chat_conclusion = "Привязанный внутренний чат менеджер/РОП через текущие CRM/timeline/OpenLines/IM проверки не найден."
        chat_next = "проверить в интерфейсе Bitrix, существует ли отдельный чат; если существует, запросить права webhook/user на im/imopenlines или подтвердить, что чат не является частью CRM-истории"

    lines = [
        f"Проверка по сделке ID: {deal_id}",
        "",
        f"Название сделки: {safe_text(deal.get('TITLE')) if isinstance(deal, dict) else ''}",
        f"Контакты сделки: {', '.join(contact_ids) if contact_ids else 'не найдены'}",
        f"Телефоны-кандидаты для связки Wazzup: {', '.join(phone_candidates) if phone_candidates else 'не найдены'}",
        f"Дата проверки: {datetime.now(MSK_TZ).isoformat(timespec='seconds')}",
        "",
        "1. Wazzup-переписка:",
        f"- видим через Bitrix: {'да' if analysis['wazzup_visible'] else 'нет'}",
        "- где проверяли:",
    ]
    lines.extend(f"  - {place}" for place in checked_places)
    lines.extend(
        [
            "- что нашли:",
            *[f"  - {line}" for line in render_hits(analysis["wazzup_hits"], preview_limit)],
            f"- вывод: {wazzup_conclusion}",
            f"- следующий шаг, если не видим: {wazzup_next}",
            "",
            "2. Внутренний чат менеджер/РОП:",
            f"- видим через Bitrix: {'да' if analysis['internal_chat_visible'] else 'нет'}",
            "- где проверяли:",
        ]
    )
    lines.extend(f"  - {place}" for place in checked_places)
    lines.extend(
        [
            "- что нашли:",
            *[f"  - {line}" for line in render_hits(analysis["internal_chat_hits"], preview_limit)],
            f"- вывод: {chat_conclusion}",
            f"- следующий шаг, если не видим: {chat_next}",
            "",
            "3. Что нужно запросить у клиента:",
            "- Подтвердить, должна ли Wazzup-переписка отображаться в Bitrix CRM timeline/activity или хранится только в Wazzup.",
            "- Если Wazzup не виден через Bitrix: Wazzup API token, список подключенных каналов, период выгрузки и разрешение на историческую выгрузку сообщений.",
            "- Подтвердить телефон клиента, по которому связывать Wazzup-историю со сделкой.",
            "- Подтвердить, есть ли у сделки отдельный внутренний чат менеджер/РОП в Bitrix.",
            "- Если чат есть: выдать webhook/user права на методы im/imopenlines или указать REST-метод, которым этот чат доступен.",
            "",
            "Технические причины по недоступным методам:",
        ]
    )
    lines.extend(f"- {reason}" for reason in (reasons or ["нет ошибок методов"]))
    return "\n".join(lines) + "\n"


def extract_internal_chat_messages(raw: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for response in raw.get("bitrix_im_probe") or []:
        if response.get("method") != "im.dialog.messages.get" or not response.get("ok"):
            continue
        for item in result_items(response):
            if not isinstance(item, dict):
                continue
            messages.append(item)

    def sort_key(item: dict[str, Any]) -> tuple[str, int]:
        item_id = item.get("id") or item.get("ID") or 0
        try:
            numeric_id = int(item_id)
        except (TypeError, ValueError):
            numeric_id = 0
        return (str(item.get("date") or item.get("DATE_CREATE") or ""), numeric_id)

    return sorted(messages, key=sort_key)


def extract_internal_chat_users(raw: dict[str, Any]) -> dict[str, str]:
    users: dict[str, str] = {}
    for response in raw.get("bitrix_im_probe") or []:
        if response.get("method") != "im.dialog.users.list" or not response.get("ok"):
            continue
        for item in result_items(response):
            user_id = str(item.get("id") or item.get("ID") or item.get("user_id") or "").strip()
            if not user_id:
                continue
            name = (
                item.get("name")
                or item.get("NAME")
                or " ".join(
                    part
                    for part in (
                        item.get("first_name") or item.get("FIRST_NAME"),
                        item.get("last_name") or item.get("LAST_NAME"),
                    )
                    if part
                )
            )
            users[user_id] = safe_text(name or f"User {user_id}", 120)
    return users


def extract_internal_chat_meta(raw: dict[str, Any]) -> dict[str, Any]:
    for response in raw.get("bitrix_im_probe") or []:
        if response.get("method") == "im.search.chat.list" and response.get("ok"):
            for item in result_items(response):
                entity_id = str(item.get("entity_id") or item.get("ENTITY_ID") or "")
                if entity_id == f"DEAL|{raw.get('deal_id')}":
                    return item
    return {}


def render_internal_chat_transcript(deal_id: str, raw: dict[str, Any]) -> str:
    deal = get_result(raw.get("deal")) if isinstance(raw.get("deal"), dict) else {}
    meta = extract_internal_chat_meta(raw)
    users = extract_internal_chat_users(raw)
    messages = extract_internal_chat_messages(raw)

    lines = [
        f"# Внутренний чат сделки {deal_id}",
        "",
        f"Сделка: {safe_text(deal.get('TITLE')) if isinstance(deal, dict) else ''}",
        f"Чат: {safe_text(meta.get('name') or meta.get('NAME')) if meta else 'не найден'}",
        f"chat_id: {meta.get('id') or meta.get('ID') or ''}",
        f"entity_id: {meta.get('entity_id') or meta.get('ENTITY_ID') or ''}",
        f"Сообщений: {len(messages)}",
        "",
    ]

    for message in messages:
        author_id = str(message.get("author_id") or message.get("AUTHOR_ID") or "").strip()
        author = users.get(author_id) or f"User {author_id}" if author_id else "System"
        date = message.get("date") or message.get("DATE_CREATE") or ""
        text = clean_message_text(message.get("text") or message.get("message") or "")
        files = message.get("files") if isinstance(message.get("files"), list) else []
        attach = message.get("attach") if isinstance(message.get("attach"), list) else []

        lines.append(f"## {date} — {author}")
        if text:
            lines.append(text)
        elif files or attach:
            lines.append("[вложение/служебное сообщение]")
        else:
            lines.append("[пустое или системное сообщение]")
        if files:
            lines.append("")
            lines.append("Файлы:")
            for file_item in files:
                if isinstance(file_item, dict):
                    lines.append(f"- {safe_text(file_item.get('name') or file_item.get('id'), 200)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    args = parse_args()
    deal_id = str(args.deal_id).strip()
    load_dotenv()
    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"), timeout=args.bitrix_timeout)

    deal_response = client.safe_call("crm.deal.get", {"id": deal_id})
    deal = get_result(deal_response)
    if not isinstance(deal, dict):
        raise SystemExit(f"Could not fetch deal {deal_id}: {deal_response.get('error')}")

    contact_ids, deal_contact_items = extract_contact_ids(client, deal_id, deal)
    contacts = {contact_id: client.safe_call("crm.contact.get", {"id": contact_id}) for contact_id in contact_ids}
    phone_candidates = extract_phone_candidates(deal, *[get_result(response) for response in contacts.values()])

    entity_histories: dict[str, Any] = {
        f"deal:{deal_id}": fetch_entity_history(client, "deal", DEAL_OWNER_TYPE_ID, deal_id),
    }
    for contact_id in contact_ids:
        entity_histories[f"contact:{contact_id}"] = fetch_entity_history(
            client,
            "contact",
            CONTACT_OWNER_TYPE_ID,
            contact_id,
        )

    raw = {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "read_only": True,
        "deal_id": deal_id,
        "deal": deal_response,
        "contact_ids": contact_ids,
        "phone_candidates": phone_candidates,
        "deal_contact_items": deal_contact_items,
        "contacts": contacts,
        "entity_histories": entity_histories,
        "bitrix_im_probe": bitrix_im_probe(client, deal_id, deal, phone_candidates),
    }
    analysis = analyze_sources(raw)
    report = render_report(deal_id, raw, analysis, args.preview_limit)
    internal_chat = render_internal_chat_transcript(deal_id, raw)

    output_prefix = args.output_prefix or f"missing_deal_chats_{deal_id}"
    report_path = PROJECT_ROOT / f"{output_prefix}_report.md"
    json_path = PROJECT_ROOT / f"{output_prefix}_raw.json"
    internal_chat_path = PROJECT_ROOT / f"{output_prefix}_internal_chat.md"
    report_path.write_text(report, encoding="utf-8")
    internal_chat_path.write_text(internal_chat, encoding="utf-8")
    save_json(json_path, {"raw": raw, "analysis": analysis})

    print(report)
    print(f"Сохранено: {report_path}")
    print(f"Сохранено: {json_path}")
    print(f"Сохранено: {internal_chat_path}")


if __name__ == "__main__":
    main()
