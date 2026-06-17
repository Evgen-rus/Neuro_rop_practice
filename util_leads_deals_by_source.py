"""
Показывает активные лиды и сделки Bitrix24 по источнику за период.

Методы Bitrix24 REST API:
- crm.lead.list: получает список активных лидов по SOURCE_ID и периоду создания.
- crm.deal.list: получает список активных сделок по SOURCE_ID и периоду создания.

По умолчанию ищет источник ЛидгенБюро:
    SOURCE_ID = 10
    период = последние 7 дней
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from setup import MSK_TZ, get_logger


DEFAULT_SOURCE_ID = "10"
DEFAULT_DAYS = 7
PAGE_SIZE = 50

logger = get_logger(__file__)


def get_env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Переменная окружения {name} не задана или пуста")
    return value


def build_api_method_url(webhook_url: str, method: str) -> str:
    return f"{webhook_url.rstrip('/')}/{method}"


def call_bitrix_api(
    webhook_url: str,
    method: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    method_url = build_api_method_url(webhook_url, method)
    response = requests.post(
        method_url,
        json=payload or {},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    try:
        result = response.json()
    except ValueError:
        result = {}

    if not response.ok:
        error_text = result.get("error_description") or result.get("error") or response.text
        raise RuntimeError(f"HTTP {response.status_code}: {error_text}")

    if result.get("error"):
        error_text = result.get("error_description") or result.get("error")
        raise RuntimeError(str(error_text))

    return result


def fetch_crm_list(
    webhook_url: str,
    method: str,
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    start: int | str = 0

    while True:
        payload = {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": filters,
            "select": ["*", "UF_*"],
            "start": start,
        }
        response = call_bitrix_api(webhook_url, method, payload)
        batch = response.get("result", [])

        if not isinstance(batch, list):
            raise RuntimeError(f"Неожиданный формат result для {method}: {type(batch)}")

        items.extend(batch)

        next_start = response.get("next")
        if next_start is None or len(batch) < PAGE_SIZE:
            break

        start = next_start

    return items


def build_date_from(days: int) -> str:
    date_from = datetime.now(MSK_TZ) - timedelta(days=days)
    return date_from.isoformat(timespec="seconds")


def log_json_section(title: str, items: List[Dict[str, Any]]) -> None:
    logger.info("")
    logger.info("%s", title)
    logger.info("Найдено: %s", len(items))
    logger.info(json.dumps(items, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Показать активные лиды и сделки Bitrix24 по SOURCE_ID за период",
    )
    parser.add_argument(
        "--source-id",
        default=DEFAULT_SOURCE_ID,
        help=f"SOURCE_ID источника. По умолчанию: {DEFAULT_SOURCE_ID}",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Период поиска в днях. По умолчанию: {DEFAULT_DAYS}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    try:
        webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
    except ValueError as error:
        logger.error("Ошибка: %s", error)
        sys.exit(1)

    date_from = build_date_from(args.days)
    logger.info(
        "Поиск активных лидов и сделок: SOURCE_ID=%s, DATE_CREATE >= %s",
        args.source_id,
        date_from,
    )

    lead_filter = {
        "SOURCE_ID": args.source_id,
        "STATUS_SEMANTIC_ID": "P",
        ">=DATE_CREATE": date_from,
    }
    deal_filter = {
        "SOURCE_ID": args.source_id,
        "CLOSED": "N",
        ">=DATE_CREATE": date_from,
    }

    try:
        leads = fetch_crm_list(webhook_url, "crm.lead.list", lead_filter)
        deals = fetch_crm_list(webhook_url, "crm.deal.list", deal_filter)
    except (requests.RequestException, RuntimeError) as error:
        logger.error("Ошибка при запросе Bitrix24: %s", error)
        sys.exit(1)

    log_json_section("Активные лиды", leads)
    log_json_section("Активные сделки", deals)


if __name__ == "__main__":
    main()
