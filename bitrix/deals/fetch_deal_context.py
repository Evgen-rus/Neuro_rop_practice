"""
Step 1. Read full customer-path context for Bitrix24 deals and save raw JSON.

This script is read-only for Bitrix24: it uses get/list/fields methods and writes
only local report files.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, as_list, get_env_required, load_json, save_json
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_DEAL_IDS = ["18507", "18493"]
DEFAULT_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEAL_OWNER_TYPE_ID = 2

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 1: fetch read-only Bitrix24 deal context into local JSON files",
    )
    parser.add_argument(
        "--deal-ids",
        nargs="+",
        default=DEFAULT_DEAL_IDS,
        help=f"Deal IDs to fetch. Default: {' '.join(DEFAULT_DEAL_IDS)}",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for raw JSON. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--pipeline-map",
        default=str(BASE_DIR / "crm_pipeline_map.json"),
        help="Optional local crm_pipeline_map.json for stage names",
    )
    return parser.parse_args()


def get_result(call_result: dict[str, Any]) -> Any:
    if not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def build_stage_lookup(pipeline_map_path: Path) -> dict[str, dict[str, Any]]:
    if not pipeline_map_path.exists():
        return {}

    try:
        crm_map = load_json(pipeline_map_path)
    except ValueError:
        logger.warning("Could not parse pipeline map: %s", pipeline_map_path)
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    for pipeline in crm_map.get("deal_pipelines", []):
        for stage in pipeline.get("stages", []):
            status_id = stage.get("status_id")
            if status_id:
                lookup[str(status_id)] = {
                    "stage": stage,
                    "pipeline": {
                        "id": pipeline.get("id"),
                        "name": pipeline.get("name"),
                        "sort": pipeline.get("sort"),
                    },
                }
    return lookup


def fetch_entity_by_id(client: BitrixReadOnlyClient, method: str, entity_id: Any) -> dict[str, Any]:
    if not entity_id:
        return {"ok": False, "method": method, "payload": {"id": entity_id}, "error": "empty id"}
    return client.safe_call(method, {"id": entity_id})


def fetch_users(client: BitrixReadOnlyClient, ids: list[Any]) -> dict[str, Any]:
    users: dict[str, Any] = {}
    for user_id in sorted({str(item) for item in ids if item}):
        response = client.safe_call("user.get", {"ID": user_id})
        result = get_result(response)
        users[user_id] = {
            "response": response,
            "user": result[0] if isinstance(result, list) and result else None,
        }
    return users


def fetch_timeline_comments(client: BitrixReadOnlyClient, deal_id: str) -> list[dict[str, Any]]:
    attempts = [
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE": "deal", "ENTITY_ID": deal_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"OWNER_TYPE_ID": DEAL_OWNER_TYPE_ID, "OWNER_ID": deal_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE_ID": DEAL_OWNER_TYPE_ID, "ENTITY_ID": deal_id},
        },
    ]

    results: list[dict[str, Any]] = []
    seen_payloads: set[str] = set()
    for payload in attempts:
        key = repr(payload)
        if key in seen_payloads:
            continue
        seen_payloads.add(key)
        response = client.safe_list_all("crm.timeline.comment.list", payload)
        results.append(response)
        if response.get("ok") and response.get("items"):
            break
    return results


def fetch_activities(client: BitrixReadOnlyClient, deal_id: str) -> dict[str, Any]:
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": {"OWNER_TYPE_ID": DEAL_OWNER_TYPE_ID, "OWNER_ID": deal_id},
        "select": ["*"],
    }
    return client.safe_list_all("crm.activity.list", payload)


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for activity in activities:
        activity_id = activity.get("ID") or activity.get("id")
        if not activity_id:
            continue
        details[str(activity_id)] = client.safe_call("crm.activity.get", {"id": activity_id})
    return details


def extract_refs(value: Any, path: str = "") -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []

    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            upper_key = str(key).upper()
            if any(token in upper_key for token in ("FILE", "ATTACH", "RECORD", "AUDIO", "URL")):
                if child not in (None, "", [], {}):
                    refs.append({"path": child_path, "key": str(key), "value": child})
            refs.extend(extract_refs(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs.extend(extract_refs(child, f"{path}[{index}]"))

    return refs


def fetch_deal_bundle(
    client: BitrixReadOnlyClient,
    deal_id: str,
    stage_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    logger.info("Fetching deal context: deal_id=%s", deal_id)
    deal_response = fetch_entity_by_id(client, "crm.deal.get", deal_id)
    deal = get_result(deal_response) or {}

    contact_ids = set(str(item) for item in as_list(deal.get("CONTACT_ID")) if item)
    contact_items_response = client.safe_call("crm.deal.contact.items.get", {"id": deal_id})
    contact_items = get_result(contact_items_response)
    if isinstance(contact_items, list):
        for item in contact_items:
            contact_id = item.get("CONTACT_ID") if isinstance(item, dict) else None
            if contact_id:
                contact_ids.add(str(contact_id))

    company_id = deal.get("COMPANY_ID")
    contacts = {
        contact_id: fetch_entity_by_id(client, "crm.contact.get", contact_id)
        for contact_id in sorted(contact_ids)
    }
    company = fetch_entity_by_id(client, "crm.company.get", company_id) if company_id else None

    activities_response = fetch_activities(client, deal_id)
    activities = activities_response.get("items", [])
    activity_details = fetch_activity_details(client, activities)
    timeline_comments = fetch_timeline_comments(client, deal_id)

    user_ids = [
        deal.get("ASSIGNED_BY_ID"),
        deal.get("CREATED_BY_ID"),
        deal.get("MODIFY_BY_ID"),
        deal.get("MOVED_BY_ID"),
    ]
    for activity in activities:
        user_ids.extend(
            [
                activity.get("RESPONSIBLE_ID"),
                activity.get("AUTHOR_ID"),
                activity.get("EDITOR_ID"),
            ]
        )

    references_source = {
        "deal": deal,
        "company": company,
        "contacts": contacts,
        "activities": activities,
        "activity_details": activity_details,
        "timeline_comments": timeline_comments,
    }

    stage_id = str(deal.get("STAGE_ID") or "")
    bundle = {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "read_only": True,
        "deal_id": deal_id,
        "stage_info": stage_lookup.get(stage_id),
        "deal": {"response": deal_response, "item": deal},
        "company": company,
        "contacts": contacts,
        "deal_contacts": contact_items_response,
        "users": fetch_users(client, user_ids),
        "fields": {
            "deal": client.safe_call("crm.deal.fields"),
            "activity": client.safe_call("crm.activity.fields"),
            "contact": client.safe_call("crm.contact.fields"),
            "company": client.safe_call("crm.company.fields"),
        },
        "product_rows": client.safe_call("crm.deal.productrows.get", {"id": deal_id}),
        "activities": activities_response,
        "activity_details": activity_details,
        "timeline_comments": timeline_comments,
        "invoice_attempts": [
            client.safe_list_all("crm.invoice.list", {"filter": {"UF_DEAL_ID": deal_id}, "select": ["*"]}),
            client.safe_list_all("crm.item.list", {"entityTypeId": 31, "filter": {"parentId2": deal_id}}),
        ],
        "file_and_recording_refs": extract_refs(references_source),
    }
    return bundle


def main() -> None:
    args = parse_args()
    load_dotenv()

    webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
    client = BitrixReadOnlyClient(webhook_url)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_lookup = build_stage_lookup(Path(args.pipeline_map))

    index: list[dict[str, Any]] = []
    for deal_id in args.deal_ids:
        bundle = fetch_deal_bundle(client, str(deal_id), stage_lookup)
        deal = bundle.get("deal", {}).get("item", {})
        output_path = output_dir / f"deal_{deal_id}_context.json"
        save_json(output_path, bundle)
        index.append(
            {
                "deal_id": str(deal_id),
                "title": deal.get("TITLE"),
                "stage_id": deal.get("STAGE_ID"),
                "output_path": str(output_path),
            }
        )
        logger.info("Saved raw deal context: %s", output_path)

    index_path = output_dir / "index.json"
    save_json(index_path, {"generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"), "items": index})
    logger.info("Saved raw index: %s", index_path)


if __name__ == "__main__":
    main()
