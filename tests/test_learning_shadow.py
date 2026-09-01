from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from api.learning_shadow import collect_learning_shadow_cases, run_learning_shadow
from openai_api.llm.learning_shadow import (
    LEARNING_SHADOW_MODEL,
    LEARNING_SHADOW_REASONING_EFFORT,
)
from storage.rop_db import (
    create_learning_shadow_run,
    get_learning_shadow_run,
    record_manager_trajectory_event,
)


DAY = date(2026, 8, 31)


class LearningShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "shadow.sqlite"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(
        self,
        *,
        key: str,
        occurred_at: str,
        event_type: str,
        recommendation_id: str | None = None,
        recommendation_kind: str | None = None,
        payload: dict | None = None,
        deal_id: str = "900",
    ) -> None:
        record_manager_trajectory_event(
            self.db_path,
            entity_type="deal",
            entity_id=deal_id,
            manager_id="10",
            event_type=event_type,
            recommendation_kind=recommendation_kind,
            recommendation_id=recommendation_id,
            source="fixture",
            source_event_key=key,
            occurred_at=occurred_at,
            payload=payload,
        )

    def test_one_case_deduplicates_recommendations_and_starts_at_first_view(self) -> None:
        self.event(
            key="before", occurred_at="2026-08-31T09:00:00+03:00",
            event_type="crm_activity_observed",
            payload={"activity_kind": "message", "direction": "2", "description": "До просмотра"},
        )
        actor = {"actor_verified": True, "actor_role": "manager", "actor_manager_id": "10"}
        self.event(
            key="view-1", occurred_at="2026-08-31T10:00:00+03:00",
            event_type="recommendation_viewed", recommendation_kind="quick_help",
            recommendation_id="101", payload={**actor, "occurrence_id": "one"},
        )
        self.event(
            key="view-2", occurred_at="2026-08-31T10:05:00+03:00",
            event_type="recommendation_viewed", recommendation_kind="quick_help",
            recommendation_id="101", payload={**actor, "occurrence_id": "two"},
        )
        self.event(
            key="view-3", occurred_at="2026-08-31T10:10:00+03:00",
            event_type="recommendation_viewed", recommendation_kind="deal_task",
            recommendation_id="202", payload={**actor, "occurrence_id": "three"},
        )
        self.event(
            key="out", occurred_at="2026-08-31T10:20:00+03:00",
            event_type="crm_activity_observed",
            payload={"activity_kind": "message", "direction": "2", "description": "Написал клиенту"},
        )
        self.event(
            key="in", occurred_at="2026-08-31T10:30:00+03:00",
            event_type="crm_activity_observed",
            payload={"activity_kind": "message", "direction": "1", "description": "Ответ клиента"},
        )

        cases = collect_learning_shadow_cases(self.db_path, from_date=DAY, to_date=DAY)

        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case["view_count"], 3)
        self.assertEqual(case["unique_recommendation_ids"], ["quick_help:101", "deal_task:202"])
        self.assertEqual(case["first_view_at"], "2026-08-31T10:00:00+03:00")
        self.assertNotIn("До просмотра", [item["content"] for item in case["timeline"]])
        self.assertEqual(len(case["action_event_ids"]), 1)
        self.assertEqual(len(case["client_event_ids"]), 1)
        self.assertEqual(case["status"], "pending")

    def test_no_action_case_never_calls_luna(self) -> None:
        self.event(
            key="view", occurred_at="2026-08-31T10:00:00+03:00",
            event_type="recommendation_viewed", recommendation_kind="quick_help",
            recommendation_id="101",
            payload={"actor_verified": True, "actor_role": "manager", "actor_manager_id": "10"},
        )
        self.event(
            key="deadline", occurred_at="2026-08-31T10:30:00+03:00",
            event_type="crm_task_history_observed",
            payload={"field": "DEADLINE", "from_value": "2026-08-31", "to_value": "2026-09-01"},
        )
        run = create_learning_shadow_run(
            self.db_path, from_date=DAY.isoformat(), to_date=DAY.isoformat(),
            model=LEARNING_SHADOW_MODEL, reasoning_effort=LEARNING_SHADOW_REASONING_EFFORT,
        )

        with patch("api.learning_shadow.analyze_learning_shadow_case") as analyze:
            run_learning_shadow(int(run["id"]), self.db_path)

        analyze.assert_not_called()
        saved = get_learning_shadow_run(self.db_path, int(run["id"]))
        assert saved is not None
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["total_cases"], 1)
        self.assertEqual(saved["no_action_cases"], 1)
        self.assertEqual(saved["llm_cases"], 0)
        self.assertEqual(saved["completed_cases"], 0)
        self.assertEqual(saved["cases"][0]["status"], "no_action_observed")

    def test_multiple_deals_produce_separate_cases(self) -> None:
        actor = {"actor_verified": True, "actor_role": "manager", "actor_manager_id": "10"}
        for deal_id in ("900", "901"):
            self.event(
                key=f"view-{deal_id}", deal_id=deal_id,
                occurred_at="2026-08-31T10:00:00+03:00",
                event_type="recommendation_viewed", recommendation_kind="deal_task",
                recommendation_id="10", payload=actor,
            )
        cases = collect_learning_shadow_cases(self.db_path, from_date=DAY, to_date=DAY)
        self.assertEqual([item["deal_id"] for item in cases], ["900", "901"])


if __name__ == "__main__":
    unittest.main()
