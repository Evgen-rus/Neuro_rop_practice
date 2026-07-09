"""
Manual read-only check for Wazzup API key and channels.

Usage:
    python scripts/test_wazzup_access.py
"""

from __future__ import annotations

import argparse
from typing import Any

from wazzup_test_utils import (
    CHANNELS_URL,
    channel_summary,
    extract_channels,
    is_messenger_channel,
    load_wazzup_api_key,
    safe_error_text,
    wazzup_request,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Wazzup API access and list channels.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds. Default: 30.")
    return parser.parse_args()


def print_channel(channel: dict[str, Any]) -> None:
    summary = channel_summary(channel)
    print(
        "- "
        f"channelId={summary['channelId'] or '-'}; "
        f"transport={summary['transport'] or '-'}; "
        f"plainId={summary['plainId'] or '-'}; "
        f"state={summary['state'] or '-'}"
    )


def main() -> None:
    args = parse_args()
    try:
        api_key = load_wazzup_api_key()
    except ValueError as error:
        print(f"Wazzup API: не работает ({error})")
        raise SystemExit(1)

    result = wazzup_request("GET", CHANNELS_URL, api_key, timeout=args.timeout)
    if not result.ok:
        print("Wazzup API: не работает")
        print(f"Ошибка: {safe_error_text(result)}")
        raise SystemExit(1)

    channels = extract_channels(result.data)
    print("Wazzup API: работает")
    print(f"Каналы найдены: {len(channels)}")

    if not channels:
        print("Каналов нет.")
        return

    print("Все каналы:")
    for channel in channels:
        print_channel(channel)

    messenger_channels = [channel for channel in channels if is_messenger_channel(channel)]
    if messenger_channels:
        print("")
        print("Мессенджер-каналы:")
        for channel in messenger_channels:
            print_channel(channel)
    else:
        print("")
        print("Мессенджер-каналы: не найдены")


if __name__ == "__main__":
    main()
