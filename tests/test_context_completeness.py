from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openai_api.llm.context_completeness import build_context_completeness_block


def write_context_gaps(root: Path, *, status: str, summary: dict, gaps: list[dict]) -> Path:
    path = root / "context_gaps.json"
    path.write_text(
        json.dumps({"context_completeness": status, "summary": summary, "gaps": gaps}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class ContextCompletenessTests(unittest.TestCase):
    def test_complete_context_is_short_and_requires_no_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(
                Path(directory),
                status="complete",
                summary={"crm_calls_found": 2, "transcript_activity_ids_found": ["1", "2"], "calls_without_transcript": 0},
                gaps=[],
            )
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=True)
        self.assertIn("status: complete", block)
        self.assertIn("manual_review_required: false", block)
        self.assertLess(len(block), 700)

    def test_partial_transcript_keeps_count_without_claiming_no_contact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(
                Path(directory),
                status="partial",
                summary={"crm_calls_found": 8, "transcript_activity_ids_found": ["1"], "calls_without_transcript": 7},
                gaps=[{"source": "call_transcript", "activity_id": "2"}],
            )
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=True)
        self.assertIn("status: partial", block)
        self.assertIn("expected_calls=8; loaded_calls=1; missing_calls=7", block)
        self.assertIn("missing sources are not evidence of absence", block)
        self.assertNotIn("no meaningful contact", block.lower())

    def test_missing_tasks_does_not_claim_tasks_absent_in_crm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(Path(directory), status="partial", summary={}, gaps=[{"source": "crm_tasks"}])
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=None)
        self.assertIn("crm_tasks=unavailable", block)
        self.assertNotIn("no crm tasks", block.lower())

    def test_missing_required_source_is_insufficient_and_requests_manual_review(self) -> None:
        block = build_context_completeness_block([], history_available=False, transcript_available=False, stage_policy_available=True)
        self.assertIn("status: insufficient", block)
        self.assertIn("manual_review_required: true", block)
        self.assertIn("required history or transcript source is unavailable", block)

    def test_critical_gap_never_leaves_context_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(
                Path(directory),
                status="complete",
                summary={},
                gaps=[{"source": "call_transcript", "severity": "critical"}],
            )
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=True)
        self.assertIn("status: partial", block)
        self.assertIn("manual_review_required: false", block)

    def test_raw_insufficient_is_not_preserved_when_required_sources_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(Path(directory), status="insufficient", summary={}, gaps=[])
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=True)
        self.assertIn("status: partial", block)
        self.assertIn("manual_review_required: false", block)

    def test_operational_details_never_leak_from_raw_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_context_gaps(
                Path(directory),
                status="partial",
                summary={},
                gaps=[
                    {
                        "source": "call_transcript",
                        "expected_audio_path": "reports/private/audio.mp3",
                        "transcribe_command": "powershell .\\venv\\Scripts\\python.exe script.py",
                        "stack_trace": "Traceback",
                    }
                ],
            )
            block = build_context_completeness_block([str(path)], history_available=True, transcript_available=True, stage_policy_available=True)
        for forbidden in ("reports/private", "powershell", "script.py", "traceback"):
            self.assertNotIn(forbidden, block.lower())


if __name__ == "__main__":
    unittest.main()
