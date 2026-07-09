"""
Manual Wazzup historical messages export check.

Usage:
    python scripts/test_wazzup_messages_dump.py --start 2026-07-01 --end 2026-07-08
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from wazzup_test_utils import (
    MESSAGES_DUMP_URL,
    download_file,
    dump_csv_path,
    dump_status_path,
    load_wazzup_api_key,
    parse_local_date,
    safe_error_text,
    save_json,
    wazzup_request,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and poll a Wazzup messages dump export.")
    parser.add_argument("--start", required=True, help="Start date in YYYY-MM-DD, converted to UTC 00:00:00.000Z.")
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD, converted to UTC 23:59:59.999Z.")
    parser.add_argument("--attempts", type=int, default=5, help="Max status checks. Default: 5.")
    parser.add_argument("--pause", type=int, default=10, help="Pause between checks in seconds. Default: 10.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds. Default: 30.")
    return parser.parse_args()


def find_first_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = find_first_key(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_first_key(item, keys)
            if found not in (None, ""):
                return found
    return None


def export_id_from_payload(payload: Any) -> str:
    value = find_first_key(payload, {"export_id", "exportId", "id"})
    return str(value or "").strip()


def status_from_payload(payload: Any) -> str:
    value = find_first_key(payload, {"status", "state"})
    return str(value or "").strip().lower()


def url_from_payload(payload: Any) -> str:
    value = find_first_key(payload, {"url", "download_url", "downloadUrl", "link"})
    return str(value or "").strip()


def save_status(export_id: str, payload: Any, request_body: dict[str, str], status_url: str) -> None:
    save_json(
        dump_status_path(),
        {
            "export_id": export_id,
            "status_url": status_url,
            "request_body": request_body,
            "response": payload,
        },
    )


def main() -> None:
    args = parse_args()
    attempts = max(1, args.attempts)
    pause = max(0, args.pause)

    request_body = {
        "start_at": parse_local_date(args.start, end_of_day=False),
        "end_at": parse_local_date(args.end, end_of_day=True),
    }

    try:
        api_key = load_wazzup_api_key()
    except ValueError as error:
        print(f"Wazzup API: не работает ({error})")
        raise SystemExit(1)

    create_result = wazzup_request("POST", MESSAGES_DUMP_URL, api_key, json_body=request_body, timeout=args.timeout)
    if not create_result.ok:
        save_status("", create_result.data, request_body, MESSAGES_DUMP_URL)
        print("Экспорт сообщений: ошибка")
        print(f"Ошибка: {safe_error_text(create_result)}")
        print(f"Raw status сохранен: {dump_status_path()}")
        raise SystemExit(1)

    export_id = export_id_from_payload(create_result.data)
    export_url = url_from_payload(create_result.data)
    if not export_id and not export_url:
        save_status("", create_result.data, request_body, MESSAGES_DUMP_URL)
        print("Экспорт сообщений: создан, но export_id/url не найден в ответе")
        print(f"Raw status сохранен: {dump_status_path()}")
        return

    status_url = f"{MESSAGES_DUMP_URL}/{export_id}" if export_id else MESSAGES_DUMP_URL
    print(f"Экспорт сообщений: создан, export_id={export_id or '-'}")

    latest_payload = create_result.data
    latest_status = status_from_payload(latest_payload)
    latest_url = export_url

    for attempt in range(1, attempts + 1):
        if export_id:
            status_result = wazzup_request("GET", status_url, api_key, timeout=args.timeout)
            if not status_result.ok:
                save_status(export_id, status_result.data, request_body, status_url)
                print("Экспорт сообщений: ошибка при проверке статуса")
                print(f"Ошибка: {safe_error_text(status_result)}")
                print(f"Raw status сохранен: {dump_status_path()}")
                raise SystemExit(1)
            latest_payload = status_result.data
            latest_status = status_from_payload(latest_payload)
            latest_url = url_from_payload(latest_payload) or latest_url
            save_status(export_id, latest_payload, request_body, status_url)

        print(f"Проверка {attempt}/{attempts}: status={latest_status or '-'}, url={'есть' if latest_url else 'нет'}")

        if latest_url or latest_status == "done":
            if latest_url:
                print(f"Ссылка на CSV: {latest_url}")
                download_file(latest_url, dump_csv_path())
                print(f"CSV скачан: {dump_csv_path()}")
            else:
                print("Экспорт готов, но url не найден в ответе. Проверь raw status.")
            print(f"Raw status сохранен: {dump_status_path()}")
            return

        if latest_status in {"pending", "processing", "created", "queued", "in_progress", ""} and attempt < attempts:
            time.sleep(pause)

    save_status(export_id, latest_payload, request_body, status_url)
    print("Экспорт сообщений: создан, но еще не готов")
    print(f"Последний status={latest_status or '-'}")
    print(f"Raw status сохранен: {dump_status_path()}")


if __name__ == "__main__":
    main()
