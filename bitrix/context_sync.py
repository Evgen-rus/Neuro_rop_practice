"""Deal context sync primitives. CRM content stays in local storage, never traces.

The raw/customer-history builders own the data model; this module only handles
safe paging, cache lifetime and atomic materialization of their existing data.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.usage_trace import bitrix_trace_context
from setup import MSK_TZ
from storage.rop_db import DEFAULT_DB_PATH, get_crm_sync_state, put_crm_sync_state

OVERLAP = timedelta(minutes=15)
RECONCILIATION_INTERVAL = timedelta(days=1)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def parsed_at(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=MSK_TZ)
    except (ValueError, TypeError):
        return None


def due(value: Any, now: datetime, interval: timedelta = RECONCILIATION_INTERVAL) -> bool:
    previous = parsed_at(value)
    return previous is None or now + OVERLAP < previous or now - previous >= interval


def source_since(container: dict | None) -> str | None:
    cursor = parsed_at((container or {}).get("last_success_at"))
    return (cursor - OVERLAP).isoformat() if cursor else None


def merge_rows(previous: list, current: list) -> list[dict]:
    rows = {}
    for item in [*previous, *current]:
        if isinstance(item, dict):
            key = str(item.get("ID") or item.get("id") or digest(item))
            rows[key] = item
    return list(rows.values())


def retained_response(response: dict, previous: dict | None, started_at: str, *, incremental: bool = True) -> dict:
    """Never substitute a failed read for a successful empty result."""
    result = copy.deepcopy(response)
    old = previous or {}
    if not response.get("ok"):
        result["items"] = copy.deepcopy(old.get("items") or [])
        result["last_success_at"] = old.get("last_success_at")
        result["stale"] = bool(old)
    else:
        result["items"] = merge_rows(old.get("items") or [], response.get("items") or []) if incremental else response.get("items", [])
        result["last_success_at"] = started_at
    return result


def has_failed_source(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("ok") is False or value.get("refresh_ok") is False:
            return True
        # Raw CRM fields may themselves contain an 'ok'; only wrappers use it.
        return any(has_failed_source(child) for key, child in value.items() if key not in {"payload", "response", "sync"})
    return isinstance(value, list) and any(has_failed_source(child) for child in value)


def retain_failed_sources(current: Any, previous: Any) -> Any:
    """Keep usable old evidence, but expose refresh failure separately from emptiness."""
    if isinstance(current, dict):
        old = previous if isinstance(previous, dict) else {}
        if current.get("ok") is False and old.get("ok"):
            return {**copy.deepcopy(old), "refresh_ok": False, "stale": True,
                    "refresh_error": current.get("error") or "Source refresh failed"}
        result = {key: retain_failed_sources(child, old.get(key)) for key, child in current.items()}
        if isinstance(current.get("response"), dict) and current["response"].get("ok") is False and "item" in old:
            result["item"] = copy.deepcopy(old["item"])
        return result
    if isinstance(current, list):
        old = previous if isinstance(previous, list) else []
        def identity(row):
            if isinstance(row, dict):
                for key in ("chat_id", "dialog_id", "entity_key", "ID", "id"):
                    if row.get(key) is not None:
                        return key, str(row[key])
            return None
        by_id = {identity(row): row for row in old if identity(row) is not None}
        return [retain_failed_sources(child, by_id.get(identity(child)) if identity(child) is not None
                else old[index] if index < len(old) and identity(old[index]) is None else None)
                for index, child in enumerate(current)]
    return current


def operational_context_fingerprint(raw: dict, customer: dict | None) -> str:
    """Internal evidence must reach MINI detection without becoming client speech."""
    def messages(response):
        rows = (response or {}).get("response", {}).get("result", {}).get("messages") or []
        return sorted(({key: row.get(key) for key in ("id", "text", "date", "author_id")} for row in rows), key=lambda row: str(row["id"]))
    task_messages = {key: messages(response) for key, response in raw.get("bitrix_task_chats", {}).items()}
    return digest({
        "tasks": {key: response.get("response", {}).get("result") for key, response in raw.get("bitrix_tasks", {}).items()},
        "task_messages": task_messages,
        "stages": sorted(raw.get("stage_history", {}).get("items", []), key=lambda row: str(row.get("ID"))),
        "communications": (customer or {}).get("normalized_communications") or [],
    })


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def local_sync_lock(path: Path):
    """OS lock: cross-process, released on crash, never stolen after a timeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("CRM workspace is busy; retry after the active job") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def timeline_delta(client: Any, entity_type: str, entity_id: str, *, since: str | None = None,
                   known: list[dict] | None = None) -> dict:
    """Only ENTITY_TYPE/ENTITY_ID are documented timeline filters.

    Read newest first through a known page boundary older than the overlap.
    Full reconciliation deliberately has no boundary (including edited history).
    """
    payload = {"filter": {"ENTITY_TYPE": entity_type, "ENTITY_ID": entity_id},
               "order": {"CREATED": "DESC", "ID": "DESC"}}
    if not since:
        with bitrix_trace_context(component="timeline_delta"):
            return client.safe_list_all("crm.timeline.comment.list", payload)
    boundary = parsed_at(since)
    known_ids = {str(row.get("ID")) for row in (known or [])}
    rows: list[dict] = []
    start = 0
    seen_starts = set()
    with bitrix_trace_context(component="timeline_delta"):
        while True:
            response = client.safe_call("crm.timeline.comment.list", {**payload, "start": start})
            if not response.get("ok"):
                return {**response, "items": []}
            data = response.get("response") or {}
            page = data.get("result")
            if not isinstance(page, list):
                return {"ok": False, "items": [], "error": "Invalid timeline page"}
            rows = merge_rows(rows, page)
            reached = any(
                (not known_ids or str(row.get("ID")) in known_ids)
                and parsed_at(row.get("CREATED")) is not None
                and boundary is not None and parsed_at(row.get("CREATED")) < boundary
                for row in page
            )
            next_start = data.get("next")
            if reached or next_start is None:
                return {"ok": True, "items": rows, "method": "crm.timeline.comment.list", "payload": payload}
            if next_start in seen_starts or next_start == start:
                return {"ok": False, "items": [], "error": "Timeline pagination did not advance"}
            seen_starts.add(start)
            start = next_start


def dialog_delta(client: Any, dialog_id: str, previous: dict | None = None, *, full: bool = False) -> dict:
    old = (previous or {}).get("response", {}).get("result") or {}
    known = {str(row.get("id") or row.get("ID")) for row in old.get("messages", [])}
    boundary = parsed_at(source_since(previous))
    result = {"messages": [], "users": [], "files": []}
    last_id = None
    with bitrix_trace_context(component="chat_update"):
        while True:
            payload = {"DIALOG_ID": dialog_id, "LIMIT": 50}
            if last_id is not None:
                payload["LAST_ID"] = last_id
            response = client.safe_call("im.dialog.messages.get", payload)
            data = (response.get("response") or {}).get("result")
            if not response.get("ok") or not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                if previous and previous.get("ok"):
                    return {**copy.deepcopy(previous), "refresh_ok": False, "stale": True,
                            "refresh_error": response.get("error") or "Invalid IM page"}
                return {**response, "ok": False, "response": {"result": copy.deepcopy(old)},
                        "last_success_at": (previous or {}).get("last_success_at"), "stale": bool(previous)}
            page = data["messages"]
            for key in result:
                result[key] = merge_rows(result[key], data.get(key) or [])
            reached = not full and any(
                str(row.get("id") or row.get("ID")) in known
                and (boundary is None or (parsed_at(row.get("date")) is not None and parsed_at(row.get("date")) < boundary))
                for row in page
            )
            if reached or len(page) < 50:
                break
            ids = [int(row.get("id") or row.get("ID")) for row in page if str(row.get("id") or row.get("ID") or "").isdigit()]
            if not ids or (last_id is not None and min(ids) >= last_id):
                if previous and previous.get("ok"):
                    return {**copy.deepcopy(previous), "refresh_ok": False, "stale": True,
                            "refresh_error": "IM pagination did not advance"}
                return {"ok": False, "response": {"result": copy.deepcopy(old)}, "error": "IM pagination did not advance",
                        "last_success_at": (previous or {}).get("last_success_at")}
            last_id = min(ids)
    for key in result:
        result[key] = merge_rows(old.get(key) or [], result[key])
    return {"ok": True, "response": {"result": result}, "last_success_at": datetime.now(MSK_TZ).isoformat()}


class ContextReadClient:
    """Per-job entity memo + daily persisted schema cache, scoped to credentials.

    Entity records are NOT reused across jobs without a reliable version signal.
    Structured invoices/products are disabled by the deal context builder.
    """
    def __init__(self, client: Any, *, db_path: str | Path = DEFAULT_DB_PATH, full: bool = False):
        self.client, self.db_path, self.full = client, db_path, full
        self.memo: dict[str, dict] = {}
        self.scope = digest(getattr(client, "webhook_url", "local-test"))

    def __getattr__(self, key):
        return getattr(self.client, key)

    @property
    def retry_callback(self):
        return self.client.retry_callback

    @retry_callback.setter
    def retry_callback(self, value):
        self.client.retry_callback = value

    def safe_call(self, method: str, payload: dict | None = None) -> dict:
        cacheable = method.endswith(".fields") or method in {
            "crm.deal.get", "crm.lead.get", "crm.contact.get", "crm.company.get",
            "crm.deal.contact.items.get", "user.get", "tasks.task.get",
        }
        key = method + ":" + digest(payload or {})
        if cacheable and key in self.memo:
            return copy.deepcopy(self.memo[key])
        schema_key = f"schema:{self.scope}:{key}"
        cached = get_crm_sync_state(self.db_path, schema_key) if method.endswith(".fields") else None
        if cached and not due(cached["updated_at"], datetime.now(MSK_TZ)):
            response = cached["payload"]
        else:
            with bitrix_trace_context(component="entity_cache" if cacheable else None):
                response = self.client.safe_call(method, payload)
            if method.endswith(".fields") and response.get("ok"):
                try:
                    put_crm_sync_state(self.db_path, schema_key, response, expected_revision=(cached or {}).get("revision", 0))
                except RuntimeError:
                    pass  # another job refreshed this cache; never overwrite it
        if cacheable and response.get("ok"):
            self.memo[key] = copy.deepcopy(response)
        return response

    def safe_list_all(self, method: str, payload: dict | None = None) -> dict:
        key = "list:" + method + ":" + digest(payload or {})
        if key not in self.memo:
            result = self.client.safe_list_all(method, payload)
            if not result.get("ok"):
                return result
            self.memo[key] = copy.deepcopy(result)
        return copy.deepcopy(self.memo[key])

    def safe_batch_call(self, requests_to_run: list) -> dict:
        # Keep batch transport while sharing already fetched entities inside a job.
        result, pending = {}, []
        for name, method, payload in requests_to_run:
            key = method + ":" + digest(payload or {})
            if key in self.memo:
                result[name] = copy.deepcopy(self.memo[key])
            else:
                pending.append((name, method, payload))
        batch = getattr(self.client, "safe_batch_call", None)
        responses = batch(pending) if callable(batch) and pending else {
            name: self.safe_call(method, payload) for name, method, payload in pending
        }
        for name, method, payload in pending:
            response = responses[name]
            result[name] = response
            if response.get("ok"):
                self.memo[method + ":" + digest(payload or {})] = copy.deepcopy(response)
        return result
