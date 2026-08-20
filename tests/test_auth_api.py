from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request, Response

import api.access as access
import api.auth as auth


def _user(role: str, *, manager_id: str | None = None, user_id: int = 1) -> dict[str, object]:
    return {
        "id": user_id,
        "login": f"{role}-{user_id}",
        "role": role,
        "manager_id": manager_id,
        "is_active": True,
    }


def _deal(deal_id: str, manager_id: str | None) -> dict[str, object]:
    return {
        "deal_id": deal_id,
        "source": "initial",
        "title": f"Deal {deal_id}",
        "manager_id": manager_id,
        "manager_name": f"Manager {manager_id}" if manager_id else "Unassigned",
        "stage_id": "NEW",
        "stage_name": "New",
        "pipeline_id": "15",
        "amount": "1000",
        "currency_id": "RUB",
        "created_at_crm": "2026-08-01T00:00:00+00:00",
        "modified_at_crm": "2026-08-02T00:00:00+00:00",
        "is_active": True,
        "probability": 50,
        "tasks": [{"id": 7, "task_text": "private task"}],
        "bitrix_tasks": [{"activity_id": "a7", "subject": "private CRM task"}],
        "communications_today": {"available": True},
        "primary_bitrix_task": {"activity_id": "a7"},
        "current_task": {"id": 7},
        "coaching": {"report_id": 9},
        "checklist": {"items": [{"id": "c1"}]},
        "manager_situation": {"state": "confirmed"},
        "private_marker": "must not cross the projection",
    }


def _request(*, cookie: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": ("198.51.100.9", 43100),
            "server": ("example.test", 443),
            "root_path": "",
        }
    )


class DealAuthorizationTests(unittest.TestCase):
    def test_manager_can_open_own_row_but_foreign_row_stays_bounded(self) -> None:
        manager = _user("manager", manager_id="10", user_id=10)
        own = _deal("101", "10")
        foreign = _deal("202", "77")

        own_access = access.deal_access(manager, own)
        foreign_access = access.deal_access(manager, foreign)

        self.assertTrue(own_access.is_own)
        self.assertTrue(own_access.can_open)
        self.assertTrue(own_access.can_edit)
        self.assertTrue(own_access.can_run_paid_ai)
        self.assertFalse(foreign_access.is_own)
        self.assertFalse(foreign_access.can_open)
        self.assertTrue(foreign_access.read_only)
        self.assertFalse(foreign_access.can_run_paid_ai)

        projected = access.project_deal_row(foreign, manager)
        expected = set(access._LIGHTWEIGHT_DEAL_FIELDS) | {
            "ownership",
            "is_own",
            "read_only",
            "can_open",
            "can_edit",
            "can_run_paid_ai",
        }
        self.assertEqual(set(projected), expected)
        for key in ("tasks", "bitrix_tasks", "coaching", "checklist", "communications_today", "private_marker"):
            self.assertNotIn(key, projected)
        self.assertIn("private_marker", access.project_deal_row(own, manager))

    def test_manager_dashboard_contains_only_own_deals(self) -> None:
        manager = _user("manager", manager_id="10", user_id=10)
        dashboard = {
            "deals": [_deal("101", "10"), _deal("202", "77")],
            "summary": {"active_deals": 2, "portfolio_amount": 2000},
            "scope": {"initial_deal_ids": ["101", "202"], "manager_ids": ["10", "77"]},
        }

        with patch.object(
            access.storage,
            "get_deal_control_metrics",
            return_value={},
            create=True,
        ):
            projected = access.scoped_dashboard(dashboard, manager)

        self.assertEqual([row["deal_id"] for row in projected["deals"]], ["101"])
        self.assertEqual(projected["summary"]["active_deals"], 1)
        self.assertEqual(projected["scope"]["initial_deal_ids"], ["101"])
        self.assertEqual(projected["scope"]["manager_ids"], ["10"])

    def test_rop_is_limited_to_configured_team_and_has_no_paid_ai(self) -> None:
        rop = _user("rop", user_id=2)
        team_deal = _deal("101", "10")
        foreign_deal = _deal("202", "77")
        with patch.object(
            access.storage,
            "get_deal_control_scope",
            return_value={"manager_ids": ["10"]},
            create=True,
        ):
            team = access.deal_access(rop, team_deal)
            foreign = access.deal_access(rop, foreign_deal)

        self.assertTrue(team.can_open)
        self.assertTrue(team.can_edit)
        self.assertFalse(team.can_run_paid_ai)
        self.assertFalse(foreign.can_open)
        self.assertTrue(foreign.read_only)

    def test_job_visibility_follows_deal_scope(self) -> None:
        admin = _user("admin")
        rop = _user("rop", user_id=2)
        manager = _user("manager", manager_id="10", user_id=10)
        deals = {"101": _deal("101", "10"), "202": _deal("202", "77")}
        team_job = {"options": {"entity_type": "deal", "ids": ["101"]}}
        foreign_job = {"options": {"entity_type": "deal", "ids": ["202"]}}
        lead_job = {"options": {"entity_type": "lead", "ids": ["501"]}}
        auto_job = {"options": {"entity_type": "auto", "ids": ["101"]}}

        with patch.object(
            access.storage,
            "get_deal_control_scope",
            return_value={"manager_ids": ["10"]},
            create=True,
        ), patch.object(access, "get_deal", side_effect=lambda deal_id: deals.get(str(deal_id))):
            self.assertTrue(access.can_view_job(team_job, admin))
            self.assertTrue(access.can_view_job(foreign_job, admin))
            self.assertTrue(access.can_view_job(team_job, rop))
            self.assertFalse(access.can_view_job(foreign_job, rop))
            self.assertTrue(access.can_view_job(team_job, manager))
            self.assertFalse(access.can_view_job(foreign_job, manager))
            self.assertFalse(access.can_view_job(lead_job, rop))
            self.assertFalse(access.can_view_job(auto_job, manager))

    def test_foreign_deal_is_forbidden_for_open_and_metrics_scope_is_checked(self) -> None:
        manager = _user("manager", manager_id="10", user_id=10)
        with patch.object(access, "get_deal", return_value=_deal("202", "77")):
            with self.assertRaises(HTTPException) as error:
                access.require_deal("202", user=manager, action="open")
        self.assertEqual(error.exception.status_code, 403)

        with patch.object(
            access.storage,
            "get_deal_control_scope",
            return_value={"manager_ids": ["10"]},
            create=True,
        ), patch.object(
            access.storage,
            "get_deal_control_metrics",
            return_value={"overall": {"tasks": 1}},
            create=True,
        ):
            with self.assertRaises(HTTPException) as error:
                access.scoped_deal_metrics(_user("rop", user_id=2), manager_id="77")
        self.assertEqual(error.exception.status_code, 403)


class AuthContractTests(unittest.TestCase):
    def test_public_user_is_a_whitelist_and_source_role_is_not_request_identity(self) -> None:
        row = {
            **_user("manager", manager_id="10", user_id=10),
            "password_hash": "argon2id-internal-value",
            "created_at": "internal",
        }
        public = auth.public_user(row)
        self.assertEqual(public, _user("manager", manager_id="10", user_id=10))
        self.assertNotIn("password_hash", public)

        from api.app import (
            DealControlBitrixTaskCompletionRequest,
            DealControlTaskOutcomeRequest,
            DealControlTaskUpdateRequest,
        )

        update = DealControlTaskUpdateRequest.model_validate({"task_text": "x", "source_role": "admin"})
        completion = DealControlBitrixTaskCompletionRequest.model_validate(
            {"deal_id": "101", "completed": True, "source_role": "admin"}
        )
        outcome = DealControlTaskOutcomeRequest.model_validate(
            {
                "contact_status": "confirmed_contact",
                "result_status": "pending",
                "source_role": "admin",
            }
        )
        self.assertNotIn("source_role", update.model_fields_set)
        self.assertNotIn("source_role", completion.model_fields_set)
        self.assertNotIn("source_role", outcome.model_fields_set)
        self.assertEqual(access.actor_source_role(_user("manager")), "manager")
        self.assertEqual(access.actor_source_role(_user("rop")), "rop")

    def test_login_sets_opaque_secure_cookie_and_returns_no_hash(self) -> None:
        session_calls: list[dict[str, object]] = []

        class FakeStorage:
            DEFAULT_DB_PATH = Path("auth-contract.sqlite")

            @staticmethod
            def get_auth_login_throttle(*args, **kwargs):
                return None

            @staticmethod
            def get_auth_user(*args, **kwargs):
                return {
                    **_user("manager", manager_id="10", user_id=10),
                    "password_hash": "argon2id-internal-value",
                }

            @staticmethod
            def verify_auth_password(password, password_hash):
                return password == "correct" and password_hash == "argon2id-internal-value"

            @staticmethod
            def record_auth_login_attempt(*args, **kwargs):
                return None

            @staticmethod
            def clear_auth_login_attempts(*args, **kwargs):
                return None

            @staticmethod
            def digest_auth_token(token):
                return "d" * 64

            @staticmethod
            def create_auth_session(*args, **kwargs):
                session_calls.append(kwargs)
                return {"id": 1}

        response = Response()
        with patch.object(auth, "storage", FakeStorage):
            result = auth.login_user(
                login="  Manager-10 ",
                password="correct",
                request=_request(),
                response=response,
            )

        self.assertEqual(result["user"], _user("manager", manager_id="10", user_id=10))
        self.assertNotIn("password_hash", result["user"])
        self.assertEqual(len(session_calls), 1)
        self.assertEqual(session_calls[0]["token_digest"], "d" * 64)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotRegex(cookie, re.compile(r"argon2id-internal-value", re.IGNORECASE))

    def test_auth_user_list_and_update_do_not_serialize_mutator_hash(self) -> None:
        from api import app as api_app

        row = {
            **_user("manager", manager_id="10", user_id=10),
            "password_hash": "argon2id-internal-value",
        }
        calls: list[dict[str, object]] = []

        def update_user(*args, **kwargs):
            calls.append(kwargs)
            return row

        with patch.object(api_app.storage, "list_auth_users", return_value=[row], create=True), patch.object(
            api_app.storage,
            "update_auth_user",
            side_effect=update_user,
            create=True,
        ):
            listed = api_app.auth_users()
            updated = api_app.auth_user_update(
                10,
                api_app.AuthUserUpdateRequest.model_validate({"manager_id": None}),
            )

        self.assertNotIn("password_hash", listed["items"][0])
        self.assertNotIn("password_hash", updated["user"])
        self.assertEqual(calls[0]["user_id"], 10)
        self.assertIsNone(calls[0]["manager_id"])


class AuthMiddlewareTests(unittest.TestCase):
    def test_public_health_login_logout_and_review_boundary(self) -> None:
        from fastapi.testclient import TestClient

        from api import app as api_app

        with patch.object(api_app, "authenticate_request", return_value=None):
            with TestClient(api_app.app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/auth/me").status_code, 401)
                self.assertEqual(
                    client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code,
                    403,
                )
                self.assertEqual(
                    client.post("/api/auth/logout", headers={"Origin": "https://testserver"}).status_code,
                    204,
                )
                self.assertIn(api_app._is_public_path("/api/review/share-token"), {True})


class AutomaticAnalysisAccessTests(unittest.TestCase):
    def test_manager_aggregate_includes_only_own_deals_without_crm_ids(self) -> None:
        manager = _user("manager", manager_id="10", user_id=10)
        items = [
            {"entity_id": "101", "decision_status": "full", "publication_status": "published"},
            {"entity_id": "202", "decision_status": "mini", "publication_status": "not_applicable"},
        ]
        with patch.object(access, "get_deal", side_effect=lambda deal_id, **_kwargs: _deal(str(deal_id), "10" if str(deal_id) == "101" else "77")):
            visible = access.scoped_automatic_analysis_items(items, manager)
        payload = access.automatic_analysis_latest_payload(
            {
                "business_date": "2026-08-18",
                "status": "running",
                "current_stage": "llm_analysis",
                "started_at": "2026-08-18T12:00:00+03:00",
                "updated_at": "2026-08-18T12:05:00+03:00",
                "finished_at": None,
            },
            visible,
        )
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["full"], 1)
        self.assertEqual(payload["mini"], 0)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["reports_published"], 1)
        dumped = json.dumps(payload)
        self.assertNotIn("entity_id", dumped)
        self.assertNotIn("101", dumped)
        self.assertNotIn("Deal 101", dumped)

    def test_latest_endpoint_requires_authentication(self) -> None:
        from fastapi.testclient import TestClient

        from api import app as api_app

        with patch.object(api_app, "authenticate_request", return_value=None):
            with TestClient(api_app.app) as client:
                self.assertEqual(client.get("/api/automatic-analysis/latest").status_code, 401)


if __name__ == "__main__":
    unittest.main()
