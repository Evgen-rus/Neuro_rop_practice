"""
Small helpers for manual Wazzup API diagnostics.

These helpers are intentionally kept outside the main Bitrix/customer-history
pipeline. They read WAZZUP_API_KEY from .env, but never print or persist it.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TMP_DIR = PROJECT_ROOT / "tmp"
CHANNELS_URL = "https://api.wazzup24.com/v3/channels"
MESSAGES_DUMP_URL = "https://tech.wazzup24.com/v2/messages/messages_dump"
MESSENGER_TRANSPORTS = {"whatsapp", "tgapi", "telegram", "max", "wa", "tg"}


@dataclass
class WazzupHttpResult:
    ok: bool
    status_code: int
    data: Any
    text: str


def load_wazzup_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    value = os.getenv("WAZZUP_API_KEY", "").strip()
    if not value:
        raise ValueError("WAZZUP_API_KEY is empty or missing in .env")
    return value


def wazzup_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def wazzup_request(
    method: str,
    url: str,
    api_key: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> WazzupHttpResult:
    response = requests.request(
        method,
        url,
        headers=wazzup_headers(api_key),
        json=json_body,
        timeout=timeout,
    )
    text = response.text or ""
    try:
        data: Any = response.json()
    except ValueError:
        data = None
    return WazzupHttpResult(
        ok=response.ok,
        status_code=response.status_code,
        data=data,
        text=text,
    )


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_channels(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("channels", "data", "result", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_channels(value)
            if nested:
                return nested
    return []


def channel_value(channel: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = channel.get(key)
        if value is not None:
            return str(value)
    return ""


def channel_summary(channel: dict[str, Any]) -> dict[str, str]:
    return {
        "channelId": channel_value(channel, "channelId", "id", "idChannel"),
        "transport": channel_value(channel, "transport", "type", "messenger"),
        "plainId": channel_value(channel, "plainId", "phone", "login"),
        "state": channel_value(channel, "state", "status"),
    }


def is_messenger_channel(channel: dict[str, Any]) -> bool:
    transport = channel_value(channel, "transport", "type", "messenger").lower()
    return transport in MESSENGER_TRANSPORTS


def parse_local_date(value: str, *, end_of_day: bool = False) -> str:
    parsed = date.fromisoformat(value)
    day_time = time.max if end_of_day else time.min
    dt = datetime.combine(parsed, day_time, tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(microsecond=999000)
    else:
        dt = dt.replace(microsecond=0)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dump_status_path() -> Path:
    return TMP_DIR / "wazzup_messages_dump_status.json"


def dump_csv_path() -> Path:
    return TMP_DIR / "wazzup_messages_dump.csv"


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def extract_phone_candidates_from_text(value: Any) -> list[str]:
    text = str(value or "")
    candidates: list[str] = []
    for match in re.finditer(r"(?:\+?\d[\d\s()._-]{8,}\d)", text):
        normalized = normalize_phone(match.group(0))
        if len(normalized) >= 10:
            candidates.append(normalized)
    return candidates


def phones_match(left: Any, right: Any) -> bool:
    left_digits = normalize_phone(left)
    right_digits = normalize_phone(right)
    if not left_digits or not right_digits:
        return False
    return left_digits == right_digits or left_digits[-10:] == right_digits[-10:]


def row_matches_any_phone(row: dict[str, Any], phones: Iterable[str]) -> bool:
    normalized_phones = [normalize_phone(phone) for phone in phones if normalize_phone(phone)]
    if not normalized_phones:
        return False
    for value in row.values():
        digits = normalize_phone(value)
        if not digits:
            continue
        if any(phones_match(digits, phone) for phone in normalized_phones):
            return True
    return False


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        sample = file.read(4096)
        file.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(file, dialect=dialect)
        return [dict(row) for row in reader]


def download_file(url: str, path: Path, *, timeout: int = 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        with path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    file.write(chunk)


def safe_error_text(result: WazzupHttpResult) -> str:
    if result.status_code == 401:
        return "401: ключ неверный или не принят Wazzup API"
    if result.status_code == 403:
        return "403: ключ работает не для этого метода или не хватает прав"
    if isinstance(result.data, dict):
        message = result.data.get("message") or result.data.get("error") or result.data.get("detail")
        if message:
            return f"HTTP {result.status_code}: {message}"
    return f"HTTP {result.status_code}: {result.text[:300]}"
