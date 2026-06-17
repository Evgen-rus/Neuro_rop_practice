import os

import requests
from dotenv import load_dotenv


def get_statuses(webhook_url: str, entity_id: str) -> dict:
    """Получает список статусов для указанного типа сущности."""
    response = requests.post(
        f"{webhook_url}/crm.status.list",
        json={"filter": {"ENTITY_ID": entity_id}},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    """Показывает доступные значения SOURCE_ID и STATUS_ID."""
    load_dotenv()
    webhook_url = os.getenv("BITRIX_WEBHOOK_URL")

    if not webhook_url:
        print("Ошибка: переменная BITRIX_WEBHOOK_URL не найдена в .env")
        return

    webhook_url = webhook_url.rstrip("/")

    source_response = get_statuses(webhook_url, "SOURCE")
    status_response = get_statuses(webhook_url, "STATUS")

    print("SOURCE:")
    print(source_response)

    print("STATUS:")
    print(status_response)


if __name__ == "__main__":
    main()