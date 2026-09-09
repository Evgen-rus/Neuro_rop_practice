from __future__ import annotations

import unittest

from bitrix.canonical_state import merge_deal_bundle


def _bundle() -> dict:
    return {
        "deal_id": "7",
        "deal": {"item": {"ID": "7", "STAGE_ID": "SYNTHETIC:NEW"}},
        "activities": {
            "ok": True,
            "items": [{"ID": "10", "TYPE_ID": "2", "END_TIME": "old"}],
        },
        "activity_details": {
            "10": {"ok": True, "response": {"result": {"ID": "10", "END_TIME": "new"}}},
        },
        "bitrix_tasks": {
            "20": {"ok": True, "response": {"result": {"task": {
                "id": "20", "status": "2", "chatId": "40",
            }}}},
        },
        "bitrix_task_chats": {
            "20": {"ok": True, "response": {"result": {"messages": [{
                "id": "50", "date": "2026-01-01T09:00:00+03:00",
                "author_id": "2", "text": "synthetic internal task message",
            }]}}},
        },
        "timeline_comments": [{"ok": True, "items": [{"ID": "30", "COMMENT": "synthetic"}]}],
    }


class CanonicalStateBundleAdapterTests(unittest.TestCase):
    def test_bundle_projects_supported_sources_and_activity_detail(self) -> None:
        state, delta = merge_deal_bundle(None, _bundle(), observed_at="2026-01-01T10:00:00+03:00")
        self.assertEqual(
            set(state["entities"]),
            {"deal:7", "activity:10", "task:20", "timeline_comment:30", "im_message:50"},
        )
        self.assertEqual(state["entities"]["activity:10"]["semantic"]["end_time"], "new")
        self.assertEqual(state["entities"]["im_message:50"]["semantic"]["dialog_id"], "chat40")
        self.assertEqual(len(delta["entries"]), 5)

    def test_failed_refresh_keeps_previous_source_entities(self) -> None:
        state, _ = merge_deal_bundle(None, _bundle(), observed_at="2026-01-01T10:00:00+03:00")
        failed = _bundle()
        failed["activities"] = {
            "ok": True,
            "refresh_ok": False,
            "items": [{"ID": "10", "TYPE_ID": "2", "END_TIME": "must-not-apply"}],
        }
        next_state, delta = merge_deal_bundle(
            state,
            failed,
            observed_at="2026-01-01T10:05:00+03:00",
        )
        self.assertEqual(next_state["source_status"]["activities"], "failed")
        self.assertEqual(next_state["entities"]["activity:10"], state["entities"]["activity:10"])
        self.assertNotIn("activity:10", {entry["key"] for entry in delta["entries"]})


if __name__ == "__main__":
    unittest.main()
