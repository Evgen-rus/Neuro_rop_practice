"""
Normalized snapshots for deal change detection.

The snapshot is deliberately smaller than the raw Bitrix bundle. It keeps
stable business/event facts and hashes long text payloads so the decision layer
can compare snapshots without depending on Markdown or volatile raw metadata.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_hash(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()


def result_items(call_container: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not call_container:
        return []
    if isinstance(call_container.get("items"), list):
        return [item for item in call_container["items"] if isinstance(item, dict)]
    if call_container.get("ok"):
        result = call_container.get("response", {}).get("result", [])
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
    return []


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def activity_kind(activity: dict[str, Any]) -> str:
    type_id = str(activity.get("TYPE_ID") or "")
    provider = " ".join(
        str(activity.get(key) or "")
        for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT")
    ).upper()

    if type_id == "2" or "CALL" in provider or "TELPHIN" in provider:
        return "call"
    if type_id == "4" or "EMAIL" in provider:
        return "email"
    if any(token in provider for token in ("IM", "OPENLINE", "CHAT", "WAZZUP", "TELEGRAM", "WHATSAPP", "MAX")):
        return "message"
    if type_id == "6" or "TASK" in provider or "TODO" in provider:
        return "task"
    return "activity"


def normalize_files(value: Any) -> list[dict[str, Any]]:
    files = []
    if not isinstance(value, list):
        return files
    for item in value:
        if isinstance(item, dict):
            files.append(
                {
                    "id": str(item.get("id") or item.get("ID") or ""),
                    "name_hash": text_hash(item.get("name") or item.get("NAME") or item.get("fileName")),
                    "url_hash": text_hash(item.get("url") or item.get("URL") or item.get("DOWNLOAD_URL")),
                }
            )
        else:
            files.append({"value_hash": text_hash(item)})
    return sorted(files, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))


def normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    kind = activity_kind(activity)
    files = normalize_files(activity.get("FILES"))
    return {
        "id": str(activity.get("ID") or ""),
        "kind": kind,
        "type_id": str(activity.get("TYPE_ID") or ""),
        "provider_id": str(activity.get("PROVIDER_ID") or ""),
        "provider_type_id": str(activity.get("PROVIDER_TYPE_ID") or ""),
        "origin_id": str(activity.get("ORIGIN_ID") or ""),
        "subject_hash": text_hash(activity.get("SUBJECT")),
        "description_hash": text_hash(activity.get("DESCRIPTION")),
        "created": activity.get("CREATED") or "",
        "last_updated": activity.get("LAST_UPDATED") or "",
        "start_time": activity.get("START_TIME") or "",
        "end_time": activity.get("END_TIME") or "",
        "deadline": activity.get("DEADLINE") or "",
        "completed": str(activity.get("COMPLETED") or ""),
        "status": str(activity.get("STATUS") or ""),
        "direction": str(activity.get("DIRECTION") or ""),
        "responsible_id": str(activity.get("RESPONSIBLE_ID") or ""),
        "author_id": str(activity.get("AUTHOR_ID") or ""),
        "editor_id": str(activity.get("EDITOR_ID") or ""),
        "files": files,
        "files_hash": stable_hash(files),
    }


def normalize_activities_from_response(
    activities_response: dict[str, Any] | None,
    details: dict[str, Any] | None,
    *,
    source: str,
    owner_type_id: str,
    owner_id: str,
) -> list[dict[str, Any]]:
    activities = result_items(activities_response)
    details = details or {}
    normalized = []
    for activity in activities:
        activity_id = str(activity.get("ID") or "")
        detail_container = details.get(activity_id)
        detail = {}
        if isinstance(detail_container, dict):
            detail = result_item(detail_container)
        row = normalize_activity({**activity, **detail} if detail else activity)
        row["source"] = source
        row["owner_type_id"] = owner_type_id
        row["owner_id"] = owner_id
        normalized.append(row)
    return normalized


def normalize_activities(
    bundle: dict[str, Any],
    *,
    source: str = "deal",
    owner_type_id: str = "2",
    owner_id: str | None = None,
    include_source_lead: bool = False,
) -> list[dict[str, Any]]:
    normalized = normalize_activities_from_response(
        bundle.get("activities"),
        bundle.get("activity_details"),
        source=source,
        owner_type_id=owner_type_id,
        owner_id=str(owner_id or bundle.get("deal_id") or bundle.get("lead_id") or ""),
    )

    source_lead = bundle.get("source_lead") or {}
    source_lead_id = str(source_lead.get("lead_id") or "")
    if include_source_lead and source_lead:
        normalized.extend(
            normalize_activities_from_response(
                source_lead.get("activities"),
                source_lead.get("activity_details"),
                source="source_lead",
                owner_type_id="1",
                owner_id=source_lead_id,
            )
        )

    return sorted(normalized, key=lambda item: (item["created"], item["deadline"], item["id"], item["source"]))


def normalize_deal_activities_only(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    activities = result_items(bundle.get("activities"))
    details = bundle.get("activity_details", {})
    merged: list[dict[str, Any]] = []
    for activity in activities:
        activity_id = str(activity.get("ID") or "")
        detail_container = details.get(activity_id)
        detail = {}
        if isinstance(detail_container, dict):
            detail = result_item(detail_container)
        merged.append({**activity, **detail} if detail else activity)

    normalized = [normalize_activity(activity) for activity in merged]
    return sorted(normalized, key=lambda item: (item["created"], item["deadline"], item["id"]))


def normalize_timeline_comments(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for attempt in bundle.get("timeline_comments") or []:
        for item in result_items(attempt):
            text = item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION")
            files = normalize_files(item.get("FILES"))
            comments.append(
                {
                    "id": str(item.get("ID") or ""),
                    "created": item.get("CREATED") or item.get("DATE_CREATE") or "",
                    "author_id": str(item.get("AUTHOR_ID") or item.get("CREATED_BY") or ""),
                    "entity_id": str(item.get("ENTITY_ID") or item.get("OWNER_ID") or ""),
                    "entity_type": str(item.get("ENTITY_TYPE") or item.get("OWNER_TYPE_ID") or ""),
                    "comment_hash": text_hash(text),
                    "files": files,
                    "files_hash": stable_hash(files),
                }
            )
    return sorted(comments, key=lambda item: (item["created"], item["id"]))


def normalize_product_rows(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in result_items(bundle.get("product_rows")):
        rows.append(
            {
                "id": str(item.get("ID") or ""),
                "product_id": str(item.get("PRODUCT_ID") or ""),
                "product_name_hash": text_hash(item.get("PRODUCT_NAME")),
                "quantity": str(item.get("QUANTITY") or ""),
                "price": str(item.get("PRICE") or ""),
            }
        )
    return sorted(rows, key=lambda item: (item["id"], item["product_id"], item["product_name_hash"] or ""))


def normalize_invoice_attempts(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    invoices: list[dict[str, Any]] = []
    for attempt in bundle.get("invoice_attempts") or []:
        if not isinstance(attempt, dict):
            continue
        for item in result_items(attempt):
            invoices.append(
                {
                    "id": str(item.get("ID") or item.get("id") or ""),
                    "stage_id": str(item.get("STAGE_ID") or item.get("stageId") or ""),
                    "status_id": str(item.get("STATUS_ID") or item.get("statusId") or ""),
                    "date_insert": item.get("DATE_INSERT") or item.get("createdTime") or "",
                    "date_update": item.get("DATE_UPDATE") or item.get("updatedTime") or "",
                    "sum": str(item.get("PRICE") or item.get("OPPORTUNITY") or item.get("sum") or ""),
                }
            )
    return sorted(invoices, key=lambda item: (item["date_insert"], item["id"]))


def normalize_file_refs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in bundle.get("file_and_recording_refs") or []:
        if not isinstance(ref, dict):
            continue
        refs.append(
            {
                "path": str(ref.get("path") or ""),
                "key": str(ref.get("key") or ""),
                "value_hash": stable_hash(ref.get("value")),
            }
        )
    return sorted(refs, key=lambda item: (item["path"], item["key"], item["value_hash"]))


COMMERCIAL_REF_KEYWORDS = (
    "КП",
    "ТКП",
    "ПТКП",
    "КОММЕРЧ",
    "СЧЕТ",
    "СЧЁТ",
    "ДОГОВОР",
    "OFFER",
    "PROPOSAL",
    "INVOICE",
    "CONTRACT",
)


def is_commercial_ref(ref: dict[str, Any]) -> bool:
    text = json.dumps(ref, ensure_ascii=False).upper()
    return any(keyword in text for keyword in COMMERCIAL_REF_KEYWORDS)


def normalize_commercial_file_refs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in bundle.get("file_and_recording_refs") or []:
        if not isinstance(ref, dict) or not is_commercial_ref(ref):
            continue
        refs.append(
            {
                "path": str(ref.get("path") or ""),
                "key": str(ref.get("key") or ""),
                "value_hash": stable_hash(ref.get("value")),
            }
        )
    return sorted(refs, key=lambda item: (item["path"], item["key"], item["value_hash"]))


def transcript_snapshot(transcript_path: Path | None) -> dict[str, Any] | None:
    if not transcript_path:
        return None
    if not transcript_path.exists():
        return {
            "path": str(transcript_path),
            "exists": False,
            "mtime": None,
            "content_hash": None,
        }
    text = transcript_path.read_text(encoding="utf-8")
    return {
        "path": str(transcript_path),
        "exists": True,
        "mtime": transcript_path.stat().st_mtime,
        "content_hash": text_hash(text),
    }


def build_deal_snapshot(raw_bundle: dict[str, Any], transcript_path: Path | None = None) -> dict[str, Any]:
    deal = raw_bundle.get("deal", {}).get("item", {}) or {}
    activities = normalize_activities(
        raw_bundle,
        source="deal",
        owner_type_id="2",
        owner_id=str(deal.get("ID") or raw_bundle.get("deal_id") or ""),
        include_source_lead=True,
    )
    comments = normalize_timeline_comments(raw_bundle)
    product_rows = normalize_product_rows(raw_bundle)
    invoices = normalize_invoice_attempts(raw_bundle)
    file_refs = normalize_file_refs(raw_bundle)
    commercial_file_refs = normalize_commercial_file_refs(raw_bundle)
    transcript = transcript_snapshot(transcript_path)
    source_lead = raw_bundle.get("source_lead") or {}
    source_lead_item = source_lead.get("lead", {}).get("item", {}) if isinstance(source_lead, dict) else {}

    snapshot = {
        "entity_type": "deal",
        "deal": {
            "id": str(deal.get("ID") or raw_bundle.get("deal_id") or ""),
            "title_hash": text_hash(deal.get("TITLE")),
            "stage_id": str(deal.get("STAGE_ID") or ""),
            "category_id": str(deal.get("CATEGORY_ID") or ""),
            "opportunity": str(deal.get("OPPORTUNITY") or ""),
            "currency_id": str(deal.get("CURRENCY_ID") or ""),
            "assigned_by_id": str(deal.get("ASSIGNED_BY_ID") or ""),
            "moved_time": deal.get("MOVED_TIME") or "",
            "moved_by_id": str(deal.get("MOVED_BY_ID") or ""),
            "closed": str(deal.get("CLOSED") or ""),
            "date_create": deal.get("DATE_CREATE") or "",
            "lead_id": str(deal.get("LEAD_ID") or ""),
        },
        "source_lead": {
            "id": str(source_lead_item.get("ID") or source_lead.get("lead_id") or ""),
            "title_hash": text_hash(source_lead_item.get("TITLE")),
            "status_id": str(source_lead_item.get("STATUS_ID") or ""),
            "status_semantic_id": str(source_lead_item.get("STATUS_SEMANTIC_ID") or ""),
            "assigned_by_id": str(source_lead_item.get("ASSIGNED_BY_ID") or ""),
            "date_create": source_lead_item.get("DATE_CREATE") or "",
            "date_closed": source_lead_item.get("DATE_CLOSED") or "",
        },
        "metadata": {
            "date_modify": deal.get("DATE_MODIFY") or "",
            "raw_generated_at": raw_bundle.get("generated_at") or "",
        },
        "activities": activities,
        "timeline_comments": comments,
        "commercial": {
            "product_rows_hash": stable_hash(product_rows),
            "invoice_refs_hash": stable_hash(invoices),
            "commercial_file_refs_hash": stable_hash(commercial_file_refs),
            "file_refs_hash": stable_hash(file_refs),
            "product_rows_count": len(product_rows),
            "invoice_refs_count": len(invoices),
            "commercial_file_refs_count": len(commercial_file_refs),
            "file_refs_count": len(file_refs),
        },
        "transcript": transcript,
    }
    snapshot["counts"] = {
        "activities": len(activities),
        "calls": sum(1 for item in activities if item["kind"] == "call"),
        "emails": sum(1 for item in activities if item["kind"] == "email"),
        "messages": sum(1 for item in activities if item["kind"] == "message"),
        "tasks": sum(1 for item in activities if item["kind"] == "task"),
        "timeline_comments": len(comments),
    }
    return snapshot


def build_lead_snapshot(raw_bundle: dict[str, Any], transcript_path: Path | None = None) -> dict[str, Any]:
    lead = result_item(raw_bundle.get("lead")) or {}
    activities = normalize_activities(
        raw_bundle,
        source="lead",
        owner_type_id="1",
        owner_id=str(lead.get("ID") or raw_bundle.get("lead_id") or ""),
    )
    comments = normalize_timeline_comments(raw_bundle)
    file_refs = normalize_file_refs(raw_bundle)
    commercial_file_refs = normalize_commercial_file_refs(raw_bundle)
    transcript = transcript_snapshot(transcript_path)

    snapshot = {
        "entity_type": "lead",
        "lead": {
            "id": str(lead.get("ID") or raw_bundle.get("lead_id") or ""),
            "title_hash": text_hash(lead.get("TITLE")),
            "status_id": str(lead.get("STATUS_ID") or ""),
            "status_semantic_id": str(lead.get("STATUS_SEMANTIC_ID") or ""),
            "opportunity": str(lead.get("OPPORTUNITY") or ""),
            "currency_id": str(lead.get("CURRENCY_ID") or ""),
            "assigned_by_id": str(lead.get("ASSIGNED_BY_ID") or ""),
            "moved_time": lead.get("MOVED_TIME") or "",
            "moved_by_id": str(lead.get("MOVED_BY_ID") or ""),
            "date_create": lead.get("DATE_CREATE") or "",
            "date_closed": lead.get("DATE_CLOSED") or "",
        },
        "metadata": {
            "date_modify": lead.get("DATE_MODIFY") or "",
            "raw_generated_at": raw_bundle.get("generated_at") or "",
        },
        "activities": activities,
        "timeline_comments": comments,
        "commercial": {
            "commercial_file_refs_hash": stable_hash(commercial_file_refs),
            "file_refs_hash": stable_hash(file_refs),
            "commercial_file_refs_count": len(commercial_file_refs),
            "file_refs_count": len(file_refs),
        },
        "transcript": transcript,
    }
    snapshot["counts"] = {
        "activities": len(activities),
        "calls": sum(1 for item in activities if item["kind"] == "call"),
        "emails": sum(1 for item in activities if item["kind"] == "email"),
        "messages": sum(1 for item in activities if item["kind"] == "message"),
        "tasks": sum(1 for item in activities if item["kind"] == "task"),
        "timeline_comments": len(comments),
    }
    return snapshot


def fingerprint_snapshot(snapshot: dict[str, Any]) -> str:
    # DATE_MODIFY/raw_generated_at are kept for inspection but excluded from the
    # fingerprint because Bitrix can update them without meaningful business
    # changes.
    stable_snapshot = dict(snapshot)
    stable_snapshot["metadata"] = {}
    return stable_hash(stable_snapshot)


def map_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in items if item.get("id")}


EMPTY_LIST_HASH = stable_hash([])


def compare_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return {
            "has_semantic_changes": True,
            "only_date_modify_changed": False,
            "changes": ["first_snapshot"],
            "details": {},
        }

    changes: list[str] = []
    details: dict[str, Any] = {}

    previous_deal = previous.get("deal", {}) or {}
    current_deal = current.get("deal", {}) or {}
    for field, change_name in (
        ("stage_id", "stage_changed"),
        ("opportunity", "amount_changed"),
        ("assigned_by_id", "assigned_manager_changed"),
        ("moved_time", "stage_moved_time_changed"),
        ("closed", "closed_flag_changed"),
    ):
        if previous_deal.get(field) != current_deal.get(field):
            changes.append(change_name)
            details[change_name] = {"before": previous_deal.get(field), "after": current_deal.get(field)}

    previous_activities = map_by_id(previous.get("activities", []) or [])
    current_activities = map_by_id(current.get("activities", []) or [])
    previous_activity_ids = set(previous_activities)
    current_activity_ids = set(current_activities)
    new_activity_ids = sorted(current_activity_ids - previous_activity_ids)
    removed_activity_ids = sorted(previous_activity_ids - current_activity_ids)
    if new_activity_ids:
        changes.append("new_activity")
        details["new_activity_ids"] = new_activity_ids
        new_kinds = {current_activities[item_id].get("kind") for item_id in new_activity_ids}
        for kind, change_name in (
            ("call", "new_call"),
            ("email", "new_email"),
            ("message", "new_message"),
            ("task", "new_task"),
        ):
            if kind in new_kinds:
                changes.append(change_name)
    if removed_activity_ids:
        changes.append("activity_removed")
        details["removed_activity_ids"] = removed_activity_ids

    updated_activity_ids = []
    task_deadline_changed = []
    task_completed_changed = []
    for activity_id in sorted(previous_activity_ids & current_activity_ids):
        before = previous_activities[activity_id]
        after = current_activities[activity_id]
        if stable_hash(before) != stable_hash(after):
            updated_activity_ids.append(activity_id)
        if before.get("kind") == "task" or after.get("kind") == "task":
            if before.get("deadline") != after.get("deadline"):
                task_deadline_changed.append(activity_id)
            if before.get("completed") != after.get("completed") or before.get("status") != after.get("status"):
                task_completed_changed.append(activity_id)
    if updated_activity_ids:
        changes.append("activity_updated")
        details["updated_activity_ids"] = updated_activity_ids
    if task_deadline_changed:
        changes.append("task_deadline_changed")
        details["task_deadline_changed_ids"] = task_deadline_changed
    if task_completed_changed:
        changes.append("task_completed_changed")
        details["task_completed_changed_ids"] = task_completed_changed

    previous_comments = map_by_id(previous.get("timeline_comments", []) or [])
    current_comments = map_by_id(current.get("timeline_comments", []) or [])
    new_comment_ids = sorted(set(current_comments) - set(previous_comments))
    updated_comment_ids = [
        item_id
        for item_id in sorted(set(previous_comments) & set(current_comments))
        if stable_hash(previous_comments[item_id]) != stable_hash(current_comments[item_id])
    ]
    if new_comment_ids:
        changes.append("new_comment")
        details["new_comment_ids"] = new_comment_ids
    if updated_comment_ids:
        changes.append("comment_updated")
        details["updated_comment_ids"] = updated_comment_ids

    previous_commercial = previous.get("commercial", {}) or {}
    current_commercial = current.get("commercial", {}) or {}
    hard_commercial_fields = ("product_rows_hash", "invoice_refs_hash", "commercial_file_refs_hash")
    changed_hard_commercial = []
    for field in hard_commercial_fields:
        if field not in previous_commercial and field in current_commercial:
            continue
        if previous_commercial.get(field, EMPTY_LIST_HASH) != current_commercial.get(field, EMPTY_LIST_HASH):
            changed_hard_commercial.append(field)
    if changed_hard_commercial:
        changes.append("commercial_refs_changed")
        details["commercial_refs_changed"] = changed_hard_commercial

    if previous_commercial.get("file_refs_hash") != current_commercial.get("file_refs_hash"):
        changes.append("file_refs_changed")
        details["file_refs_changed"] = ["file_refs_hash"]

    previous_transcript = previous.get("transcript") or {}
    current_transcript = current.get("transcript") or {}
    if previous_transcript.get("content_hash") != current_transcript.get("content_hash"):
        changes.append("transcript_changed")

    only_date_modify_changed = (
        not changes
        and (previous.get("metadata") or {}).get("date_modify") != (current.get("metadata") or {}).get("date_modify")
    )

    return {
        "has_semantic_changes": bool(changes),
        "only_date_modify_changed": only_date_modify_changed,
        "changes": sorted(set(changes)),
        "details": details,
    }


def compare_lead_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return {
            "has_semantic_changes": True,
            "only_date_modify_changed": False,
            "changes": ["first_snapshot"],
            "details": {},
        }

    changes: list[str] = []
    details: dict[str, Any] = {}

    previous_lead = previous.get("lead", {}) or {}
    current_lead = current.get("lead", {}) or {}
    for field, change_name in (
        ("status_id", "status_changed"),
        ("status_semantic_id", "status_semantic_changed"),
        ("opportunity", "amount_changed"),
        ("assigned_by_id", "assigned_manager_changed"),
        ("moved_time", "status_moved_time_changed"),
        ("date_closed", "date_closed_changed"),
    ):
        if previous_lead.get(field) != current_lead.get(field):
            changes.append(change_name)
            details[change_name] = {"before": previous_lead.get(field), "after": current_lead.get(field)}

    previous_activities = map_by_id(previous.get("activities", []) or [])
    current_activities = map_by_id(current.get("activities", []) or [])
    previous_activity_ids = set(previous_activities)
    current_activity_ids = set(current_activities)
    new_activity_ids = sorted(current_activity_ids - previous_activity_ids)
    removed_activity_ids = sorted(previous_activity_ids - current_activity_ids)
    if new_activity_ids:
        changes.append("new_activity")
        details["new_activity_ids"] = new_activity_ids
        new_kinds = {current_activities[item_id].get("kind") for item_id in new_activity_ids}
        for kind, change_name in (
            ("call", "new_call"),
            ("email", "new_email"),
            ("message", "new_message"),
            ("task", "new_task"),
        ):
            if kind in new_kinds:
                changes.append(change_name)
    if removed_activity_ids:
        changes.append("activity_removed")
        details["removed_activity_ids"] = removed_activity_ids

    updated_activity_ids = []
    task_deadline_changed = []
    task_completed_changed = []
    for activity_id in sorted(previous_activity_ids & current_activity_ids):
        before = previous_activities[activity_id]
        after = current_activities[activity_id]
        if stable_hash(before) != stable_hash(after):
            updated_activity_ids.append(activity_id)
        if before.get("kind") == "task" or after.get("kind") == "task":
            if before.get("deadline") != after.get("deadline"):
                task_deadline_changed.append(activity_id)
            if before.get("completed") != after.get("completed") or before.get("status") != after.get("status"):
                task_completed_changed.append(activity_id)
    if updated_activity_ids:
        changes.append("activity_updated")
        details["updated_activity_ids"] = updated_activity_ids
    if task_deadline_changed:
        changes.append("task_deadline_changed")
        details["task_deadline_changed_ids"] = task_deadline_changed
    if task_completed_changed:
        changes.append("task_completed_changed")
        details["task_completed_changed_ids"] = task_completed_changed

    previous_comments = map_by_id(previous.get("timeline_comments", []) or [])
    current_comments = map_by_id(current.get("timeline_comments", []) or [])
    new_comment_ids = sorted(set(current_comments) - set(previous_comments))
    updated_comment_ids = [
        item_id
        for item_id in sorted(set(previous_comments) & set(current_comments))
        if stable_hash(previous_comments[item_id]) != stable_hash(current_comments[item_id])
    ]
    if new_comment_ids:
        changes.append("new_comment")
        details["new_comment_ids"] = new_comment_ids
    if updated_comment_ids:
        changes.append("comment_updated")
        details["updated_comment_ids"] = updated_comment_ids

    previous_commercial = previous.get("commercial", {}) or {}
    current_commercial = current.get("commercial", {}) or {}
    if "commercial_file_refs_hash" in previous_commercial:
        if previous_commercial.get("commercial_file_refs_hash") != current_commercial.get("commercial_file_refs_hash"):
            changes.append("commercial_refs_changed")
            details["commercial_refs_changed"] = ["commercial_file_refs_hash"]
    if previous_commercial.get("file_refs_hash") != current_commercial.get("file_refs_hash"):
        changes.append("file_refs_changed")
        details["file_refs_changed"] = ["file_refs_hash"]

    previous_transcript = previous.get("transcript") or {}
    current_transcript = current.get("transcript") or {}
    if previous_transcript.get("content_hash") != current_transcript.get("content_hash"):
        changes.append("transcript_changed")

    only_date_modify_changed = (
        not changes
        and (previous.get("metadata") or {}).get("date_modify") != (current.get("metadata") or {}).get("date_modify")
    )

    return {
        "has_semantic_changes": bool(changes),
        "only_date_modify_changed": only_date_modify_changed,
        "changes": sorted(set(changes)),
        "details": details,
    }
