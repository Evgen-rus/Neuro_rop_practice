"""Synthetic Bitrix transport shared by Phase 2 integration tests and benchmark.

No real credentials, CRM content, sockets, OpenAI or audio downloads are used.
All pagination/batch handling above requests.post remains production code.
"""
from __future__ import annotations
import copy
import importlib.util
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

from setup import MSK_TZ
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

from api.crm_change_gate import acknowledge_refresh, plan_automatic_refresh
from bitrix.client import BitrixReadOnlyClient
from storage.rop_db import get_crm_sync_state, init_db, upsert_deal_control_deal


def fetch_module():
    path = Path(__file__).resolve().parents[1] / "bitrix/deals/1_fetch_deals_context.py"
    spec = importlib.util.spec_from_file_location("phase2_fixture_fetch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_payload(query: str) -> dict:
    result = {}
    for name, value in parse_qsl(query):
        parts = re.findall(r"[^\[\]]+", name)
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if name.endswith("[]"):
            node.setdefault(parts[-1], []).append(value)
        else:
            node[parts[-1]] = value
    return result


class SyntheticBitrix:
    def __init__(self, count=1, *, now=None):
        self.now = now or datetime.now(MSK_TZ).replace(microsecond=0)
        self.physical = 0
        self.commands = Counter()
        self.fail = set()
        self.deals, self.contacts, self.activities, self.comments, self.stages, self.chats, self.tasks = {}, {}, {}, {}, {}, {}, {}
        stamp = (self.now - timedelta(hours=2)).isoformat()
        for offset in range(count):
            deal_id, contact_id = str(10001 + offset), str(20001 + offset)
            self.deals[deal_id] = {"ID": deal_id, "TITLE": "Синтетическая сделка", "CONTACT_ID": contact_id,
                "STAGE_ID": "NEW", "ASSIGNED_BY_ID": "7", "OPPORTUNITY": "100", "CURRENCY_ID": "RUB",
                "DATE_MODIFY": stamp, "DATE_CREATE": stamp}
            self.contacts[contact_id] = {"ID": contact_id, "NAME": "Синтетический контакт", "DATE_MODIFY": stamp}
            self.activities[deal_id] = [{"ID": str(30000 + offset), "OWNER_ID": deal_id, "OWNER_TYPE_ID": "2",
                "TYPE_ID": "4", "PROVIDER_ID": "CRM_EMAIL", "SUBJECT": "Тестовое письмо", "COMPLETED": "Y",
                "LAST_UPDATED": stamp, "START_TIME": stamp, "CREATED": stamp, "FILES": []}]
            self.comments[("deal", deal_id)] = [{"ID": str(40000 + offset * 1000 + index), "CREATED": stamp,
                "AUTHOR_ID": "7", "COMMENT": f"Синтетический комментарий {index}"} for index in range(120)]
            self.stages[deal_id] = [{"ID": str(50000 + offset), "OWNER_ID": deal_id, "CREATED_TIME": stamp, "STAGE_ID": "NEW"}]

    def reset_counts(self):
        self.physical = 0
        self.commands.clear()

    def post(self, url, *, json, **kwargs):
        self.physical += 1
        method = url.rsplit("/", 1)[-1]
        data = self.dispatch(method, json)
        return SimpleNamespace(ok=True, status_code=200, json=lambda: data,
                               raise_for_status=lambda: None)

    def dispatch(self, method, params):
        if method == "batch":
            results, errors, nexts = {}, {}, {}
            for name, command in params["cmd"].items():
                nested, _, query = command.partition("?")
                data = self.dispatch(nested, decode_payload(query))
                if data.get("error"):
                    errors[name] = data
                else:
                    results[name] = data["result"]
                    if "next" in data:
                        nexts[name] = data["next"]
            return {"result": {"result": results, "result_error": errors, "result_next": nexts}}
        self.commands[method] += 1
        if method in self.fail:
            return {"error": "FIXTURE_UNAVAILABLE", "error_description": "Synthetic unavailable source"}
        entity_id = str(params.get("id") or params.get("ID") or "")
        filters = params.get("filter") or {}
        if method.endswith(".fields"):
            return {"result": {"ID": {"type": "integer", "title": "ID"}}}
        if method == "crm.deal.get":
            return {"result": copy.deepcopy(self.deals[entity_id])}
        if method == "crm.contact.get":
            return {"result": copy.deepcopy(self.contacts[entity_id])}
        if method == "crm.deal.contact.items.get":
            return {"result": [{"CONTACT_ID": self.deals[entity_id]["CONTACT_ID"]}]}
        if method == "user.get":
            return {"result": [{"ID": entity_id, "NAME": "Тестовый менеджер"}]}
        if method == "tasks.task.get":
            return {"result": {"task": copy.deepcopy(self.tasks[str(params["taskId"])])}}
        if method in {"crm.deal.productrows.get", "crm.invoice.list", "crm.item.list"}:
            return {"result": []}
        if method == "crm.activity.get":
            return {"result": copy.deepcopy(next(row for rows in self.activities.values() for row in rows if row["ID"] == entity_id))}
        if method == "crm.activity.list":
            ids = filters.get("OWNER_ID", [])
            ids = [str(ids)] if not isinstance(ids, list) else [str(value) for value in ids]
            rows = [row for entity in ids for row in self.activities.get(entity, []) if str(row.get("OWNER_TYPE_ID")) == str(filters.get("OWNER_TYPE_ID"))]
            since = filters.get(">LAST_UPDATED") or filters.get(">=LAST_UPDATED")
            if since:
                rows = [row for row in rows if row.get("LAST_UPDATED", "") >= since]
            return self.page(rows, params)
        if method == "crm.timeline.comment.list":
            assert set(filters) == {"ENTITY_TYPE", "ENTITY_ID"}, "No invented timeline filters"
            rows = self.comments.get((filters["ENTITY_TYPE"], str(filters["ENTITY_ID"])), [])
            rows = sorted(rows, key=lambda row: (row["CREATED"], int(row["ID"])), reverse=(params.get("order", {}).get("CREATED") == "DESC"))
            return self.page(rows, params)
        if method == "crm.stagehistory.list":
            rows = self.stages.get(str(filters["OWNER_ID"]), [])
            if filters.get(">=CREATED_TIME"):
                rows = [row for row in rows if row["CREATED_TIME"] >= filters[">=CREATED_TIME"]]
            return self.page(rows, params, wrapped=True)
        if method in {"crm.deal.list", "crm.contact.list", "crm.company.list"}:
            records = self.deals if method == "crm.deal.list" else self.contacts if method == "crm.contact.list" else {}
            ids = filters.get("ID")
            ids = [str(ids)] if ids is not None and not isinstance(ids, list) else ids
            rows = [row for row in records.values() if ids is None or row["ID"] in ids]
            if filters.get("CONTACT_ID"):
                rows = [row for row in rows if row.get("CONTACT_ID") == str(filters["CONTACT_ID"])]
            return self.page(rows, params)
        if method == "im.search.chat.list":
            return {"result": [{"id": key, "entity_id": value["entity_id"], "name": "Синтетический чат"} for key, value in self.chats.items()]}
        if method == "im.dialog.messages.get":
            key = str(params["DIALOG_ID"]).removeprefix("chat")
            rows = self.chats[key]["messages"]
            rows = [row for row in rows if not params.get("LAST_ID") or int(row["id"]) < int(params["LAST_ID"])]
            rows = sorted(rows, key=lambda row: int(row["id"]), reverse=True)[:int(params.get("LIMIT", 50))]
            return {"result": {"messages": copy.deepcopy(rows), "users": [], "files": []}}
        if method == "im.dialog.users.list":
            return {"result": []}
        if method == "im.chat.get":
            return {"result": {"id": params["CHAT_ID"]}}
        raise AssertionError(f"Unsupported synthetic method {method}")

    @staticmethod
    def page(rows, params, wrapped=False):
        start = int(params.get("start", 0))
        selected = copy.deepcopy(rows[start:start + 50])
        result = {"result": {"items": selected} if wrapped else selected}
        if start + 50 < len(rows):
            result["next"] = start + 50
        return result


class SnapshotHarness:
    """Context-only harness. Acknowledgement stands in for successful analysis."""
    def __init__(self, count=1):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.db = self.root / "fixture.sqlite"
        self.raw = self.root / "raw"
        self.workspace = self.root / "workspaces"
        self.audio = self.root / "audio"
        self.remote = SyntheticBitrix(count)
        self.ids = list(self.remote.deals)
        self.fetch = fetch_module()
        self.client = BitrixReadOnlyClient("https://fixture.invalid/rest/test")
        self.stack.enter_context(patch("bitrix.client.requests.post", side_effect=self.remote.post))
        self.stack.enter_context(patch.dict("os.environ", {"BITRIX_USAGE_DAILY_DIR": str(self.root / "trace"), "BITRIX_DENY_WRITE_METHODS": "1"}))
        init_db(self.db)
        self.sync_portfolio()

    def sync_portfolio(self):
        for deal_id, deal in self.remote.deals.items():
            upsert_deal_control_deal(self.db, deal_id=deal_id, source="initial", title=deal["TITLE"],
                manager_id=deal["ASSIGNED_BY_ID"], manager_name="Тестовый менеджер", stage_id=deal["STAGE_ID"],
                stage_name=deal["STAGE_ID"], pipeline_id="0", amount=deal["OPPORTUNITY"], currency_id="RUB",
                created_at_crm=deal["DATE_CREATE"], modified_at_crm=deal["DATE_MODIFY"], is_active=True)

    def plans(self, now=None):
        return plan_automatic_refresh(db_path=self.db, deal_ids=self.ids, client=self.client,
            now=now or self.remote.now, workspace_root=self.workspace, audio_root=self.audio)

    def refresh(self, deal_id, plan, *, acknowledge=True):
        args = ["fetch", "--deal-ids", deal_id, "--output-dir", str(self.raw), "--db-path", str(self.db),
                "--include-related-contact-deals", "--include-internal-context"]
        if plan["mode"] == "incremental":
            args.append("--incremental-context")
        if plan.get("reasons"):
            args.append("--rediscover-chats")
        with patch.object(self.fetch, "load_dotenv"), patch.object(self.fetch, "get_env_required", return_value="https://fixture.invalid/rest/test"), patch.object(sys, "argv", args):
            self.fetch.main()
        if acknowledge:
            acknowledge_refresh(self.db, deal_id, plan, workspace_root=self.workspace)

    def initial(self):
        plans = self.plans()
        for deal_id, plan in plans.items():
            self.refresh(deal_id, plan)

    def state(self, deal_id=None):
        return get_crm_sync_state(self.db, f"deal_context:{deal_id or self.ids[0]}")

    def close(self):
        self.stack.close()
