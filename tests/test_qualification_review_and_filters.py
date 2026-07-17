from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from api.candidates import lead_qualification_matches
from storage.rop_db import (
    list_qualification_reviews,
    save_qualification_review,
    save_ui_report,
)


class QualificationReviewAndFilterTests(unittest.TestCase):
    def test_qualification_review_round_trip_keeps_structured_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite"
            report_id = save_ui_report(
                db,
                entity_type="lead",
                entity_id="42",
                report_json={"lead_state": {"qualification": "B"}},
            )

            save_qualification_review(
                db,
                report_id=report_id,
                is_correct=False,
                issue_fields=["authority", "category"],
                corrected_statuses={"authority": "confirmed"},
                corrected_category="A",
                comment="Клиент сам принимает решение.",
            )

            review = list_qualification_reviews(db, report_id)[0]
            self.assertFalse(review["is_correct"])
            self.assertEqual(review["issue_fields"], ["authority", "category"])
            self.assertEqual(review["corrected_statuses"], {"authority": "confirmed"})
            self.assertEqual(review["corrected_category"], "A")
            gc.collect()

    def test_bant_filters_require_saved_analysis_and_do_not_infer_unknown_leads(self) -> None:
        analyzed = {
            "entity_type": "lead",
            "lead_qualification": {
                "category": "B",
                "statuses": {
                    "budget": "confirmed",
                    "authority": "unknown",
                    "need": "confirmed",
                    "timeframe": "confirmed",
                },
            },
        }
        not_analyzed = {"entity_type": "lead"}
        legacy = {"entity_type": "lead", "lead_category": "C", "lead_analysis_available": True}

        self.assertTrue(lead_qualification_matches(analyzed, categories={"B"}, bant_filter="authority"))
        self.assertTrue(lead_qualification_matches(analyzed, categories=set(), bant_filter="incomplete"))
        self.assertFalse(lead_qualification_matches(analyzed, categories={"A"}, bant_filter=""))
        self.assertFalse(lead_qualification_matches(not_analyzed, categories={"B"}, bant_filter=""))
        self.assertFalse(lead_qualification_matches(not_analyzed, categories=set(), bant_filter="unknown"))
        self.assertTrue(lead_qualification_matches(legacy, categories={"C"}, bant_filter=""))
        self.assertFalse(lead_qualification_matches(legacy, categories={"C"}, bant_filter="timeframe"))


if __name__ == "__main__":
    unittest.main()
