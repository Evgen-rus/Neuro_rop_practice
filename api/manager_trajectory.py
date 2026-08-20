"""Manual read-only Bitrix collection and local manager-trajectory reporting."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from api.candidates import DEAL_OWNER_TYPE_ID, LEAD_OWNER_TYPE_ID, parse_bitrix_dt
from bitrix.client import BitrixReadOnlyClient
from bitrix.customer_history import activity_type
from setup import MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_deal_control_scope,
    get_manager_trajectory_collection_state,
    list_deal_control_deals,
    list_manager_trajectory_events,
    observe_manager_trajectory_entity,
    record_manager_trajectory_event,
    save_manager_trajectory_collection_state,
)


COLLECTION_KEY = "bitrix_manager_wide"
COLLECTION_OVERLAP = timedelta(minutes=15)
ACTIVITY_SELECT = [
    "ID", "OWNER_ID", "OWNER_TYPE_ID", "TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID",
    "ASSOCIATED_ENTITY_ID", "RESPONSIBLE_ID", "DIRECTION", "COMPLETED",
    "START_TIME", "END_TIME", "DEADLINE", "CREATED", "LAST_UPDATED",
]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=MSK_TZ)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(MSK_TZ).isoformat(timespec="seconds")


def _bitrix_datetime(value: datetime) -> str:
    return _iso(value)


def _activity_occurred_at(activity: dict[str, Any]) -> str:
    for key in ("START_TIME", "END_TIME", "CREATED", "LAST_UPDATED", "DEADLINE"):
        parsed = parse_bitrix_dt(activity.get(key))
        if parsed is not None:
            return _iso(parsed)
    return _iso(datetime.now(MSK_TZ))


def _activity_version(activity: dict[str, Any]) -> str:
    updated = str(activity.get("LAST_UPDATED") or "").strip()
    if updated:
        return updated
    stable = {
        key: activity.get(key)
        for key in (
            "ID", "OWNER_TYPE_ID", "OWNER_ID", "RESPONSIBLE_ID", "TYPE_ID",
            "PROVIDER_ID", "DIRECTION", "COMPLETED", "START_TIME", "END_TIME", "DEADLINE",
        )
    }
    return hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _owner(activity: dict[str, Any]) -> tuple[str, str] | None:
    owner_type = str(activity.get("OWNER_TYPE_ID") or "")
    entity_type = (
        "lead" if owner_type == str(LEAD_OWNER_TYPE_ID)
        else "deal" if owner_type == str(DEAL_OWNER_TYPE_ID)
        else None
    )
    owner_id = str(activity.get("OWNER_ID") or "").strip()
    return (entity_type, owner_id) if entity_type and owner_id else None


def _activity_kind(activity: dict[str, Any]) -> str:
    provider = str(activity.get("PROVIDER_ID") or "").upper()
    if provider.startswith("CRM_TASKS_"):
        return "task"
    kind = activity_type(activity)
    return kind if kind in {"call", "email", "message"} else "other"


def _fetch_entities(
    client: BitrixReadOnlyClient,
    *,
    entity_type: str,
    manager_ids: list[str],
    from_at: datetime,
    to_at: datetime,
) -> dict[str, Any]:
    is_deal = entity_type == "deal"
    response = client.safe_list_all(
        "crm.deal.list" if is_deal else "crm.lead.list",
        {
            "order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
            "filter": {
                "ASSIGNED_BY_ID": manager_ids,
                ">=DATE_MODIFY": _bitrix_datetime(from_at),
                "<DATE_MODIFY": _bitrix_datetime(to_at),
            },
            "select": [
                "ID", "ASSIGNED_BY_ID", "DATE_MODIFY",
                "STAGE_ID" if is_deal else "STATUS_ID",
            ],
        },
    )
    return response


def collect_manager_trajectory(
    client: BitrixReadOnlyClient,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    manager_ids: list[str] | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
) -> dict[str, Any]:
    """Collect manager-associated CRM facts; never writes to Bitrix or calls AI."""
    scope = get_deal_control_scope(db_path)
    managers = list(dict.fromkeys(
        str(item).strip()
        for item in (manager_ids if manager_ids is not None else scope.get("manager_ids") or [])
        if str(item).strip()
    ))
    if not managers:
        raise ValueError("В deal-control scope не настроены manager_ids")
    end = _aware(to_at or datetime.now(MSK_TZ)).astimezone(MSK_TZ)
    state = get_manager_trajectory_collection_state(db_path, collection_key=COLLECTION_KEY)
    if from_at is not None:
        start = _aware(from_at).astimezone(MSK_TZ)
    elif state and state.get("last_success_at"):
        parsed = datetime.fromisoformat(str(state["last_success_at"]).replace("Z", "+00:00"))
        start = _aware(parsed).astimezone(MSK_TZ) - COLLECTION_OVERLAP
    else:
        start = end - timedelta(days=1)
    if start >= end:
        raise ValueError("Начало периода должно быть раньше окончания")

    activity_response = client.safe_list_all(
        "crm.activity.list",
        {
            "order": {"LAST_UPDATED": "ASC", "ID": "ASC"},
            "filter": {
                "RESPONSIBLE_ID": managers,
                ">=LAST_UPDATED": _bitrix_datetime(start),
                "<LAST_UPDATED": _bitrix_datetime(end),
            },
            "select": ACTIVITY_SELECT,
        },
    )
    deal_response = _fetch_entities(
        client, entity_type="deal", manager_ids=managers, from_at=start, to_at=end,
    )
    lead_response = _fetch_entities(
        client, entity_type="lead", manager_ids=managers, from_at=start, to_at=end,
    )
    responses = {
        "activities": activity_response,
        "deals": deal_response,
        "leads": lead_response,
    }
    errors = {
        name: str(response.get("error") or f"{name} unavailable")
        for name, response in responses.items()
        if not response.get("ok")
    }
    counts = {"activities": 0, "stage_changes": 0, "ignored_outside_scope": 0}
    allowed = set(managers)

    if activity_response.get("ok"):
        for activity in activity_response.get("items") or []:
            manager_id = str(activity.get("RESPONSIBLE_ID") or "").strip()
            owner = _owner(activity)
            if manager_id not in allowed or owner is None:
                counts["ignored_outside_scope"] += 1
                continue
            entity_type, entity_id = owner
            activity_id = str(activity.get("ID") or "").strip()
            if not activity_id:
                continue
            record_manager_trajectory_event(
                db_path,
                entity_type=entity_type,
                entity_id=entity_id,
                manager_id=manager_id,
                event_type="crm_activity_observed",
                source="bitrix",
                source_event_key=f"crm_activity:{activity_id}:{_activity_version(activity)}",
                occurred_at=_activity_occurred_at(activity),
                payload={
                    "activity_id": activity_id,
                    "activity_kind": _activity_kind(activity),
                    "owner_type": entity_type,
                    "owner_id": entity_id,
                    "responsible_id": manager_id,
                    "direction": str(activity.get("DIRECTION") or "") or None,
                    "completed": str(activity.get("COMPLETED") or "").upper() in {"Y", "1", "TRUE"},
                    "last_updated": str(activity.get("LAST_UPDATED") or "") or None,
                },
            )
            counts["activities"] += 1

    for entity_type, response in (("deal", deal_response), ("lead", lead_response)):
        if not response.get("ok"):
            continue
        stage_field = "STAGE_ID" if entity_type == "deal" else "STATUS_ID"
        for entity in response.get("items") or []:
            manager_id = str(entity.get("ASSIGNED_BY_ID") or "").strip()
            entity_id = str(entity.get("ID") or "").strip()
            if manager_id not in allowed or not entity_id:
                counts["ignored_outside_scope"] += 1
                continue
            event = observe_manager_trajectory_entity(
                db_path,
                entity_type=entity_type,
                entity_id=entity_id,
                manager_id=manager_id,
                stage_id=str(entity.get(stage_field) or "") or None,
                modified_at=str(entity.get("DATE_MODIFY") or "") or None,
            )
            counts["stage_changes"] += int(event is not None)

    status = "success" if not errors else "partial"
    collection_state = save_manager_trajectory_collection_state(
        db_path,
        collection_key=COLLECTION_KEY,
        status=status,
        successful_through=_iso(end) if not errors else None,
        error="; ".join(f"{name}: {value}" for name, value in errors.items()) or None,
    )
    return {
        "status": status,
        "period": {"from": _iso(start), "to": _iso(end)},
        "manager_ids": managers,
        "counts": counts,
        "errors": errors,
        "collection_state": collection_state,
    }


def _event_datetime(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00")).astimezone(MSK_TZ)


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _crm_action(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    return {
        "event_id": int(event["id"]),
        "action_type": "crm_activity",
        "activity_id": str(payload.get("activity_id") or "") or None,
        "entity_type": str(event.get("entity_type") or ""),
        "entity_id": str(event.get("entity_id") or ""),
        "occurred_at": event.get("occurred_at"),
        "recorded_at": event.get("recorded_at"),
        "activity_kind": str(payload.get("activity_kind") or "other"),
        "direction": payload.get("direction"),
        "completed": payload.get("completed"),
        "last_updated": payload.get("last_updated"),
    }


def _stage_action(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    return {
        "event_id": int(event["id"]),
        "action_type": "stage_change",
        "activity_id": None,
        "entity_type": str(event.get("entity_type") or ""),
        "entity_id": str(event.get("entity_id") or ""),
        "occurred_at": event.get("occurred_at"),
        "recorded_at": event.get("recorded_at"),
        "activity_kind": "stage_change",
        "direction": None,
        "completed": None,
        "from_stage_id": payload.get("from_stage_id"),
        "to_stage_id": payload.get("to_stage_id"),
    }


def _unique_crm_actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse LAST_UPDATED versions while preserving the latest known activity facts."""
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") != "crm_activity_observed":
            continue
        action = _crm_action(row)
        activity_key = action.get("activity_id") or f"event:{action['event_id']}"
        key = (str(action["entity_type"]), str(action["entity_id"]), str(activity_key))
        previous = latest.get(key)
        action_version = str(action.get("last_updated") or action.get("recorded_at") or "")
        previous_version = str((previous or {}).get("last_updated") or (previous or {}).get("recorded_at") or "")
        if previous is None or action_version >= previous_version:
            latest[key] = action
    return sorted(latest.values(), key=lambda item: (str(item.get("occurred_at") or ""), int(item["event_id"])))


def _recommendation_usage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("event_type") not in {
            "recommendation_generated", "recommendation_shown", "recommendation_viewed",
        }:
            continue
        kind = str(row.get("recommendation_kind") or "")
        recommendation_id = str(row.get("recommendation_id") or "")
        deal_id = str(row.get("entity_id") or "")
        if not kind or not recommendation_id or not deal_id:
            continue
        item = grouped.setdefault(
            (kind, recommendation_id, deal_id),
            {
                "recommendation_kind": kind,
                "recommendation_id": recommendation_id,
                "deal_id": deal_id,
                "analysis_run_id": row.get("analysis_run_id"),
                "report_id": row.get("report_id"),
                "generated_at": [],
                "shown_at": [],
                "viewed_at": [],
                "view_occurrences": [],
            },
        )
        event_type = str(row.get("event_type") or "")
        if event_type == "recommendation_generated":
            item["generated_at"].append(row.get("occurred_at"))
        elif event_type == "recommendation_shown":
            item["shown_at"].append(row.get("occurred_at"))
        else:
            payload = _event_payload(row)
            item["viewed_at"].append(row.get("occurred_at"))
            item["view_occurrences"].append({
                "event_id": int(row["id"]),
                "occurred_at": row.get("occurred_at"),
                "occurrence_id": payload.get("occurrence_id"),
                "tracking_precision": "occurrence" if payload.get("occurrence_id") else "legacy_at_least_once",
            })
    result = []
    for item in grouped.values():
        for field in ("generated_at", "shown_at", "viewed_at"):
            item[field] = sorted(value for value in item[field] if value)
        item["view_occurrences"] = sorted(
            item["view_occurrences"], key=lambda value: (str(value.get("occurred_at") or ""), int(value["event_id"])),
        )
        item["generated_count"] = len(item["generated_at"])
        item["shown_count"] = len(item["shown_at"])
        item["view_count"] = len(item["view_occurrences"])
        precisions = {value["tracking_precision"] for value in item["view_occurrences"]}
        item["view_tracking_precision"] = (
            "occurrence" if precisions == {"occurrence"}
            else "legacy_at_least_once" if precisions == {"legacy_at_least_once"}
            else "mixed" if precisions
            else "none"
        )
        result.append(item)
    return sorted(
        result,
        key=lambda item: (
            str(item.get("deal_id") or ""), str(item.get("recommendation_kind") or ""),
            str(item.get("recommendation_id") or ""),
        ),
    )


def _action_reference(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": action.get("event_id"),
        "action_type": action.get("action_type"),
        "activity_id": action.get("activity_id"),
        "entity_type": action.get("entity_type"),
        "entity_id": action.get("entity_id"),
        "occurred_at": action.get("occurred_at"),
        "activity_kind": action.get("activity_kind"),
        "direction": action.get("direction"),
        "completed": action.get("completed"),
        "from_stage_id": action.get("from_stage_id"),
        "to_stage_id": action.get("to_stage_id"),
    }


def _view_correlations(
    recommendations: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    report_end: datetime,
) -> list[dict[str, Any]]:
    views_by_deal: dict[str, list[dict[str, Any]]] = {}
    for recommendation in recommendations:
        for occurrence in recommendation.get("view_occurrences") or []:
            views_by_deal.setdefault(str(recommendation["deal_id"]), []).append({
                **occurrence,
                "recommendation_kind": recommendation["recommendation_kind"],
                "recommendation_id": recommendation["recommendation_id"],
            })
    actions_by_deal: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        if action.get("entity_type") == "deal":
            actions_by_deal.setdefault(str(action.get("entity_id") or ""), []).append(action)
    all_actions = sorted(actions, key=lambda value: str(value.get("occurred_at") or ""))
    result: list[dict[str, Any]] = []
    for deal_id, raw_views in views_by_deal.items():
        views = sorted(raw_views, key=lambda value: (str(value.get("occurred_at") or ""), int(value["event_id"])))
        deal_actions = sorted(actions_by_deal.get(deal_id) or [], key=lambda value: str(value.get("occurred_at") or ""))
        projected_views: list[dict[str, Any]] = []
        for index, view in enumerate(views):
            viewed_at = datetime.fromisoformat(str(view["occurred_at"]).replace("Z", "+00:00")).astimezone(MSK_TZ)
            previous_at = (
                datetime.fromisoformat(str(views[index - 1]["occurred_at"]).replace("Z", "+00:00")).astimezone(MSK_TZ)
                if index else None
            )
            next_at = (
                datetime.fromisoformat(str(views[index + 1]["occurred_at"]).replace("Z", "+00:00")).astimezone(MSK_TZ)
                if index + 1 < len(views) else report_end
            )
            before = [
                action for action in deal_actions
                if (previous_at is None or _event_datetime(action) > previous_at)
                and _event_datetime(action) < viewed_at
            ]
            after = [
                action for action in deal_actions
                if _event_datetime(action) > viewed_at and _event_datetime(action) < next_at
            ]
            window_end = min(viewed_at + timedelta(minutes=60), report_end)
            same_deal_60m = [
                action for action in deal_actions
                if viewed_at < _event_datetime(action) <= window_end
            ]
            other_60m = [
                action for action in all_actions
                if action.get("entity_type") == "deal"
                and str(action.get("entity_id") or "") != deal_id
                and viewed_at < _event_datetime(action) <= window_end
            ]
            projected_views.append({
                **view,
                "view_index_for_deal": index + 1,
                "previous_view_at": views[index - 1].get("occurred_at") if index else None,
                "next_view_at": views[index + 1].get("occurred_at") if index + 1 < len(views) else None,
                "actions_before_since_previous_view": [_action_reference(item) for item in before],
                "actions_after_until_next_view": [_action_reference(item) for item in after],
                "same_deal_actions_within_60m": [_action_reference(item) for item in same_deal_60m],
                "other_deal_actions_within_60m": len(other_60m),
                "first_same_deal_action_after": _action_reference(after[0]) if after else None,
                "minutes_to_first_same_deal_action": (
                    round((_event_datetime(after[0]) - viewed_at).total_seconds() / 60, 1) if after else None
                ),
            })
        result.append({"deal_id": deal_id, "views": projected_views})
    return sorted(result, key=lambda value: str(value["deal_id"]))


def build_manager_trajectory_report(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    from_at: datetime,
    to_at: datetime,
    manager_ids: list[str] | None = None,
) -> dict[str, Any]:
    start = _aware(from_at).astimezone(MSK_TZ)
    end = _aware(to_at).astimezone(MSK_TZ)
    scope = get_deal_control_scope(db_path)
    managers = list(dict.fromkeys(
        str(item).strip()
        for item in (manager_ids if manager_ids is not None else scope.get("manager_ids") or [])
        if str(item).strip()
    ))
    events = list_manager_trajectory_events(
        db_path,
        from_at=_iso(start),
        to_at=_iso(end),
        manager_ids=managers,
    )
    deals = list_deal_control_deals(db_path, active_only=False)
    deal_meta = {str(item.get("deal_id") or ""): item for item in deals}
    manager_names: dict[str, str] = {}
    for deal in deals:
        manager_id = str(deal.get("manager_id") or "")
        manager_name = str(deal.get("manager_name") or "").strip()
        if manager_id and manager_name:
            manager_names[manager_id] = manager_name
    grouped: list[dict[str, Any]] = []
    warnings = [
        "Bitrix responsible_id означает связь активности с менеджером, но не доказывает физического автора.",
        "COMPLETED=Y не доказывает содержательный контакт с клиентом.",
        "Период отчёта отбирает CRM-факты по occurred_at; запись могла быть получена позже по LAST_UPDATED.",
        "Смена стадии означает разницу между ручными сборами, а не полную историю переходов Bitrix.",
    ]
    excluded_unverified_total = 0
    for manager_id in managers:
        rows = [item for item in events if str(item.get("manager_id") or "") == manager_id]
        unverified_lifecycle = [
            item for item in rows
            if item.get("event_type") in {"recommendation_shown", "recommendation_viewed"}
            and not _verified_manager_actor(item, manager_id)
        ]
        excluded_unverified_total += len(unverified_lifecycle)
        counted_rows = [
            item for item in rows
            if item.get("event_type") not in {"recommendation_shown", "recommendation_viewed"}
            or _verified_manager_actor(item, manager_id)
        ]
        counts: dict[str, int] = {}
        for item in counted_rows:
            event_type = str(item.get("event_type") or "")
            counts[event_type] = counts.get(event_type, 0) + 1
        crm_actions = _unique_crm_actions(rows)
        stage_actions = [
            _stage_action(item) for item in rows
            if item.get("event_type") in {"deal_stage_changed", "lead_stage_changed"}
        ]
        recommendations = _recommendation_usage(counted_rows)
        activity_counts: dict[str, int] = {}
        for action in crm_actions:
            kind = str(action.get("activity_kind") or "other")
            activity_counts[kind] = activity_counts.get(kind, 0) + 1
        work_entities: list[dict[str, Any]] = []
        entity_keys = sorted({
            (str(item.get("entity_type") or ""), str(item.get("entity_id") or ""))
            for item in [*crm_actions, *stage_actions]
            if item.get("entity_type") and item.get("entity_id")
        })
        for entity_type, entity_id in entity_keys:
            metadata = deal_meta.get(entity_id) if entity_type == "deal" else None
            work_entities.append({
                "entity_type": entity_type,
                "entity_id": entity_id,
                "title": metadata.get("title") if metadata else None,
                "stage_id": metadata.get("stage_id") if metadata else None,
                "stage_name": metadata.get("stage_name") if metadata else None,
                "is_active": bool(metadata.get("is_active")) if metadata else None,
                "crm_actions": [
                    _action_reference(item) for item in crm_actions
                    if item.get("entity_type") == entity_type and str(item.get("entity_id")) == entity_id
                ],
                "stage_changes": [
                    item for item in stage_actions
                    if item.get("entity_type") == entity_type and str(item.get("entity_id")) == entity_id
                ],
            })
        viewed = [item for item in counted_rows if item.get("event_type") == "recommendation_viewed"]
        windows: list[dict[str, Any]] = []
        for event in viewed:
            viewed_at = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
            window_end = viewed_at + timedelta(minutes=60)
            observed = [
                item for item in rows
                if item.get("source") == "bitrix"
                and viewed_at <= datetime.fromisoformat(str(item["occurred_at"]).replace("Z", "+00:00")) <= window_end
            ]
            target = [
                item for item in observed
                if item.get("entity_type") == event.get("entity_type")
                and str(item.get("entity_id")) == str(event.get("entity_id"))
            ]
            windows.append({
                "recommendation_kind": event.get("recommendation_kind"),
                "recommendation_id": event.get("recommendation_id"),
                "viewed_at": event.get("occurred_at"),
                "window_to": _iso(window_end),
                "target_entity_events": len(target),
                "other_entity_events": len(observed) - len(target),
                "observation": "наблюдались CRM-события" if observed else "CRM-событий не наблюдалось",
            })
        grouped.append({
            "manager_id": manager_id,
            "manager_name": manager_names.get(manager_id),
            "counts": counts,
            "entities": len({(item.get("entity_type"), item.get("entity_id")) for item in rows}),
            "excluded_unverified_lifecycle_events": len(unverified_lifecycle),
            "quick_help_generated": sum(
                item.get("event_type") == "recommendation_generated"
                and item.get("recommendation_kind") == "quick_help"
                for item in rows
            ),
            "viewed_windows_60m": windows,
            "workday": {
                "unique_crm_actions": len(crm_actions),
                "activity_counts": activity_counts,
                "stage_changes": len(stage_actions),
                "deals_touched": sum(item["entity_type"] == "deal" for item in work_entities),
                "leads_touched": sum(item["entity_type"] == "lead" for item in work_entities),
                "entities": work_entities,
            },
            "product_usage": {
                "recommendations": recommendations,
                "generated": counts.get("recommendation_generated", 0),
                "shown": counts.get("recommendation_shown", 0),
                "viewed": counts.get("recommendation_viewed", 0),
            },
            "correlations": _view_correlations(
                recommendations, [*crm_actions, *stage_actions], report_end=end,
            ),
        })
    if excluded_unverified_total:
        warnings.append(
            f"Исключено неподтверждённых shown/viewed событий: {excluded_unverified_total}. "
            "Они не считаются использованием рекомендации менеджером."
        )
    return {
        "schema_version": 2,
        "period": {"from": _iso(start), "to": _iso(end), "timezone": "Europe/Moscow"},
        "collection_status": get_manager_trajectory_collection_state(db_path, collection_key=COLLECTION_KEY),
        "summary": {
            "managers": len(grouped),
            "unique_crm_actions": sum(item["workday"]["unique_crm_actions"] for item in grouped),
            "recommendations_generated": sum(item["product_usage"]["generated"] for item in grouped),
            "recommendations_shown": sum(item["product_usage"]["shown"] for item in grouped),
            "recommendations_viewed": sum(item["product_usage"]["viewed"] for item in grouped),
        },
        "managers": grouped,
        "warnings": warnings,
    }


def _verified_manager_actor(event: dict[str, Any], manager_id: str) -> bool:
    payload = event.get("payload")
    return bool(
        isinstance(payload, dict)
        and payload.get("actor_verified") is True
        and payload.get("actor_role") == "manager"
        and str(payload.get("actor_manager_id") or "") == str(manager_id)
    )
