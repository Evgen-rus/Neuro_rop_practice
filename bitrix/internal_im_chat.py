"""
Read-only helpers for Bitrix internal IM chats bound to CRM entities.

This module deliberately keeps Bitrix IM chat messages separate from client
touchpoints. The resulting rows are meant for `internal_context`.
"""

from __future__ import annotations

import re
from typing import Any

from bitrix.client import BitrixReadOnlyClient


CRM_ENTITY_TYPE = "CRM"


def get_result(call_result: dict[str, Any] | None) -> Any:
    if not call_result or not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def result_items(call_result: dict[str, Any] | None, keys: tuple[str, ...] = ("items",)) -> list[dict[str, Any]]:
    if not call_result:
        return []
    if isinstance(call_result.get("items"), list):
        return [item for item in call_result["items"] if isinstance(item, dict)]
    result = get_result(call_result)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in keys:
            if isinstance(result.get(key), list):
                return [item for item in result[key] if isinstance(item, dict)]
        return [item for item in result.values() if isinstance(item, dict)]
    return []


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


def clean_inline(value: Any, limit: int | None = None) -> str:
    text = clean_message_text(value)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def clean_file_name(value: Any, limit: int | None = None) -> str:
    text = clean_inline(value, limit)
    return re.sub(r"\s+(\.[A-Za-zА-Яа-я0-9]{1,12})$", r"\1", text)


def normalize_entity_type(entity_type: str) -> str:
    normalized = entity_type.lower().strip()
    if normalized not in {"deal", "lead"}:
        raise ValueError("entity_type must be 'deal' or 'lead'")
    return normalized


def crm_entity_id(entity_type: str, entity_id: str) -> str:
    return f"{normalize_entity_type(entity_type).upper()}|{entity_id}"


def search_terms(entity_type: str, entity_id: str, title: str, extra_terms: list[str] | None = None) -> list[str]:
    entity_type = normalize_entity_type(entity_type)
    prefix = "Сделка" if entity_type == "deal" else "Лид"
    terms = [title, f"{prefix}: {title}" if title else "", str(entity_id)]
    terms.extend(extra_terms or [])
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        cleaned = str(term or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def user_name(user: dict[str, Any]) -> str:
    explicit = user.get("name") or user.get("NAME")
    if explicit:
        return clean_inline(explicit, 120)
    parts = [
        user.get("first_name") or user.get("FIRST_NAME"),
        user.get("last_name") or user.get("LAST_NAME"),
    ]
    name = " ".join(str(part) for part in parts if part)
    return clean_inline(name, 120)


def users_by_id(users_response: dict[str, Any] | None) -> dict[str, str]:
    users: dict[str, str] = {}
    for user in result_items(users_response, keys=("users", "items")):
        user_id = str(user.get("id") or user.get("ID") or user.get("user_id") or "").strip()
        if not user_id:
            continue
        users[user_id] = user_name(user) or f"User {user_id}"
    return users


def file_names_for_message(message: dict[str, Any], files_by_id: dict[str, dict[str, Any]]) -> list[str]:
    file_ids: list[str] = []
    raw_files = message.get("files") or message.get("FILES") or []
    if isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, dict):
                file_id = item.get("id") or item.get("ID")
                if file_id:
                    file_ids.append(str(file_id))
            elif item:
                file_ids.append(str(item))

    params = message.get("params") or message.get("PARAMS") or {}
    if isinstance(params, dict):
        for key in ("FILE_ID", "FILE_IDS", "FILES"):
            value = params.get(key)
            if isinstance(value, list):
                file_ids.extend(str(item) for item in value if item)
            elif value:
                file_ids.append(str(value))

    names: list[str] = []
    for file_id in file_ids:
        file_item = files_by_id.get(str(file_id))
        if file_item:
            names.append(clean_file_name(file_item.get("name") or file_item.get("NAME") or file_id, 180))
        else:
            names.append(f"file_id={file_id}")
    return names


def files_by_id(messages_response: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    result = get_result(messages_response)
    if not isinstance(result, dict):
        return {}
    files = result.get("files") or result.get("FILES") or []
    if not isinstance(files, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        file_id = str(file_item.get("id") or file_item.get("ID") or "").strip()
        if file_id:
            rows[file_id] = file_item
    return rows


def is_useful_message(message: dict[str, Any], text: str, file_names: list[str]) -> bool:
    if text:
        system_noise = (
            text.startswith("Создан чат")
            or text.startswith("К чату присоединился")
            or text.startswith("Чат создан")
        )
        if system_noise and str(message.get("author_id") or message.get("AUTHOR_ID") or "") in {"", "0"}:
            return False
        return True
    return bool(file_names)


def fetch_internal_im_chats(
    client: BitrixReadOnlyClient,
    *,
    entity_type: str,
    entity_id: str,
    title: str,
    extra_search_terms: list[str] | None = None,
    message_limit: int = 100,
) -> dict[str, Any]:
    entity_type = normalize_entity_type(entity_type)
    expected_entity_id = crm_entity_id(entity_type, entity_id)
    search_responses: list[dict[str, Any]] = []

    for term in search_terms(entity_type, entity_id, title, extra_search_terms):
        search_responses.append(client.safe_call("im.search.chat.list", {"FIND": term}))

    chats: list[dict[str, Any]] = []
    seen_chat_ids: set[str] = set()
    for response in search_responses:
        for chat in result_items(response, keys=("items", "chats")):
            chat_entity_id = str(chat.get("entity_id") or chat.get("ENTITY_ID") or "")
            chat_id = str(chat.get("id") or chat.get("ID") or "").strip()
            if not chat_id or chat_entity_id != expected_entity_id or chat_id in seen_chat_ids:
                continue
            seen_chat_ids.add(chat_id)
            dialog_id = f"chat{chat_id}"
            messages_response = client.safe_call(
                "im.dialog.messages.get",
                {"DIALOG_ID": dialog_id, "LIMIT": message_limit},
            )
            users_response = client.safe_call("im.dialog.users.list", {"DIALOG_ID": dialog_id})
            chat_response = client.safe_call("im.chat.get", {"CHAT_ID": chat_id})
            chats.append(
                {
                    "chat_id": chat_id,
                    "dialog_id": dialog_id,
                    "entity_type": CRM_ENTITY_TYPE,
                    "entity_id": chat_entity_id,
                    "title": chat.get("name") or chat.get("NAME") or chat.get("title") or chat.get("TITLE"),
                    "chat": chat,
                    "chat_response": chat_response,
                    "messages_response": messages_response,
                    "users_response": users_response,
                }
            )

    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "crm_entity_id": expected_entity_id,
        "search_responses": search_responses,
        "chats": chats,
    }


def internal_chat_events(chat_bundle: dict[str, Any], *, source_entity_type: str, source_entity_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chat in chat_bundle.get("chats") or []:
        users = users_by_id(chat.get("users_response"))
        file_lookup = files_by_id(chat.get("messages_response"))
        for message in result_items(chat.get("messages_response"), keys=("messages", "items")):
            text = clean_message_text(message.get("text") or message.get("message") or message.get("MESSAGE") or "")
            file_names = file_names_for_message(message, file_lookup)
            if not is_useful_message(message, text, file_names):
                continue
            author_id = str(message.get("author_id") or message.get("AUTHOR_ID") or "").strip()
            author = users.get(author_id) or (f"User {author_id}" if author_id else "System")
            parts = []
            if text:
                parts.append(text)
            parts.extend(f"Файл: {name}" for name in file_names if name)
            event_id = str(message.get("id") or message.get("ID") or "")
            events.append(
                {
                    "when": message.get("date") or message.get("DATE_CREATE"),
                    "category": "internal_im_chat",
                    "event_type": "internal_chat_message",
                    "entity_key": f"{source_entity_type}:{source_entity_id}",
                    "entity_type": source_entity_type,
                    "entity_id": str(source_entity_id),
                    "id": f"im:{chat.get('chat_id')}:{event_id}" if event_id else f"im:{chat.get('chat_id')}",
                    "author_id": author_id,
                    "author": author,
                    "subject": f"{author}: {clean_inline(parts[0], 120) if parts else ''}",
                    "text": f"Автор: {author}\n" + "\n".join(parts),
                    "chat_id": chat.get("chat_id"),
                    "dialog_id": chat.get("dialog_id"),
                    "source": "bitrix_im",
                    "raw": message,
                }
            )

    return sorted(events, key=lambda item: (str(item.get("when") or ""), str(item.get("id") or "")))


def append_internal_chat_events(bundle: dict[str, Any], chat_events: list[dict[str, Any]]) -> dict[str, Any]:
    if not chat_events:
        return bundle
    existing_internal = list(bundle.get("internal_context") or [])
    existing_timeline = list(bundle.get("unified_timeline") or [])
    existing_ids = {str(item.get("id") or "") for item in existing_internal + existing_timeline}
    new_events = [event for event in chat_events if str(event.get("id") or "") not in existing_ids]
    if not new_events:
        return bundle

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("when") or ""), str(item.get("id") or ""))

    bundle["internal_context"] = sorted(existing_internal + new_events, key=sort_key)
    bundle["unified_timeline"] = sorted(existing_timeline + new_events, key=sort_key)
    bundle["internal_im_chat"] = {
        "events_added": len(new_events),
        "chat_ids": sorted({str(event.get("chat_id")) for event in new_events if event.get("chat_id")}),
    }
    return bundle
