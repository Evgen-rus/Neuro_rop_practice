"""
Собирает карту структуры CRM Bitrix24 для будущего нейроанализа.

Методы Bitrix24 REST API:
- crm.status.list: получает статусы лидов, источники и fallback-список стадий
  сделок по ENTITY_ID.
- crm.dealcategory.list: получает список воронок сделок.
- crm.dealcategory.stage.list: получает этапы конкретной воронки сделок.

Лог содержит два блока:
1. Читаемая структура CRM: лиды как отдельная воронка, воронки сделок и этапы.
2. Полный JSON карты.

Машинная карта дополнительно сохраняется в crm_pipeline_map.json.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_OUTPUT_FILE = BASE_DIR / "crm_pipeline_map.json"

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
        raise RuntimeError(f"{method}: HTTP {response.status_code}: {error_text}")

    if result.get("error"):
        error_text = result.get("error_description") or result.get("error")
        raise RuntimeError(f"{method}: {error_text}")

    return result


def extract_list(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = response.get("result", [])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return list(result.values())
    return []


def get_statuses(webhook_url: str, entity_id: str) -> List[Dict[str, Any]]:
    response = call_bitrix_api(
        webhook_url,
        "crm.status.list",
        {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
    )
    return extract_list(response)


def get_deal_categories(webhook_url: str) -> List[Dict[str, Any]]:
    response = call_bitrix_api(
        webhook_url,
        "crm.dealcategory.list",
        {"order": {"SORT": "ASC", "ID": "ASC"}},
    )
    return extract_list(response)


def is_active_category(category: Dict[str, Any]) -> bool:
    for key in ("ACTIVE", "IS_ACTIVE"):
        value = str(category.get(key, "")).upper()
        if value in {"N", "0", "FALSE"}:
            return False
    return True


def get_deal_category_stages(
    webhook_url: str,
    category_id: int,
) -> List[Dict[str, Any]]:
    try:
        response = call_bitrix_api(
            webhook_url,
            "crm.dealcategory.stage.list",
            {"id": category_id},
        )
        stages = extract_list(response)
        if stages:
            return stages
    except RuntimeError as error:
        logger.warning(
            "Не удалось получить этапы через crm.dealcategory.stage.list для CATEGORY_ID=%s: %s",
            category_id,
            error,
        )

    entity_id = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
    return get_statuses(webhook_url, entity_id)


def normalize_stage(stage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": stage.get("ID"),
        "entity_id": stage.get("ENTITY_ID"),
        "status_id": stage.get("STATUS_ID"),
        "name": stage.get("NAME"),
        "name_init": stage.get("NAME_INIT"),
        "sort": stage.get("SORT"),
        "semantics": stage.get("SEMANTICS"),
        "color": stage.get("COLOR"),
        "system": stage.get("SYSTEM"),
        "raw": stage,
    }


def build_crm_map(webhook_url: str) -> Dict[str, Any]:
    lead_stages = get_statuses(webhook_url, "STATUS")
    sources = get_statuses(webhook_url, "SOURCE")
    raw_categories = get_deal_categories(webhook_url)
    active_categories = [category for category in raw_categories if is_active_category(category)]

    deal_pipelines: List[Dict[str, Any]] = []
    for category in active_categories:
        category_id = int(category.get("ID", 0))
        stages = get_deal_category_stages(webhook_url, category_id)
        deal_pipelines.append(
            {
                "id": category_id,
                "name": category.get("NAME"),
                "sort": category.get("SORT"),
                "is_default": category_id == 0,
                "raw": category,
                "stages": [normalize_stage(stage) for stage in stages],
            }
        )

    return {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "lead_pipeline": {
            "id": "lead",
            "name": "Лиды",
            "entity_id": "STATUS",
            "stages": [normalize_stage(stage) for stage in lead_stages],
        },
        "deal_pipelines": deal_pipelines,
        "sources": [normalize_stage(source) for source in sources],
        "raw": {
            "deal_categories": raw_categories,
            "active_deal_categories": active_categories,
            "lead_statuses": lead_stages,
            "sources": sources,
        },
    }


def log_stage_rows(stages: List[Dict[str, Any]]) -> None:
    logger.info("%-8s %-32s %-40s %-10s %s", "ID", "STATUS_ID", "NAME", "SORT", "SEM")
    logger.info("-" * 105)

    if not stages:
        logger.info("Нет этапов")
        return

    for stage in stages:
        logger.info(
            "%-8s %-32s %-40s %-10s %s",
            stage.get("id") or "",
            stage.get("status_id") or "",
            stage.get("name") or "",
            stage.get("sort") or "",
            stage.get("semantics") or "",
        )


def log_readable_map(crm_map: Dict[str, Any]) -> None:
    logger.info("")
    logger.info("БЛОК 1. ЧИТАЕМАЯ СТРУКТУРА CRM")
    logger.info("=" * 105)

    lead_pipeline = crm_map["lead_pipeline"]
    logger.info("")
    logger.info("Воронка лидов: %s", lead_pipeline["name"])
    log_stage_rows(lead_pipeline["stages"])

    logger.info("")
    logger.info("Воронки сделок")
    logger.info("=" * 105)
    if not crm_map["deal_pipelines"]:
        logger.info("Активные воронки сделок не найдены")

    for pipeline in crm_map["deal_pipelines"]:
        logger.info("")
        logger.info(
            "Воронка сделок: ID=%s | NAME=%s | SORT=%s",
            pipeline.get("id"),
            pipeline.get("name"),
            pipeline.get("sort"),
        )
        log_stage_rows(pipeline["stages"])

    logger.info("")
    logger.info("Источники")
    logger.info("=" * 105)
    log_stage_rows(crm_map["sources"])


def log_full_json(crm_map: Dict[str, Any]) -> None:
    logger.info("")
    logger.info("БЛОК 2. ПОЛНЫЙ JSON КАРТЫ CRM")
    logger.info("=" * 105)
    logger.info(json.dumps(crm_map, ensure_ascii=False, indent=2))


def save_crm_map(crm_map: Dict[str, Any], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(crm_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("JSON карта CRM сохранена: %s", output_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать карту воронок и этапов CRM Bitrix24",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help=f"Файл для сохранения JSON карты. По умолчанию: {DEFAULT_OUTPUT_FILE}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    try:
        webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
        crm_map = build_crm_map(webhook_url)
    except (ValueError, requests.RequestException, RuntimeError) as error:
        logger.error("Не удалось собрать карту CRM: %s", error)
        sys.exit(1)

    output_file = Path(args.output)
    if not output_file.is_absolute():
        output_file = BASE_DIR / output_file

    log_readable_map(crm_map)
    log_full_json(crm_map)
    save_crm_map(crm_map, output_file)


if __name__ == "__main__":
    main()
