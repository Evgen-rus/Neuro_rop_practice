"""
Build preview customer_path/LLM context with Bitrix internal IM chat included.

This is a read-only experiment. It does not modify the main pipeline outputs
unless the chosen output names point to preview files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required, load_json, save_json
from bitrix.customer_history_report import render_customer_history_markdown
from bitrix.internal_im_chat import append_internal_chat_events, fetch_internal_im_chats, internal_chat_events
from setup import BASE_DIR


DEFAULT_DEAL_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"
DEFAULT_LEAD_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "leads"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview internal Bitrix IM chat in customer history context")
    parser.add_argument("--entity-type", choices=["deal", "lead"], required=True)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--message-limit", type=int, default=100)
    parser.add_argument("--bitrix-timeout", type=int, default=12)
    return parser.parse_args()


def load_deal_llm_module() -> Any:
    path = PROJECT_ROOT / "bitrix" / "deals" / "4_build_deals_llm_context.py"
    spec = importlib.util.spec_from_file_location("deal_llm_context_preview", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load deal LLM context module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle_path(entity_type: str, entity_id: str) -> Path:
    if entity_type == "deal":
        workspace = DEFAULT_DEAL_WORKSPACE_ROOT / f"deal_{entity_id}"
    else:
        workspace = DEFAULT_LEAD_WORKSPACE_ROOT / f"lead_{entity_id}"
    return workspace / "raw" / f"{entity_type}_{entity_id}_customer_history_bundle.json"


def history_dir(entity_type: str, entity_id: str) -> Path:
    if entity_type == "deal":
        workspace = DEFAULT_DEAL_WORKSPACE_ROOT / f"deal_{entity_id}"
    else:
        workspace = DEFAULT_LEAD_WORKSPACE_ROOT / f"lead_{entity_id}"
    path = workspace / "history"
    path.mkdir(parents=True, exist_ok=True)
    return path


def root_item(bundle: dict[str, Any], entity_type: str) -> dict[str, Any]:
    if entity_type == "deal":
        item = (bundle.get("deal") or {}).get("item")
        return item if isinstance(item, dict) else {}
    lead_response = bundle.get("lead")
    if isinstance(lead_response, dict) and lead_response.get("ok"):
        result = (lead_response.get("response") or {}).get("result")
        return result if isinstance(result, dict) else {}
    return {}


def title_from_bundle(bundle: dict[str, Any], entity_type: str) -> str:
    root = bundle.get("root_entity") or {}
    item = root_item(bundle, entity_type)
    return str(root.get("title") or item.get("TITLE") or item.get("NAME") or "").strip()


def phone_terms_from_root(item: dict[str, Any]) -> list[str]:
    terms: set[str] = set()
    for value in (item.get("TITLE"), item.get("COMMENTS"), item.get("SOURCE_DESCRIPTION")):
        if value:
            for part in str(value).replace("\n", " ").split():
                digits = "".join(char for char in part if char.isdigit())
                if len(digits) >= 10:
                    terms.add(digits)
    phones = item.get("PHONE")
    if isinstance(phones, list):
        for phone in phones:
            if isinstance(phone, dict) and phone.get("VALUE"):
                terms.add(str(phone["VALUE"]))
    elif phones:
        terms.add(str(phones))
    return sorted(terms)


def render_chat_markdown(entity_type: str, entity_id: str, chat_bundle: dict[str, Any], events: list[dict[str, Any]]) -> str:
    lines = [
        f"# Внутренний Bitrix IM чат: {entity_type} {entity_id}",
        "",
        f"CRM entity id: {chat_bundle.get('crm_entity_id')}",
        f"Найдено чатов: {len(chat_bundle.get('chats') or [])}",
        f"Полезных сообщений для контекста: {len(events)}",
        "",
        "Важно: это внутренний контекст команды, не слова клиента.",
        "",
    ]
    for event in events:
        text = str(event.get("text") or "").strip()
        if text.startswith("Автор:"):
            text = "\n".join(text.splitlines()[1:]).strip()
        lines.extend(
            [
                f"## {event.get('when') or '-'} — {event.get('author') or '-'}",
                text or "-",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_chat_llm_section(events: list[dict[str, Any]]) -> str:
    lines = [
        "",
        "## Preview. Полный внутренний чат команды по текущей сущности",
        "",
        "Не считать этот блок словами клиента. Это внутренняя переписка менеджера/РОПа/команды по сделке или лиду.",
        "Системные события скрыты. Вложения показаны только названием файла, без ссылок и скачивания.",
        "",
    ]
    if not events:
        lines.append("- Внутренний чат не найден или полезных сообщений нет.")
        return "\n".join(lines) + "\n"

    for event in events:
        text = str(event.get("text") or "").strip()
        if text.startswith("Автор:"):
            text = "\n".join(text.splitlines()[1:]).strip()
        lines.extend(
            [
                f"- {event.get('when') or '-'}; author={event.get('author') or '-'}; "
                f"id={event.get('id') or '-'}:",
                text or "-",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    args = parse_args()
    load_dotenv()

    raw_path = bundle_path(args.entity_type, args.entity_id)
    if not raw_path.exists():
        raise SystemExit(f"Customer history bundle not found: {raw_path}")

    original_bundle = load_json(raw_path)
    augmented_bundle = deepcopy(original_bundle)
    item = root_item(original_bundle, args.entity_type)
    title = title_from_bundle(original_bundle, args.entity_type)
    phone_terms = phone_terms_from_root(item)

    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"), timeout=args.bitrix_timeout)
    chat_bundle = fetch_internal_im_chats(
        client,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        title=title,
        extra_search_terms=phone_terms,
        message_limit=args.message_limit,
    )
    events = internal_chat_events(chat_bundle, source_entity_type=args.entity_type, source_entity_id=args.entity_id)
    append_internal_chat_events(augmented_bundle, events)

    out_dir = history_dir(args.entity_type, args.entity_id)
    prefix = f"{args.entity_type}_{args.entity_id}"
    preview_bundle_path = out_dir / f"{prefix}_customer_history_bundle_with_internal_chat_preview.json"
    customer_path_preview = out_dir / f"{prefix}_customer_path_with_internal_chat_preview.md"
    chat_preview = out_dir / f"{prefix}_internal_chat_preview.md"
    llm_preview = out_dir / f"{prefix}_llm_context_with_internal_chat_preview.md"

    save_json(preview_bundle_path, {"internal_im_chat": chat_bundle, "bundle": augmented_bundle})
    customer_path_preview.write_text(render_customer_history_markdown(augmented_bundle), encoding="utf-8")
    chat_preview.write_text(render_chat_markdown(args.entity_type, args.entity_id, chat_bundle, events), encoding="utf-8")

    if args.entity_type == "deal":
        deal_llm = load_deal_llm_module()
        llm_text = deal_llm.build_customer_history_llm_context(augmented_bundle)
        llm_preview.write_text(llm_text + render_chat_llm_section(events), encoding="utf-8")
    else:
        llm_preview.write_text(
            "# Preview LLM context with internal chat\n\n"
            "Lead-specific compact LLM renderer is not separated yet; inspect customer_path preview.\n",
            encoding="utf-8",
        )

    print(f"Internal chat events added: {len(events)}")
    print(f"Saved: {preview_bundle_path}")
    print(f"Saved: {customer_path_preview}")
    print(f"Saved: {llm_preview}")
    print(f"Saved: {chat_preview}")


if __name__ == "__main__":
    main()
