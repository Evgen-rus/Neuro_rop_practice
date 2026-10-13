from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import api.app as app_api
from api.deal_control import _manager_names
from bitrix.client import fetch_user_names
from api.candidates import (
    LEAD_CONTROL_HISTORY_KEY_PREFIX,
    LEAD_CONTROL_SNAPSHOT_KEY,
    LEAD_CONTROL_GROUPS,
    LeadControlConfigurationError,
    attach_saved_lead_qualification,
    fetch_profile_leads,
    load_lead_control_history,
    lead_control_snapshot_candidate,
    profile_candidates_preview,
    resolve_excluded_lead_statuses,
    resolve_lead_control_group,
    search_candidates,
)
from storage.rop_db import (
    default_analysis_profile,
    get_crm_sync_state,
    put_crm_sync_state,
    save_ui_report,
)


def _pipeline_map() -> dict[str, object]:
    return {
        "lead_pipeline": {
            "stages": [
                {
                    "status_id": status_id,
                    "name": config["name"],
                    "semantics": config["semantics"],
                }
                for group in LEAD_CONTROL_GROUPS.values()
                for status_id, config in group.items()
            ]
        }
    }


class FakeLeadClient:
    def __init__(
        self,
        statuses: list[dict[str, object]],
        leads: list[dict[str, object]] | None = None,
        users: list[dict[str, object]] | None = None,
        user_error: Exception | None = None,
    ):
        self.statuses = statuses
        self.leads = leads or []
        self.users = users or []
        self.user_error = user_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_all(self, method: str, payload: dict[str, object] | None = None) -> list[dict[str, object]]:
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.status.list":
            return self.statuses
        if method == "crm.lead.list":
            return self.leads
        if method == "user.get":
            if self.user_error:
                raise self.user_error
            wanted = {str(value) for value in (payload.get("filter") or {}).get("ID", [])}
            return [row for row in self.users if str(row.get("ID") or "") in wanted]
        raise AssertionError(method)

    def safe_list_all(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        if method != "crm.activity.list":
            raise AssertionError(method)
        return {"ok": True, "items": []}


class FakeLeadHistoryClient:
    def __init__(self, *, activities: dict[str, object] | Exception, comments: dict[str, object] | Exception):
        self.responses = {
            "crm.activity.list": activities,
            "crm.timeline.comment.list": comments,
        }
        self.calls: list[tuple[str, dict[str, object]]] = []

    def safe_list_all(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((method, payload or {}))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return dict(response)


def _candidate(entity_id: str, status_id: str, manager_id: str, *, created_days_ago: int = 1) -> dict[str, object]:
    created = (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=created_days_ago)).isoformat(timespec="seconds")
    return {
        "entity_type": "lead",
        "entity_id": entity_id,
        "title": f"Lead {entity_id}",
        "stage_id": status_id,
        "status": LEAD_CONTROL_GROUPS["active"].get(status_id, LEAD_CONTROL_GROUPS["closed"].get(status_id, {})).get("name", ""),
        "manager_id": manager_id,
        "date_create": created,
        "date_modify": created,
        "priority": "low",
        "score": 0,
        "attention_reason": "",
        "reasons": [],
        "reason_codes": [],
        "signals": [],
        "analysis_freshness": "missing",
    }


def _all_statuses() -> dict[str, dict[str, object]]:
    return {status_id: config for group in LEAD_CONTROL_GROUPS.values() for status_id, config in group.items()}


def _save_snapshot(db_path: Path, candidates: list[dict[str, object]], *, updated_at: str = "2026-10-06T10:00:00+03:00") -> int:
    return put_crm_sync_state(
        db_path,
        LEAD_CONTROL_SNAPSHOT_KEY,
        {"schema_version": 1, "crm_updated_at": updated_at, "candidates": candidates},
        expected_revision=0,
    )


class LeadControlMappingTests(unittest.TestCase):
    def test_shared_user_name_lookup_formats_last_and_first_name(self) -> None:
        client = FakeLeadClient(
            [],
            users=[
                {"ID": "2767", "LAST_NAME": "Сидоров", "NAME": "Иван"},
                {"ID": "2775", "LAST_NAME": "Петрова", "NAME": "Мария"},
            ],
        )
        names = fetch_user_names(client, {"2775", "2767"})
        self.assertEqual(names, {"2767": "Сидоров Иван", "2775": "Петрова Мария"})
        self.assertEqual(client.calls, [("user.get", {"filter": {"ID": ["2767", "2775"]}})])

    def test_existing_deal_manager_lookup_keeps_trace_fallback_contract(self) -> None:
        client = FakeLeadClient([], users=[{"ID": "2767", "LAST_NAME": "Сидоров", "NAME": "Иван"}])
        self.assertEqual(_manager_names(client, {"2767"}), {"2767": "Сидоров Иван"})
        self.assertEqual(client.calls, [("user.get", {"filter": {"ID": ["2767"]}})])
        failed = FakeLeadClient([], user_error=RuntimeError("unavailable"))
        self.assertEqual(_manager_names(failed, {"2767"}), {})

    def test_group_requires_exact_local_id_name_and_semantics(self) -> None:
        with patch("api.candidates.load_pipeline_map", return_value=_pipeline_map()):
            resolved = resolve_lead_control_group("active")
        self.assertEqual(resolved["stage_ids"], ["UC_W1ZZ1V", "UC_4QFNZN"])
        self.assertEqual(resolved["required_statuses"], LEAD_CONTROL_GROUPS["active"])

        wrong = _pipeline_map()
        wrong["lead_pipeline"]["stages"][0]["name"] = "Похожий статус"
        with patch("api.candidates.load_pipeline_map", return_value=wrong):
            with self.assertRaises(LeadControlConfigurationError):
                resolve_lead_control_group("active")

    def test_empty_local_mapping_fails_closed(self) -> None:
        with patch("api.candidates.load_pipeline_map", return_value={"lead_pipeline": {"stages": []}}):
            with self.assertRaises(LeadControlConfigurationError):
                resolve_lead_control_group("active")

    def test_live_status_mismatch_fails_closed(self) -> None:
        client = FakeLeadClient([{
            "STATUS_ID": "UC_W1ZZ1V",
            "NAME": "Идентификация (BANT) (хол.отдел)",
            "SEMANTICS": "F",
        }])
        required = {"UC_W1ZZ1V": LEAD_CONTROL_GROUPS["active"]["UC_W1ZZ1V"]}
        with self.assertRaises(LeadControlConfigurationError):
            resolve_excluded_lead_statuses(client, {}, required_statuses=required)

    def test_missing_live_status_fails_closed(self) -> None:
        client = FakeLeadClient([{
            "STATUS_ID": "UC_W1ZZ1V",
            "NAME": "Идентификация (BANT) (хол.отдел)",
            "SEMANTICS": None,
        }])
        with self.assertRaises(LeadControlConfigurationError):
            resolve_excluded_lead_statuses(client, {}, required_statuses=LEAD_CONTROL_GROUPS["active"])

    def test_lead_search_filters_status_and_manager_before_scoring(self) -> None:
        required = LEAD_CONTROL_GROUPS["active"]
        statuses = [
            {"STATUS_ID": status_id, "NAME": values["name"], "SEMANTICS": values["semantics"]}
            for status_id, values in required.items()
        ]
        client = FakeLeadClient(
            statuses,
            leads=[{
                "ID": "17",
                "STATUS_ID": "UC_W1ZZ1V",
                "ASSIGNED_BY_ID": "42",
                "DATE_CREATE": "2026-10-01T10:00:00+03:00",
            }],
        )
        profile = default_analysis_profile()
        profile["lead"]["all_stages"] = False
        profile["lead"]["stage_ids"] = list(required)
        leads, _ = fetch_profile_leads(
            client,
            profile=profile,
            period={
                "timezone": "Europe/Moscow",
                "period_from": "2026-09-06T00:00:00+03:00",
                "period_to": "2026-10-06T00:00:00+03:00",
            },
            required_statuses=required,
            manager_id="42",
        )
        lead_call = next(payload for method, payload in client.calls if method == "crm.lead.list")
        self.assertEqual(lead_call["filter"]["STATUS_ID"], list(required))
        self.assertEqual(lead_call["filter"]["ASSIGNED_BY_ID"], "42")
        self.assertEqual([item["ID"] for item in leads], ["17"])

    def test_control_preview_keeps_all_four_statuses_without_legacy_signals(self) -> None:
        statuses = [
            {"STATUS_ID": status_id, "NAME": values["name"], "SEMANTICS": values["semantics"]}
            for group in LEAD_CONTROL_GROUPS.values()
            for status_id, values in group.items()
        ]
        leads = [
            {
                "ID": str(index),
                "TITLE": f"Lead {index}",
                "STATUS_ID": status_id,
                "STATUS_SEMANTIC_ID": "F" if values["semantics"] == "F" else "P",
                "SOURCE_ID": "DMP1" if index == 1 else "WEB",
                "ASSIGNED_BY_ID": "42",
                "DATE_CREATE": "2026-10-01T10:00:00+03:00",
                "DATE_MODIFY": "2026-10-02T10:00:00+03:00",
            }
            for index, (status_id, values) in enumerate(
                ((status_id, values) for group in LEAD_CONTROL_GROUPS.values() for status_id, values in group.items()),
                start=1,
            )
        ]
        client = FakeLeadClient(statuses, leads)
        profile = default_analysis_profile()
        profile["lead"]["all_stages"] = False
        profile["lead"]["stage_ids"] = [status_id for group in LEAD_CONTROL_GROUPS.values() for status_id in group]
        profile["deal"]["enabled"] = False
        profile["review_view"] = "all"
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.detect_candidate_signals", side_effect=AssertionError("activity heuristics must not run")):
            preview = profile_candidates_preview(
                {"profile": profile},
                client=client,
                db_path=Path(directory) / "rop.sqlite3",
                period_override={
                    "timezone": "Europe/Moscow",
                    "period_from": "2026-09-06T00:00:00+03:00",
                    "period_to": "2026-10-06T00:00:00+03:00",
                    "as_of": "2026-10-06T10:00:00+03:00",
                },
                required_lead_statuses={
                    status_id: values
                    for group in LEAD_CONTROL_GROUPS.values()
                    for status_id, values in group.items()
                },
            )
        self.assertEqual({item["stage_id"] for item in preview["candidates"]}, set(profile["lead"]["stage_ids"]))
        self.assertEqual(len(preview["candidates"]), 4)
        self.assertTrue(all(item["score"] == 0 for item in preview["candidates"]))
        self.assertTrue(all(item["signals"] == [] for item in preview["candidates"]))
        self.assertEqual([method for method, _ in client.calls], ["crm.status.list", "crm.lead.list", "user.get"])

    def test_legacy_review_reconciliation_still_receives_signal_hash(self) -> None:
        lead = {
            "ID": "17",
            "TITLE": "Legacy lead",
            "STATUS_ID": "legacy-status",
            "STATUS_SEMANTIC_ID": "P",
            "ASSIGNED_BY_ID": "42",
            "DATE_CREATE": "2026-10-01T10:00:00+03:00",
            "DATE_MODIFY": "2026-10-02T10:00:00+03:00",
        }
        observed: list[dict[str, object]] = []

        def record_reconciled(_db_path: Path, candidates: list[dict[str, object]], *, as_of: str) -> None:
            observed.extend(candidates)

        profile = default_analysis_profile()
        profile["deal"]["enabled"] = False
        profile["review_view"] = "all"
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.fetch_profile_leads", return_value=([lead], {"raw": 1, "selected": 1})), \
             patch("api.candidates.fetch_profile_deals", return_value=([], {"fresh": 0, "portfolio": 0, "selected": 0})), \
             patch("api.candidates.fetch_lead_handoffs", return_value=([], [])), \
             patch("api.candidates.load_pipeline_stage_names", return_value={}), \
             patch("api.candidates.load_status_map", return_value={}), \
             patch("api.candidates.list_crm_pipelines", return_value={"deal_pipelines": []}), \
             patch("api.candidates.fetch_candidate_activities_bulk", return_value={("lead", "17"): ([], None)}), \
             patch("api.candidates.detect_candidate_signals", return_value=([{
                 "reason_code": "fixture",
                 "priority": "low",
                 "score": 1,
                 "label": "Fixture signal",
             }], {"method_gap": False})), \
             patch("api.candidates.attach_saved_lead_qualification", side_effect=lambda rows, **_: rows), \
             patch("api.candidates.reconcile_candidate_cases", side_effect=record_reconciled):
            profile_candidates_preview(
                {"profile": profile},
                client=object(),
                db_path=Path(directory) / "rop.sqlite3",
                period_override={
                    "timezone": "Europe/Moscow",
                    "period_from": "2026-09-06T00:00:00+03:00",
                    "period_to": "2026-10-06T00:00:00+03:00",
                    "as_of": "2026-10-06T10:00:00+03:00",
                },
            )
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].get("signal_hash"))


class LeadControlRouteTests(unittest.TestCase):
    def test_search_forces_window_and_uses_resolved_group_and_manager(self) -> None:
        body = app_api.CandidatesSearchRequest(
            entity_type="lead",
            lead_control_group="active",
            manager_id="42",
            created_days=7,
            modified_days=12,
            days=90,
            stage_ids=["untrusted-stage"],
            refresh_crm=True,
            save=True,
        )
        resolved = {
            group: {
                "stage_ids": list(statuses),
                "required_statuses": statuses,
            }
            for group, statuses in LEAD_CONTROL_GROUPS.items()
        }
        with patch.object(app_api, "_require_admin_or_rop"), \
             patch.object(app_api, "resolve_lead_control_group", side_effect=lambda group: resolved[group]) as resolve, \
             patch.object(app_api, "search_candidates", return_value={"candidates": []}) as search, \
             patch.object(app_api, "save_candidate_filter") as save:
            app_api.candidates_search(body)

        args = search.call_args.kwargs
        self.assertEqual(args["created_days"], 30)
        self.assertEqual(args["modified_days"], 0)
        self.assertIsNone(args["days"])
        self.assertEqual(args["stage_ids"], resolved["active"]["stage_ids"])
        self.assertEqual(args["lead_manager_id"], "42")
        self.assertEqual(args["required_lead_statuses"], _all_statuses())
        self.assertTrue(args["refresh_crm"])
        self.assertEqual(resolve.call_args_list[0].args, ("active",))
        self.assertEqual(resolve.call_args_list[1].args, ("closed",))
        saved = save.call_args.args[1]
        self.assertEqual(saved["created_days"], 30)
        self.assertEqual(saved["modified_days"], 0)
        self.assertEqual(saved["stage_ids"], resolved["active"]["stage_ids"])

    def test_mismatched_group_configuration_returns_422_without_saving(self) -> None:
        body = app_api.CandidatesSearchRequest(lead_control_group="closed", save=True)
        with patch.object(app_api, "_require_admin_or_rop"), \
             patch.object(app_api, "resolve_lead_control_group", side_effect=LeadControlConfigurationError("mapping mismatch")), \
             patch.object(app_api, "save_candidate_filter") as save:
            with self.assertRaises(HTTPException) as raised:
                app_api.candidates_search(body)
        self.assertEqual(raised.exception.status_code, 422)
        save.assert_not_called()

    def test_refresh_errors_do_not_expose_raw_exception_details(self) -> None:
        resolved = {
            group: {"stage_ids": list(statuses), "required_statuses": statuses}
            for group, statuses in LEAD_CONTROL_GROUPS.items()
        }
        body = app_api.CandidatesSearchRequest(lead_control_group="active", refresh_crm=True)
        with patch.object(app_api, "_require_admin_or_rop"), \
             patch.object(app_api, "resolve_lead_control_group", side_effect=lambda group: resolved[group]), \
             patch.object(app_api, "search_candidates", side_effect=RuntimeError("https://example.invalid/very-secret-token")):
            with self.assertRaises(HTTPException) as raised:
                app_api.candidates_search(body)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("Сохранённый список не изменён", raised.exception.detail)
        self.assertNotIn("example.invalid", raised.exception.detail)
        self.assertNotIn("very-secret-token", raised.exception.detail)

    def test_group_and_manager_are_restricted_to_lead_control(self) -> None:
        with patch.object(app_api, "_require_admin_or_rop"):
            with self.assertRaises(HTTPException) as wrong_entity:
                app_api.candidates_search(app_api.CandidatesSearchRequest(entity_type="deal", lead_control_group="active"))
            with self.assertRaises(HTTPException) as manager_without_group:
                app_api.candidates_search(app_api.CandidatesSearchRequest(manager_id="42"))
            with self.assertRaises(HTTPException) as refresh_without_group:
                app_api.candidates_search(app_api.CandidatesSearchRequest(refresh_crm=True))
        self.assertEqual(wrong_entity.exception.status_code, 422)
        self.assertEqual(manager_without_group.exception.status_code, 422)
        self.assertEqual(refresh_without_group.exception.status_code, 422)


class LeadControlHistoryTests(unittest.TestCase):
    def test_history_reads_comments_activities_and_local_call_transcript_then_uses_cache(self) -> None:
        activities = {
            "ok": True,
            "items": [
                {
                    "ID": "call-1", "TYPE_ID": "2", "SUBJECT": "Call", "DESCRIPTION": "CRM call note",
                    "DIRECTION": "2", "COMPLETED": "Y", "START_TIME": "2026-10-06T10:00:00+03:00",
                    "END_TIME": "2026-10-06T10:02:00+03:00",
                },
                {
                    "ID": "email-1", "TYPE_ID": "4", "SUBJECT": "Email", "DESCRIPTION": "Inbound email body",
                    "DIRECTION": "1", "COMPLETED": "Y", "START_TIME": "2026-10-06T09:00:00+03:00",
                },
                {
                    "ID": "task-later", "TYPE_ID": "6", "SUBJECT": "Later task", "COMPLETED": "N",
                    "START_TIME": "2026-10-04T09:00:00+03:00", "DEADLINE": "2026-10-20T09:00:00+03:00",
                },
                {
                    "ID": "task-sooner", "TYPE_ID": "6", "SUBJECT": "Sooner task", "COMPLETED": "N",
                    "START_TIME": "2026-10-05T09:00:00+03:00", "DEADLINE": "2026-10-10T09:00:00+03:00",
                },
                {
                    "ID": "task-completed", "TYPE_ID": "6", "SUBJECT": "Completed task", "COMPLETED": "Y",
                    "START_TIME": "2026-10-01T09:00:00+03:00", "DEADLINE": "2026-10-07T09:00:00+03:00",
                },
            ],
        }
        comments = {
            "ok": True,
            "items": [{
                "ID": "comment-1", "CREATED": "2026-10-06T08:00:00+03:00", "AUTHOR_ID": "user-7",
                "COMMENT": "Internal CRM note",
            }],
        }
        client = FakeLeadHistoryClient(activities=activities, comments=comments)
        transcript = "Synthetic local transcript content for a verified call. " * 3
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.make_client", return_value=client), \
             patch("api.jobs._call_transcript", return_value=transcript), \
             patch("api.jobs._transcript_confirms_contact", return_value=True):
            db_path = Path(directory) / "rop.sqlite3"
            result = load_lead_control_history("17", db_path=db_path)
            with patch("api.candidates.make_client", side_effect=AssertionError("cached history called Bitrix")):
                cached = load_lead_control_history("17", db_path=db_path)

        self.assertEqual([method for method, _ in client.calls], ["crm.activity.list", "crm.timeline.comment.list"])
        self.assertEqual(client.calls[0][1]["filter"], {"OWNER_TYPE_ID": 1, "OWNER_ID": "17"})
        self.assertEqual(client.calls[1][1]["filter"], {"ENTITY_TYPE": "lead", "ENTITY_ID": "17"})
        self.assertTrue(result["available"])
        self.assertEqual(result["comments"][0], {
            "id": "comment-1",
            "created_at": "2026-10-06T08:00:00+03:00",
            "text": "Internal CRM note",
            "author_id": "user-7",
        })
        by_id = {item["id"]: item for item in result["communications"]}
        self.assertEqual(by_id["crm_activity:call-1"]["text"], transcript)
        self.assertEqual(by_id["crm_activity:call-1"]["kind"], "contact")
        self.assertTrue(by_id["crm_activity:call-1"]["transcript_available"])
        self.assertEqual(by_id["crm_activity:email-1"]["text"], "Inbound email body")
        self.assertEqual(by_id["crm_activity:email-1"]["kind"], "contact")
        self.assertEqual(result["current_task"], {
            "subject": "Sooner task",
            "deadline": "2026-10-10T09:00:00+03:00",
            "status": "open",
        })
        self.assertEqual(cached, result)

    def test_refresh_keeps_last_successful_source_when_other_source_fails(self) -> None:
        old_communication = {
            "id": "crm_activity:old-call", "occurred_at": "2026-10-01T10:00:00+03:00",
            "channel": "call", "kind": "attempt", "text": "Saved CRM history", "transcript_available": False,
        }
        old_task = {"subject": "Saved open task", "deadline": "2026-10-12T09:00:00+03:00", "status": "open"}
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            key = f"{LEAD_CONTROL_HISTORY_KEY_PREFIX}17"
            put_crm_sync_state(
                db_path,
                key,
                {
                    "schema_version": 1,
                    "lead_id": "17",
                    "available": True,
                    "comments": [{"id": "old-comment", "text": "Saved note"}],
                    "communications": [old_communication],
                    "current_task": old_task,
                    "task_checked": True,
                    "limitations": [],
                    "fetched_at": "2026-10-01T10:00:00+03:00",
                },
                expected_revision=0,
            )
            client = FakeLeadHistoryClient(
                activities=RuntimeError("synthetic private transport detail"),
                comments={"ok": True, "items": [{"ID": "new-comment", "COMMENT": "New note"}]},
            )
            with patch("api.candidates.make_client", return_value=client):
                result = load_lead_control_history("17", refresh_crm=True, db_path=db_path)
            saved = get_crm_sync_state(db_path, key)["payload"]

        self.assertEqual([method for method, _ in client.calls], ["crm.activity.list", "crm.timeline.comment.list"])
        self.assertEqual(result["communications"], [old_communication])
        self.assertEqual(result["current_task"], old_task)
        self.assertEqual([item["id"] for item in result["comments"]], ["new-comment"])
        self.assertTrue(result["task_checked"] if "task_checked" in result else saved["task_checked"])
        self.assertTrue(any("активности" in item for item in result["limitations"]))
        self.assertNotIn("private transport detail", repr(result))
        self.assertEqual(saved["communications"], [old_communication])
        self.assertEqual(saved["current_task"], old_task)

    def test_history_route_enforces_numeric_id_and_rop_team_scope(self) -> None:
        expected = {
            "lead_id": "17", "available": True, "comments": [], "communications": [],
            "current_task": None, "limitations": [], "fetched_at": "2026-10-06T10:00:00+03:00",
        }
        with patch.object(app_api, "auth_current_user", return_value={"role": "rop"}), \
             patch.object(app_api, "lead_control_snapshot_candidate", return_value={"entity_type": "lead", "manager_id": "42"}), \
             patch.object(app_api, "rop_team_manager_ids", return_value={"42"}), \
             patch.object(app_api, "load_lead_control_history", return_value=expected) as load:
            result = app_api.lead_control_history_get("0017", refresh_crm=True)
        self.assertEqual(result, expected)
        load.assert_called_once_with("17", refresh_crm=True, db_path=app_api.DEFAULT_DB_PATH)

        with patch.object(app_api, "auth_current_user", return_value={"role": "rop"}), \
             patch.object(app_api, "lead_control_snapshot_candidate", return_value={"entity_type": "lead", "manager_id": "99"}), \
             patch.object(app_api, "rop_team_manager_ids", return_value={"42"}), \
             patch.object(app_api, "load_lead_control_history") as load:
            with self.assertRaises(HTTPException) as denied:
                app_api.lead_control_history_get("17")
        self.assertEqual(denied.exception.status_code, 403)
        load.assert_not_called()

        with patch.object(app_api, "auth_current_user", return_value={"role": "admin"}), \
             patch.object(app_api, "lead_control_snapshot_candidate") as candidate:
            with self.assertRaises(HTTPException) as invalid:
                app_api.lead_control_history_get("17/secret")
        self.assertEqual(invalid.exception.status_code, 422)
        candidate.assert_not_called()

        with patch.object(app_api, "auth_current_user", return_value={"role": "manager"}):
            with self.assertRaises(HTTPException) as role_denied:
                app_api.lead_control_history_get("17")
        self.assertEqual(role_denied.exception.status_code, 403)

    def test_history_route_does_not_expose_transport_error(self) -> None:
        with patch.object(app_api, "auth_current_user", return_value={"role": "admin"}), \
             patch.object(app_api, "lead_control_snapshot_candidate", return_value={"entity_type": "lead"}), \
             patch.object(app_api, "load_lead_control_history", side_effect=RuntimeError("https://example.invalid/private-token")):
            with self.assertRaises(HTTPException) as raised:
                app_api.lead_control_history_get("17")
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("example.invalid", raised.exception.detail)
        self.assertNotIn("private-token", raised.exception.detail)


class LeadControlSnapshotTests(unittest.TestCase):
    def test_cache_read_is_local_and_filters_group_manager_window_before_limit(self) -> None:
        active_id = "UC_W1ZZ1V"
        rows = [
            _candidate(f"other-{index}", active_id, "7", created_days_ago=1)
            for index in range(101)
        ]
        rows.extend([
            _candidate("target-old", active_id, "42", created_days_ago=20),
            _candidate("target-new", active_id, "42", created_days_ago=2),
            _candidate("too-old", active_id, "42", created_days_ago=31),
            _candidate("closed", "UC_X6NW6I", "42", created_days_ago=1),
        ])
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            _save_snapshot(db_path, rows)
            put_crm_sync_state(
                db_path,
                f"{LEAD_CONTROL_HISTORY_KEY_PREFIX}target-new",
                {
                    "schema_version": 1,
                    "lead_id": "target-new",
                    "available": True,
                    "comments": [],
                    "communications": [],
                    "current_task": {"subject": "Cached task", "deadline": "2026-10-12", "status": "open"},
                    "task_checked": True,
                    "limitations": [],
                    "fetched_at": "2026-10-06T10:00:00+03:00",
                },
                expected_revision=0,
            )
            with patch("api.candidates.make_client", side_effect=AssertionError("cache read called Bitrix")), \
                 patch("api.candidates.candidate_freshness", return_value="missing"), \
                 patch("api.jobs.list_jobs", return_value=[]):
                result = search_candidates(
                    entity_type="lead",
                    lead_control_group="active",
                    lead_manager_id="42",
                    required_lead_statuses=_all_statuses(),
                    limit=1,
                    db_path=db_path,
                )
        self.assertTrue(result["ready"])
        self.assertTrue(result["crm_snapshot_available"])
        self.assertEqual(result["crm_updated_at"], "2026-10-06T10:00:00+03:00")
        self.assertEqual(result["summary"]["total_scored"], 2)
        self.assertEqual([item["entity_id"] for item in result["candidates"]], ["target-new"])
        self.assertEqual(result["candidates"][0]["crm_task"], {"subject": "Cached task", "deadline": "2026-10-12", "status": "open"})
        self.assertTrue(result["candidates"][0]["crm_task_checked"])

    def test_empty_cache_is_ready_and_prompts_for_explicit_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.make_client", side_effect=AssertionError("default read called Bitrix")):
            result = search_candidates(
                entity_type="lead",
                lead_control_group="active",
                required_lead_statuses=_all_statuses(),
                db_path=Path(directory) / "rop.sqlite3",
            )
        self.assertTrue(result["ready"])
        self.assertFalse(result["crm_snapshot_available"])
        self.assertIsNone(result["crm_updated_at"])
        self.assertEqual(result["candidates"], [])
        self.assertIn("Обновить Bitrix", result["ready_message"])

    def test_explicit_refresh_persists_all_four_statuses(self) -> None:
        statuses = [
            {"STATUS_ID": status_id, "NAME": values["name"], "SEMANTICS": values["semantics"]}
            for status_id, values in _all_statuses().items()
        ]
        leads = [
            {
                "ID": str(index),
                "TITLE": f"Lead {index}",
                "STATUS_ID": status_id,
                "STATUS_SEMANTIC_ID": "F" if values["semantics"] == "F" else "P",
                "ASSIGNED_BY_ID": "2767" if index % 2 else "2775",
                "SOURCE_ID": "DMP1" if index == 1 else "WEB",
                "DATE_CREATE": (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=1)).isoformat(),
                "DATE_MODIFY": (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=1)).isoformat(),
            }
            for index, (status_id, values) in enumerate(_all_statuses().items(), start=1)
        ]
        users = [
            {"ID": "2767", "LAST_NAME": "Сидоров", "NAME": "Иван"},
            {"ID": "2775", "LAST_NAME": "Петрова", "NAME": "Мария"},
        ]
        client = FakeLeadClient(statuses, leads, users=users)
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.make_client", return_value=client), \
             patch("api.jobs.list_jobs", return_value=[]):
            db_path = Path(directory) / "rop.sqlite3"
            result = search_candidates(
                entity_type="lead",
                lead_control_group="active",
                required_lead_statuses=_all_statuses(),
                refresh_crm=True,
                db_path=db_path,
            )
            stored = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
            with patch("api.candidates.make_client", side_effect=AssertionError("cache read called Bitrix")):
                cached = search_candidates(
                    entity_type="lead",
                    lead_control_group="active",
                    required_lead_statuses=_all_statuses(),
                    db_path=db_path,
                )
        self.assertEqual([method for method, _ in client.calls], ["crm.status.list", "crm.lead.list", "user.get"])
        user_call = next(payload for method, payload in client.calls if method == "user.get")
        self.assertEqual(user_call["filter"]["ID"], ["2767", "2775"])
        self.assertTrue(result["crm_snapshot_available"])
        self.assertEqual(len(stored["payload"]["candidates"]), 4)
        self.assertEqual(stored["payload"]["manager_names"], {"2767": "Сидоров Иван", "2775": "Петрова Мария"})
        self.assertEqual(
            {item["stage_id"] for item in stored["payload"]["candidates"]},
            set(_all_statuses()),
        )
        self.assertEqual(
            {item["entity_id"] for item in result["candidates"]},
            {"1", "2"},
            msg=repr([(item.get("entity_id"), item.get("stage_id"), item.get("manager_id")) for item in result["candidates"]]),
        )
        self.assertEqual(result["manager_names"], stored["payload"]["manager_names"])
        self.assertEqual(
            {item["manager_name"] for item in cached["candidates"]},
            {"Сидоров Иван", "Петрова Мария"},
        )
        self.assertEqual(cached["manager_names"], stored["payload"]["manager_names"])

    def test_refresh_preserves_saved_names_when_user_lookup_fails(self) -> None:
        statuses = [
            {"STATUS_ID": status_id, "NAME": values["name"], "SEMANTICS": values["semantics"]}
            for status_id, values in _all_statuses().items()
        ]
        lead = {
            "ID": "17",
            "TITLE": "Lead 17",
            "STATUS_ID": "UC_W1ZZ1V",
            "STATUS_SEMANTIC_ID": "P",
            "ASSIGNED_BY_ID": "2767",
            "DATE_CREATE": (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=1)).isoformat(),
            "DATE_MODIFY": (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=1)).isoformat(),
        }
        rows = [_candidate("17", "UC_W1ZZ1V", "2767")]
        rows[0]["manager_name"] = "Сидоров Иван"
        client = FakeLeadClient(statuses, [lead], user_error=RuntimeError("user lookup unavailable"))
        with tempfile.TemporaryDirectory() as directory, \
             patch("api.candidates.make_client", return_value=client), \
             patch("api.jobs.list_jobs", return_value=[]):
            db_path = Path(directory) / "rop.sqlite3"
            put_crm_sync_state(
                db_path,
                LEAD_CONTROL_SNAPSHOT_KEY,
                {
                    "schema_version": 1,
                    "crm_updated_at": "2026-10-06T10:00:00+03:00",
                    "candidates": rows,
                    "manager_names": {"2767": "Сидоров Иван"},
                },
                expected_revision=0,
            )
            result = search_candidates(
                entity_type="lead",
                lead_control_group="active",
                required_lead_statuses=_all_statuses(),
                refresh_crm=True,
                db_path=db_path,
            )
            stored = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
        self.assertEqual(result["manager_names"], {"2767": "Сидоров Иван"})
        self.assertEqual(result["candidates"][0]["manager_name"], "Сидоров Иван")
        self.assertEqual(stored["payload"]["manager_names"], {"2767": "Сидоров Иван"})

    def test_failed_live_mapping_check_retains_previous_snapshot(self) -> None:
        rows = [_candidate("saved", "UC_W1ZZ1V", "42")]
        mismatch = [
            {"STATUS_ID": status_id, "NAME": values["name"], "SEMANTICS": values["semantics"]}
            for status_id, values in _all_statuses().items()
        ]
        mismatch[-1] = {**mismatch[-1], "NAME": "Unexpected name"}
        client = FakeLeadClient(mismatch, [])
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            _save_snapshot(db_path, rows)
            before = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
            with patch("api.candidates.make_client", return_value=client):
                with self.assertRaises(LeadControlConfigurationError):
                    search_candidates(
                        entity_type="lead",
                        lead_control_group="active",
                        required_lead_statuses=_all_statuses(),
                        refresh_crm=True,
                        db_path=db_path,
                    )
            after = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["payload"], before["payload"])

    def test_cache_read_reattaches_latest_saved_report(self) -> None:
        stale_row = _candidate("17", "UC_W1ZZ1V", "42")
        stale_row.update({"latest_report_id": -1, "lead_category": "stale"})
        analysis = {
            "qualification_assessment": {"lead_category": {"value": "B"}},
            "closure_review": {"verdict": "disputed", "reason": "Saved closure evidence"},
        }
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            _save_snapshot(db_path, [stale_row])
            report_id = save_ui_report(db_path, entity_type="lead", entity_id="17", report_json=analysis)
            with patch("api.candidates.candidate_freshness", return_value="fresh"), \
                 patch("api.jobs.list_jobs", return_value=[]):
                result = search_candidates(
                    entity_type="lead",
                    lead_control_group="active",
                    required_lead_statuses=_all_statuses(),
                    db_path=db_path,
                )
        row = result["candidates"][0]
        self.assertEqual(row["latest_report_id"], report_id)
        self.assertEqual(row["lead_category"], "B")
        self.assertEqual(row["lead_closure_verdict"], "disputed")
        self.assertFalse(row["lead_closure_confirmed"])
        self.assertNotEqual(row["latest_report_id"], -1)

    def test_stale_refresh_writer_keeps_newer_snapshot_and_reports_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            _save_snapshot(db_path, [_candidate("old", "UC_W1ZZ1V", "42")])
            real_put = put_crm_sync_state

            def concurrent_writer(path: Path, key: str, payload: dict[str, object], *, expected_revision: int) -> int:
                real_put(path, key, {
                    "schema_version": 1,
                    "crm_updated_at": "newer-writer",
                    "candidates": [_candidate("newer", "UC_W1ZZ1V", "42")],
                }, expected_revision=expected_revision)
                return real_put(path, key, payload, expected_revision=expected_revision)

            with patch("api.candidates.profile_candidates_preview", return_value={"candidates": [_candidate("stale", "UC_W1ZZ1V", "42")]}), \
                 patch("api.candidates.put_crm_sync_state", side_effect=concurrent_writer):
                with self.assertRaisesRegex(RuntimeError, "newer snapshot"):
                    search_candidates(
                        entity_type="lead",
                        lead_control_group="active",
                        required_lead_statuses=_all_statuses(),
                        refresh_crm=True,
                        db_path=db_path,
                    )
            stored = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(stored["payload"]["crm_updated_at"], "newer-writer")
        self.assertEqual(stored["payload"]["candidates"][0]["entity_id"], "newer")

    def test_candidates_search_requires_admin_or_rop_session_role(self) -> None:
        with patch.object(app_api, "auth_current_user", return_value={"role": "manager"}):
            with self.assertRaises(HTTPException) as raised:
                app_api.candidates_search(app_api.CandidatesSearchRequest(save=False))
        self.assertEqual(raised.exception.status_code, 403)

    def test_analysis_caps_normalized_unique_lead_ids_at_ten(self) -> None:
        body = app_api.AnalyzeRequest(entity_type="lead", ids=", ".join(str(i) for i in range(1, 12)))
        with patch.object(app_api, "_require_analyze_scope", return_value={"role": "admin"}), \
             patch.object(app_api, "start_analyze_job") as start:
            with self.assertRaises(HTTPException) as raised:
                app_api.analyze(body)
        self.assertEqual(raised.exception.status_code, 422)
        start.assert_not_called()

        deduplicated = app_api.AnalyzeRequest(entity_type="lead", ids=", ".join([*(str(i) for i in range(1, 11)), "1"]))
        with patch.object(app_api, "_require_analyze_scope", return_value={"role": "admin"}), \
             patch.object(app_api, "start_analyze_job", return_value={"job_id": "test"}) as start:
            app_api.analyze(deduplicated)
        self.assertEqual(len(start.call_args.args[0].ids), 10)

    def test_rop_can_start_confirmed_explicit_lead_analysis_but_manager_and_force_llm_cannot(self) -> None:
        body = app_api.AnalyzeRequest(entity_type="lead", ids="17", analyze=True, confirm_paid=True)
        with patch.object(app_api, "auth_current_user", return_value={"role": "rop"}), \
             patch.object(app_api, "start_analyze_job", return_value={"job_id": "test"}) as start:
            app_api.analyze(body)
        self.assertEqual(start.call_args.args[0].entity_type, "lead")
        self.assertTrue(start.call_args.args[0].analyze)

        with patch.object(app_api, "auth_current_user", return_value={"role": "manager"}), \
             patch.object(app_api, "start_analyze_job") as start:
            with self.assertRaises(HTTPException) as manager_denied:
                app_api.analyze(body)
        self.assertEqual(manager_denied.exception.status_code, 403)
        start.assert_not_called()

        force_body = app_api.AnalyzeRequest(entity_type="lead", ids="17", force_llm=True, confirm_paid=True)
        with patch.object(app_api, "auth_current_user", return_value={"role": "rop"}), \
             patch.object(app_api, "start_analyze_job") as start:
            with self.assertRaises(HTTPException) as force_denied:
                app_api.analyze(force_body)
        self.assertEqual(force_denied.exception.status_code, 403)
        start.assert_not_called()

    def test_auto_mode_caps_leads_but_preserves_deal_batches(self) -> None:
        body = app_api.AnalyzeRequest(entity_type="auto", ids=", ".join(str(i) for i in range(1, 12)))
        with patch.object(app_api, "_require_analyze_scope", return_value={"role": "admin"}), \
             patch.object(app_api, "resolve_auto_lead_ids", return_value=[str(i) for i in range(1, 12)]), \
             patch.object(app_api, "start_analyze_job") as start:
            with self.assertRaises(HTTPException) as raised:
                app_api.analyze(body)
        self.assertEqual(raised.exception.status_code, 422)
        start.assert_not_called()

        with patch.object(app_api, "_require_analyze_scope", return_value={"role": "admin"}), \
             patch.object(app_api, "resolve_auto_lead_ids", return_value=[]), \
             patch.object(app_api, "start_analyze_job", return_value={"job_id": "test"}) as start:
            app_api.analyze(body)
        self.assertEqual(len(start.call_args.args[0].ids), 11)


class SavedLeadAnalysisTests(unittest.TestCase):
    def test_candidate_enrichment_uses_latest_saved_analysis_and_status(self) -> None:
        analysis = {
            "lead_state": {"summary": "Issue from saved analysis"},
            "main_risk": {"risk_level": "high", "description": "Issue from saved analysis"},
            "rop_manager_message_block": {"check_for_rop": "Next action from saved analysis"},
            "qualification_assessment": {
                "lead_category": {"value": "B"},
                "lead_route": {"status": "valid"},
                "bant": {"budget": {"status": "confirmed"}},
            },
            "closure_review": {"verdict": "disputed", "reason": "Closure reason from saved analysis"},
        }
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "rop.sqlite3"
            report_id = save_ui_report(
                db_path,
                entity_type="lead",
                entity_id="17",
                report_json=analysis,
            )
            candidate = {"entity_type": "lead", "entity_id": "17", "analysis_freshness": "fresh"}
            with patch("api.jobs.list_jobs", return_value=[]):
                rows = attach_saved_lead_qualification([candidate], db_path=db_path)

        row = rows[0]
        self.assertEqual(row["latest_report_id"], report_id)
        self.assertTrue(row["analyzed_at"])
        self.assertEqual(row["analysis_status"], "ready")
        self.assertEqual(row["lead_category"], "B")
        self.assertEqual(row["lead_issue"], "Issue from saved analysis")
        self.assertEqual(row["lead_next_action"], "Next action from saved analysis")
        self.assertEqual(row["lead_closure_verdict"], "disputed")
        self.assertEqual(row["lead_closure_reason"], "Closure reason from saved analysis")

    def test_candidate_status_supports_running_error_and_not_analyzed(self) -> None:
        rows = [
            {"entity_type": "lead", "entity_id": "1", "analysis_freshness": "missing"},
            {"entity_type": "lead", "entity_id": "2", "analysis_freshness": "failed"},
            {"entity_type": "lead", "entity_id": "3", "analysis_freshness": "missing"},
        ]
        job = {
            "status": "running",
            "options": {"entity_type": "lead", "ids": ["3"]},
            "entity_progress": {},
        }
        with tempfile.TemporaryDirectory() as directory, patch("api.jobs.list_jobs", return_value=[job]):
            enriched = attach_saved_lead_qualification(rows, db_path=Path(directory) / "rop.sqlite3")
        self.assertEqual([item["analysis_status"] for item in enriched], ["not_analyzed", "error", "running"])


if __name__ == "__main__":
    unittest.main()
