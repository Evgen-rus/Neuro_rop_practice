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

from bitrix.client import BitrixReadOnlyClient, as_list, get_env_required, load_json, save_json
from progress_events import retry_progress_callback
from bitrix.customer_history import (
    DEFAULT_HISTORY_DAYS,
    activity_cursor_value,
    activity_details_from_list,
    build_customer_history_bundle,
    history_period,
    incremental_since,
    in_period_by_any_date,
    is_real_id,
    merge_items_by_id,
)
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


def fetch_activities(client: BitrixReadOnlyClient, lead_id: str, *, updated_after: str | None = None) -> dict[str, Any]:
    activity_filter: dict[str, Any] = {"OWNER_TYPE_ID": LEAD_OWNER_TYPE_ID, "OWNER_ID": lead_id}
    if updated_after:
        activity_filter[">LAST_UPDATED"] = updated_after
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": activity_filter,
        "select": ["*", "FILES", "COMMUNICATIONS"],
    }
    return client.safe_list_all("crm.activity.list", payload)


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    del client
    return activity_details_from_list(activities)


def fetch_timeline_comments(
    client: BitrixReadOnlyClient,
    lead_id: str,
    *,
    created_after: str | None = None,
) -> list[dict[str, Any]]:
    timeline_filter: dict[str, Any] = {"ENTITY_TYPE": "lead", "ENTITY_ID": lead_id}
    del created_after
    return [
        client.safe_list_all(
            "crm.timeline.comment.list",
            {"order": {"CREATED": "ASC", "ID": "ASC"}, "filter": timeline_filter},
        )
    ]


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


def merge_timeline_responses(
    responses: list[dict[str, Any]],
    previous_responses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_items = [item for response in responses for item in response.get("items") or [] if isinstance(item, dict)]
    previous_items = [
        item
        for response in previous_responses
        for item in response.get("items") or []
        if isinstance(item, dict)
    ]
    base = dict(responses[0]) if responses else {"ok": False, "items": []}
    base["items"] = merge_items_by_id(previous_items, current_items)
    return [base]


def root_history_from_bundle(bundle: dict[str, Any], *, history_days: int) -> dict[str, Any]:
    period = history_period(history_days)
    activities = [
        item
        for item in ((bundle.get("activities") or {}).get("items") or [])
        if in_period_by_any_date(item, period, ("START_TIME", "DEADLINE", "CREATED", "LAST_UPDATED"))
    ]
    timeline_items = [
        item
        for response in bundle.get("timeline_comments") or []
        for item in response.get("items") or []
        if in_period_by_any_date(item, period, ("CREATED", "DATE_CREATE"))
    ]
    timeline_base = dict((bundle.get("timeline_comments") or [{}])[0])
    timeline_base["items"] = timeline_items
    activities_container = dict(bundle.get("activities") or {})
    activities_container["items"] = activities
    return {
        "entity_type": "lead",
        "entity_id": str(bundle.get("lead_id") or ""),
        "sync_mode": ((bundle.get("sync") or {}).get("mode") or "full"),
        "updated_after": (bundle.get("sync") or {}).get("updated_after"),
        "activities": activities_container,
        "activity_details": activity_details_from_list(activities),
        "timeline_comments": [timeline_base],
    }


def fetch_lead_bundle(
    client: BitrixReadOnlyClient,
    lead_id: str,
    *,
    previous_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.info("Fetching lead context: lead_id=%s", lead_id)
    sync_started_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    lead_response = fetch_entity_by_id(client, "crm.lead.get", lead_id)
    lead = get_result(lead_response) or {}

    contact_ids = {str(item).strip() for item in as_list(lead.get("CONTACT_ID")) if is_real_id(item)}
    company_ids = {str(item).strip() for item in as_list(lead.get("COMPANY_ID")) if is_real_id(item)}

    updated_after = incremental_since(previous_bundle)
    incremental = bool(previous_bundle and updated_after)
    activities = fetch_activities(client, lead_id, updated_after=updated_after if incremental else None)
    if incremental:
        merged_items = merge_items_by_id(
            ((previous_bundle or {}).get("activities") or {}).get("items") or [],
            activities.get("items") or [],
        )
        activities = {**activities, "items": merged_items}
    activity_items = activities.get("items", [])
    activity_details = fetch_activity_details(client, activity_items)
    timeline_comments = fetch_timeline_comments(
        client,
        lead_id,
        created_after=updated_after if incremental else None,
    )
    if incremental:
        timeline_comments = merge_timeline_responses(
            timeline_comments,
            (previous_bundle or {}).get("timeline_comments") or [],
        )
    activity_sync_ok = bool(activities.get("ok"))
    activity_cursor = sync_started_at if activity_sync_ok else activity_cursor_value(previous_bundle)

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
        "sync": {
            "mode": "incremental" if incremental else "full",
            "updated_after": updated_after if incremental else None,
            "activity_cursor": activity_cursor,
            "activity_sync_ok": activity_sync_ok,
            "automatic_full_reconciliation": False,
        },
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
        output_path = output_dir / f"lead_{lead_id}_context.json"
        try:
            previous_bundle = load_json(output_path) if output_path.exists() else None
        except ValueError:
            previous_bundle = None
        bundle = fetch_lead_bundle(
            client,
            str(lead_id),
            previous_bundle=previous_bundle if isinstance(previous_bundle, dict) else None,
        )
        save_json(output_path, bundle)
        customer_history_path = None
        if args.include_related_contact_deals:
            customer_history_path = output_dir / f"lead_{lead_id}_customer_history_bundle.json"
            try:
                previous_customer_history = load_json(customer_history_path) if customer_history_path.exists() else None
            except ValueError:
                previous_customer_history = None
            customer_history = build_customer_history_bundle(
                client,
                root_type="lead",
                root_id=str(lead_id),
                history_days=args.history_days,
                include_internal_context=args.include_internal_context,
                pipeline_map_path=Path(args.pipeline_map),
                previous_bundle=previous_customer_history if isinstance(previous_customer_history, dict) else None,
                root_response_override=bundle.get("lead"),
                root_history_override=root_history_from_bundle(bundle, history_days=args.history_days),
                preloaded_contacts=bundle.get("contacts") or {},
                preloaded_companies=bundle.get("companies") or {},
            )
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
