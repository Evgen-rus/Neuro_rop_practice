"""
Вывод всех полей лида Bitrix24 по ID (JSON).

Использование:
    python util_get_lead_by_id.py 12345
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from setup import get_logger


load_dotenv()

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
        timeout=15,
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


def fetch_lead(webhook_url: str, lead_id: int) -> Dict[str, Any]:
    result = call_bitrix_api(
        webhook_url,
        "crm.lead.get",
        {"id": lead_id},
    )
    lead = result.get("result")
    if not lead:
        raise ValueError(f"Лид с ID {lead_id} не найден")
    return lead


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Вывести все поля лида Bitrix24 по ID (JSON)",
    )
    parser.add_argument(
        "lead_id",
        type=int,
        help="ID лида в Bitrix24 (число из URL или ответа API)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
    except ValueError as error:
        print(f"Ошибка: {error}")
        sys.exit(1)

    lead_id = args.lead_id
    logger.info("Запрос лида ID=%s", lead_id)

    try:
        lead = fetch_lead(webhook_url, lead_id)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        print(f"Не удалось получить лид {lead_id}: {error}")
        logger.error("Ошибка получения лида ID=%s: %s", lead_id, error)
        sys.exit(1)

    print(json.dumps(lead, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
