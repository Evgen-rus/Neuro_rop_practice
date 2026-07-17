"""
Step 1. Read full customer-path context for Bitrix24 leads and save raw JSON.

This script is read-only for Bitrix24: it saves the lead card, activities,
activity details, timeline comments, contact/company records, and discovered
file/audio references.
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

from bitrix.client import BitrixReadOnlyClient, as_list, get_env_required, save_json
from progress_events import retry_progress_callback
from bitrix.customer_history import DEFAULT_HISTORY_DAYS, build_customer_history_bundle, is_real_id
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"
LEAD_OWNER_TYPE_ID = 1

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Step 1: fetch read-only Bitrix24 lead context into local JSON files")
    parser.add_argument("--lead-ids", nargs="+", required=True, help="Lead IDs to fetch")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Raw JSON output dir")
    parser.add_argument(
        "--history-days",
        type=int,
        default=DEFAULT_HISTORY_DAYS,
        help=f"Customer history period in days. Default: {DEFAULT_HISTORY_DAYS}",
    )
    parser.add_argument(
        "--include-related-contact-deals",
        action="store_true",
        help="Also save *_customer_history_bundle.json with contact and related deals history.",
    )
    parser.add_argument(
        "--include-internal-context",
        action="store_true",
        help="Include timeline comments/internal notes in customer history bundle.",
    )
    parser.add_argument(
        "--pipeline-map",
        default=str(BASE_DIR / "crm_pipeline_map.json"),
        help="Optional local crm_pipeline_map.json for deal stage names in related deals.",
    )
    return parser.parse_args()


def get_result(call_result: dict[str, Any]) -> Any:
    if not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def fetch_entity_by_id(client: BitrixReadOnlyClient, method: str, entity_id: Any) -> dict[str, Any]:
    if not is_real_id(entity_id):
        return {"ok": False, "method": method, "payload": {"id": entity_id}, "error": "empty id"}
    return client.safe_call(method, {"id": entity_id})


def fetch_activities(client: BitrixReadOnlyClient, lead_id: str) -> dict[str, Any]:
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": {"OWNER_TYPE_ID": LEAD_OWNER_TYPE_ID, "OWNER_ID": lead_id},
        "select": ["*"],
    }
    return client.safe_list_all("crm.activity.list", payload)


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for activity in activities:
        activity_id = activity.get("ID") or activity.get("id")
        if activity_id:
            details[str(activity_id)] = client.safe_call("crm.activity.get", {"id": activity_id})
    return details


def fetch_timeline_comments(client: BitrixReadOnlyClient, lead_id: str) -> list[dict[str, Any]]:
    attempts = [
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE": "lead", "ENTITY_ID": lead_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"OWNER_TYPE_ID": LEAD_OWNER_TYPE_ID, "OWNER_ID": lead_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE_ID": LEAD_OWNER_TYPE_ID, "ENTITY_ID": lead_id},
        },
    ]
    return [client.safe_list_all("crm.timeline.comment.list", payload) for payload in attempts]


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


def fetch_lead_bundle(client: BitrixReadOnlyClient, lead_id: str) -> dict[str, Any]:
    logger.info("Fetching lead context: lead_id=%s", lead_id)
    lead_response = fetch_entity_by_id(client, "crm.lead.get", lead_id)
    lead = get_result(lead_response) or {}

    contact_ids = {str(item).strip() for item in as_list(lead.get("CONTACT_ID")) if is_real_id(item)}
    company_ids = {str(item).strip() for item in as_list(lead.get("COMPANY_ID")) if is_real_id(item)}

    activities = fetch_activities(client, lead_id)
    activity_items = activities.get("items", [])
    activity_details = fetch_activity_details(client, activity_items)
    timeline_comments = fetch_timeline_comments(client, lead_id)

    contacts = {
        contact_id: fetch_entity_by_id(client, "crm.contact.get", contact_id)
        for contact_id in sorted(contact_ids)
    }
    companies = {
        company_id: fetch_entity_by_id(client, "crm.company.get", company_id)
        for company_id in sorted(company_ids)
    }

    bundle = {
        "lead_id": str(lead_id),
        "generated_at": datetime.now(MSK_TZ).isoformat(),
        "lead": lead_response,
        "contacts": contacts,
        "companies": companies,
        "activities": activities,
        "activity_details": activity_details,
        "timeline_comments": timeline_comments,
    }
    bundle["file_and_recording_refs"] = extract_refs(bundle)
    return bundle


def main() -> None:
    args = parse_args()
    load_dotenv()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"))

    index_items = []
    for lead_id in args.lead_ids:
        client.retry_callback = retry_progress_callback(
            "lead", str(lead_id), "crm_context", detail="Запрос к Bitrix"
        )
        bundle = fetch_lead_bundle(client, str(lead_id))
        output_path = output_dir / f"lead_{lead_id}_context.json"
        save_json(output_path, bundle)
        customer_history_path = None
        if args.include_related_contact_deals:
            customer_history = build_customer_history_bundle(
                client,
                root_type="lead",
                root_id=str(lead_id),
                history_days=args.history_days,
                include_internal_context=args.include_internal_context,
                pipeline_map_path=Path(args.pipeline_map),
            )
            customer_history_path = output_dir / f"lead_{lead_id}_customer_history_bundle.json"
            save_json(customer_history_path, customer_history)
            logger.info("Saved customer history bundle: %s", customer_history_path)
        index_items.append(
            {
                "lead_id": str(lead_id),
                "output_path": str(output_path),
                "customer_history_path": str(customer_history_path) if customer_history_path else None,
            }
        )
        logger.info("Saved raw lead context: %s", output_path)

    save_json(
        output_dir / "index.json",
        {"generated_at": datetime.now(MSK_TZ).isoformat(), "items": index_items},
    )


if __name__ == "__main__":
    main()
