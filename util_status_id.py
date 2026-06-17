"""
Показывает справочники источников и статусов лидов Bitrix24.

Методы Bitrix24 REST API:
- crm.status.list: получает элементы справочников CRM. В этом скрипте
  используется для SOURCE_ID (источники) и STATUS_ID (статусы лидов).
"""

import os
from typing import Any

import requests
from dotenv import load_dotenv

from setup import get_logger


logger = get_logger(__file__)


def get_statuses(webhook_url: str, entity_id: str) -> dict:
    """Получает список статусов для указанного типа сущности."""
    response = requests.post(
        f"{webhook_url}/crm.status.list",
        json={"filter": {"ENTITY_ID": entity_id}},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def get_status_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает список статусов из ответа Bitrix24."""
    result = response.get("result", [])
    return result if isinstance(result, list) else []


def log_statuses(title: str, statuses: list[dict[str, Any]]) -> None:
    """Пишет статусы в лог в читаемом табличном виде."""
    logger.info("")
    logger.info("%s", title)
    logger.info("-" * 88)
    logger.info("%-8s %-25s %-40s %s", "ID", "STATUS_ID", "NAME", "SORT")
    logger.info("-" * 88)

    if not statuses:
        logger.info("Нет данных")
        return

    for status in statuses:
        logger.info(
            "%-8s %-25s %-40s %s",
            status.get("ID", ""),
            status.get("STATUS_ID", ""),
            status.get("NAME", ""),
            status.get("SORT", ""),
        )


def main() -> None:
    """Показывает доступные значения SOURCE_ID и STATUS_ID."""
    load_dotenv()
    webhook_url = os.getenv("BITRIX_WEBHOOK_URL")

    if not webhook_url:
        logger.error("Ошибка: переменная BITRIX_WEBHOOK_URL не найдена в .env")
        return

    webhook_url = webhook_url.rstrip("/")

    try:
        source_response = get_statuses(webhook_url, "SOURCE")
        status_response = get_statuses(webhook_url, "STATUS")
    except requests.RequestException as error:
        logger.error("Ошибка при запросе статусов Bitrix24: %s", error)
        return

    source_statuses = get_status_items(source_response)
    lead_statuses = get_status_items(status_response)

    log_statuses("SOURCE_ID - источники лидов", source_statuses)
    log_statuses("STATUS_ID - статусы лидов", lead_statuses)


if __name__ == "__main__":
    main()
