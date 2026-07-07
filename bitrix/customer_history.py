"""
Read-only customer history bundle builder for lead/deal analysis.

The bundle broadens a root lead/deal context to the customer level:
root entity -> contact(s) -> related contact deals -> CRM activities and
timeline comments. It does not write anything to Bitrix.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.client import BitrixReadOnlyClient, as_list, load_json
from setup import BASE_DIR, MSK_TZ


LEAD_OWNER_TYPE_ID = 1
DEAL_OWNER_TYPE_ID = 2
CONTACT_OWNER_TYPE_ID = 3

DEFAULT_HISTORY_DAYS = 365


def get_result(call_result: dict[str, Any] | None) -> Any:
    if not call_result or not call_result.get("ok"):
        return None
    return call_result.get("response", {}).get("result")


def result_item(call_result: dict[str, Any] | None) -> dict[str, Any]:
    result = get_result(call_result)
    return result if isinstance(result, dict) else {}


def result_items(call_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_result:
        return []
    if isinstance(call_result.get("items"), list):
        return [item for item in call_result["items"] if isinstance(item, dict)]
    result = get_result(call_result)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return [item for item in result["items"] if isinstance(item, dict)]
    return []


def clean_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\[url=([^\]]+)\]([^\[]+)\[/url\]", r"\2", text, flags=re.I)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<head[^>]*>.*?</head>", "", text, flags=re.I | re.S)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</div\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if limit and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def parse_bitrix_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed


def history_period(days: int) -> dict[str, Any]:
    normalized_days = max(1, int(days or DEFAULT_HISTORY_DAYS))
    date_to = datetime.now(MSK_TZ)
    date_from = date_to - timedelta(days=normalized_days)
    return {
        "days": normalized_days,
        "date_from": date_from.isoformat(timespec="seconds"),
        "date_to": date_to.isoformat(timespec="seconds"),
    }


def in_period_by_any_date(item: dict[str, Any], period: dict[str, Any], date_keys: tuple[str, ...]) -> bool:
    date_from = parse_bitrix_datetime(period.get("date_from"))
    if not date_from:
        return True
    dates = [parse_bitrix_datetime(item.get(key)) for key in date_keys]
    dates = [value for value in dates if value is not None]
    if not dates:
        return True
    return any(value >= date_from for value in dates)


def build_stage_lookup(pipeline_map_path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = pipeline_map_path or BASE_DIR / "crm_pipeline_map.json"
    if not path.exists():
        return {}
    try:
        crm_map = load_json(path)
    except ValueError:
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    for pipeline in crm_map.get("deal_pipelines", []):
        for stage in pipeline.get("stages", []):
            status_id = stage.get("status_id")
            if not status_id:
                continue
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


def fetch_activities_for_owner(client: BitrixReadOnlyClient, owner_type_id: int, owner_id: str) -> dict[str, Any]:
    payload = {
        "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
        "filter": {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": owner_id},
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


def fetch_timeline_comments(
    client: BitrixReadOnlyClient,
    entity_id: str,
    *,
    entity_type: str,
    owner_type_id: int,
) -> list[dict[str, Any]]:
    attempts = [
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": entity_id},
        },
        {
            "order": {"CREATED": "ASC", "ID": "ASC"},
            "filter": {"ENTITY_TYPE_ID": owner_type_id, "ENTITY_ID": entity_id},
        },
    ]
    responses = []
    for payload in attempts:
        response = client.safe_list_all("crm.timeline.comment.list", payload)
        responses.append(response)
        if response.get("ok") and response.get("items"):
            break
    return responses


def contact_ids_from_deal(client: BitrixReadOnlyClient, deal_id: str, deal: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    contact_ids = {str(item) for item in as_list(deal.get("CONTACT_ID")) if item}
    contact_items_response = client.safe_call("crm.deal.contact.items.get", {"id": deal_id})
    contact_items = get_result(contact_items_response)
    if isinstance(contact_items, list):
        for item in contact_items:
            if isinstance(item, dict) and item.get("CONTACT_ID"):
                contact_ids.add(str(item["CONTACT_ID"]))
    return sorted(contact_ids), contact_items_response


def contact_ids_from_lead(lead: dict[str, Any]) -> list[str]:
    return sorted({str(item) for item in as_list(lead.get("CONTACT_ID")) if item})


def multifield_values(entity: dict[str, Any], field: str) -> list[str]:
    value = entity.get(field) or []
    if isinstance(value, list):
        return [str(item.get("VALUE")) for item in value if isinstance(item, dict) and item.get("VALUE")]
    if value:
        return [str(value)]
    return []


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def phones_match(left: Any, right: Any) -> bool:
    left_digits = normalize_phone(left)
    right_digits = normalize_phone(right)
    if not left_digits or not right_digits:
        return False
    return left_digits == right_digits or left_digits[-10:] == right_digits[-10:]


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def fallback_candidates_from_root(root_type: str, root_item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for phone in multifield_values(root_item, "PHONE"):
        candidates.append({"type": "phone", "value": phone, "source": f"{root_type}.PHONE"})
    for email in multifield_values(root_item, "EMAIL"):
        candidates.append({"type": "email", "value": email, "source": f"{root_type}.EMAIL"})
    company_id = root_item.get("COMPANY_ID")
    if company_id:
        candidates.append({"type": "company_id", "value": str(company_id), "source": f"{root_type}.COMPANY_ID"})
    company_title = root_item.get("COMPANY_TITLE")
    if company_title:
        candidates.append({"type": "company_title", "value": str(company_title), "source": f"{root_type}.COMPANY_TITLE"})
    return candidates


def extract_entity_ids_from_duplicate_result(value: Any, entity_type: str) -> set[str]:
    entity_ids: set[str] = set()
    expected_key = entity_type.upper()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).upper() == expected_key:
                if isinstance(child, list):
                    entity_ids.update(str(item) for item in child if item)
                elif isinstance(child, dict):
                    entity_ids.update(str(item) for item in child.keys() if item)
                    entity_ids.update(str(item) for item in child.values() if isinstance(item, (str, int)) and item)
                elif child:
                    entity_ids.add(str(child))
            else:
                entity_ids.update(extract_entity_ids_from_duplicate_result(child, entity_type))
    elif isinstance(value, list):
        for child in value:
            entity_ids.update(extract_entity_ids_from_duplicate_result(child, entity_type))
    return entity_ids


def extract_contact_ids_from_duplicate_result(value: Any) -> set[str]:
    return extract_entity_ids_from_duplicate_result(value, "CONTACT")


def contact_matches_candidate(contact: dict[str, Any], candidate: dict[str, Any]) -> bool:
    candidate_type = str(candidate.get("type") or "")
    candidate_value = candidate.get("value")
    if candidate_type == "phone":
        return any(phones_match(phone, candidate_value) for phone in multifield_values(contact, "PHONE"))
    if candidate_type == "email":
        expected = normalize_email(candidate_value)
        return any(normalize_email(email) == expected for email in multifield_values(contact, "EMAIL"))
    return False


def lead_matches_candidate(lead: dict[str, Any], candidate: dict[str, Any]) -> bool:
    candidate_type = str(candidate.get("type") or "")
    candidate_value = candidate.get("value")
    if candidate_type == "phone":
        return any(phones_match(phone, candidate_value) for phone in multifield_values(lead, "PHONE"))
    if candidate_type == "email":
        expected = normalize_email(candidate_value)
        return any(normalize_email(email) == expected for email in multifield_values(lead, "EMAIL"))
    return False


def search_contact_ids_by_candidate(
    client: BitrixReadOnlyClient,
    candidate: dict[str, Any],
) -> tuple[set[str], list[dict[str, Any]]]:
    candidate_type = str(candidate.get("type") or "")
    value = str(candidate.get("value") or "").strip()
    attempts: list[dict[str, Any]] = []
    contact_ids: set[str] = set()
    if candidate_type not in {"phone", "email"} or not value:
        return contact_ids, attempts

    comm_type = "PHONE" if candidate_type == "phone" else "EMAIL"
    duplicate_payloads = [
        {"type": comm_type, "values": [value], "entity_type": "CONTACT"},
        {"type": comm_type, "values": [value]},
    ]
    for payload in duplicate_payloads:
        response = client.safe_call("crm.duplicate.findbycomm", payload)
        attempts.append({"method": "crm.duplicate.findbycomm", "payload": payload, "response": response})
        if response.get("ok"):
            contact_ids.update(extract_contact_ids_from_duplicate_result(get_result(response)))

    list_payload = {
        "order": {"ID": "ASC"},
        "filter": {comm_type: value},
        "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME", "PHONE", "EMAIL"],
    }
    list_response = client.safe_list_all("crm.contact.list", list_payload)
    attempts.append({"method": "crm.contact.list", "payload": list_payload, "response": list_response})
    for item in result_items(list_response):
        if item.get("ID"):
            contact_ids.add(str(item["ID"]))

    return contact_ids, attempts


def search_lead_ids_by_candidate(
    client: BitrixReadOnlyClient,
    candidate: dict[str, Any],
) -> tuple[set[str], list[dict[str, Any]]]:
    candidate_type = str(candidate.get("type") or "")
    value = str(candidate.get("value") or "").strip()
    attempts: list[dict[str, Any]] = []
    lead_ids: set[str] = set()
    if candidate_type not in {"phone", "email"} or not value:
        return lead_ids, attempts

    comm_type = "PHONE" if candidate_type == "phone" else "EMAIL"
    duplicate_payloads = [
        {"type": comm_type, "values": [value], "entity_type": "LEAD"},
        {"type": comm_type, "values": [value]},
    ]
    for payload in duplicate_payloads:
        response = client.safe_call("crm.duplicate.findbycomm", payload)
        attempts.append({"method": "crm.duplicate.findbycomm", "payload": payload, "response": response})
        if response.get("ok"):
            lead_ids.update(extract_entity_ids_from_duplicate_result(get_result(response), "LEAD"))

    list_payload = {
        "order": {"ID": "ASC"},
        "filter": {comm_type: value},
        "select": ["*", "UF_*"],
    }
    list_response = client.safe_list_all("crm.lead.list", list_payload)
    attempts.append({"method": "crm.lead.list", "payload": list_payload, "response": list_response})
    for item in result_items(list_response):
        if item.get("ID"):
            lead_ids.add(str(item["ID"]))

    return lead_ids, attempts


def resolve_contact_ids_by_fallback(
    client: BitrixReadOnlyClient,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    verified_matches: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        found_ids, candidate_attempts = search_contact_ids_by_candidate(client, candidate)
        attempts.extend(candidate_attempts)
        for contact_id in sorted(found_ids):
            response = fetch_entity_by_id(client, "crm.contact.get", contact_id)
            attempts.append(
                {
                    "method": "crm.contact.get",
                    "payload": {"id": contact_id},
                    "candidate": candidate,
                    "response": response,
                }
            )
            contact = result_item(response)
            if contact and contact_matches_candidate(contact, candidate):
                verified_matches[contact_id] = {
                    "contact_id": contact_id,
                    "matched_by": candidate.get("type"),
                    "source": candidate.get("source"),
                    "value": candidate.get("value"),
                }

    verified_contact_ids = sorted(verified_matches.keys(), key=lambda item: int(item) if item.isdigit() else item)
    return {
        "attempts": attempts,
        "verified_matches": list(verified_matches.values()),
        "verified_contact_ids": verified_contact_ids,
        "auto_contact_ids": verified_contact_ids if len(verified_contact_ids) == 1 else [],
        "ambiguous": len(verified_contact_ids) > 1,
    }


def resolve_lead_ids_by_fallback(
    client: BitrixReadOnlyClient,
    candidates: list[dict[str, Any]],
    *,
    root_lead_id: str | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    verified_matches: dict[str, dict[str, Any]] = {}
    lead_responses: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        found_ids, candidate_attempts = search_lead_ids_by_candidate(client, candidate)
        attempts.extend(candidate_attempts)
        for lead_id in sorted(found_ids):
            if root_lead_id and str(lead_id) == str(root_lead_id):
                continue
            response = fetch_entity_by_id(client, "crm.lead.get", lead_id)
            attempts.append(
                {
                    "method": "crm.lead.get",
                    "payload": {"id": lead_id},
                    "candidate": candidate,
                    "response": response,
                }
            )
            lead = result_item(response)
            if lead and lead_matches_candidate(lead, candidate):
                lead_responses[str(lead_id)] = response
                verified_matches[str(lead_id)] = {
                    "lead_id": str(lead_id),
                    "matched_by": candidate.get("type"),
                    "source": candidate.get("source"),
                    "value": candidate.get("value"),
                }

    verified_lead_ids = sorted(verified_matches.keys(), key=lambda item: int(item) if item.isdigit() else item)
    return {
        "attempts": attempts,
        "verified_matches": list(verified_matches.values()),
        "verified_lead_ids": verified_lead_ids,
        "lead_responses": lead_responses,
    }


def fetch_contacts(client: BitrixReadOnlyClient, contact_ids: list[str]) -> dict[str, Any]:
    return {
        contact_id: fetch_entity_by_id(client, "crm.contact.get", contact_id)
        for contact_id in sorted({str(item) for item in contact_ids if item})
    }


def fetch_company(client: BitrixReadOnlyClient, company_id: Any) -> dict[str, Any] | None:
    return fetch_entity_by_id(client, "crm.company.get", company_id) if company_id else None


def fetch_contact_deals(client: BitrixReadOnlyClient, contact_id: str) -> dict[str, Any]:
    payload = {
        "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
        "filter": {"CONTACT_ID": contact_id},
        "select": ["*", "UF_*"],
    }
    return client.safe_list_all("crm.deal.list", payload)


def fetch_deals_by_lead_id(client: BitrixReadOnlyClient, lead_id: str) -> dict[str, Any]:
    payload = {
        "order": {"DATE_MODIFY": "DESC", "ID": "DESC"},
        "filter": {"LEAD_ID": str(lead_id)},
        "select": ["*", "UF_*"],
    }
    return client.safe_list_all("crm.deal.list", payload)


def deal_summary(deal: dict[str, Any], stage_lookup: dict[str, dict[str, Any]]) -> dict[str, Any]:
    stage_id = str(deal.get("STAGE_ID") or "")
    stage_info = stage_lookup.get(stage_id) or {}
    stage = stage_info.get("stage") or {}
    pipeline = stage_info.get("pipeline") or {}
    return {
        "id": str(deal.get("ID") or ""),
        "title": deal.get("TITLE"),
        "category_id": deal.get("CATEGORY_ID"),
        "pipeline": pipeline,
        "stage_id": stage_id,
        "stage": stage,
        "stage_name": stage.get("name") or stage_id,
        "semantic_id": deal.get("STAGE_SEMANTIC_ID"),
        "opportunity": deal.get("OPPORTUNITY"),
        "currency_id": deal.get("CURRENCY_ID"),
        "date_create": deal.get("DATE_CREATE"),
        "date_modify": deal.get("DATE_MODIFY"),
        "closedate": deal.get("CLOSEDATE"),
        "assigned_by_id": deal.get("ASSIGNED_BY_ID"),
        "source_id": deal.get("SOURCE_ID"),
        "lead_id": deal.get("LEAD_ID"),
        "contact_id": deal.get("CONTACT_ID"),
        "company_id": deal.get("COMPANY_ID"),
        "is_closed": str(deal.get("CLOSED") or "").upper() in ("Y", "1", "TRUE"),
        "raw": deal,
    }


def lead_summary(lead: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(lead.get("ID") or ""),
        "title": lead.get("TITLE"),
        "status_id": lead.get("STATUS_ID"),
        "status_semantic_id": lead.get("STATUS_SEMANTIC_ID"),
        "opportunity": lead.get("OPPORTUNITY"),
        "currency_id": lead.get("CURRENCY_ID"),
        "date_create": lead.get("DATE_CREATE"),
        "date_modify": lead.get("DATE_MODIFY"),
        "date_closed": lead.get("DATE_CLOSED"),
        "assigned_by_id": lead.get("ASSIGNED_BY_ID"),
        "source_id": lead.get("SOURCE_ID"),
        "contact_id": lead.get("CONTACT_ID"),
        "company_id": lead.get("COMPANY_ID"),
        "raw": lead,
    }


def root_entity_response(client: BitrixReadOnlyClient, root_type: str, root_id: str) -> dict[str, Any]:
    if root_type == "lead":
        return fetch_entity_by_id(client, "crm.lead.get", root_id)
    if root_type == "deal":
        return fetch_entity_by_id(client, "crm.deal.get", root_id)
    raise ValueError(f"Unsupported root_type: {root_type}")


def owner_type_id(entity_type: str) -> int:
    if entity_type == "lead":
        return LEAD_OWNER_TYPE_ID
    if entity_type == "deal":
        return DEAL_OWNER_TYPE_ID
    if entity_type == "contact":
        return CONTACT_OWNER_TYPE_ID
    raise ValueError(f"Unsupported entity_type: {entity_type}")


def fetch_entity_history(
    client: BitrixReadOnlyClient,
    entity_type: str,
    entity_id: str,
    period: dict[str, Any],
) -> dict[str, Any]:
    owner_id = owner_type_id(entity_type)
    activities_response = fetch_activities_for_owner(client, owner_id, entity_id)
    activities = [
        item
        for item in result_items(activities_response)
        if in_period_by_any_date(item, period, ("START_TIME", "DEADLINE", "CREATED", "LAST_UPDATED"))
    ]
    activity_details = fetch_activity_details(client, activities)
    timeline_comments = fetch_timeline_comments(
        client,
        entity_id,
        entity_type=entity_type,
        owner_type_id=owner_id,
    )
    filtered_timeline = []
    for attempt in timeline_comments:
        items = [
            item
            for item in result_items(attempt)
            if in_period_by_any_date(item, period, ("CREATED", "DATE_CREATE"))
        ]
        response = dict(attempt)
        response["items"] = items
        filtered_timeline.append(response)
    return {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "activities": {**activities_response, "items": activities},
        "activity_details": activity_details,
        "timeline_comments": filtered_timeline,
    }


def activity_type(activity: dict[str, Any]) -> str:
    type_id = str(activity.get("TYPE_ID") or "")
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()
    if type_id == "2" or "CALL" in provider or "TELPHIN" in provider:
        return "call"
    if type_id == "4" or "EMAIL" in provider:
        return "email"
    if any(token in provider for token in ("IM", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")):
        return "message"
    if type_id == "6" or "TASK" in provider or "TODO" in provider:
        return "task"
    return "activity"


def is_openline_activity(activity: dict[str, Any]) -> bool:
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()
    return "OPENLINE" in provider or "OPEN_LINE" in provider


def merge_activity_detail(activity: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or "")
    detail_container = details.get(activity_id)
    detail = result_item(detail_container) if isinstance(detail_container, dict) else {}
    return {**activity, **detail} if detail else dict(activity)


def timeline_comment_items(history: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for attempt in history.get("timeline_comments") or []:
        for item in result_items(attempt):
            text = clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"), 500)
            identity = (str(item.get("ID") or ""), str(item.get("CREATED") or item.get("DATE_CREATE") or ""), text)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(item)
    return rows


def build_history_sections(bundle: dict[str, Any]) -> dict[str, Any]:
    client_touchpoints: list[dict[str, Any]] = []
    internal_context: list[dict[str, Any]] = []
    tasks_and_control: list[dict[str, Any]] = []
    system_events: list[dict[str, Any]] = []
    ignored_openline_events: list[dict[str, Any]] = []

    for entity_key, history in (bundle.get("activities_by_entity") or {}).items():
        entity_type = history.get("entity_type")
        entity_id = str(history.get("entity_id") or "")
        details = history.get("activity_details") or {}
        for activity in result_items(history.get("activities")):
            item = merge_activity_detail(activity, details)
            kind = activity_type(item)
            row = {
                "when": item.get("START_TIME") or item.get("CREATED") or item.get("DEADLINE"),
                "category": "activity",
                "event_type": kind,
                "entity_key": entity_key,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "id": str(item.get("ID") or ""),
                "subject": clean_text(item.get("SUBJECT"), 300),
                "text": clean_text(item.get("DESCRIPTION"), 900),
                "direction": item.get("DIRECTION"),
                "completed": item.get("COMPLETED"),
                "raw": item,
            }
            if is_openline_activity(item):
                ignored_openline_events.append(row)
            elif kind in {"call", "email", "message"}:
                client_touchpoints.append(row)
            elif kind == "task":
                tasks_and_control.append(row)
            else:
                system_events.append(row)

        for comment in timeline_comment_items(history):
            row = {
                "when": comment.get("CREATED") or comment.get("DATE_CREATE"),
                "category": "timeline_comment",
                "event_type": "internal_comment",
                "entity_key": entity_key,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "id": str(comment.get("ID") or ""),
                "author_id": comment.get("AUTHOR_ID") or comment.get("CREATED_BY"),
                "text": clean_text(comment.get("COMMENT") or comment.get("TEXT") or comment.get("DESCRIPTION"), 1200),
                "raw": comment,
            }
            internal_context.append(row)

    for deal in bundle.get("related_deals") or []:
        system_events.append(
            {
                "when": deal.get("date_modify") or deal.get("date_create"),
                "category": "deal_state",
                "event_type": "related_deal_current_state",
                "entity_key": f"deal:{deal.get('id')}",
                "entity_type": "deal",
                "entity_id": str(deal.get("id") or ""),
                "id": str(deal.get("id") or ""),
                "subject": clean_text(deal.get("title"), 300),
                "text": clean_text(
                    f"Воронка: {(deal.get('pipeline') or {}).get('name') or deal.get('category_id')}; "
                    f"стадия: {deal.get('stage_name')}; сумма: {deal.get('opportunity')} {deal.get('currency_id') or ''}",
                    700,
                ),
            }
        )

    for lead in bundle.get("related_leads") or []:
        system_events.append(
            {
                "when": lead.get("date_modify") or lead.get("date_create"),
                "category": "lead_state",
                "event_type": "related_lead_current_state",
                "entity_key": f"lead:{lead.get('id')}",
                "entity_type": "lead",
                "entity_id": str(lead.get("id") or ""),
                "id": str(lead.get("id") or ""),
                "subject": clean_text(lead.get("title"), 300),
                "text": clean_text(
                    f"Статус: {lead.get('status_id')}; сумма: {lead.get('opportunity')} {lead.get('currency_id') or ''}",
                    700,
                ),
            }
        )

    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("when") or ""), str(item.get("id") or ""))

    return {
        "client_touchpoints": sorted(client_touchpoints, key=sort_key),
        "internal_context": sorted(internal_context, key=sort_key),
        "tasks_and_control": sorted(tasks_and_control, key=sort_key),
        "system_events": sorted(system_events, key=sort_key),
        "ignored_openline_events": sorted(ignored_openline_events, key=sort_key),
        "unified_timeline": sorted(
            client_touchpoints + internal_context + tasks_and_control + system_events,
            key=sort_key,
        ),
    }


def unavailable_sources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    unavailable: list[dict[str, Any]] = [
        {
            "source": "task_comments",
            "reason": "not_implemented",
            "note": "Комментарии к задачам не выгружаются отдельным методом в текущем MVP.",
        }
    ]
    for entity_key, history in (bundle.get("activities_by_entity") or {}).items():
        activities = history.get("activities") or {}
        if not activities.get("ok"):
            unavailable.append({"source": "crm.activity.list", "entity": entity_key, "reason": activities.get("error")})
        timeline_attempts = history.get("timeline_comments") or []
        timeline_has_success = any(attempt.get("ok") for attempt in timeline_attempts)
        if not timeline_has_success:
            for attempt in timeline_attempts:
                if not attempt.get("ok"):
                    unavailable.append(
                        {"source": "crm.timeline.comment.list", "entity": entity_key, "reason": attempt.get("error")}
                    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in unavailable:
        key = (str(item.get("source") or ""), str(item.get("entity") or ""), str(item.get("reason") or item.get("note") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def tasks_by_entity(activities_by_entity: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for entity_key, history in activities_by_entity.items():
        details = history.get("activity_details") or {}
        tasks = []
        for activity in result_items(history.get("activities")):
            item = merge_activity_detail(activity, details)
            if activity_type(item) == "task":
                tasks.append(item)
        rows[entity_key] = tasks
    return rows


def build_customer_history_bundle(
    client: BitrixReadOnlyClient,
    *,
    root_type: str,
    root_id: str,
    history_days: int = DEFAULT_HISTORY_DAYS,
    include_internal_context: bool = True,
    pipeline_map_path: Path | None = None,
) -> dict[str, Any]:
    root_type = root_type.lower().strip()
    if root_type not in {"lead", "deal"}:
        raise ValueError("root_type must be 'lead' or 'deal'")

    period = history_period(history_days)
    stage_lookup = build_stage_lookup(pipeline_map_path)
    diagnostics: dict[str, Any] = {
        "missing_contact": False,
        "contact_id_missing": False,
        "fallback_match_used": False,
        "fallback_candidates": [],
        "fallback_matches": [],
        "fallback_lead_matches": [],
        "fallback_attempts": [],
        "fallback_related_leads_used": False,
        "unavailable_sources": [],
        "warnings": [],
    }

    root_response = root_entity_response(client, root_type, root_id)
    root_item = result_item(root_response)
    contact_items_response: dict[str, Any] | None = None
    related_lead_responses: dict[str, dict[str, Any]] = {}
    if root_type == "deal":
        contact_ids, contact_items_response = contact_ids_from_deal(client, root_id, root_item)
    else:
        contact_ids = contact_ids_from_lead(root_item)

    if not contact_ids:
        diagnostics["contact_id_missing"] = True
        diagnostics["fallback_candidates"] = fallback_candidates_from_root(root_type, root_item)
        fallback_result = resolve_contact_ids_by_fallback(client, diagnostics["fallback_candidates"])
        diagnostics["fallback_attempts"] = fallback_result["attempts"]
        diagnostics["fallback_matches"] = fallback_result["verified_matches"]
        if fallback_result["auto_contact_ids"]:
            contact_ids = fallback_result["auto_contact_ids"]
            diagnostics["fallback_match_used"] = True
            diagnostics["warnings"].append(
                "CONTACT_ID отсутствовал, контакт найден и подтвержден через fallback по телефону/email."
            )
        elif root_type == "lead":
            lead_fallback = resolve_lead_ids_by_fallback(
                client,
                diagnostics["fallback_candidates"],
                root_lead_id=root_id,
            )
            diagnostics["fallback_attempts"].extend(lead_fallback["attempts"])
            diagnostics["fallback_lead_matches"] = lead_fallback["verified_matches"]
            related_lead_responses = lead_fallback["lead_responses"]
            related_contact_ids = []
            for response in related_lead_responses.values():
                related_contact_ids.extend(contact_ids_from_lead(result_item(response)))
            if related_contact_ids:
                contact_ids = sorted({str(item) for item in related_contact_ids if item})
                diagnostics["fallback_match_used"] = True
                diagnostics["fallback_related_leads_used"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, контакт найден через подтвержденный дубль-лид по телефону/email."
                )
            elif related_lead_responses:
                diagnostics["missing_contact"] = True
                diagnostics["fallback_related_leads_used"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, fallback нашел подтвержденные дубль-лиды, но контакта в них нет."
                )
            elif fallback_result["ambiguous"]:
                diagnostics["missing_contact"] = True
                diagnostics["warnings"].append(
                    "CONTACT_ID отсутствовал, fallback нашел несколько подтвержденных контактов. Автосклейка не применена."
                )
            else:
                diagnostics["missing_contact"] = True
                diagnostics["warnings"].append(
                    "Контакт по CONTACT_ID не найден. Fallback по телефону/email не нашел подтвержденный контакт или дубль-лид."
                )
        elif fallback_result["ambiguous"]:
            diagnostics["missing_contact"] = True
            diagnostics["warnings"].append(
                "CONTACT_ID отсутствовал, fallback нашел несколько подтвержденных контактов. Автосклейка не применена."
            )
        else:
            diagnostics["missing_contact"] = True
            diagnostics["warnings"].append(
                "Контакт по CONTACT_ID не найден. Fallback по телефону/email не нашел подтвержденный контакт."
            )

    contacts = fetch_contacts(client, contact_ids)
    primary_contact_id = contact_ids[0] if contact_ids else None

    related_deals_by_id: dict[str, dict[str, Any]] = {}
    contact_deal_responses: dict[str, Any] = {}
    lead_deal_responses: dict[str, Any] = {}
    if root_type == "lead" and root_item.get("ID"):
        lead_id = str(root_item["ID"])
        response = fetch_deals_by_lead_id(client, lead_id)
        lead_deal_responses[lead_id] = response
        if not response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки лида {lead_id}: {response.get('error')}")
        for deal in result_items(response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    for lead_id, response in related_lead_responses.items():
        lead_deal_response = fetch_deals_by_lead_id(client, str(lead_id))
        lead_deal_responses[str(lead_id)] = lead_deal_response
        if not lead_deal_response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки дубль-лида {lead_id}: {lead_deal_response.get('error')}")
            continue
        for deal in result_items(lead_deal_response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    for contact_id in contact_ids:
        response = fetch_contact_deals(client, contact_id)
        contact_deal_responses[contact_id] = response
        if not response.get("ok"):
            diagnostics["warnings"].append(f"Не удалось получить сделки контакта {contact_id}: {response.get('error')}")
            continue
        for deal in result_items(response):
            if not in_period_by_any_date(deal, period, ("DATE_CREATE", "DATE_MODIFY", "CLOSEDATE", "BEGINDATE")):
                continue
            deal_id = str(deal.get("ID") or "")
            if deal_id:
                related_deals_by_id[deal_id] = deal

    if root_type == "deal" and root_item.get("ID"):
        related_deals_by_id[str(root_item["ID"])] = root_item

    related_deals = [
        deal_summary(deal, stage_lookup)
        for deal in sorted(related_deals_by_id.values(), key=lambda item: int(item.get("ID") or 0))
    ]
    related_leads = [
        lead_summary(result_item(response))
        for _lead_id, response in sorted(
            related_lead_responses.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
        )
    ]

    company_ids = {str(item.get("company_id")) for item in related_deals if item.get("company_id")}
    company_ids.update(str(item.get("company_id")) for item in related_leads if item.get("company_id"))
    if root_type == "lead" and root_item.get("COMPANY_ID"):
        company_ids.add(str(root_item["COMPANY_ID"]))
    companies = {company_id: fetch_company(client, company_id) for company_id in sorted(company_ids)}

    activities_by_entity: dict[str, Any] = {}
    if root_item:
        activities_by_entity[f"{root_type}:{root_id}"] = fetch_entity_history(client, root_type, root_id, period)
    for lead in related_leads:
        lead_id = str(lead.get("id") or "")
        if lead_id:
            activities_by_entity[f"lead:{lead_id}"] = fetch_entity_history(client, "lead", lead_id, period)
    for contact_id in contact_ids:
        activities_by_entity[f"contact:{contact_id}"] = fetch_entity_history(client, "contact", contact_id, period)
    for deal in related_deals:
        deal_id = str(deal.get("id") or "")
        if deal_id:
            activities_by_entity[f"deal:{deal_id}"] = fetch_entity_history(client, "deal", deal_id, period)

    bundle: dict[str, Any] = {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "read_only": True,
        "bundle_type": "customer_history_bundle",
        "root_entity": {
            "type": root_type,
            "id": str(root_id),
            "title": root_item.get("TITLE") or root_item.get("NAME") or root_item.get("COMPANY_TITLE"),
        },
        "history_period": period,
        "include_internal_context": bool(include_internal_context),
        "lead": root_response if root_type == "lead" else None,
        "deal": {"response": root_response, "item": root_item} if root_type == "deal" else None,
        "contact_resolution": {
            "strategy": (
                "fallback_phone_email"
                if diagnostics["fallback_match_used"]
                else "fallback_related_lead_phone_email"
                if diagnostics["fallback_related_leads_used"]
                else "CONTACT_ID"
            ),
            "primary_contact_id": primary_contact_id,
            "contact_ids": contact_ids,
            "deal_contact_items": contact_items_response,
            "contact_id_missing": diagnostics["contact_id_missing"],
        },
        "contacts": contacts,
        "contact": contacts.get(primary_contact_id) if primary_contact_id else None,
        "companies": companies,
        "contact_deal_responses": contact_deal_responses,
        "lead_deal_responses": lead_deal_responses,
        "related_leads": related_leads,
        "related_lead_responses": related_lead_responses,
        "related_deals": related_deals,
        "activities_by_entity": activities_by_entity,
        "timeline_comments_by_entity": {
            entity_key: history.get("timeline_comments") or []
            for entity_key, history in activities_by_entity.items()
        },
        "tasks_by_entity": tasks_by_entity(activities_by_entity),
        "diagnostics": diagnostics,
    }

    sections = build_history_sections(bundle)
    if not include_internal_context:
        sections["internal_context"] = []
        sections["unified_timeline"] = [
            item for item in sections["unified_timeline"] if item.get("category") != "timeline_comment"
        ]
    bundle.update(sections)
    bundle["diagnostics"]["unavailable_sources"] = unavailable_sources(bundle)
    if sections.get("ignored_openline_events"):
        bundle["diagnostics"]["warnings"].append(
            f"Открытые линии не использовались: проигнорировано событий {len(sections['ignored_openline_events'])}."
        )
    return bundle
