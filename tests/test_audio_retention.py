from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bitrix.deals.download_deals_call_audio import (
    existing_transcriptions_by_activity,
    process_call,
    record_transcribed_and_purged,
)


class AudioRetentionTests(unittest.TestCase):
    def test_recorded_purge_keeps_missing_only_from_downloading_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "activity_42.mp3"
            transcript_path = root / "call_42_transcript.json"
            manifest_path = root / "deal_1_call_audio_manifest.json"
            audio_path.write_bytes(b"audio")
            transcript_path.write_text("{}", encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "calls": [
                            {
                                "activity_id": "42",
                                "downloads": [{"ok": True, "local_path": str(audio_path), "status": "downloaded"}],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            audio_path.unlink()
            marked = record_transcribed_and_purged(
                manifest_path,
                audio_path,
                "42",
                {"txt_path": "transcript.txt", "md_path": "transcript.md", "json_path": str(transcript_path)},
            )

            self.assertTrue(marked)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            transcription = existing_transcriptions_by_activity(manifest)["42"]
            result = process_call(
                client=None,
                deal_audio_dir=root,
                activity={"ID": "42", "FILES": []},
                timeline=[],
                existing_transcription=transcription,
                missing_only=True,
            )
            self.assertEqual(result["status"], "transcribed_and_purged")
            self.assertEqual(result["downloads"], [])

    def test_missing_transcript_bundle_does_not_suppress_redownload(self) -> None:
        manifest = {
            "calls": [
                {
                    "activity_id": "42",
                    "transcription": {
                        "status": "transcribed_and_purged",
                        "transcript_json_path": "C:/does-not-exist/transcript.json",
                    },
                }
            ]
        }
        self.assertEqual(existing_transcriptions_by_activity(manifest), {})


if __name__ == "__main__":
    unittest.main()
