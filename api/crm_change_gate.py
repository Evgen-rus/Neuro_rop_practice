"""Cheap, conservative gate BEFORE automatic deal pipelines.

Uses the existing portfolio/trajectory plus batched version/head probes. A head
probe is not a history download: bursts are drained by the normal context job.
No successful job acknowledgement means retry, never an idle cache hit.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from bitrix.context_sync import OVERLAP, digest, due, parsed_at
from bitrix.deals.download_deals_call_audio import (
    audio_file_discovery_expired, call_activities, load_existing_manifest, should_recheck_recording,
)
from bitrix.usage_trace import bitrix_trace_context
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import (
    crm_trajectory_signal_versions, get_crm_sync_state, list_deal_control_deals, put_crm_sync_state,
)

WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"
AUDIO_ROOT = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"


def transcript_signature(deal_id: str, workspace_root: Path = WORKSPACE_ROOT) -> str:
    folder = workspace_root / f"deal_{deal_id}" / "transcripts"
    digestor = hashlib.sha256()
    for path in sorted(folder.glob("*.json")):
        digestor.update(path.name.encode("utf-8"))
        digestor.update(path.read_bytes())
    # TXT is also a supported local/manual transcript input.
    for path in sorted(folder.glob("*.txt")):
        digestor.update(path.name.encode("utf-8"))
        digestor.update(path.read_bytes())
    return digestor.hexdigest()


def entity_histories(payload: dict) -> dict[str, dict]:
    raw = payload.get("context") or {}
    result = dict((payload.get("customer_history") or {}).get("activities_by_entity") or {})
    if raw.get("deal_id"):
        result[f"deal:{raw['deal_id']}"] = raw
    source = raw.get("source_lead") or {}
    if source.get("lead_id"):
        result[f"lead:{source['lead_id']}"] = source
    return result


def time_signal(raw: dict, now: datetime) -> list[str]:
    """Only threshold crossings; elapsed time must not force CRM polling."""
    overdue, latest = [], []
    for item in (raw.get("activities") or {}).get("items", []):
        deadline = parsed_at(item.get("DEADLINE"))
        if str(item.get("COMPLETED") or "N") != "Y" and deadline and deadline < now:
            overdue.append(str(item.get("ID")))
        for key in ("LAST_UPDATED", "START_TIME", "END_TIME", "CREATED"):
            value = parsed_at(item.get(key))
            if value:
                latest.append(value)
    return sorted(overdue) + (["stale_activity"] if latest and (now - max(latest)).total_seconds() >= 172800 else [])


def audio_due(payload: dict, deal_id: str, now: datetime, *, audio_root: Path = AUDIO_ROOT) -> bool:
    manifest = load_existing_manifest(audio_root / f"deal_{deal_id}_call_audio_manifest.json")
    rows = {str(row.get("activity_id")): row for row in manifest.get("calls", [])}
    for activity in call_activities(payload.get("context") or {}):
        row = rows.get(str(activity.get("ID"))) or {}
        if should_recheck_recording(activity, now=now):
            return True  # includes already-transcribed recordings: check growth
        if not activity.get("FILES") and not audio_file_discovery_expired(activity, now=now):
            return True
        if activity.get("FILES") and row.get("status") not in {"transcribed_and_purged", "no_files_check_expired"}:
            # Pending downloads/readiness/transcription are not gated by DATE_MODIFY.
            if not row.get("downloads") or any(not item.get("is_short_no_answer") for item in row.get("downloads", [])):
                return True
    return any(row.get("audio_kind") == "max_voice" and row.get("status") != "transcribed_and_purged"
               for row in rows.values())


def _result(response: dict) -> Any:
    return (response.get("response") or {}).get("result")


def _different(rows: list[dict], known: list[dict], keys: tuple[str, ...] | None = None) -> bool:
    old = {str(row.get("ID") or row.get("id")): row for row in known}
    for row in rows:
        previous = old.get(str(row.get("ID") or row.get("id")))
        if previous is None:
            return True
        if keys:
            if any(str(row.get(key) or "") != str(previous.get(key) or "") for key in keys):
                return True
        elif digest(row) != digest(previous):
            return True
    return False


def probe_changes(client: Any, payloads: dict[str, dict], *, now: datetime | None = None) -> dict[str, set[str]]:
    current = now or datetime.now(MSK_TZ)
    def failed_before(response):
        return response.get("ok") is False or response.get("refresh_ok") is False

    def failure_due(deal_id, was_failed):
        retry_at = parsed_at(payloads[deal_id].get("context", {}).get("sync", {}).get("retry_at"))
        return not was_failed or retry_at is None or current >= retry_at

    changes = {deal_id: set() for deal_id in payloads}
    owners: dict[str, set[str]] = {}
    histories: dict[str, dict[str, dict]] = {}
    requests: dict[str, tuple[str, dict]] = {}
    known: dict[str, dict[str, list[dict]]] = {}
    linked_records: dict[str, dict[str, dict]] = {}
    prior_failures: dict[str, dict[str, bool]] = {}
    for deal_id, payload in payloads.items():
        for entity_key, history in entity_histories(payload).items():
            owners.setdefault(entity_key, set()).add(deal_id)
            histories.setdefault(entity_key, {})[deal_id] = history
            entity_type, entity_id = entity_key.split(":", 1)
            key = "timeline:" + entity_key
            requests[key] = ("crm.timeline.comment.list", {"filter": {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id},
                             "order": {"CREATED": "DESC", "ID": "DESC"}, "start": 0})
            known.setdefault(key, {})[deal_id] = [row for response in history.get("timeline_comments", []) for row in response.get("items", [])]
            prior_failures.setdefault(key, {})[deal_id] = any(failed_before(response) for response in history.get("timeline_comments", []))
            owners.setdefault(key, set()).add(deal_id)
        raw = payload.get("context") or {}
        customer = payload.get("customer_history") or {}
        for kind, containers in (("contact", {**customer.get("contacts", {}), **raw.get("contacts", {})}),
                                 ("company", customer.get("companies", {}))):
            for entity_id, container in containers.items():
                linked_records.setdefault(f"{kind}:{entity_id}", {})[deal_id] = _result(container) or {}
                owners.setdefault(f"{kind}:{entity_id}", set()).add(deal_id)
        for item in customer.get("related_deals", []):
            entity_id = str(item.get("id") or "")
            if entity_id and entity_id != deal_id:
                linked_records.setdefault(f"deal:{entity_id}", {})[deal_id] = {"ID": entity_id, "DATE_MODIFY": item.get("date_modify")}
                owners.setdefault(f"deal:{entity_id}", set()).add(deal_id)
        for task_id in raw.get("bitrix_open_task_ids", []):
            key = "task:" + str(task_id)
            requests[key] = ("tasks.task.get", {"taskId": task_id, "select": ["*"]})
            known.setdefault(key, {})[deal_id] = [_result(raw.get("bitrix_tasks", {}).get(task_id, {})) or {}]
            prior_failures.setdefault(key, {})[deal_id] = failed_before(raw.get("bitrix_tasks", {}).get(task_id, {}))
            owners.setdefault(key, set()).add(deal_id)
        chats = [chat for bundle in customer.get("internal_im_chats_by_entity", {}).values() for chat in bundle.get("chats", [])]
        for task_id, response in raw.get("bitrix_task_chats", {}).items():
            task = (_result(raw.get("bitrix_tasks", {}).get(task_id, {})) or {}).get("task") or {}
            if task.get("chatId"):
                chats.append({"dialog_id": f"chat{task['chatId']}", "messages_response": response})
        for chat in chats:
            dialog_id = str(chat.get("dialog_id") or "")
            if not dialog_id:
                continue
            key = "chat:" + dialog_id
            requests[key] = ("im.dialog.messages.get", {"DIALOG_ID": dialog_id, "LIMIT": 50})
            known.setdefault(key, {})[deal_id] = (_result(chat.get("messages_response") or {}) or {}).get("messages") or []
            prior_failures.setdefault(key, {})[deal_id] = failed_before(chat.get("messages_response") or {})
            owners.setdefault(key, set()).add(deal_id)
    with bitrix_trace_context(component="cheap_change_detection"):
        for kind, owner_type in (("deal", 2), ("lead", 1), ("contact", 3)):
            keys = [key for key in histories if key.startswith(kind + ":")]
            if not keys:
                continue
            cursors = [parsed_at((history.get("sync") or {}).get("activity_cursor"))
                       or parsed_at(history.get("generated_at")) for key in keys for history in histories[key].values()]
            # Related histories use the parent success cursor, not generated file time.
            fallback = [parsed_at((p.get("customer_history", {}).get("sync") or {}).get("activity_cursor")) for p in payloads.values() if p.get("customer_history")]
            dates = [value for value in [*cursors, *fallback] if value]
            filters = {"OWNER_TYPE_ID": owner_type, "OWNER_ID": [key.split(":", 1)[1] for key in keys]}
            if dates:
                filters[">=LAST_UPDATED"] = (min(dates) - OVERLAP).isoformat()
            response = client.safe_list_all("crm.activity.list", {"filter": filters,
                "order": {"LAST_UPDATED": "ASC", "ID": "ASC"},
                "select": ["ID", "OWNER_ID", "OWNER_TYPE_ID", "LAST_UPDATED", "FILES"]})
            for key in keys:
                if not response.get("ok"):
                    for deal_id, history in histories[key].items():
                        if failure_due(deal_id, failed_before(history.get("activities") or {})):
                            changes[deal_id].add("probe_error")
                    continue
                rows = [row for row in response.get("items", []) if str(row.get("OWNER_ID")) == key.split(":", 1)[1]]
                for deal_id, history in histories[key].items():
                    if _different(rows, history.get("activities", {}).get("items", []), ("LAST_UPDATED", "FILES")):
                        changes[deal_id].add("activity")
        for kind in ("contact", "company", "deal"):
            keys = [key for key in linked_records if key.startswith(kind + ":")]
            if not keys:
                continue
            response = client.safe_list_all(f"crm.{kind}.list", {"filter": {"ID": [key.split(":", 1)[1] for key in keys]}, "select": ["ID", "DATE_MODIFY"]})
            records = {str(row.get("ID")): row for row in response.get("items", [])}
            for key in keys:
                item = records.get(key.split(":", 1)[1])
                for deal_id, previous in linked_records[key].items():
                    if not response.get("ok") or not item or str(item.get("DATE_MODIFY") or "") != str(previous.get("DATE_MODIFY") or ""):
                        changes[deal_id].add("linked_entity")
        batch_requests = [(key, method, params) for key, (method, params) in requests.items()]
        batch = getattr(client, "safe_batch_call", None)
        responses = batch(batch_requests) if callable(batch) and batch_requests else {
            key: client.safe_call(method, params) for key, method, params in batch_requests
        }
        for key, response in responses.items():
            data = _result(response)
            for deal_id, previous in known[key].items():
                was_failed = prior_failures.get(key, {}).get(deal_id, False)
                if not response.get("ok") and not failure_due(deal_id, was_failed):
                    continue
                changed = not response.get("ok")
                changed |= was_failed and bool(response.get("ok"))
                if key.startswith("timeline:"):
                    changed |= not isinstance(data, list) or _different(data or [], previous)
                elif key.startswith("chat:"):
                    changed |= not isinstance(data, dict) or _different((data or {}).get("messages", []), previous, ("text", "date", "author_id"))
                else:
                    changed |= digest(data) != digest(previous[0])
                if changed:
                    changes[deal_id].add(key.split(":", 1)[0])
    return changes


def plan_automatic_refresh(*, db_path: str | Path, deal_ids: list[str], client: Any = None,
                           now: datetime | None = None, sources_ok: bool = True,
                           workspace_root: Path = WORKSPACE_ROOT, audio_root: Path = AUDIO_ROOT) -> dict[str, dict]:
    current = now or datetime.now(MSK_TZ)
    versions = crm_trajectory_signal_versions(db_path)
    portfolio = {str(row["deal_id"]): row for row in list_deal_control_deals(db_path)}
    plans, probe_payloads = {}, {}
    for deal_id in deal_ids:
        stored = get_crm_sync_state(db_path, f"deal_context:{deal_id}")
        ack = get_crm_sync_state(db_path, f"deal_ack:{deal_id}")
        payload, acknowledged = (stored or {}).get("payload", {}), (ack or {}).get("payload", {})
        raw = payload.get("context") or {}
        events = {key: versions.get(key, 0) for key in entity_histories(payload)}
        events[f"deal:{deal_id}"] = versions.get(f"deal:{deal_id}", 0)
        plan = {"mode": "skip", "reasons": [], "events": events, "ack_revision": (ack or {}).get("revision", 0)}
        if not raw or not ack or not raw.get("sync", {}).get("activity_cursor"):
            plan.update(mode="full", reasons=["initial_or_unacknowledged"])
        elif due(payload.get("full_attempt_at") or payload.get("full_success_at"), current):
            plan.update(mode="full", reasons=["reconciliation"])
        else:
            deal = (raw.get("deal") or {}).get("item") or {}
            row = portfolio.get(deal_id) or {}
            if any(str(deal.get(field) or "") != str(row.get(local) or "") for field, local in (
                ("DATE_MODIFY", "modified_at_crm"), ("STAGE_ID", "stage_id"), ("ASSIGNED_BY_ID", "manager_id"), ("OPPORTUNITY", "amount"),
            )):
                plan["reasons"].append("deal_fields")
            if events != acknowledged.get("events"):
                plan["reasons"].append("trajectory")
            retry_at = parsed_at(raw.get("sync", {}).get("retry_at"))
            retry_due = raw.get("sync", {}).get("retry_required") and (retry_at is None or current >= retry_at)
            if retry_due or stored["revision"] != acknowledged.get("context_revision"):
                plan["reasons"].append("recovery")
            if not sources_ok:
                plan["reasons"].append("collection_error")
            if plan["reasons"]:
                plan["mode"] = "incremental"
            else:
                probe_payloads[deal_id] = payload
            if plan["mode"] == "skip" and transcript_signature(deal_id, workspace_root) != acknowledged.get("transcript_signature"):
                plan.update(mode="local", reasons=["new_transcript"])
            if plan["mode"] == "skip" and time_signal(raw, current) != acknowledged.get("time_signal"):
                plan.update(mode="local", reasons=["time_threshold"])
            if plan["mode"] in {"skip", "local"} and audio_due(payload, deal_id, current, audio_root=audio_root):
                plan["mode"] = "audio"
                plan["reasons"].append("audio_recheck")
        plans[deal_id] = plan
    if probe_payloads:
        if client is None:
            from api.candidates import make_client
            client = make_client()
        for deal_id, reasons in probe_changes(client, probe_payloads, now=current).items():
            if reasons:
                plans[deal_id]["mode"] = "incremental"
                plans[deal_id]["reasons"].extend(sorted(reasons))
    return plans


def acknowledge_refresh(db_path: str | Path, deal_id: str, plan: dict, *, workspace_root: Path = WORKSPACE_ROOT) -> None:
    stored = get_crm_sync_state(db_path, f"deal_context:{deal_id}")
    if not stored:
        return
    # Preserve the observed event watermark from enqueue time. A newer scheduler
    # observation during the job must still trigger the next refresh.
    events = dict(plan.get("events") or {})
    for key in entity_histories(stored["payload"]):
        events.setdefault(key, 0)
    put_crm_sync_state(db_path, f"deal_ack:{deal_id}", {
        "events": events, "context_revision": stored["revision"],
        "transcript_signature": transcript_signature(deal_id, workspace_root),
        "time_signal": time_signal(stored["payload"].get("context") or {}, datetime.now(MSK_TZ)),
    }, expected_revision=plan.get("ack_revision", 0))
