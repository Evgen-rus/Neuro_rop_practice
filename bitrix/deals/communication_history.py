"""Add the explicit source lead's saved communications to a deal history.

No CRM calls or writes: the full deal fetch already owns source_lead context.
Also used when reading older workspaces whose customer history omitted it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bitrix.customer_history import build_history_sections, build_normalized_communications


def source_lead_id(deal: dict[str, Any]) -> str:
    value = str(deal.get("LEAD_ID") or "").strip()
    return value if value.isdecimal() and int(value) > 0 else ""


def include_source_lead_communications(
    bundle: dict[str, Any], context: dict[str, Any], *, deal_id: str,
) -> dict[str, Any]:
    """Use only LEAD_ID of this deal; never infer a link via shared contacts."""
    deal = (context.get("deal") or {}).get("item") or {}
    lead_id = source_lead_id(deal)
    source = context.get("source_lead") or {}
    if (
        str(deal.get("ID") or "") != str(deal_id)
        or not lead_id
        or str(source.get("lead_id") or "") != lead_id
    ):
        return bundle

    key = f"lead:{lead_id}"
    history = {**source, "entity_type": "lead", "entity_id": lead_id}
    sections = build_history_sections({"activities_by_entity": {key: history}})
    result = dict(bundle)
    result["activities_by_entity"] = {**(bundle.get("activities_by_entity") or {}), key: history}
    result["timeline_comments_by_entity"] = {
        **(bundle.get("timeline_comments_by_entity") or {}),
        key: source.get("timeline_comments") or [],
    }

    # Keep existing chat/context rows and do not turn lead tasks into deal tasks.
    # Activity IDs are global in Bitrix, so a second binding is not a new event.
    for section in ("client_touchpoints", "internal_context"):
        if section == "internal_context" and bundle.get("include_internal_context") is False:
            continue
        rows = list(bundle.get(section) or [])
        indexes = {(str(row.get("category") or ""), str(row.get("id") or "")): i for i, row in enumerate(rows)}
        for row in sections[section]:
            marker = (str(row.get("category") or ""), str(row.get("id") or ""))
            if marker not in indexes:
                indexes[marker] = len(rows)
                rows.append(row)
            elif rows[indexes[marker]].get("entity_key") == key:
                rows[indexes[marker]] = row
        result[section] = sorted(rows, key=lambda row: (str(row.get("when") or ""), str(row.get("id") or "")))
    timeline = list(bundle.get("unified_timeline") or [])
    indexes = {(str(row.get("category") or ""), str(row.get("id") or "")): i for i, row in enumerate(timeline)}
    for section in ("client_touchpoints", "internal_context"):
        for row in result.get(section) or []:
            marker = (str(row.get("category") or ""), str(row.get("id") or ""))
            if marker not in indexes:
                indexes[marker] = len(timeline)
                timeline.append(row)
            elif timeline[indexes[marker]].get("entity_key") == key:
                timeline[indexes[marker]] = row
    result["unified_timeline"] = sorted(timeline, key=lambda row: (str(row.get("when") or ""), str(row.get("id") or "")))
    result["normalized_communications"] = build_normalized_communications(result)
    lead_call_ids = {row["id"] for row in sections["client_touchpoints"] if row.get("event_type") == "call"}
    for event in result["normalized_communications"]:
        if event.get("channel") == "call" and lead_call_ids.intersection(event.get("source_ids") or []):
            event["source_lead_id"] = lead_id
    return result


def include_saved_source_lead_communications(
    bundle: dict[str, Any], context_path: Path, *, deal_id: str,
) -> dict[str, Any]:
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return bundle
    if not isinstance(context, dict):
        return bundle
    return include_source_lead_communications(bundle, context, deal_id=deal_id)
