from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from api.daily_control import _day_activity_kind
from api.deal_control import _comment_preview_with_worklogs
from bitrix.manager_worklog import parse_manager_worklog
from openai_api.change_detection.decision_engine import MINI_RECOMMENDATION_NO_LLM, decide_deal_processing
from openai_api.change_detection.snapshot import build_deal_snapshot, compare_snapshots, fingerprint_snapshot
from setup import MSK_TZ
from storage.rop_db import (
    list_deal_manager_worklogs,
    list_manager_trajectory_events,
    save_deal_manager_worklog,
)


V1 = """21.08 — обсудили оборудование
11.08 — отправили КП
08.07 — первый контакт"""
V2 = """31.08 — клиент попросил исправить описание
26.08 — переписываемся по почте
21.08 — обсудили оборудование
11.08 — отправили КП
08.07 — первый контакт"""


def worklog(text: str, comment_id: str = "123") -> dict:
    parsed = parse_manager_worklog({
        "ID": comment_id,
        "CREATED": "2026-07-08T10:00:00+03:00",
        "AUTHOR_ID": "7",
        "COMMENT": text,
    })
    assert parsed is not None
    return parsed


def llm_context_module():
    path = Path(__file__).parents[1] / "bitrix" / "deals" / "4_build_deals_llm_context.py"
    spec = importlib.util.spec_from_file_location("manager_worklog_llm_context_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManagerWorklogIntegrationTests(unittest.TestCase):
    def test_same_comment_created_date_detects_one_version_change_and_sorts_two_worklogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            first = save_deal_manager_worklog(
                db_path, deal_id="1", manager_id="7", worklog=worklog(V1),
                detected_at="2026-08-21T12:00:00+03:00",
            )
            changed = save_deal_manager_worklog(
                db_path, deal_id="1", manager_id="7", worklog=worklog(V2),
                detected_at="2026-08-31T12:00:00+03:00",
            )
            unchanged = save_deal_manager_worklog(
                db_path, deal_id="1", manager_id="7", worklog=worklog(V2),
                detected_at="2026-08-31T13:00:00+03:00",
            )
            save_deal_manager_worklog(
                db_path, deal_id="1", manager_id="7",
                worklog=worklog("02.09 — уточнили бюджет закупки\n01.09 — получили ответ руководства\n30.08 — отправили расчёт", "456"),
                detected_at="2026-09-02T12:00:00+03:00",
            )

            self.assertTrue(first["created"])
            self.assertTrue(changed["changed"])
            self.assertFalse(unchanged["changed"])
            self.assertNotEqual(changed["previous_hash"], worklog(V2)["content_hash"])
            saved = list_deal_manager_worklogs(db_path, deal_id="1")
            self.assertEqual([item["comment_id"] for item in saved], ["456", "123"])
            self.assertEqual(saved[1]["latest_entry_date"], "2026-08-31")
            events = list_manager_trajectory_events(
                db_path,
                from_at="2026-08-01T00:00:00+03:00",
                to_at="2026-09-30T00:00:00+03:00",
            )
            edits = [item for item in events if item["event_type"] == "manager_worklog_changed"]
            self.assertEqual(len(edits), 1)
            self.assertNotIn("text", edits[0]["payload"])

    def test_preview_and_llm_keep_worklog_separate_from_normal_comments(self) -> None:
        parsed = worklog(V2)
        preview = _comment_preview_with_worklogs(
            [
                {"id": "123", "created_at": "2026-07-08T10:00:00+03:00", "author_id": "7", "text": V2},
                {"id": "999", "created_at": "2026-08-30T10:00:00+03:00", "author_id": "7", "text": "Отправил клиенту видео"},
            ],
            count=2,
            synced_at=datetime(2026, 8, 31, 12, tzinfo=MSK_TZ),
        )
        self.assertEqual([item["id"] for item in preview["items"]], ["999"])
        self.assertEqual([item["comment_id"] for item in preview["_worklogs"]], ["123"])

        module = llm_context_module()
        rows = module.manager_worklogs_section([parsed])
        rendered = "\n".join(rows)
        self.assertIn("2026-08-31", rendered)
        self.assertIn("не дословные слова клиента", rendered)
        comments = module.timeline_comments({
            "manager_worklogs": [parsed],
            "timeline_comments": [{"ok": True, "items": [
                {"ID": "123", "CREATED": "2026-07-08T10:00:00+03:00", "COMMENT": V2},
                {"ID": "999", "CREATED": "2026-08-30T10:00:00+03:00", "COMMENT": "Обычный комментарий"},
            ]}],
        })
        self.assertEqual([item["ID"] for item in comments], ["999"])

    def test_worklog_change_is_soft_and_never_daily_client_work(self) -> None:
        raw_v1 = {
            "deal_id": "1",
            "deal": {"item": {"ID": "1", "ASSIGNED_BY_ID": "7"}},
            "timeline_comments": [{"ok": True, "items": []}],
            "manager_worklogs": [worklog(V1)],
        }
        raw_v2 = {**raw_v1, "manager_worklogs": [worklog(V2)]}
        previous = build_deal_snapshot(raw_v1)
        current = build_deal_snapshot(raw_v2)
        diff = compare_snapshots(previous, current)
        decision = decide_deal_processing(
            previous_state={"current_fingerprint": fingerprint_snapshot(previous), "last_analysis": {}},
            current_snapshot=current,
            fingerprint=fingerprint_snapshot(current),
            diff=diff,
        )

        self.assertIn("manager_worklog_changed", diff["changes"])
        self.assertEqual(decision.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertIsNone(_day_activity_kind({"event_type": "manager_worklog_changed", "payload": {}}))


if __name__ == "__main__":
    unittest.main()
