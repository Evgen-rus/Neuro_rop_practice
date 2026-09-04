from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.access import deals_visible_for_dashboard
from api.deal_control import build_deal_control_dashboard, build_deal_control_deal
from storage.rop_db import (
    connect,
    get_deal_daily_quality_state,
    get_latest_ui_report,
    init_db,
    list_deal_control_deals,
    list_deal_daily_quality_states,
    list_latest_ui_reports,
    reset_init_db_cache,
    save_ui_report,
    upsert_deal_control_deal,
)


def _user(role: str, *, manager_id: str | None = None) -> dict[str, object]:
    return {
        "id": 1,
        "login": role,
        "role": role,
        "manager_id": manager_id,
        "is_active": True,
    }


def _save_deal(db_path: Path, *, deal_id: str, manager_id: str) -> None:
    upsert_deal_control_deal(
        db_path,
        deal_id=deal_id,
        source="initial",
        title=f"Сделка {deal_id}",
        manager_id=manager_id,
        manager_name=f"Менеджер {manager_id}",
        stage_id="C15:NEW",
        stage_name="Новая",
        pipeline_id="15",
        amount="1000",
        currency_id="RUB",
        created_at_crm="2026-07-19T09:00:00+03:00",
        modified_at_crm="2026-07-20T09:00:00+03:00",
        is_active=True,
    )


class DealControlReadPathTests(unittest.TestCase):
    def test_init_db_does_not_reopen_sqlite_after_first_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            reset_init_db_cache()
            with patch("storage.rop_db.connect", wraps=connect) as wrapped:
                init_db(db_path)
                first = wrapped.call_count
                init_db(db_path)
                init_db(db_path)
            self.assertGreater(first, 0)
            self.assertEqual(wrapped.call_count, first)

    def test_bulk_latest_reports_match_individual_getters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _save_deal(db_path, deal_id="101", manager_id="10")
            _save_deal(db_path, deal_id="202", manager_id="20")
            first = save_ui_report(db_path, entity_type="deal", entity_id="101", report_json={"v": 1})
            save_ui_report(db_path, entity_type="deal", entity_id="101", report_json={"v": 2})
            save_ui_report(db_path, entity_type="deal", entity_id="202", report_json={"v": 3})
            bulk = list_latest_ui_reports(db_path, entity_type="deal", entity_ids=["101", "202", "303"])
            one = get_latest_ui_report(db_path, entity_type="deal", entity_id="101")
            two = get_latest_ui_report(db_path, entity_type="deal", entity_id="202")
            missing = get_latest_ui_report(db_path, entity_type="deal", entity_id="303")
            self.assertNotEqual(int(bulk["101"]["id"]), first)
            self.assertEqual(bulk["101"], one)
            self.assertEqual(bulk["202"], two)
            self.assertNotIn("303", bulk)
            self.assertIsNone(missing)
            self.assertEqual(bulk["101"]["report_json"], {"v": 2})

    def test_bulk_daily_quality_matches_individual_getter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            init_db(db_path)
            bulk = list_deal_daily_quality_states(
                db_path,
                deal_ids=["101", "202"],
                business_date="2026-07-20",
            )
            one = get_deal_daily_quality_state(db_path, deal_id="101", business_date="2026-07-20")
            self.assertEqual(bulk, {})
            self.assertIsNone(one)

    def test_manager_viewer_projects_only_own_deals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _save_deal(db_path, deal_id="101", manager_id="10")
            _save_deal(db_path, deal_id="202", manager_id="77")
            visible = deals_visible_for_dashboard(
                list_deal_control_deals(db_path),
                _user("manager", manager_id="10"),
            )
            self.assertEqual([row["deal_id"] for row in visible], ["101"])
            with patch("api.deal_control.list_crm_pipelines", return_value={"deal_pipelines": []}):
                dashboard = build_deal_control_dashboard(
                    db_path=db_path,
                    viewer=_user("manager", manager_id="10"),
                )
            self.assertEqual([row["deal_id"] for row in dashboard["deals"]], ["101"])

    def test_single_deal_builder_does_not_scan_portfolio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _save_deal(db_path, deal_id="101", manager_id="10")
            _save_deal(db_path, deal_id="202", manager_id="20")
            with patch("api.deal_control.list_deal_control_deals", wraps=list_deal_control_deals) as listed, \
                 patch("api.deal_control.list_crm_pipelines", return_value={"deal_pipelines": []}):
                build_deal_control_deal(db_path=db_path, deal_id="101")
            listed.assert_not_called()

    def test_dashboard_build_does_not_call_bitrix_or_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            _save_deal(db_path, deal_id="101", manager_id="10")
            with patch("api.deal_control.make_client") as bitrix, \
                 patch("api.deal_control.list_crm_pipelines", return_value={"deal_pipelines": []}):
                build_deal_control_dashboard(db_path=db_path)
            bitrix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
