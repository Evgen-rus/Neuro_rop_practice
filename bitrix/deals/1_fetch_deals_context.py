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
from bitrix.usage_trace import bitrix_trace_context
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


DEFAULT_DEAL_IDS = ["18507", "18493"]
DEFAULT_OUTPUT_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEAL_OWNER_TYPE_ID = 2
LEAD_OWNER_TYPE_ID = 1
MAX_OPEN_TASKS_FOR_CONTEXT = 3
TASK_CLOSED_STATUSES = {"5", "7"}
ATTACHMENT_KEY_TOKENS = ("ATTACH", "DOWNLOAD", "FILE", "DISK", "STORAGE")

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
    if not is_real_id(entity_id):
        return {"ok": False, "method": method, "payload": {"id": entity_id}, "error": "empty id"}
    with bitrix_trace_context(component="entity_get"):
        return client.safe_call(method, {"id": entity_id})


def fetch_users(client: BitrixReadOnlyClient, ids: list[Any]) -> dict[str, Any]:
    users: dict[str, Any] = {}
    user_ids = sorted({str(item) for item in ids if item})
    requests_to_run = [
        (f"user:{user_id}", "user.get", {"ID": user_id})
        for user_id in user_ids
    ]
    batch = getattr(client, "safe_batch_call", None)
    responses = (
        batch(requests_to_run)
        if callable(batch)
        else {
            key: client.safe_call(method, payload)
            for key, method, payload in requests_to_run
        }
    )
    for user_id in user_ids:
        response = responses[f"user:{user_id}"]
        result = get_result(response)
        users[user_id] = {
            "response": response,
            "user": result[0] if isinstance(result, list) and result else None,
        }
    return users


def fetch_field_schemas(client: BitrixReadOnlyClient, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    methods = {
        "deal": "crm.deal.fields",
        "activity": "crm.activity.fields",
        "contact": "crm.contact.fields",
        "company": "crm.company.fields",
    }
    rows: dict[str, Any] = {}
    for key, method in methods.items():
        cached = (previous or {}).get(key)
        rows[key] = cached if isinstance(cached, dict) and cached.get("ok") else client.safe_call(method)
    return rows


def fetch_timeline_comments(
    client: BitrixReadOnlyClient,
    entity_id: str,
    *,
    entity_type: str = "deal",
    owner_type_id: int = DEAL_OWNER_TYPE_ID,
    created_after: str | None = None,
) -> list[dict[str, Any]]:
    del owner_type_id
    timeline_filter: dict[str, Any] = {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id}
    del created_after
    with bitrix_trace_context(component="timeline"):
        return [
            client.safe_list_all(
                "crm.timeline.comment.list",
                {"order": {"CREATED": "ASC", "ID": "ASC"}, "filter": timeline_filter},
            )
        ]


def fetch_activities_for_owner(
    client: BitrixReadOnlyClient,
    owner_type_id: int,
    owner_id: str,
    *,
    updated_after: str | None = None,
) -> dict[str, Any]:
    activity_filter: dict[str, Any] = {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": owner_id}
    if updated_after:
        activity_filter[">LAST_UPDATED"] = updated_after
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": activity_filter,
        "select": ["*", "FILES", "COMMUNICATIONS"],
    }
    with bitrix_trace_context(component="per_deal_context"):
        return client.safe_list_all("crm.activity.list", payload)


def fetch_activities(client: BitrixReadOnlyClient, deal_id: str) -> dict[str, Any]:
    return fetch_activities_for_owner(client, DEAL_OWNER_TYPE_ID, deal_id)


def fetch_activity_details(client: BitrixReadOnlyClient, activities: list[dict[str, Any]]) -> dict[str, Any]:
    del client
    return activity_details_from_list(activities)


def merge_list_response(
    response: dict[str, Any],
    previous_items: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = merge_items_by_id(previous_items, response.get("items") or [])
    value = dict(response)
    value["items"] = merged
    return value


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
        "entity_type": "deal",
        "entity_id": str(bundle.get("deal_id") or ""),
        "sync_mode": ((bundle.get("sync") or {}).get("mode") or "full"),
        "updated_after": (bundle.get("sync") or {}).get("updated_after"),
        "activities": activities_container,
        "activity_details": activity_details_from_list(activities),
        "timeline_comments": [timeline_base],
    }


def task_ids_from_activities(activities: list[dict[str, Any]]) -> list[str]:
    task_ids: set[str] = set()
    for activity in activities:
        provider_id = str(activity.get("PROVIDER_ID") or "").upper()
        if provider_id != "CRM_TASKS_TASK":
            continue
        task_id = activity.get("ASSOCIATED_ENTITY_ID")
        if is_real_id(task_id):
            task_ids.add(str(task_id))
    return sorted(task_ids, key=int)


def task_item(call_result: dict[str, Any] | None) -> dict[str, Any]:
    if not call_result or not call_result.get("ok"):
        return {}
    result = call_result.get("response", {}).get("result")
    if not isinstance(result, dict):
        return {}
    task = result.get("task")
    return task if isinstance(task, dict) else {}


def select_open_task_ids(
    task_responses: dict[str, dict[str, Any]],
    *,
    limit: int = MAX_OPEN_TASKS_FOR_CONTEXT,
) -> list[str]:
    rows: list[tuple[str, str]] = []
    for task_id, response in task_responses.items():
        task = task_item(response)
        if not task:
            continue
        status = str(task.get("status") or "")
        if status in TASK_CLOSED_STATUSES or task.get("closedDate"):
            continue
        deadline = str(task.get("deadline") or "9999-12-31T23:59:59+03:00")
        rows.append((deadline, str(task_id)))
    rows.sort(
        key=lambda item: (
            item[0],
            int(item[1]) if item[1].isdigit() else sys.maxsize,
            item[1],
        )
    )
    return [task_id for _, task_id in rows[:limit]]


def task_chat_id(task: dict[str, Any]) -> str | None:
    value = task.get("chatId") or task.get("chat_id")
    return str(value) if is_real_id(value) else None


def strip_attachment_references(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            upper_key = str(key).upper()
            if any(token in upper_key for token in ATTACHMENT_KEY_TOKENS):
                cleaned[key] = "[excluded: attachment reference]"
            else:
                cleaned[key] = strip_attachment_references(child)
        return cleaned
    if isinstance(value, list):
        return [strip_attachment_references(item) for item in value]
    return value


def fetch_deal_stage_history(client: BitrixReadOnlyClient, deal_id: str) -> dict[str, Any]:
    with bitrix_trace_context(component="stage_history"):
        return client.safe_list_all(
            "crm.stagehistory.list",
            {
                "entityTypeId": DEAL_OWNER_TYPE_ID,
                "order": {"CREATED_TIME": "ASC", "ID": "ASC"},
                "filter": {"OWNER_ID": deal_id},
                "select": ["*"],
            },
        )


def fetch_task_context(
    client: BitrixReadOnlyClient,
    activities: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, dict[str, Any]]]:
    task_ids = task_ids_from_activities(activities)
    requests_to_run = [
        (f"task:{task_id}", "tasks.task.get", {"taskId": task_id, "select": ["*"]})
        for task_id in task_ids
    ]
    with bitrix_trace_context(component="task_history"):
        batch = getattr(client, "safe_batch_call", None)
        responses = (
            batch(requests_to_run)
            if callable(batch)
            else {
                key: client.safe_call(method, payload)
                for key, method, payload in requests_to_run
            }
        )
        task_responses = {
            task_id: responses[f"task:{task_id}"]
            for task_id in task_ids
        }
        open_task_ids = select_open_task_ids(task_responses)
        task_chats: dict[str, dict[str, Any]] = {}
        for task_id in open_task_ids:
            chat_id = task_chat_id(task_item(task_responses.get(task_id)))
            if not chat_id:
                continue
            response = client.safe_call(
                "im.dialog.messages.get",
                {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50},
            )
            task_chats[task_id] = strip_attachment_references(response)
        return task_responses, open_task_ids, task_chats


def fetch_source_lead_context(
    client: BitrixReadOnlyClient,
    lead_id: Any,
    *,
    previous_context: dict[str, Any] | None = None,
    updated_after: str | None = None,
) -> dict[str, Any] | None:
    if not lead_id:
        return None

    lead_id = str(lead_id)
    if previous_context and str(previous_context.get("lead_id") or "") != lead_id:
        previous_context = None
        updated_after = None
    lead_response = fetch_entity_by_id(client, "crm.lead.get", lead_id)
    incremental = bool(previous_context and updated_after)
    activities_response = fetch_activities_for_owner(
        client,
        LEAD_OWNER_TYPE_ID,
        lead_id,
        updated_after=updated_after if incremental else None,
    )
    if incremental:
        activities_response = merge_list_response(
            activities_response,
            ((previous_context or {}).get("activities") or {}).get("items") or [],
        )
    activities = activities_response.get("items", [])
    activity_details = fetch_activity_details(client, activities)
    timeline_comments = fetch_timeline_comments(
        client,
        lead_id,
        entity_type="lead",
        owner_type_id=LEAD_OWNER_TYPE_ID,
        created_after=updated_after if incremental else None,
    )
    if incremental:
        timeline_comments = merge_timeline_responses(
            timeline_comments,
            (previous_context or {}).get("timeline_comments") or [],
        )

    return {
        "lead_id": lead_id,
        "lead": {"response": lead_response, "item": get_result(lead_response) or {}},
        "activities": activities_response,
        "activity_details": activity_details,
        "timeline_comments": timeline_comments,
        "sync_mode": "incremental" if incremental else "full",
        "activity_fetch_ok": bool(activities_response.get("ok")),
    }


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
    *,
    previous_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.info("Fetching deal context: deal_id=%s", deal_id)
    sync_started_at = datetime.now(MSK_TZ).isoformat(timespec="seconds")
    deal_response = fetch_entity_by_id(client, "crm.deal.get", deal_id)
    deal = get_result(deal_response) or {}

    contact_ids = {str(item).strip() for item in as_list(deal.get("CONTACT_ID")) if is_real_id(item)}
    contact_items_response = client.safe_call("crm.deal.contact.items.get", {"id": deal_id})
    contact_items = get_result(contact_items_response)
    if isinstance(contact_items, list):
        for item in contact_items:
            contact_id = item.get("CONTACT_ID") if isinstance(item, dict) else None
            if is_real_id(contact_id):
                contact_ids.add(str(contact_id).strip())

    company_id = deal.get("COMPANY_ID")
    contacts = {
        contact_id: fetch_entity_by_id(client, "crm.contact.get", contact_id)
        for contact_id in sorted(contact_ids)
    }
    company = fetch_entity_by_id(client, "crm.company.get", company_id) if is_real_id(company_id) else None

    updated_after = incremental_since(previous_bundle)
    incremental = bool(previous_bundle and updated_after)
    activities_response = fetch_activities_for_owner(
        client,
        DEAL_OWNER_TYPE_ID,
        deal_id,
        updated_after=updated_after if incremental else None,
    )
    if incremental:
        activities_response = merge_list_response(
            activities_response,
            ((previous_bundle or {}).get("activities") or {}).get("items") or [],
        )
    activities = activities_response.get("items", [])
    activity_details = fetch_activity_details(client, activities)
    stage_history = fetch_deal_stage_history(client, deal_id)
    bitrix_tasks, open_task_ids, bitrix_task_chats = fetch_task_context(client, activities)
    timeline_comments = fetch_timeline_comments(
        client,
        deal_id,
        created_after=updated_after if incremental else None,
    )
    if incremental:
        timeline_comments = merge_timeline_responses(
            timeline_comments,
            (previous_bundle or {}).get("timeline_comments") or [],
        )
    source_lead = fetch_source_lead_context(
        client,
        deal.get("LEAD_ID"),
        previous_context=(previous_bundle or {}).get("source_lead"),
        updated_after=updated_after,
    )
    activity_sync_ok = bool(activities_response.get("ok")) and (
        source_lead is None or bool(source_lead.get("activity_fetch_ok"))
    )
    activity_cursor = sync_started_at if activity_sync_ok else activity_cursor_value(previous_bundle)

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
    if source_lead:
        source_lead_item = source_lead.get("lead", {}).get("item", {})
        user_ids.extend(
            [
                source_lead_item.get("ASSIGNED_BY_ID"),
                source_lead_item.get("CREATED_BY_ID"),
                source_lead_item.get("MODIFY_BY_ID"),
            ]
        )
        for activity in source_lead.get("activities", {}).get("items", []):
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
        "source_lead": source_lead,
    }

    stage_id = str(deal.get("STAGE_ID") or "")
    bundle = {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "read_only": True,
        "deal_id": deal_id,
        "stage_info": stage_lookup.get(stage_id),
        "stage_history": stage_history,
        "stage_history_lookup": {
            str(item.get("STAGE_ID")): stage_lookup.get(str(item.get("STAGE_ID")))
            for item in stage_history.get("items", [])
            if isinstance(item, dict) and item.get("STAGE_ID")
        },
        "deal": {"response": deal_response, "item": deal},
        "company": company,
        "contacts": contacts,
        "deal_contacts": contact_items_response,
        "users": fetch_users(client, user_ids),
        "fields": fetch_field_schemas(client, (previous_bundle or {}).get("fields")),
        "product_rows": None,
        "source_lead": source_lead,
        "activities": activities_response,
        "activity_details": activity_details,
        "bitrix_tasks": bitrix_tasks,
        "bitrix_open_task_ids": open_task_ids,
        "bitrix_task_chats": bitrix_task_chats,
        "timeline_comments": timeline_comments,
        "invoice_attempts": [],
        "file_and_recording_refs": extract_refs(references_source),
        "sync": {
            "mode": "incremental" if incremental else "full",
            "updated_after": updated_after if incremental else None,
            "activity_cursor": activity_cursor,
            "activity_sync_ok": activity_sync_ok,
            "automatic_full_reconciliation": False,
        },
    }
    with bitrix_trace_context(component="product_rows"):
        bundle["product_rows"] = client.safe_call("crm.deal.productrows.get", {"id": deal_id})
    with bitrix_trace_context(component="invoice"):
        bundle["invoice_attempts"] = [
            client.safe_list_all("crm.invoice.list", {"filter": {"UF_DEAL_ID": deal_id}, "select": ["*"]}),
            client.safe_list_all("crm.item.list", {"entityTypeId": 31, "filter": {"parentId2": deal_id}}),
        ]
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
        client.retry_callback = retry_progress_callback(
            "deal", str(deal_id), "crm_context", detail="Запрос к Bitrix"
        )
        output_path = output_dir / f"deal_{deal_id}_context.json"
        try:
            previous_bundle = load_json(output_path) if output_path.exists() else None
        except ValueError:
            previous_bundle = None
        bundle = fetch_deal_bundle(
            client,
            str(deal_id),
            stage_lookup,
            previous_bundle=previous_bundle if isinstance(previous_bundle, dict) else None,
        )
        deal = bundle.get("deal", {}).get("item", {})
        save_json(output_path, bundle)
        customer_history_path = None
        if args.include_related_contact_deals:
            customer_history_path = output_dir / f"deal_{deal_id}_customer_history_bundle.json"
            try:
                previous_customer_history = load_json(customer_history_path) if customer_history_path.exists() else None
            except ValueError:
                previous_customer_history = None
            company_id = str(deal.get("COMPANY_ID") or "").strip()
            preloaded_companies = {company_id: bundle.get("company")} if company_id and bundle.get("company") else {}
            customer_history = build_customer_history_bundle(
                client,
                root_type="deal",
                root_id=str(deal_id),
                history_days=args.history_days,
                include_internal_context=args.include_internal_context,
                pipeline_map_path=Path(args.pipeline_map),
                previous_bundle=previous_customer_history if isinstance(previous_customer_history, dict) else None,
                root_response_override=(bundle.get("deal") or {}).get("response"),
                root_history_override=root_history_from_bundle(bundle, history_days=args.history_days),
                root_contact_items_override=bundle.get("deal_contacts"),
                preloaded_contacts=bundle.get("contacts") or {},
                preloaded_companies=preloaded_companies,
            )
            customer_history["deal_operational_context"] = {
                "deal": bundle.get("deal"),
                "fields": {"deal": (bundle.get("fields") or {}).get("deal")},
                "stage_history": bundle.get("stage_history"),
                "stage_history_lookup": bundle.get("stage_history_lookup"),
                "bitrix_tasks": bundle.get("bitrix_tasks"),
                "bitrix_open_task_ids": bundle.get("bitrix_open_task_ids"),
                "bitrix_task_chats": bundle.get("bitrix_task_chats"),
            }
            save_json(customer_history_path, customer_history)
            logger.info("Saved customer history bundle: %s", customer_history_path)
        index.append(
            {
                "deal_id": str(deal_id),
                "title": deal.get("TITLE"),
                "stage_id": deal.get("STAGE_ID"),
                "output_path": str(output_path),
                "customer_history_path": str(customer_history_path) if customer_history_path else None,
            }
        )
        logger.info("Saved raw deal context: %s", output_path)

    index_path = output_dir / "index.json"
    save_json(index_path, {"generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"), "items": index})
    logger.info("Saved raw index: %s", index_path)


if __name__ == "__main__":
    main()
