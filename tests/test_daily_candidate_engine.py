from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from api.candidates import (
    deduplicate_journeys,
    candidate_freshness,
    detect_candidate_signals,
    evaluate_call_method,
    fetch_profile_deals,
    fetch_profile_leads,
    profile_candidates_preview,
    profile_period_bounds,
)
from storage.rop_db import default_analysis_profile
from storage.rop_db import upsert_entity_state


MSK = ZoneInfo("Europe/Moscow")


class FakeBitrixClient:
    def __init__(self, *, leads=None, deals=None, sources=None, statuses=None, activities=None):
        self.leads = leads or []
        self.deals = deals or []
        self.sources = sources or []
        self.statuses = statuses or []
        self.activities = activities or {}
        self.calls = []

    def list_all(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.status.list":
            entity_id = str((payload.get("filter") or {}).get("ENTITY_ID") or "")
            return self.sources if entity_id == "SOURCE" else self.statuses
        if method == "crm.lead.list":
            return list(self.leads)
        if method == "crm.deal.list":
            filter_payload = payload.get("filter") or {}
            rows = list(self.deals)
            if filter_payload.get("LEAD_ID"):
                lead_ids = {str(item) for item in filter_payload["LEAD_ID"]}
                return [row for row in rows if str(row.get("LEAD_ID") or "") in lead_ids]
            if filter_payload.get("CATEGORY_ID"):
                pipeline_ids = {str(item) for item in filter_payload["CATEGORY_ID"]}
                rows = [row for row in rows if str(row.get("CATEGORY_ID") or "") in pipeline_ids]
            if filter_payload.get("STAGE_ID"):
                stage_ids = {str(item) for item in filter_payload["STAGE_ID"]}
                rows = [row for row in rows if str(row.get("STAGE_ID") or "") in stage_ids]
            if filter_payload.get("CLOSED") == "N":
                rows = [row for row in rows if str(row.get("CLOSED") or "N") == "N"]
            return rows
        raise AssertionError(method)

    def safe_list_all(self, method, payload=None):
        if method != "crm.activity.list":
            raise AssertionError(method)
        owner_id = ((payload or {}).get("filter") or {}).get("OWNER_ID")
        owner_ids = [str(item) for item in owner_id] if isinstance(owner_id, list) else [str(owner_id or "")]
        items = []
        for entity_id in owner_ids:
            items.extend([{**item, "OWNER_ID": str(item.get("OWNER_ID") or entity_id)} for item in self.activities.get(entity_id, [])])
        return {"ok": True, "items": items}


class DailyCandidateEngineTests(unittest.TestCase):
    def test_period_presets_are_moscow_calendar_windows(self):
        now = datetime(2026, 7, 16, 8, 30, tzinfo=MSK)
        period = profile_period_bounds("today_and_yesterday", now=now)
        self.assertEqual(period["period_from"], "2026-07-15T00:00:00+03:00")
        self.assertEqual(period["period_to"], "2026-07-17T00:00:00+03:00")
        yesterday = profile_period_bounds("yesterday", now=now)
        self.assertEqual(yesterday["period_to"], "2026-07-16T00:00:00+03:00")

    def test_lead_scope_excludes_live_dmp_and_negative_categories(self):
        profile = default_analysis_profile()
        period = profile_period_bounds("today", now=datetime(2026, 7, 16, 10, tzinfo=MSK))
        client = FakeBitrixClient(
            sources=[
                {"STATUS_ID": "UC_LIVE_DMP", "NAME": "DMP - Ответственный"},
                {"STATUS_ID": "WEB", "NAME": "Сайт"},
            ],
            statuses=[{"STATUS_ID": "UC_SPAM", "NAME": "СПАМ"}],
            leads=[
                {"ID": "1", "SOURCE_ID": "UC_LIVE_DMP", "STATUS_ID": "NEW", "DATE_CREATE": "2026-07-16T08:00:00+03:00"},
                {"ID": "2", "SOURCE_ID": "WEB", "STATUS_ID": "UC_SPAM", "DATE_CREATE": "2026-07-16T08:00:00+03:00"},
                {"ID": "3", "SOURCE_ID": "WEB", "STATUS_ID": "NEW", "DATE_CREATE": "2026-07-16T08:00:00+03:00"},
            ],
        )
        rows, stats = fetch_profile_leads(client, profile=profile, period=period)
        self.assertEqual([row["ID"] for row in rows], ["3"])
        self.assertEqual(stats["excluded_by_source"], 1)
        self.assertEqual(stats["excluded_by_status"], 1)
        self.assertIn("UC_LIVE_DMP", stats["excluded_sources"])

    def test_deal_scope_keeps_old_active_portfolio_and_deduplicates(self):
        profile = default_analysis_profile()
        period = profile_period_bounds("today", now=datetime(2026, 7, 16, 10, tzinfo=MSK))
        client = FakeBitrixClient(
            deals=[
                {"ID": "10", "CATEGORY_ID": "15", "STAGE_ID": "C15:NEW", "CLOSED": "N", "DATE_CREATE": "2025-01-01T10:00:00+03:00"},
                {"ID": "11", "CATEGORY_ID": "15", "STAGE_ID": "C15:4", "CLOSED": "Y", "DATE_CREATE": "2026-07-16T09:00:00+03:00"},
            ]
        )
        rows, stats = fetch_profile_deals(client, profile=profile, period=period)
        self.assertEqual({row["ID"] for row in rows}, {"10", "11"})
        self.assertEqual(stats["selected"], 2)

    def test_handoff_outside_profile_is_warning_not_main_card(self):
        profile = default_analysis_profile()
        profile["period_preset"] = "today"
        leads = [{
            "ID": "7", "TITLE": "Лид 7", "SOURCE_ID": "WEB", "STATUS_ID": "CONVERTED", "STATUS_SEMANTIC_ID": "S",
            "DATE_CREATE": "2026-07-16T08:00:00+03:00", "DATE_MODIFY": "2026-07-16T08:00:00+03:00",
        }]
        deals = [{
            "ID": "8", "LEAD_ID": "7", "TITLE": "Сделка вне профиля", "CATEGORY_ID": "99", "STAGE_ID": "C99:NEW",
            "CLOSED": "N", "DATE_CREATE": "2026-07-16T09:00:00+03:00", "DATE_MODIFY": "2026-07-16T09:00:00+03:00",
        }]
        client = FakeBitrixClient(leads=leads, deals=deals, statuses=[{"STATUS_ID": "CONVERTED", "NAME": "Сконвертирован"}])
        with tempfile.TemporaryDirectory() as directory, patch("api.candidates.load_pipeline_stage_names", return_value={}):
            preview = profile_candidates_preview(
                {"id": 1, "name": "Тест", "version": 1, "profile": profile},
                client=client,
                now=datetime(2026, 7, 16, 10, tzinfo=MSK),
                db_path=Path(directory) / "rop.sqlite3",
            )
        warning = preview["scope"]["handoff_warning"]
        self.assertEqual(warning["outside_profile_count"], 1)
        self.assertEqual(preview["candidates"], [])

    def test_call_gap_is_soft_signal_and_direction_is_visible(self):
        activities = [
            {
                "ID": "1",
                "TYPE_ID": "2",
                "DIRECTION": "1",
                "COMPLETED": "N",
                "START_TIME": "2026-07-16T08:00:00+03:00",
            }
        ]
        call = evaluate_call_method(activities, now=datetime(2026, 7, 16, 14, tzinfo=MSK))
        self.assertEqual(call["incoming"], 1)
        self.assertEqual(call["outgoing"], 0)
        self.assertTrue(call["method_gap"])
        period = profile_period_bounds("today", now=datetime(2026, 7, 16, 14, tzinfo=MSK))
        signals, _ = detect_candidate_signals(
            {"ID": "1", "STATUS_SEMANTIC_ID": "P", "DATE_CREATE": "2026-07-16T07:00:00+03:00"},
            entity_type="lead",
            stage_name="Новый",
            activities=activities,
            period=period,
            now=datetime(2026, 7, 16, 14, tzinfo=MSK),
        )
        self.assertIn("call_method_gap", [item["reason_code"] for item in signals])

    def test_journey_prefers_deal_and_keeps_origin_lead(self):
        lead = {"entity_type": "lead", "entity_id": "7", "journey_key": "lead:7", "signals": [], "reason_codes": [], "reasons": []}
        deal = {"entity_type": "deal", "entity_id": "8", "journey_key": "lead:7", "signals": [], "reason_codes": [], "reasons": []}
        result = deduplicate_journeys([lead, deal])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["entity_id"], "8")
        self.assertEqual(result[0]["origin_lead"]["entity_id"], "7")

    def test_freshness_uses_normalized_snapshot_not_date_modify_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "rop.sqlite3"
            snapshot = {
                "entity_type": "deal",
                "deal": {"id": "8", "stage_id": "C15:NEW", "category_id": "15", "opportunity": "100"},
                "metadata": {"date_modify": "2026-07-15T10:00:00+03:00"},
                "activities": [{"id": "1", "source": "deal", "completed": "N", "deadline": "2026-07-20", "start_time": "", "last_updated": "", "direction": ""}],
            }
            upsert_entity_state(
                db,
                entity_type="deal",
                entity_id="8",
                fingerprint="abc",
                snapshot=snapshot,
                last_analysis_status="analyzed",
                last_analysis_at="2026-07-15T10:00:00+03:00",
                last_analysis={"risk_level": "low"},
            )
            entity = {"ID": "8", "STAGE_ID": "C15:NEW", "CATEGORY_ID": "15", "OPPORTUNITY": "100", "DATE_MODIFY": "2026-07-16T10:00:00+03:00"}
            activities = [{"ID": "1", "COMPLETED": "N", "DEADLINE": "2026-07-20"}]
            self.assertEqual(candidate_freshness("deal", entity, activities=activities, db_path=db), "date_modified_only")
            changed = [{"ID": "2", "COMPLETED": "N", "DEADLINE": "2026-07-21"}]
            self.assertEqual(candidate_freshness("deal", entity, activities=changed, db_path=db), "changed")
            entity["STAGE_ID"] = "C15:PREPARATION"
            self.assertEqual(candidate_freshness("deal", entity, activities=activities, db_path=db), "changed")

    def test_preview_never_calls_llm_and_highlights_limited_workset(self):
        profile = default_analysis_profile()
        profile["period_preset"] = "today"
        profile["limits"].update({"workset": 1, "new_slots": 1, "backlog_slots": 0, "paid_per_run": 1})
        leads = [
            {
                "ID": "1", "TITLE": "Лид 1", "SOURCE_ID": "WEB", "STATUS_ID": "BAD", "STATUS_SEMANTIC_ID": "F",
                "DATE_CREATE": "2026-07-16T08:00:00+03:00", "DATE_MODIFY": "2026-07-16T08:00:00+03:00",
            },
            {
                "ID": "2", "TITLE": "Лид 2", "SOURCE_ID": "WEB", "STATUS_ID": "BAD", "STATUS_SEMANTIC_ID": "F",
                "DATE_CREATE": "2026-07-16T09:00:00+03:00", "DATE_MODIFY": "2026-07-16T09:00:00+03:00",
            },
        ]
        client = FakeBitrixClient(leads=leads, statuses=[{"STATUS_ID": "BAD", "NAME": "Негатив"}])
        with tempfile.TemporaryDirectory() as directory, patch("api.candidates.load_pipeline_stage_names", return_value={}):
            preview = profile_candidates_preview(
                {"id": 1, "name": "Тест", "version": 1, "profile": profile},
                client=client,
                now=datetime(2026, 7, 16, 10, tzinfo=MSK),
                db_path=Path(directory) / "rop.sqlite3",
            )
        self.assertFalse(preview["llm_called"])
        self.assertEqual(preview["summary"]["total"], 2)
        self.assertEqual(preview["summary"]["workset"], 1)
        self.assertEqual(sum(bool(item["workset_selected"]) for item in preview["candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
