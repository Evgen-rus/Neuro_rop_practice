from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bitrix.context_diagnostics import build_context_diagnostics, render_llm_context, render_manual_actions


def write_context(root: Path) -> tuple[Path, Path]:
    entity_dir = root / "lead_42"
    context_path = entity_dir / "history" / "lead_42_customer_history_bundle.json"
    context_path.parent.mkdir(parents=True)
    context_path.write_text(
        json.dumps(
            {
                "bundle_type": "customer_history_bundle",
                "client_touchpoints": [
                    {"event_type": "call", "id": "123", "entity_key": "lead:42"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (entity_dir / "audio").mkdir()
    return entity_dir, context_path


def write_manifest(entity_dir: Path, call: dict) -> None:
    path = entity_dir / "audio" / "lead_42_call_audio_manifest.json"
    path.write_text(json.dumps({"calls": [call]}, ensure_ascii=False), encoding="utf-8")


class ContextDiagnosticsTests(unittest.TestCase):
    def test_short_no_answer_skip_is_not_a_missing_transcript_and_reaches_llm_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entity_dir, context_path = write_context(Path(directory))
            write_manifest(
                entity_dir,
                {
                    "activity_id": "123",
                    "call_quality": "short_no_answer",
                    "skip_transcribe": True,
                    "downloads": [{"ok": True, "local_path": "activity_123.mp3"}],
                },
            )
            payload = build_context_diagnostics("lead", "42", entity_dir, context_path)

        self.assertEqual(payload["context_completeness"], "full_enough")
        self.assertEqual(payload["summary"]["calls_without_transcript"], 0)
        self.assertEqual(payload["summary"]["short_no_answer_skipped_calls"], 1)
        self.assertFalse(any(item.get("source") in {"call_audio_transcript", "call_transcript"} for item in payload["gaps"]))
        self.assertNotIn("Скачать запись звонка", render_manual_actions(payload))
        llm_context = render_llm_context(payload)
        self.assertIn("Коротких звонков, штатно пропущенных по политике: 1", llm_context)
        self.assertIn("контакт не подтвержден", llm_context)
        self.assertIn("фактическое наличие ответа не установлено", llm_context)

    def test_incomplete_recording_remains_a_missing_transcript_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entity_dir, context_path = write_context(Path(directory))
            (entity_dir / "audio" / "activity_123.mp3").write_bytes(b"synthetic")
            write_manifest(
                entity_dir,
                {
                    "activity_id": "123",
                    "call_quality": "recording_incomplete",
                    "skip_transcribe": True,
                    "downloads": [{"ok": True, "local_path": "activity_123.mp3"}],
                },
            )
            payload = build_context_diagnostics("lead", "42", entity_dir, context_path)

        self.assertEqual(payload["context_completeness"], "partial")
        self.assertEqual(payload["summary"]["calls_without_transcript"], 1)
        self.assertEqual(payload["summary"]["short_no_answer_skipped_calls"], 0)
        self.assertEqual(payload["gaps"][0]["source"], "call_transcript")

    def test_missing_transcript_without_a_manifest_remains_a_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entity_dir, context_path = write_context(Path(directory))
            payload = build_context_diagnostics("lead", "42", entity_dir, context_path)

        self.assertEqual(payload["summary"]["calls_without_transcript"], 1)
        self.assertEqual(payload["gaps"][0]["source"], "call_audio_transcript")
        self.assertTrue(payload["critical_missing"])


if __name__ == "__main__":
    unittest.main()
