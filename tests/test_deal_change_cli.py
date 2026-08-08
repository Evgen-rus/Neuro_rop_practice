from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai_api.llm import analyze_deal, analyze_deal_if_changed


class DealChangeCliTests(unittest.TestCase):
    def test_latest_transcript_ignores_generated_all_calls_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcripts = Path(temp_dir)
            source = transcripts / "call_42.md"
            aggregate = transcripts / "deal_7_all_calls_transcript.md"
            source.write_text("source", encoding="utf-8")
            aggregate.write_text("aggregate", encoding="utf-8")
            aggregate.touch()

            self.assertEqual(analyze_deal.latest_transcript(transcripts), source)
            self.assertEqual(analyze_deal_if_changed.latest_transcript_or_none(transcripts), source)

    def test_dry_run_decision_does_not_save_snapshot_or_analysis_state(self) -> None:
        args = SimpleNamespace(
            deal_id="7",
            deal_root="unused",
            db_path=None,
            transcript="none",
            model=None,
            force_llm=False,
            dry_run_decision=True,
        )
        decision = Mock()
        decision.as_dict.return_value = {"status": "SKIPPED_NO_CHANGES"}
        with (
            patch.object(analyze_deal_if_changed, "parse_args", return_value=args),
            patch.object(analyze_deal_if_changed, "load_dotenv"),
            patch.object(analyze_deal_if_changed, "init_db"),
            patch.object(analyze_deal_if_changed, "raw_bundle_path", return_value=Path("raw.json")),
            patch.object(analyze_deal_if_changed, "resolve_transcript_for_snapshot", return_value=(None, "none")),
            patch.object(analyze_deal_if_changed, "load_json", return_value={}),
            patch.object(analyze_deal_if_changed, "build_deal_snapshot", return_value={"deal": {}}),
            patch.object(analyze_deal_if_changed, "fingerprint_snapshot", return_value="fingerprint"),
            patch.object(analyze_deal_if_changed, "get_entity_state", return_value=None),
            patch.object(analyze_deal_if_changed, "compare_snapshots", return_value={"changes": []}),
            patch.object(analyze_deal_if_changed, "get_entity_memory", return_value=None),
            patch.object(analyze_deal_if_changed, "decide_deal_processing", return_value=decision),
            patch.object(analyze_deal_if_changed, "save_json") as save_json,
            patch.object(analyze_deal_if_changed, "save_analysis_run") as save_analysis_run,
        ):
            analyze_deal_if_changed.main()

        save_json.assert_not_called()
        save_analysis_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
