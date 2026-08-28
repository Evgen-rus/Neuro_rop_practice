from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openai_api.audio.short_call import load_recording_durations


class RecordingDurationsTests(unittest.TestCase):
    def test_reads_only_measured_download_or_purged_recording_duration(self):
        calls = [
            {"activity_id": "1", "downloads": [
                {"ok": True, "duration_seconds": 70},
                {"ok": True, "duration_seconds": 70},
                {"ok": False, "duration_seconds": 900},
            ]},
            {"activity_id": "2", "transcription": {
                "status": "transcribed_and_purged", "source_duration_seconds": 40,
            }},
            {"activity_id": "3", "duration_seconds": 300, "downloads": [
                {"ok": True, "expected_call_duration_seconds": 300},
            ]},
            {"activity_id": "4", "transcription": {
                "status": "stale_source_grew", "source_duration_seconds": 40,
            }},
            {"activity_id": "5", "audio_kind": "max_voice", "downloads": [
                {"ok": True, "duration_seconds": 40},
            ]},
            {"activity_id": "6", "downloads": [{"ok": True, "duration_seconds": 0}]},
        ]
        for value in (None, True, "70", -1, float("nan"), float("inf"), {}, []):
            calls.append({"activity_id": f"invalid-{len(calls)}", "downloads": [
                {"ok": True, "duration_seconds": value},
            ]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"calls": calls}, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()
            self.assertEqual(load_recording_durations([path]), {"1": 70, "2": 40, "6": 0})
            self.assertEqual(path.read_bytes(), before)

    def test_current_manifest_wins_over_workspace_copy_even_when_duration_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("current.json", "copy.json")]
            for path, duration in zip(paths, (None, 70)):
                path.write_text(json.dumps({"calls": [{
                    "activity_id": "1", "downloads": [{"ok": True, "duration_seconds": duration}],
                }]}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_recording_durations(paths), {})
            self.assertEqual(load_recording_durations([Path(directory) / "missing.json", paths[1]]), {"1": 70})

    def test_missing_or_malformed_manifest_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            self.assertEqual(load_recording_durations([path]), {})
            for content in ("{", "[]", '{"calls": {}}', '{"calls": [null, {"activity_id": "1", "downloads": {}}]}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    self.assertEqual(load_recording_durations([path]), {})
