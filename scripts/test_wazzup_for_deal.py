"""
Manual Wazzup CSV match check for one Bitrix deal.

Usage:
    python scripts/test_wazzup_for_deal.py --deal-id 18619 --start 2026-07-01 --end 2026-07-08

The script does not create Wazzup exports. First run:
    python scripts/test_wazzup_messages_dump.py --start 2026-07-01 --end 2026-07-08
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required
from bitrix.customer_history import contact_ids_from_deal, get_result, is_real_id, multifield_values, result_item
from wazzup_test_utils import (
    CHANNELS_URL,
    channel_summary,
    dump_csv_path,
    extract_channels,
    extract_phone_candidates_from_text,
    is_messenger_channel,
    load_wazzup_api_key,
    normalize_phone,
    read_csv_rows,
    row_matches_any_phone,
    safe_error_text,
    save_json,
    wazzup_request,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find Wazzup CSV messages matching one Bitrix deal phones.")
    parser.add_argument("--deal-id", required=True, help="Bitrix deal ID.")
    parser.add_argument("--start", required=True, help="Export period start date, kept in output metadata.")
    parser.add_argument("--end", required=True, help="Export period end date, kept in output metadata.")
    parser.add_argument("--csv", default=str(dump_csv_path()), help="Wazzup dump CSV path. Default: tmp/wazzup_messages_dump.csv.")
    parser.add_argument("--bitrix-timeout", type=int, default=20, help="Bitrix HTTP timeout in seconds. Default: 20.")
    parser.add_argument("--wazzup-timeout", type=int, default=30, help="Wazzup HTTP timeout in seconds. Default: 30.")
    return parser.parse_args()


def unique_phones(values: list[str]) -> list[str]:
    phones = []
    seen = set()
    for value in values:
        normalized = normalize_phone(value)
        if len(normalized) < 10 or normalized in seen:
            continue
        seen.add(normalized)
        phones.append(normalized)
    return phones


def deal_text_values(deal: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key, value in deal.items():
        if isinstance(value, str) and value.strip():
            values.append(value)
        elif isinstance(value, (int, float)) and key in {"PHONE", "TITLE"}:
            values.append(str(value))
    return values


def extract_deal_phones(deal: dict[str, Any], contacts: dict[str, dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    phone_values: list[str] = []
    sources: dict[str, list[str]] = {"deal_fields": [], "contacts": []}

    for value in multifield_values(deal, "PHONE"):
        phone_values.append(value)
        sources["deal_fields"].append(value)

    for text in deal_text_values(deal):
        for candidate in extract_phone_candidates_from_text(text):
            phone_values.append(candidate)
            sources["deal_fields"].append(candidate)

    for contact_id, contact in contacts.items():
        for value in multifield_values(contact, "PHONE"):
            phone_values.append(value)
            sources["contacts"].append(f"contact:{contact_id}:{value}")

    return unique_phones(phone_values), sources


def check_wazzup_channels(timeout: int) -> tuple[bool, int, str]:
    try:
        api_key = load_wazzup_api_key()
    except ValueError as error:
        return False, 0, str(error)

    result = wazzup_request("GET", CHANNELS_URL, api_key, timeout=timeout)
    if not result.ok:
        return False, 0, safe_error_text(result)

    channels = extract_channels(result.data)
    summaries = [channel_summary(channel) for channel in channels]
    messenger = [channel for channel in channels if is_messenger_channel(channel)]
    detail = f"всего {len(channels)}"
    if messenger:
        detail += f", мессенджер-каналов {len(messenger)}"
    return True, len(channels), detail


def fetch_deal_and_contacts(deal_id: str, timeout: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    load_dotenv(PROJECT_ROOT / ".env")
    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"), timeout=timeout)

    deal_response = client.safe_call("crm.deal.get", {"id": deal_id})
    deal = result_item(deal_response)
    if not deal:
        raise RuntimeError(f"crm.deal.get did not return deal {deal_id}: {deal_response.get('error') or deal_response}")

    contact_ids, contact_items_response = contact_ids_from_deal(client, deal_id, deal)
    contacts: dict[str, dict[str, Any]] = {}
    contact_responses: dict[str, Any] = {}
    for contact_id in contact_ids:
        if not is_real_id(contact_id):
            continue
        response = client.safe_call("crm.contact.get", {"id": contact_id})
        contact_responses[contact_id] = response
        contact = get_result(response)
        if isinstance(contact, dict):
            contacts[contact_id] = contact

    diagnostics = {
        "deal_response_ok": deal_response.get("ok"),
        "contact_ids": contact_ids,
        "contact_items_response_ok": contact_items_response.get("ok"),
        "contact_responses_ok": {contact_id: response.get("ok") for contact_id, response in contact_responses.items()},
    }
    return deal, contacts, contact_ids, diagnostics


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    output_path = PROJECT_ROOT / "tmp" / f"deal_{args.deal_id}_wazzup_messages.json"

    wazzup_ok, channels_count, channels_detail = check_wazzup_channels(args.wazzup_timeout)

    try:
        deal, contacts, contact_ids, bitrix_diagnostics = fetch_deal_and_contacts(args.deal_id, args.bitrix_timeout)
    except Exception as error:
        print(f"Wazzup API: {'работает' if wazzup_ok else 'не работает'}")
        print(f"Каналы найдены: {channels_detail if wazzup_ok else channels_count}")
        print("Экспорт сообщений: не проверял")
        print("Сообщения по сделке найдены: нет")
        print(f"Ошибка Bitrix: {error}")
        raise SystemExit(1)

    phones, phone_sources = extract_deal_phones(deal, contacts)

    if not csv_path.exists():
        save_json(
            output_path,
            {
                "deal_id": args.deal_id,
                "period": {"start": args.start, "end": args.end},
                "csv_path": str(csv_path),
                "csv_exists": False,
                "wazzup_api_ok": wazzup_ok,
                "channels_count": channels_count,
                "bitrix": bitrix_diagnostics,
                "contact_ids": contact_ids,
                "phones": phones,
                "phone_sources": phone_sources,
                "messages": [],
            },
        )
        print(f"Wazzup API: {'работает' if wazzup_ok else 'не работает'}")
        print(f"Каналы найдены: {channels_detail if wazzup_ok else channels_count}")
        print("Экспорт сообщений: CSV не найден")
        print("Сообщения по сделке найдены: нет")
        print("Количество сообщений: 0")
        print(f"Что делать дальше: сначала запусти test_wazzup_messages_dump.py, затем повтори этот скрипт. Результат сохранен: {output_path}")
        return

    rows = read_csv_rows(csv_path)
    matched_rows = [row for row in rows if row_matches_any_phone(row, phones)]

    save_json(
        output_path,
        {
            "deal_id": args.deal_id,
            "deal_title": deal.get("TITLE"),
            "period": {"start": args.start, "end": args.end},
            "csv_path": str(csv_path),
            "csv_exists": True,
            "csv_rows_count": len(rows),
            "wazzup_api_ok": wazzup_ok,
            "channels_count": channels_count,
            "bitrix": bitrix_diagnostics,
            "contact_ids": contact_ids,
            "phones": phones,
            "phone_sources": phone_sources,
            "messages_count": len(matched_rows),
            "messages": matched_rows,
        },
    )

    print(f"Wazzup API: {'работает' if wazzup_ok else 'не работает'}")
    if not wazzup_ok:
        print(f"Каналы найдены: 0 ({channels_detail})")
    else:
        print(f"Каналы найдены: {channels_detail}")
    print("Экспорт сообщений: CSV готов")
    print(f"Телефоны сделки: {', '.join(phones) if phones else 'не найдены'}")
    print(f"Сообщения по сделке найдены: {'да' if matched_rows else 'нет'}")
    print(f"Количество сообщений: {len(matched_rows)}")
    print(f"Что делать дальше: проверь JSON и структуру полей CSV: {output_path}")


if __name__ == "__main__":
    main()
