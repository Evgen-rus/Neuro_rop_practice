"""Cheap, conservative gate BEFORE automatic deal pipelines.

Uses the existing portfolio/trajectory plus batched version/head probes. A head
probe is not a history download: bursts are drained by the normal context job.
No successful job acknowledgement means retry, never an idle cache hit.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bitrix.context_sync import OVERLAP, digest, due, parsed_at
from bitrix.deals.download_deals_call_audio import (
    audio_file_discovery_expired, call_activities, client_day_related_call_activities,
    load_existing_manifest, should_recheck_recording,
)
from bitrix.usage_trace import bitrix_trace_context
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import (
    crm_trajectory_signal_versions, get_crm_sync_state, list_crm_trajectory_signals_since,
    list_deal_control_deals, put_crm_sync_state,
)

WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"
AUDIO_ROOT = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"
TRAJECTORY_GATE_ACK_PREFIX = "deal_trajectory_gate_ack:"


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
    context = payload.get("context") or {}
    activities = list(call_activities(context))
    skip_ids = {str(item.get("ID") or "") for item in activities if item.get("ID")}
    lead_id = str((context.get("source_lead") or {}).get("lead_id") or "")
    activities.extend(
        client_day_related_call_activities(
            payload.get("customer_history") or {},
            deal_id=str(deal_id),
            lead_id=lead_id,
            now=now,
            skip_ids=skip_ids,
        )
    )
    for activity in activities:
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


def activity_probe_since(payload: dict, probe: dict | None) -> str | None:
    """Lower bound for cheap activity list: last successful probe or fetch, not the oldest deal in the pool."""
    stamps = []
    for value in (
        (probe or {}).get("activity_probe_at"),
        ((payload.get("context") or {}).get("sync") or {}).get("activity_cursor"),
        ((payload.get("customer_history") or {}).get("sync") or {}).get("activity_cursor"),
    ):
        parsed = parsed_at(value)
        if parsed:
            stamps.append(parsed)
    if not stamps:
        return None
    latest = max(stamps).astimezone(MSK_TZ).replace(second=0, microsecond=0)
    return (latest - OVERLAP).isoformat(timespec="seconds")


def record_activity_probe(db_path: str | Path, deal_id: str, now: datetime) -> None:
    key = f"deal_probe:{deal_id}"
    stored = get_crm_sync_state(db_path, key)
    try:
        put_crm_sync_state(
            db_path,
            key,
            {"activity_probe_at": now.isoformat(timespec="seconds")},
            expected_revision=(stored or {}).get("revision", 0),
        )
    except RuntimeError:
        pass


def deal_job_can_acknowledge(progress: dict | None) -> bool:
    """Ack only after a terminal deal decision or audio idle, never after a failed analysis."""
    row = progress or {}
    if row.get("stage") == "audio_idle" and row.get("status") == "done":
        return True
    if row.get("publish_ready") is True and row.get("status") != "error":
        from progress_events import compact_decision_status
        return compact_decision_status(row.get("decision_status")) in {"full", "mini", "skip"}
    return False


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


def _same_amount(left: Any, right: Any) -> bool:
    """Compare finite amounts exactly; missing/invalid values never become zero."""
    left_text = "" if left is None else str(left).strip()
    right_text = "" if right is None else str(right).strip()
    if not left_text or not right_text:
        return left_text == right_text
    try:
        left_number, right_number = Decimal(left_text), Decimal(right_text)
    except InvalidOperation:
        return left_text == right_text
    if not left_number.is_finite() or not right_number.is_finite():
        return left_text == right_text
    return left_number == right_number


def _trajectory_direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "in", "incoming", "входящий"}:
        return "incoming"
    if normalized in {"2", "out", "outgoing", "исходящий"}:
        return "outgoing"
    return "unknown"


def trajectory_event_disposition(event: dict[str, Any]) -> str:
    """Classify a new trajectory fact for the automatic-analysis gate.

    Only explicit manager-side operational facts are soft. Anything that may
    contain a client signal, a stage/business change, or cannot be identified
    confidently keeps the previous conservative heavy path.
    """
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type in {"deal_stage_changed", "lead_stage_changed", "crm_stage_history_observed"}:
        return "hard"
    if event_type == "crm_business_field_changed":
        field_name = str(payload.get("field_name") or "").strip().upper()
        return "soft" if field_name in {"DATE_MODIFY", "MODIFY_BY_ID", "COMMENTS"} else "hard"
    if event_type == "crm_task_history_observed":
        return "soft"
    if event_type == "crm_timeline_comment_observed":
        return "hard" if payload.get("is_messenger_mirror") else "soft"
    if event_type == "crm_activity_planned":
        return "soft"
    if event_type != "crm_activity_observed":
        return "hard"

    kind = str(payload.get("activity_kind") or "").strip().lower()
    if kind == "task":
        return "soft"
    if kind not in {"call", "email", "message"}:
        return "hard"
    if not bool(payload.get("completed")):
        return "hard"
    direction = _trajectory_direction(payload.get("direction"))
    if kind in {"email", "message"}:
        return "soft" if direction == "outgoing" else "hard"
    if kind == "call":
        # A recording may appear after the first CRM observation. Keep calls on
        # the existing audio/context path rather than permanently hiding their ID.
        return "hard"
    return "hard"


def _trajectory_probe_ignores(events: list[dict[str, Any]]) -> dict[str, set[str]]:
    ignores: dict[str, set[str]] = {}
    for event in events:
        if trajectory_event_disposition(event) != "soft":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        entity_key = f"{event.get('entity_type')}:{event.get('entity_id')}"
        activity_id = str(payload.get("activity_id") or "").strip()
        if activity_id:
            ignores.setdefault(f"activity:{entity_key}", set()).add(activity_id)
        comment_id = str(payload.get("comment_id") or "").strip()
        if comment_id:
            ignores.setdefault(f"timeline:{entity_key}", set()).add(comment_id)
        task_id = str(payload.get("task_id") or payload.get("associated_entity_id") or "").strip()
        if task_id:
            ignores.setdefault("task", set()).add(task_id)
    return ignores


def _stored_probe_ignores(payload: dict[str, Any]) -> dict[str, set[str]]:
    raw = payload.get("probe_ignores") if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): {str(value) for value in values if str(value)}
        for key, values in raw.items()
        if isinstance(values, list)
    }


def _merge_probe_ignores(*groups: dict[str, set[str]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for group in groups:
        for key, values in group.items():
            result.setdefault(key, set()).update(values)
    return result


def _save_trajectory_gate_ack(
    db_path: str | Path,
    deal_id: str,
    *,
    events: dict[str, int],
    probe_ignores: dict[str, set[str]],
    expected_revision: int,
) -> None:
    try:
        put_crm_sync_state(
            db_path,
            f"{TRAJECTORY_GATE_ACK_PREFIX}{deal_id}",
            {
                "events": dict(events),
                "probe_ignores": {key: sorted(values) for key, values in probe_ignores.items() if values},
            },
            expected_revision=expected_revision,
        )
    except RuntimeError:
        # A concurrent scheduler/job retained a newer watermark. Re-reading it
        # next cycle is safer than replacing it with this older observation.
        pass


def probe_changes(client: Any, payloads: dict[str, dict], *, now: datetime | None = None,
                  probe_states: dict[str, dict] | None = None,
                  ignored_ids: dict[str, dict[str, set[str]]] | None = None) -> dict[str, set[str]]:
    current = now or datetime.now(MSK_TZ)
    probe_states = probe_states or {}
    ignored_ids = ignored_ids or {}
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
            buckets: dict[str | None, list[str]] = {}
            for key in keys:
                owner_since = [activity_probe_since(payloads[deal_id], probe_states.get(deal_id))
                               for deal_id in owners.get(key, ()) if deal_id in payloads]
                buckets.setdefault(min((value for value in owner_since if value), default=None), []).append(key)
            for since, group_keys in buckets.items():
                filters = {"OWNER_TYPE_ID": owner_type, "OWNER_ID": [key.split(":", 1)[1] for key in group_keys]}
                if since:
                    filters[">=LAST_UPDATED"] = since
                response = client.safe_list_all("crm.activity.list", {"filter": filters,
                    "order": {"LAST_UPDATED": "ASC", "ID": "ASC"},
                    "select": ["ID", "OWNER_ID", "OWNER_TYPE_ID", "LAST_UPDATED", "FILES"]})
                for key in group_keys:
                    if not response.get("ok"):
                        for deal_id, history in histories[key].items():
                            if failure_due(deal_id, failed_before(history.get("activities") or {})):
                                changes[deal_id].add("probe_error")
                        continue
                    rows = [row for row in response.get("items", []) if str(row.get("OWNER_ID")) == key.split(":", 1)[1]]
                    for deal_id, history in histories[key].items():
                        ignored = ignored_ids.get(deal_id, {}).get(f"activity:{key}", set())
                        rows_for_deal = [row for row in rows if str(row.get("ID") or row.get("id") or "") not in ignored]
                        if _different(rows_for_deal, history.get("activities", {}).get("items", []), ("LAST_UPDATED", "FILES")):
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
                    if not response.get("ok"):
                        if failure_due(deal_id, True):
                            changes[deal_id].add("probe_error")
                        continue
                    if item and str(item.get("DATE_MODIFY") or "") != str(previous.get("DATE_MODIFY") or ""):
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
                    ignored = ignored_ids.get(deal_id, {}).get(key, set())
                    rows = [
                        row for row in data
                        if str(row.get("ID") or row.get("id") or "") not in ignored
                    ] if isinstance(data, list) else []
                    changed |= not isinstance(data, list) or _different(rows, previous)
                elif key.startswith("chat:"):
                    changed |= not isinstance(data, dict) or _different((data or {}).get("messages", []), previous, ("text", "date", "author_id"))
                else:
                    task_id = key.split(":", 1)[1]
                    if task_id not in ignored_ids.get(deal_id, {}).get("task", set()):
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
    trajectory_probe_ignores: dict[str, dict[str, set[str]]] = {}
    soft_ack_candidates: dict[str, tuple[dict[str, int], dict[str, set[str]], int]] = {}
    for deal_id in deal_ids:
        stored = get_crm_sync_state(db_path, f"deal_context:{deal_id}")
        ack = get_crm_sync_state(db_path, f"deal_ack:{deal_id}")
        trajectory_ack = get_crm_sync_state(db_path, f"{TRAJECTORY_GATE_ACK_PREFIX}{deal_id}")
        payload, acknowledged = (stored or {}).get("payload", {}), (ack or {}).get("payload", {})
        trajectory_acknowledged = (
            (trajectory_ack or {}).get("payload", {})
            if trajectory_ack is not None
            else acknowledged
        )
        raw = payload.get("context") or {}
        events = {key: versions.get(key, 0) for key in entity_histories(payload)}
        events[f"deal:{deal_id}"] = versions.get(f"deal:{deal_id}", 0)
        prior_events = {
            key: int((trajectory_acknowledged.get("events") or {}).get(key, 0) or 0)
            for key in events
        }
        trajectory_events = list_crm_trajectory_signals_since(db_path, prior_events)
        hard_trajectory = [event for event in trajectory_events if trajectory_event_disposition(event) == "hard"]
        soft_trajectory = [event for event in trajectory_events if trajectory_event_disposition(event) == "soft"]
        probe_ignores = _merge_probe_ignores(
            _stored_probe_ignores(trajectory_acknowledged),
            _trajectory_probe_ignores(soft_trajectory),
        )
        trajectory_probe_ignores[deal_id] = probe_ignores
        plan = {
            "mode": "skip",
            "reasons": [],
            "events": events,
            "ack_revision": (ack or {}).get("revision", 0),
            "trajectory_ack_revision": (trajectory_ack or {}).get("revision", 0),
            "trajectory_event_types": sorted({str(event.get("event_type") or "unknown") for event in hard_trajectory}),
            "trajectory_soft_event_types": sorted({str(event.get("event_type") or "unknown") for event in soft_trajectory}),
        }
        if not raw or not ack or not raw.get("sync", {}).get("activity_cursor"):
            plan.update(mode="full", reasons=["initial_or_unacknowledged"])
        elif due(payload.get("full_attempt_at") or payload.get("full_success_at"), current):
            plan.update(mode="full", reasons=["reconciliation"])
        else:
            deal = (raw.get("deal") or {}).get("item") or {}
            row = portfolio.get(deal_id) or {}
            if any(str(deal.get(field) or "") != str(row.get(local) or "") for field, local in (
                ("STAGE_ID", "stage_id"), ("ASSIGNED_BY_ID", "manager_id"),
            )) or not _same_amount(deal.get("OPPORTUNITY"), row.get("amount")):
                plan["reasons"].append("deal_fields")
            if hard_trajectory:
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
        if soft_trajectory and not hard_trajectory and sources_ok:
            soft_ack_candidates[deal_id] = (
                events,
                probe_ignores,
                (trajectory_ack or {}).get("revision", 0),
            )
        plans[deal_id] = plan
    if probe_payloads:
        if client is None:
            from api.candidates import make_client
            client = make_client()
        probe_states = {
            deal_id: ((get_crm_sync_state(db_path, f"deal_probe:{deal_id}") or {}).get("payload") or {})
            for deal_id in probe_payloads
        }
        for deal_id, reasons in probe_changes(
            client,
            probe_payloads,
            now=current,
            probe_states=probe_states,
            ignored_ids=trajectory_probe_ignores,
        ).items():
            if reasons:
                plans[deal_id]["mode"] = "incremental"
                plans[deal_id]["reasons"].extend(sorted(reasons))
            if "probe_error" not in reasons:
                record_activity_probe(db_path, deal_id, current)
    for deal_id, (events, probe_ignores, expected_revision) in soft_ack_candidates.items():
        if plans[deal_id]["mode"] == "skip":
            _save_trajectory_gate_ack(
                db_path,
                deal_id,
                events=events,
                probe_ignores=probe_ignores,
                expected_revision=expected_revision,
            )
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
    _save_trajectory_gate_ack(
        db_path,
        deal_id,
        events=events,
        probe_ignores={},
        expected_revision=plan.get("trajectory_ack_revision", 0),
    )
